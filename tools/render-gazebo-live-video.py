#!/usr/bin/env python3
"""Render recorded ROS camera/pose evidence with real Live audio and captions.

This is an evidence replay, not a screen recording. Timing uses local receipt times;
OpenAI Live does not provide output-audio playback timestamps.
Requires Pillow and ffmpeg. No cloud calls.
"""

import argparse
import bisect
import json
from pathlib import Path
import subprocess
import textwrap
import wave

from PIL import Image, ImageDraw, ImageFont


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main(directory):
    timeline = load(directory / "timeline.json", [])
    poses = load(directory / "poses.json", [])
    stamps = load(directory / "camera-frame-times.json", [])
    live = load(directory / "live-events.json", [])
    timings = load(directory / "live-audio-timing.json", [])
    start = next(e["seconds"] for e in timeline if e["kind"] == "camera_started")
    stop = next(
        e["seconds"] for e in reversed(timeline) if e["kind"] == "voice_finished"
    )
    spoken = [
        e["seconds"]
        for e in timeline
        if e["kind"] == "transcript" and e["role"] == "assistant"
    ]
    if spoken:
        stop = min(stop, spoken[-1] + 8)
    duration = stop - start
    # Place returned chunks on a software playback queue using arrival times.
    started = next(
        e["seconds"]
        for e in timeline
        if e["kind"] == "live" and e["type"] == "session.started"
    )
    session = next(e["seconds"] for e in live if e["type"] == "session.started")
    live_zero = started - session
    length = int((duration + 5) * 24000)
    import array

    mix = array.array("h", [0]) * length

    def add(pcm, offset, gain):
        samples = array.array("h")
        samples.frombytes(pcm)
        begin = max(0, int(offset * 24000))
        for i, value in enumerate(samples):
            if begin + i >= length:
                break
            mix[begin + i] = max(-32768, min(32767, mix[begin + i] + int(value * gain)))

    with wave.open(str(directory / "request.wav")) as w:
        add(w.readframes(w.getnframes()), started - start + 0.02, 0.8)
    with wave.open(str(directory / "live-response.wav")) as w:
        output = w.readframes(w.getnframes())
    cursor = 0
    end = 0
    for chunk in timings:
        at = max(chunk["received_seconds"], end)
        count = chunk["frames"] * 2
        add(output[cursor : cursor + count], live_zero + at - start, 0.95)
        cursor += count
        end = at + chunk["frames"] / 24000
    with wave.open(str(directory / "interaction-audio.wav"), "wb") as w:
        w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        w.writeframes(mix.tobytes())
    fontpath = "/System/Library/Fonts/Supplemental/Arial.ttf"
    fonts = {n: ImageFont.truetype(fontpath, n) for n in [14, 17, 20, 24, 32]}
    decoder = subprocess.Popen(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(directory / "camera.mp4"),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            "1280x720",
            "-framerate",
            "10",
            "-i",
            "-",
            "-i",
            str(directory / "interaction-audio.wav"),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(directory / "interaction.mp4"),
        ],
        stdin=subprocess.PIPE,
    )
    pose_times = [p["seconds"] for p in poses]
    current = None
    index = -1
    dark = "#101c29"
    panel = "#1a2a3a"
    white = "#edf5fa"
    muted = "#a5b7c8"
    cyan = "#71d9ef"

    def point(x, y):
        return (int(850 + (x + 2) * 70), int(465 - (y + 2) * 70))

    for f in range(int(duration * 10) + 1):
        now = start + f / 10
        target = max(0, bisect.bisect_right(stamps, now) - 1)
        while index < target:
            raw = decoder.stdout.read(320 * 240 * 3)
            if len(raw) < 320 * 240 * 3:
                break
            current = Image.frombytes("RGB", (320, 240), raw)
            index += 1
        image = Image.new("RGB", (1280, 720), dark)
        draw = ImageDraw.Draw(image)
        draw.text(
            (32, 20),
            "ORBO  /  VOICE → NAVIGATION → VERIFIED VISION",
            font=fonts[32],
            fill=white,
        )
        draw.text(
            (33, 64),
            "Gazebo simulation • ROS 2 Jazzy / Nav2 • Live NEAR • OpenAI GPT-Live",
            font=fonts[17],
            fill=muted,
        )
        draw.text(
            (33, 98), "ACTUAL ROS CAMERA  ·  320 × 240", font=fonts[17], fill=cyan
        )
        if current:
            image.paste(current.resize((640, 480), Image.Resampling.NEAREST), (32, 126))
        draw.rectangle((704, 126, 1248, 606), fill=panel)
        draw.text((728, 143), "RECORDED ROBOT POSITION", font=fonts[20], fill=white)
        draw.rectangle((*point(-2, 2), *point(2, -2)), outline=muted, width=2)
        # Furniture footprints from the simulation scene, schematic only.
        for x0, y0, x1, y1, label in [
            (-0.85, 1.25, 0.85, 1.95, "sofa"),
            (-0.43, 0.18, 0.43, 0.72, "table"),
            (1.4, 0.4, 1.9, 1.4, "TV"),
            (-1.7, 0.3, -1.1, 0.95, "chair"),
        ]:
            xy = (*point(x0, y1), *point(x1, y0))
            draw.rectangle(xy, fill="#536170")
            draw.text((xy[0] + 2, xy[1] + 2), label, font=fonts[14], fill=white)
        for x, c in [(1.25, "#f04b4b"), (0.95, "#497bff"), (0.63, "#c19358")]:
            x, y = point(x, 0.35)
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=c)
        path = [
            point(p["x"], p["y"]) for p in poses[: bisect.bisect_right(pose_times, now)]
        ]
        if len(path) > 1:
            draw.line(path, fill=cyan, width=3)
        if path:
            x, y = path[-1]
            draw.ellipse(
                (x - 7, y - 7, x + 7, y + 7), fill="#ffffff", outline=cyan, width=2
            )
        x, y = point(1, -0.55)
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), outline="#80e7a5", width=2)
        draw.text(
            (730, 475),
            f"Elapsed {f / 10:05.1f}s  ·  replay at real time",
            font=fonts[17],
            fill=muted,
        )
        seen = [e for e in timeline if e["seconds"] <= now]
        status = "Listening to the synthetic spoken request"
        calls = [e for e in seen if e["kind"].startswith("near_")]
        if calls:
            e = calls[-1]
            if e["kind"] == "near_started":
                status = (
                    "NEAR interpreting the request"
                    if e["call"] == 1
                    else "NEAR verifying and describing the camera image"
                )
            elif e["kind"] == "near_completed":
                status = (
                    "Nav2 driving to the TV viewpoint"
                    if e["call"] == 1
                    else "Verified result returned to the conversation"
                )
            else:
                status = "Provider call failed; see the recorded report"
        for j, line in enumerate(textwrap.wrap(status, 43)):
            draw.text((730, 512 + 27 * j), line, font=fonts[20], fill=cyan)
        transcripts = [
            e for e in seen if e["kind"] == "transcript" and now - e["seconds"] < 13
        ]
        role = transcripts[-1]["role"] if transcripts else None
        text = "".join(e["text"] for e in transcripts if e["role"] == role)
        if text:
            text = (
                "YOU (synthetic voice): " if role == "user" else "ORBO (OpenAI Live): "
            ) + text
        else:
            text = "The robot camera and position are recorded from Gazebo. No physical robot is connected."
        for j, line in enumerate(textwrap.wrap(text[-210:], 108)[:2]):
            draw.text((33, 626 + 26 * j), line, font=fonts[20], fill=white)
        draw.text(
            (33, 688),
            "Evidence replay • synthetic input speech • actual provider audio • audio timing reconstructed from receipt times",
            font=fonts[14],
            fill=muted,
        )
        encoder.stdin.write(image.tobytes())
    encoder.stdin.close()
    encoder.wait(timeout=120)
    decoder.stdout.close()
    decoder.terminate()
    decoder.wait()
    if encoder.returncode:
        raise SystemExit("Video encoding failed")
    print(directory / "interaction.mp4")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    main(p.parse_args().directory.resolve())
