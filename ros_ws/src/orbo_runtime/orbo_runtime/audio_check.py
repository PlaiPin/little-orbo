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


async def run(args):
    import sounddevice as sd

    if args.command == "list":
        return {
            "devices": list(sd.query_devices()),
            "hostapis": list(sd.query_hostapis()),
        }
    if args.command == "check":
        sd.check_input_settings(
            device=device(args.input), channels=1, dtype="int16", samplerate=16000
        )
        sd.check_output_settings(
            device=device(args.output), channels=1, dtype="int16", samplerate=24000
        )
        return {
            "input_16khz_mono": "supported",
            "output_24khz_mono": "supported",
            "physical_audio": "unverified",
        }
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
    parser.add_argument("command", choices=["list", "check", "record", "tone"])
    parser.add_argument("--input", help="PortAudio index or unique device substring")
    parser.add_argument("--output", help="PortAudio index or unique device substring")
    parser.add_argument("--seconds", type=float, default=2)
    args = parser.parse_args(args)
    try:
        print(json.dumps(asyncio.run(run(args)), indent=2))
    except Exception as error:
        parser.exit(1, f"Local audio check failed: {error}\n")
