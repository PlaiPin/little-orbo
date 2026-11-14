#!/usr/bin/env python3
"""Opt-in synthetic Gazebo demo using production runtime and live providers.

Retains synthetic camera frames, prompts, verified answers and Live audio locally.
Run in the isolated Pixi ROS environment; never connects to physical hardware.
"""

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
import tomllib
import wave

from orbo_runtime.cli import LocalService
from orbo_runtime.live_e2e import live_stage
from orbo_runtime.live_voice import LiveWebSocket
from orbo_runtime.providers import serializable

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


class RecordedTransport(LiveWebSocket):
    def __init__(self, directory, event):
        super().__init__()
        self.directory, self.event = directory, event
        self.chunks, self.events = [], []
        self.started = None

    async def connect(self):
        await super().connect()
        self.started = time.monotonic()

    def record(self, direction, event):
        now = time.monotonic()
        clean = dict(event)
        if event["type"] == "session.output_audio.delta":
            pcm = base64.b64decode(clean.pop("delta"))
            self.chunks.append((now - self.started, pcm))
            clean["pcm_bytes"] = len(pcm)
        clean.pop("audio", None)
        if event["type"] != "session.input_audio.append":
            self.events.append(
                {"seconds": now - self.started, "direction": direction, **clean}
            )
        if event["type"] in {
            "session.started",
            "session.delegation.requested",
            "session.commentary.appended",
            "session.closed",
        }:
            self.event("live", type=event["type"])
        if "transcript" in event["type"]:
            self.event(
                "transcript",
                role="user" if "input" in event["type"] else "assistant",
                text=event.get("delta", ""),
            )

    async def send(self, event):
        self.record("sent", event)
        await super().send(event)

    async def receive(self):
        event = await super().receive()
        self.record("received", event)
        return event

    def write(self):
        save(self.directory / "live-events.json", self.events)
        # Concatenated output is listenable; reception offsets are saved separately.
        with wave.open(str(self.directory / "live-response.wav"), "wb") as output:
            output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            output.writeframes(b"".join(pcm for _, pcm in self.chunks))
        save(
            self.directory / "live-audio-timing.json",
            [
                {"received_seconds": at, "frames": len(pcm) // 2}
                for at, pcm in self.chunks
            ],
        )


async def main(args):
    if args.voice_only and not args.voice:
        raise SystemExit("--voice-only requires --voice")
    if not args.allow_cloud or not args.allow_motion:
        raise SystemExit("Requires --allow-cloud --allow-motion (Gazebo only)")
    if (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION")) != (
        "100",
        "little-orbo-embodied-test",
    ):
        raise SystemExit(
            "Use tools/run-gazebo-near-live.sh in the isolated simulation environment"
        )
    keys = args.scenario == "keys"
    if keys and args.voice and not args.request_audio:
        raise SystemExit(
            "The keys scenario requires explicitly generated --request-audio"
        )
    waypoint, place = ("sofa", "couch") if keys else ("tv", "tv area")
    goal = (0.9, 0.85) if keys else (1.0, -0.55)
    width, height = 320, 240
    recording_fps = 5 if keys else 10
    model_path = (
        ROOT
        / "ros_ws/src/orbo_simulation/models"
        / ("keys_scene" if keys else "vision_props")
        / "model.sdf"
    )
    phrase = (
        "Hey Orbo, are my keys on the couch?"
        if keys
        else "Please go to the TV area and describe the visible objects and their colors."
    )
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    events = []

    def event(kind, **values):
        row = {"seconds": round(time.monotonic() - started, 3), "kind": kind, **values}
        events.append(row)
        save(out / "timeline.json", events)
        print(json.dumps(row), flush=True)

    config = tomllib.loads((ROOT / "deploy/profiles/gazebo-embodied.toml").read_text())
    config["runtime"].update(
        database=str(out / "journal.db"), socket=str(out / "runtime.sock")
    )
    # Unix socket paths on macOS are limited to 104 bytes.
    config["runtime"]["socket"] = f"/tmp/orbo-live-{os.getpid()}.sock"
    config["near"].update(enabled=True, timeout_seconds=120)
    if keys:
        config["near"]["max_completion_tokens"] = 4096
    config["embodied"]["image_directory"] = str(out / "private-images")
    if keys:
        config["embodied"]["camera_calibration"] = "gazebo-pinhole-320x240"
    save(out / "runtime-config.json", config)
    labels = {
        "provenance": "synthetic",
        "expected_props": [
            {
                "item": "bottle",
                "color": "red",
                "count": "one",
                "position": [1.25, 0.35],
            },
            {"item": "book", "color": "blue", "count": "one", "position": [0.95, 0.25]},
            {"item": "box", "color": "brown", "count": "one", "position": [0.63, 0.35]},
        ],
        "labels_sent_to_model": False,
        "model_sha256": hashlib.sha256(
            (
                ROOT / "ros_ws/src/orbo_simulation/models/vision_props/model.sdf"
            ).read_bytes()
        ).hexdigest(),
    }
    if keys:
        labels = {
            "provenance": "synthetic",
            "expected_props": [
                {
                    "item": "keys",
                    "count": 2,
                    "accessory": "blue key fob",
                    "surface": "floor",
                    "nearby": "sofa",
                    "position": [0.24, 1.03],
                }
            ],
            "location_labels_sent_to_model": False,
            "reference_description": "Two metal keys on a ring with a blue rectangular fob",
            "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        }
    save(out / "ground-truth.json", labels)
    log = (out / "gazebo.log").open("w")
    proc = subprocess.Popen(
        [
            "ros2",
            "launch",
            "orbo_simulation",
            "room.launch.py",
            "gui:=false",
            "embodied:=true",
            f"camera_width:={width}",
            f"camera_height:={height}",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    service = server = recorder = None
    recording = None
    overview_recorder = None
    overview = {}
    overview_times = []
    frame_times = []
    report = {
        "passed": False,
        "provenance": "synthetic",
        "provider": "live NEAR, E2EE and provider_tee verification",
        "voice_requested": args.voice,
        "scenario": args.scenario,
        "goal": {"waypoint": waypoint, "x": goal[0], "y": goal[1]},
    }
    body = {}
    poses = []
    from gz.transport13 import Node, SubscribeOptions
    from gz.msgs10.pose_v_pb2 import Pose_V

    transport = Node()
    options = SubscribeOptions()
    options.msgs_per_sec = 10

    def body_received(message):
        for p in message.pose:
            if p.name == "orbo":
                sample = {
                    "seconds": round(time.monotonic() - started, 3),
                    "x": p.position.x,
                    "y": p.position.y,
                }
                body.update(sample)
                poses.append(sample)

    transport.subscribe(
        Pose_V, "/world/orbo_room/dynamic_pose/info", body_received, options
    )
    if keys:
        from gz.msgs10.image_pb2 import Image

        def overview_received(message):
            if (
                message.width == 960
                and message.height == 640
                and len(message.data) == 960 * 640 * 3
            ):
                overview.update(data=bytes(message.data))

        transport.subscribe(Image, "/orbo_demo/overview", overview_received, options)
    try:
        service = LocalService(config)
        await service.broker.adapter.ready()
        event("ready")
        spawn = await asyncio.create_subprocess_exec(
            "ros2",
            "run",
            "ros_gz_sim",
            "create",
            "-world",
            "orbo_room",
            "-file",
            str(model_path),
            "-name",
            "keys_scene" if keys else "vision_props",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(spawn.communicate(), 30)
        (out / "spawn.log").write_bytes(stdout)
        if spawn.returncode:
            raise RuntimeError("Prop spawning failed")
        event("props_spawned")
        owner, environment = service.owner, service.embodied.environment
        for scope, target in [
            ("task.execute", environment),
            ("robot.command", "orbo-gazebo"),
            ("observation.capture", environment),
            ("observation.retain", environment),
            ("observation.infer", environment),
            ("cloud.reasoning", "near"),
            ("memory.read", environment),
            ("memory.write", environment),
            ("result.release", "local"),
            ("result.release", "openai"),
        ]:
            service.store.grant(
                owner, scope, target, True, service.store.clock.utc() + 1200
            )
        await service.call(
            {
                "method": "place.register",
                "args": {
                    "name": place,
                    "aliases": ["sofa"] if keys else [],
                    "viewpoints": [waypoint],
                },
            }
        )
        if keys:
            service.embodied.memory.reference(
                owner,
                environment,
                "My keys: two metal keys on a ring with a blue rectangular key fob.",
                attributes=["two metal keys", "blue key fob"],
            )
            overview_recorder = subprocess.Popen(
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
                    "960x640",
                    "-framerate",
                    str(recording_fps),
                    "-i",
                    "-",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    str(out / "overview.mp4"),
                ],
                stdin=subprocess.PIPE,
            )
        complete = service.reasoner.complete
        call_number = 0

        sdk_create = service.reasoner.client.chat.completions.create

        async def observed_create(**kwargs):
            completion = await sdk_create(**kwargs)
            save(
                out / f"completion-metadata-{call_number:02d}.json",
                {
                    "response_id": completion.id,
                    "model": completion.model,
                    "finish_reasons": [c.finish_reason for c in completion.choices],
                    "usage": completion.usage.model_dump(mode="json")
                    if completion.usage
                    else {},
                    "max_completion_tokens": kwargs.get("max_completion_tokens"),
                    "verification": "metadata only; see response receipt for verification",
                },
            )
            return completion

        service.reasoner.client.chat.completions.create = observed_create

        async def recorded_complete(messages, tools):
            nonlocal call_number
            call_number += 1
            number = call_number
            begin = time.monotonic()
            event("near_started", call=number)
            # Keep exact image bytes separately, replace data URLs with file/hash pointers.
            public = json.loads(json.dumps(messages))
            for msg in public:
                if isinstance(msg.get("content"), list):
                    for part in msg["content"]:
                        if part.get("type") == "image_url":
                            raw = base64.b64decode(
                                part["image_url"]["url"].split(",", 1)[1]
                            )
                            name = f"capture-{number:02d}.png"
                            (out / name).write_bytes(raw)
                            part["image_url"] = {
                                "file": name,
                                "sha256": hashlib.sha256(raw).hexdigest(),
                            }
            save(out / f"prompt-{number:02d}.json", public)
            try:
                reply = await complete(messages, tools)
                save(
                    out / f"response-{number:02d}.json",
                    serializable(dataclasses.asdict(reply)),
                )
                event(
                    "near_completed",
                    call=number,
                    elapsed=round(time.monotonic() - begin, 3),
                    message={"content": reply.message.get("content")},
                    usage=reply.usage,
                )
                return reply
            except Exception as error:
                event(
                    "near_failed",
                    call=number,
                    error=type(error).__name__,
                    reason=str(error) if type(error).__name__ == "Rejected" else None,
                )
                raise

        service.reasoner.complete = recorded_complete
        recorder = subprocess.Popen(
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
                f"{width}x{height}",
                "-framerate",
                str(recording_fps),
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                str(out / "camera.mp4"),
            ],
            stdin=subprocess.PIPE,
        )

        async def record():
            last = None
            while True:
                try:
                    frame = service.embodied.frames.snapshot()
                    last = frame.data
                except Exception:
                    pass
                if last:
                    await asyncio.to_thread(recorder.stdin.write, last)
                    frame_times.append(round(time.monotonic() - started, 3))
                    if len(frame_times) % 10 == 0:
                        from orbo_runtime.observations import encode_png

                        def write_preview():
                            (out / "latest.png").write_bytes(encode_png(frame)[0])

                        await asyncio.to_thread(write_preview)
                if overview_recorder and overview.get("data"):
                    await asyncio.to_thread(
                        overview_recorder.stdin.write, overview["data"]
                    )
                    overview_times.append(round(time.monotonic() - started, 3))
                await asyncio.sleep(1 / recording_fps)

        recording = asyncio.create_task(record())
        event("camera_started")
        await asyncio.sleep(2)
        server = await asyncio.start_unix_server(
            service.handle, config["runtime"]["socket"], limit=65537
        )
        Path(config["runtime"]["socket"]).chmod(0o600)
        if not args.voice_only:
            start = dict(body)
            event("structured_task_started", body=start)
            task = await service.call(
                {
                    "method": "task.submit",
                    "args": {
                        "key": "gazebo-live-description",
                        "request": {
                            "kind": "describe",
                            "target": "Describe the visible objects and their colors in this view.",
                            "purpose": "owner-requested synthetic live navigation and vision test",
                            "places": [place],
                            "allowed_actions": [
                                "navigate",
                                "capture",
                                "infer",
                                "report",
                            ],
                            "budget": {
                                "seconds": 240,
                                "observations": 1,
                                "provider_calls": 1,
                            },
                        },
                    },
                }
            )
            await asyncio.wait_for(service.embodied.assistant.jobs[task["id"]], 250)
            task = service.embodied.assistant.tasks.get(owner, task["id"])
            release = await service.call(
                {"method": "task.result", "args": {"task_id": task["id"]}}
            )
            proof = await service.call(
                {"method": "task.evidence", "args": {"task_id": task["id"]}}
            )
            report.update(
                task=task,
                release=release,
                provider_evidence=proof,
                navigation_start=start,
                navigation_end=dict(body),
                goal_position_error_m=math.hypot(
                    body["x"] - goal[0], body["y"] - goal[1]
                ),
            )
            event(
                "structured_task_finished",
                state=task["state"],
                release=release,
                goal_position_error_m=report["goal_position_error_m"],
            )
            report["passed"] = (
                task["state"] == "completed"
                and proof["live_service"]
                and report["goal_position_error_m"] < 0.15
            )
            save(out / "report.json", report)
        if args.voice:
            (out / "spoken-request.txt").write_text(phrase + "\n")
            if args.request_audio:
                from orbo_runtime.live_e2e import read_audio

                read_audio(args.request_audio)
                shutil.copyfile(args.request_audio, out / "request.wav")
                if args.request_audio.with_suffix(".json").exists():
                    shutil.copyfile(
                        args.request_audio.with_suffix(".json"),
                        out / "input-voice.json",
                    )
            else:
                subprocess.run(
                    [
                        "say",
                        "-v",
                        "Samantha",
                        "-r",
                        "155",
                        "-o",
                        str(out / "request.aiff"),
                        phrase,
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-i",
                        str(out / "request.aiff"),
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_s16le",
                        str(out / "request.wav"),
                    ],
                    check=True,
                )
            event(
                "voice_started",
                body=dict(body),
                text=phrase,
                input_voice=args.input_voice
                if args.request_audio
                else "macOS Samantha synthetic speech",
            )
            voice = RecordedTransport(out, event)
            try:
                report["voice"] = await asyncio.wait_for(
                    live_stage(
                        {
                            "audio": str(out / "request.wav"),
                            "near_model": config["near"]["model"],
                            "live_backend": "runtime",
                            "socket": config["runtime"]["socket"],
                            "live_seconds": 240 if keys else 120,
                            "stage_seconds": 360 if keys else 240,
                        },
                        transport=voice,
                    ),
                    380 if keys else 260,
                )
            except Exception as error:
                report["voice"] = {
                    "passed": False,
                    "error": type(error).__name__,
                    "detail": getattr(error, "evidence", None),
                    "cause": type(error.__cause__).__name__
                    if error.__cause__
                    else None,
                }
            finally:
                voice.write()
            event("voice_finished", result=report["voice"], body=dict(body))
            if args.voice_only:
                report["passed"] = bool(
                    report["voice"].get("finalized")
                    and report["voice"].get("verified_delegations")
                )
                report["goal_position_error_m"] = math.hypot(
                    body["x"] - goal[0], body["y"] - goal[1]
                )
        report["operations"] = [
            dict(row)
            for row in service.store.db.execute(
                "SELECT task_id,capability,state,result FROM task_operations"
            )
        ]
        completed = {
            op["capability"]
            for op in report["operations"]
            if op["state"] == "completed"
        }
        report["passed"] = bool(
            report["passed"]
            and report["goal_position_error_m"] < 0.15
            and {"navigate", "capture", "infer"} <= completed
            and (not args.voice or report["voice"].get("finalized"))
        )
        report["observations"] = [
            json.loads(row[0])
            for row in service.store.db.execute(
                "SELECT body FROM evidence_observations"
            )
        ]
        save(out / "report.json", report)
    except Exception as error:
        report["error"] = type(error).__name__
        report["reason"] = str(error)[:500]
        event("failed", error=type(error).__name__, reason=str(error)[:500])
        save(out / "report.json", report)
        raise
    finally:
        save(out / "poses.json", poses)
        save(out / "camera-frame-times.json", frame_times)
        save(out / "overview-frame-times.json", overview_times)
        if recording:
            recording.cancel()
            await asyncio.gather(recording, return_exceptions=True)
        if recorder:
            recorder.stdin.close()
            recorder.wait(timeout=30)
        if overview_recorder:
            overview_recorder.stdin.close()
            overview_recorder.wait(timeout=30)
        if server:
            server.close()
            await server.wait_closed()
        if service:
            await service.close()
        Path(config["runtime"]["socket"]).unlink(missing_ok=True)
        os.killpg(proc.pid, signal.SIGINT)
        try:
            await asyncio.to_thread(proc.wait, 20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        log.close()
        event("finished", passed=report["passed"])
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--allow-motion", action="store_true")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--scenario", choices=["props", "keys"], default="props")
    parser.add_argument("--request-audio", type=Path)
    parser.add_argument("--input-voice", default="external synthetic speech")
    parser.add_argument("--voice-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
