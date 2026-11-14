"""Bounded PCM workers for Pi PortAudio/ALSA. No raw audio is persisted."""

import asyncio
from collections import deque
import io
import threading
import time
import wave

from .contracts import Rejected


class Playback:
    def __init__(
        self, *, sample_rate=24000, device=None, queue_seconds=2, stream_factory=None
    ):
        if not 0.1 <= queue_seconds <= 2:
            raise Rejected("audio queue limit must be at most two seconds")
        self.sample_rate, self.device = sample_rate, device
        self.max_bytes = int(sample_rate * 2 * queue_seconds)
        self.stream_factory = stream_factory
        self.generation = 0
        self.queue = deque()
        self.queued_bytes = 0
        self.lock = threading.Lock()
        self.stream = None
        self.played_frames = 0
        self.first_playback = None
        self.failure = None
        self.last_stop = 0.0
        self.active = False

    def interrupt(self):
        with self.lock:
            self.generation += 1
            self.queue.clear()
            self.queued_bytes = 0
        if self.stream:
            self.stream.abort()
        self.last_stop = time.monotonic()

    def _callback(self, outdata, frames, timing, status):
        output = bytearray(frames * 2)
        written = 0
        with self.lock:
            if status:
                self.failure = "portaudio_status"
            while self.queue and written < len(output):
                chunk = self.queue.popleft()
                take = min(len(chunk), len(output) - written)
                output[written : written + take] = chunk[:take]
                if take < len(chunk):
                    self.queue.appendleft(chunk[take:])
                self.queued_bytes -= take
                written += take
            if written:
                self.played_frames += written // 2
                if self.first_playback is None:
                    self.first_playback = time.monotonic()
        outdata[:] = output

    async def play(self, chunks):
        if self.active:
            raise Rejected("playback already active")
        self.active = True
        generation = self.generation
        self.played_frames, self.first_playback, self.failure = 0, None, None
        factory = self.stream_factory
        if factory is None:
            import sounddevice

            factory = sounddevice.RawOutputStream
        try:
            self.stream = factory(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.device,
                blocksize=max(1, self.sample_rate // 50),
                callback=self._callback,
            )
            self.stream.start()
            async with asyncio.timeout(125):
                async for chunk in chunks:
                    if not chunk or len(chunk) % 2 or len(chunk) > self.max_bytes:
                        raise Rejected("invalid PCM chunk")
                    async with asyncio.timeout(2):
                        while self.queued_bytes + len(chunk) > self.max_bytes:
                            self._check(generation)
                            await asyncio.sleep(0.005)
                    self._check(generation)
                    with self.lock:
                        self.queue.append(bytes(chunk))
                        self.queued_bytes += len(chunk)
                while self.queued_bytes:
                    self._check(generation)
                    await asyncio.sleep(0.005)
                self._check(generation)
                if self.played_frames == 0:
                    raise Rejected("empty playback")
                # PortAudio stop drains queued device buffers; this is software playback evidence.
                await asyncio.to_thread(self.stream.stop)
                self._check(generation)
                return {
                    "frames": self.played_frames,
                    "first_playback_mono": self.first_playback,
                    "evidence": "software_playback",
                    "completed": True,
                }
        except BaseException:
            self.interrupt()
            raise
        finally:
            if self.stream:
                self.stream.close()
                self.stream = None
            with self.lock:
                self.queue.clear()
                self.queued_bytes = 0
            self.active = False
            self.last_stop = time.monotonic()

    def _check(self, generation):
        if generation != self.generation:
            raise Rejected("obsolete playback generation")
        if self.failure:
            raise Rejected("audio device underrun or failure")


class Capture:
    def __init__(self, playback, *, device=None, aec_qualified=False):
        self.playback, self.device, self.aec_qualified = playback, device, aec_qualified
        self.active = False

    async def record(self, stop_event, *, max_seconds=30, hands_free=False, guard=None):
        if self.active or not 0.1 <= max_seconds <= 30:
            raise Rejected("invalid capture state or duration")
        if hands_free and not self.aec_qualified:
            raise Rejected(
                "hands-free capture requires commissioned echo-cancelled input"
            )
        if self.playback.active:
            if hands_free:
                if not self.aec_qualified:
                    raise Rejected("echo cancellation required")
            else:
                self.playback.interrupt()
        # Half-duplex tail gate avoids transcribing the robot's own last utterance.
        if not hands_free:
            await asyncio.sleep(
                max(0, self.playback.last_stop + 0.25 - time.monotonic())
            )
        import sounddevice

        frames = bytearray()
        lock = threading.Lock()
        failure = []
        limit = int(16000 * 2 * max_seconds)

        def callback(indata, count, timing, status):
            with lock:
                if status:
                    failure.append(True)
                available = limit - len(frames)
                if available > 0:
                    frames.extend(bytes(indata)[:available])

        self.active = True
        try:
            with sounddevice.RawInputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                device=self.device,
                blocksize=320,
                callback=callback,
            ):
                deadline = time.monotonic() + max_seconds
                while not stop_event.is_set() and time.monotonic() < deadline:
                    if guard:
                        guard()
                    await asyncio.sleep(0.01)
            if guard:
                guard()
            if failure or not frames:
                raise Rejected("capture overflow or empty utterance")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(frames)
            return output.getvalue()
        finally:
            self.active = False
