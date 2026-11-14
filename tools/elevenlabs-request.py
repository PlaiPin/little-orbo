#!/usr/bin/env python3
"""Generate one explicitly selected synthetic request as Live-compatible PCM WAV."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import wave
import httpx
from orbo_runtime.credentials import credential


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="eleven_multilingual_v2")
    parser.add_argument("--allow-cloud", action="store_true")
    args = parser.parse_args()
    if not args.allow_cloud or not re.fullmatch(r"[A-Za-z0-9]{20}", args.voice_id):
        parser.error("An explicit voice selection and --allow-cloud are required")
    if not 1 <= len(args.text) <= 500 or args.output.exists():
        parser.error("Use 1–500 characters and a new output file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "text": args.text,
        "model_id": args.model,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.75,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    response = httpx.post(
        "https://api.elevenlabs.io/v1/text-to-speech/" + args.voice_id,
        params={"output_format": "pcm_24000"},
        headers={"xi-api-key": credential("ELEVENLABS_API_KEY")},
        json=payload,
        timeout=90,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"ElevenLabs generation failed (HTTP {response.status_code}); no retry performed"
        )
    pcm = response.content
    if len(pcm) % 2 or not 4800 <= len(pcm) <= 960000:
        raise SystemExit("Output is not bounded PCM16 suitable for the Live input")
    with wave.open(str(args.output), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    metadata = {
        "provider": "elevenlabs",
        "voice_id": args.voice_id,
        **payload,
        "sample_rate": 24000,
        "seconds": len(pcm) / 48000,
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "request_id": response.headers.get("request-id"),
        "synthetic_speech": True,
    }
    args.output.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(
        json.dumps(
            {
                "audio": str(args.output),
                "seconds": metadata["seconds"],
                "voice_id": args.voice_id,
            }
        )
    )


if __name__ == "__main__":
    main()
