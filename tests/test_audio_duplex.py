import asyncio
import struct

import pytest

from orbo_runtime.audio_check import duplex_probe
from orbo_runtime.contracts import Rejected


class Stream:
    """Loop output back into input to expose measurable probe energy."""

    def __init__(self, *, callback, samplerate, **kwargs):
        self.callback, self.rate = callback, samplerate
        self.closed = self.aborted = False
        self.status = False

    def start(self):
        async def tick():
            previous = bytearray(self.rate // 50 * 2)
            while True:
                output = bytearray(len(previous))
                self.callback(previous, output, len(previous) // 2, None, self.status)
                previous = output
                await asyncio.sleep(0)

        self.task = asyncio.create_task(tick())

    def abort(self):
        self.aborted = True
        self.task.cancel()

    def close(self):
        self.closed = True


async def test_duplex_measures_loopback_without_certifying_echo_or_saving_audio():
    streams = []

    def factory(**kwargs):
        assert kwargs["device"] == ("mic", "speaker")
        assert kwargs["dtype"] == "int16" and kwargs["channels"] == 1
        stream = Stream(**kwargs)
        streams.append(stream)
        return stream

    result = await duplex_probe(
        seconds=2, input_device="mic", output_device="speaker", stream_factory=factory,
    )
    assert result["captured_frames"] == result["submitted_output_frames"] == 48000
    assert result["input_level_change_db"] > 20
    assert result["during_probe_input"]["clipped_samples"] == 0
    assert result["aec_qualified"] is False
    assert result["physical_audibility"] == "unverified"
    assert result["raw_audio_saved"] is False
    assert streams[0].closed and streams[0].aborted


async def test_duplex_device_failure_closes_stream():
    streams = []

    def factory(**kwargs):
        stream = Stream(**kwargs)
        stream.status = True
        streams.append(stream)
        return stream

    with pytest.raises(Rejected, match="device status"):
        await duplex_probe(stream_factory=factory)
    assert streams[0].closed and streams[0].aborted


async def test_duplex_clipping_and_cancellation_are_observable():
    class Clipped(Stream):
        def start(self):
            async def tick():
                while True:
                    frames = self.rate // 50
                    self.callback(struct.pack("<h", 32767) * frames,
                                  bytearray(frames * 2), frames, None, False)
                    await asyncio.sleep(0)
            self.task = asyncio.create_task(tick())

    result = await duplex_probe(seconds=2, stream_factory=Clipped)
    assert result["baseline_input"]["clipped_samples"] == 24000

    streams = []
    started = asyncio.Event()

    class Stalled(Stream):
        def start(self):
            self.task = asyncio.create_task(asyncio.sleep(60))
            started.set()

    def factory(**kwargs):
        streams.append(Stalled(**kwargs))
        return streams[-1]

    job = asyncio.create_task(duplex_probe(stream_factory=factory))
    await started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert streams[0].closed and streams[0].aborted


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), 0, 11])
async def test_invalid_duration_never_opens_stream(seconds):
    with pytest.raises(Rejected):
        await duplex_probe(seconds=seconds, stream_factory=lambda **kw: pytest.fail())
