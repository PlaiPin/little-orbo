"""Local WaveShare/PortAudio checks. No credentials, uploads or recorded files."""

import argparse
import asyncio
import io
import json
import math
import struct
import wave

from .audio import Capture, Playback
from .contracts import Rejected


def device(value):
    return int(value) if value and value.isdecimal() else value


def levels(wav):
    with wave.open(io.BytesIO(wav), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise Rejected("expected mono PCM16 capture")
        rate = reader.getframerate()
        data = reader.readframes(reader.getnframes())
    samples = [v[0] for v in struct.iter_unpack("<h", data)]
    if not samples:
        raise Rejected("empty capture")
    return {
        "frames": len(samples),
        "sample_rate": rate,
        "rms_dbfs": 20
        * math.log10(
            max(1e-9, math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768)
        ),
        "peak_dbfs": 20 * math.log10(max(1e-9, max(abs(v) for v in samples) / 32768)),
        "clipped_samples": sum(abs(v) >= 32767 for v in samples),
        "raw_audio_saved": False,
    }


async def duplex_probe(
    *, seconds=4, input_device=None, output_device=None, sample_rate=24000,
    stream_factory=None,
):
    """Exercise Live's simultaneous PCM format without opening a cloud session.

    Capture silence then a quiet speaker probe, retaining only aggregate levels.
    This detects device/stream failures; it cannot certify echo cancellation or
    establish that a person heard the output. Qualification remains acoustic.
    """
    if not math.isfinite(seconds) or not 2 <= seconds <= 10:
        raise Rejected("duplex test duration must be 2–10 seconds")
    if sample_rate not in {16000, 24000}:
        raise Rejected("duplex sample rate must be 16000 or 24000")
    if stream_factory is None:
        import sounddevice

        stream_factory = sounddevice.RawStream
    total = int(sample_rate * seconds)
    split = total // 2
    probe = b"\x00\x00" * split + b"".join(
        struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
        for i in range(total - split)
    )
    captured = bytearray()
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    failure = []

    def callback(indata, outdata, frames, timing, status):
        outdata[:] = b"\x00" * len(outdata)
        if done.is_set() or failure:
            return
        if status or len(indata) != frames * 2 or len(outdata) != frames * 2:
            failure.append("audio device status or invalid PCM frame")
            loop.call_soon_threadsafe(done.set)
            return
        offset = len(captured)
        count = min(frames * 2, len(probe) - offset)
        outdata[:count] = probe[offset:offset + count]
        captured.extend(bytes(indata)[:count])
        if len(captured) == len(probe):
            loop.call_soon_threadsafe(done.set)

    stream = stream_factory(
        samplerate=sample_rate, channels=1, dtype="int16",
        device=(input_device, output_device), blocksize=sample_rate // 50,
        callback=callback,
    )
    try:
        stream.start()
        async with asyncio.timeout(seconds + 5):
            await done.wait()
        if failure:
            raise Rejected(failure[0])
    finally:
        try:
            stream.abort()
        finally:
            stream.close()

    def measure(pcm):
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(pcm)
        return levels(output.getvalue())

    quiet = measure(captured[:split * 2])
    playing = measure(captured[split * 2:])
    return {
        "evidence": "local_duplex_device_stream",
        "sample_rate": sample_rate,
        "captured_frames": len(captured) // 2,
        "submitted_output_frames": total,
        "baseline_input": quiet,
        "during_probe_input": playing,
        "input_level_change_db": playing["rms_dbfs"] - quiet["rms_dbfs"],
        "raw_audio_saved": False,
        "aec_qualified": False,
        "physical_audibility": "unverified",
    }


async def run(args):
    import sounddevice as sd

    if args.command == "list":
        return {
            "devices": list(sd.query_devices()),
            "hostapis": list(sd.query_hostapis()),
        }
    if args.command == "check":
        sd.check_input_settings(
            device=device(args.input), channels=1, dtype="int16", samplerate=args.rate
        )
        sd.check_output_settings(
            device=device(args.output), channels=1, dtype="int16", samplerate=args.rate
        )
        return {
            "sample_rate": args.rate,
            "input_mono_pcm16": "supported",
            "output_mono_pcm16": "supported",
            "physical_audio": "unverified",
        }
    if args.command == "duplex":
        return await duplex_probe(
            seconds=args.seconds, input_device=device(args.input),
            output_device=device(args.output), sample_rate=args.rate,
        )
    if not 0.1 <= args.seconds <= 10:
        raise Rejected("audio test duration must be 0.1–10 seconds")
    playback = Playback(device=device(args.output))
    if args.command == "record":
        capture = Capture(playback, device=device(args.input))
        wav = await capture.record(asyncio.Event(), max_seconds=args.seconds)
        return levels(wav)

    async def tone():
        total = int(24000 * args.seconds)
        for start in range(0, total, 480):
            yield b"".join(
                struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * i / 24000)))
                for i in range(start, min(total, start + 480))
            )

    return await playback.play(tone())


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "check", "record", "tone", "duplex"])
    parser.add_argument("--input", help="PortAudio index or unique device substring")
    parser.add_argument("--output", help="PortAudio index or unique device substring")
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--rate", type=int, choices=[16000, 24000], default=24000,
                        help="PCM rate for check/duplex; Live defaults to 24000")
    args = parser.parse_args(args)
    try:
        print(json.dumps(asyncio.run(run(args)), indent=2))
    except Exception as error:
        parser.exit(1, f"Local audio check failed: {error}\n")


if __name__ == "__main__":
    main()
