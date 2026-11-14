#!/usr/bin/env python3
"""Compose actual Gazebo overview/camera recordings and provider audio, offline."""

import argparse
import array
import bisect
import json
from pathlib import Path
import subprocess
import textwrap
import wave
from PIL import Image, ImageDraw, ImageFont


def read(path):
    return json.loads(path.read_text())


class Frames:
    def __init__(self, path, timestamps):
        metadata = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )["streams"][0]
        self.size = (metadata["width"], metadata["height"])
        self.times = timestamps
        self.index = -1
        self.image = None
        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def at(self, second):
        if second < self.times[0]:
            return None
        wanted = max(0, bisect.bisect_right(self.times, second) - 1)
        while self.index < wanted:
            raw = self.proc.stdout.read(self.size[0] * self.size[1] * 3)
            if len(raw) != self.size[0] * self.size[1] * 3:
                break
            self.image = Image.frombytes("RGB", self.size, raw)
            self.index += 1
        return self.image

    def close(self):
        self.proc.terminate()
        self.proc.stdout.close()
        self.proc.wait(timeout=10)


def main(directory):
    timeline = read(directory / "timeline.json")
    live = read(directory / "live-events.json")
    overview_times = read(directory / "overview-frame-times.json")
    camera_times = read(directory / "camera-frame-times.json")
    if not overview_times:
        raise SystemExit(
            "No third-person Gazebo recording; refusing to substitute a schematic"
        )
    start = overview_times[0]
    finished = next(e["seconds"] for e in timeline if e["kind"] == "voice_finished")
    spoken = [
        e["seconds"]
        for e in timeline
        if e["kind"] == "transcript" and e["role"] == "assistant"
    ]
    stop = min(finished, spoken[-1] + 6) if spoken else finished
    duration = stop - start
    started = next(
        e["seconds"]
        for e in timeline
        if e["kind"] == "live" and e["type"] == "session.started"
    )
    session = next(e["seconds"] for e in live if e["type"] == "session.started")
    zero = started - session
    mix = array.array("h", [0]) * int((duration + 5) * 24000)

    def add(pcm, at, gain):
        samples = array.array("h")
        samples.frombytes(pcm)
        offset = int(at * 24000)
        for i, v in enumerate(samples):
            if 0 <= offset + i < len(mix):
                mix[offset + i] = max(
                    -32768, min(32767, mix[offset + i] + int(v * gain))
                )

    with wave.open(str(directory / "request.wav")) as wav:
        add(wav.readframes(wav.getnframes()), started - start + 0.02, 0.9)
    with wave.open(str(directory / "live-response.wav")) as wav:
        raw = wav.readframes(wav.getnframes())
    cursor = 0
    end = 0
    for chunk in read(directory / "live-audio-timing.json"):
        at = max(end, chunk["received_seconds"])
        size = chunk["frames"] * 2
        add(raw[cursor : cursor + size], zero + at - start, 0.95)
        cursor += size
        end = at + chunk["frames"] / 24000
    with wave.open(str(directory / "interaction-audio.wav"), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(mix.tobytes())
    robot = Frames(directory / "camera.mp4", camera_times)
    overview = Frames(directory / "overview.mp4", overview_times)
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
            "1280x800",
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
            "19",
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
    fonts = {
        s: ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", s)
        for s in [14, 17, 20, 25, 32]
    }
    for i in range(int(duration * 10) + 1):
        now = start + i / 10
        image = Image.new("RGB", (1280, 800), "#101c29")
        d = ImageDraw.Draw(image)
        d.text(
            (24, 20), "ORBO / ARE MY KEYS ON THE COUCH?", font=fonts[32], fill="#f1f6fa"
        )
        d.text(
            (24, 65),
            "Jessica · ElevenLabs input   /   GPT-Live conversation   /   Nav2 navigation   /   NEAR vision",
            font=fonts[17],
            fill="#a8bfcd",
        )
        d.text(
            (24, 104),
            "ACTUAL GAZEBO VIEW · THIRD-PERSON CAMERA",
            font=fonts[17],
            fill="#7ce3ed",
        )
        d.text((816, 104), "ROBOT CAMERA · 320 × 240", font=fonts[17], fill="#7ce3ed")
        a = overview.at(now)
        b = robot.at(now)
        if a:
            image.paste(a.resize((768, 512), Image.Resampling.LANCZOS), (24, 134))
        if b:
            image.paste(b.resize((440, 330), Image.Resampling.LANCZOS), (816, 134))
        else:
            d.text(
                (816, 260),
                "Awaiting localized camera frame",
                font=fonts[20],
                fill="#a8bfcd",
            )
        d.text(
            (816, 470),
            "Last localized frame; held during travel",
            font=fonts[14],
            fill="#a8bfcd",
        )
        seen = [e for e in timeline if e["seconds"] <= now]
        calls = [e for e in seen if e["kind"].startswith("near_")]
        status = "Listening to the question"
        if calls:
            last = calls[-1]
            if last["kind"] == "near_started":
                status = (
                    "NEAR interpreting the request"
                    if last["call"] == 1
                    else "NEAR checking the captured image"
                )
            elif last["kind"] == "near_completed":
                status = (
                    "Driving to the couch using the map"
                    if last["call"] == 1
                    else "Validating the vision response"
                )
            else:
                status = "Provider check failed; see report"
        if any(
            e["kind"] == "live" and e["type"] == "session.commentary.appended"
            for e in seen
        ):
            status = "Observation accepted by GPT-Live"
        d.text(
            (816, 489),
            f"{i / 10:05.1f}s · real-time playback",
            font=fonts[17],
            fill="#a8bfcd",
        )
        for j, line in enumerate(textwrap.wrap(status, 32)):
            d.text((816, 524 + j * 29), line, font=fonts[25], fill="#7ce3ed")
        d.text((816, 607), "Known map + AMCL / Nav2", font=fonts[17], fill="#a8bfcd")
        transcript = [
            e for e in seen if e["kind"] == "transcript" and now - e["seconds"] < 14
        ]
        if transcript:
            role = transcript[-1]["role"]
            text = ("JESSICA: " if role == "user" else "ORBO: ") + "".join(
                e["text"] for e in transcript if e["role"] == role
            )
        else:
            text = "Synthetic scene. The model receives the robot snapshot; the overview camera is only for this video."
        for j, line in enumerate(textwrap.wrap(text[-240:], 110)[:3]):
            d.text((24, 668 + j * 27), line, font=fonts[20], fill="#f1f6fa")
        d.text(
            (24, 774),
            "Recorded simulator frames + actual provider audio · Software receipt timing · No physical robot or live SLAM",
            font=fonts[14],
            fill="#a8bfcd",
        )
        encoder.stdin.write(image.tobytes())
    encoder.stdin.close()
    encoder.wait(timeout=60)
    robot.close()
    overview.close()
    if encoder.returncode:
        raise SystemExit("Video encoding failed")
    print(directory / "interaction.mp4")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    main(parser.parse_args().directory.resolve())
