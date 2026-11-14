"""Real Gazebo navigation/camera plus explicitly synthetic verified-vision fixture.

This proves orchestration, short authority expiry and evidence plumbing. It does
not measure model accuracy, real signatures, physical hardware or provider access.
"""

import asyncio
import base64
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ORBO_EMBODIED_GAZEBO_TESTS") != "1",
    reason="explicit isolated embodied Gazebo lane",
)
ROOT = Path(__file__).resolve().parents[2]


class InconclusiveVisionFixture:
    calls = 0

    async def complete(self, messages, tools):
        from orbo_runtime.contracts import digest
        from orbo_runtime.providers import VerifiedReply

        self.calls += 1
        assert tools == []
        context = json.loads(messages[1]["content"][0]["text"])
        images = [
            p["image_url"]["url"]
            for p in messages[1]["content"]
            if p["type"] == "image_url"
        ]
        assert images and all(
            base64.b64decode(i.split(",", 1)[1]).startswith(b"\x89PNG") for i in images
        )
        body = {
            "task_id": context["task_id"],
            "revision": context["revision"],
            "status": "unknown",
            "identity": "unconfirmed",
            "observation_ids": context["required_json"]["observation_ids"],
            "occluded": False,
            "reason": "insufficient_view",
            "next_viewpoint": None,
            "matched_attributes": [],
        }
        message = {"content": json.dumps(body)}
        return VerifiedReply(
            message,
            {
                "provider": "near",
                "model": "synthetic-inconclusive-fixture",
                "e2ee": True,
                "signature_kind": "provider_tee",
                "response_id": "fixture-only-not-a-real-provider-proof",
                "message_digest": digest(message),
            },
            {},
        )


async def test_gazebo_task_navigation_capture_release_and_lease_expiry(tmp_path):
    assert os.environ.get("ROS_DOMAIN_ID") == "100"
    assert os.environ.get("GZ_PARTITION") == "little-orbo-embodied-test"
    from gz.msgs10.pose_v_pb2 import Pose_V
    from gz.transport13 import Node as GzNode, SubscribeOptions
    from geometry_msgs.msg import TwistStamped
    from orbo_runtime.broker import Broker
    from orbo_runtime.embodied_assistant import EmbodiedAssistant
    from orbo_runtime.evidence_memory import EvidenceMemory
    from orbo_runtime.gazebo_adapter import GazeboAdapter
    from orbo_runtime.observations import ObservationService, ROSFrameSource
    from orbo_runtime.storage import Store
    from orbo_runtime.vision import VerifiedVision

    target = ROOT / "artifacts/test-evidence/gazebo-embodied"
    target.mkdir(parents=True, exist_ok=True)
    report = {
        "provenance": "synthetic",
        "physics": "Gazebo DART",
        "passed": False,
        "provider": "deterministic fixture; no real verification or model-accuracy evidence",
    }
    log_path = tmp_path / "gazebo-embodied.log"
    log = log_path.open("w")
    process = subprocess.Popen(
        [
            "ros2",
            "launch",
            "orbo_simulation",
            "room.launch.py",
            "gui:=false",
            "embodied:=true",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    adapter = assistant = store = None
    transport = GzNode()
    body = {}

    def body_received(message):
        for p in message.pose:
            if p.name == "orbo":
                body.update(x=p.position.x, y=p.position.y, at=time.monotonic())

    samples = SubscribeOptions()
    samples.msgs_per_sec = 20
    transport.subscribe(
        Pose_V, "/world/orbo_room/dynamic_pose/info", body_received, samples
    )

    async def wait_for(predicate, seconds=60):
        deadline = time.monotonic() + seconds
        while not predicate():
            assert process.poll() is None, log_path.read_text()[-5000:]
            assert time.monotonic() < deadline, log_path.read_text()[-5000:]
            await asyncio.sleep(0.05)

    try:
        adapter = GazeboAdapter(ROOT / "ros_ws/src/orbo_simulation/config/goals.yaml")
        await adapter.ready()
        store = Store(tmp_path / "tasks.db")
        memory = EvidenceMemory(store, tmp_path / "images")
        frames = ROSFrameSource(
            adapter.node,
            adapter.pose_source.snapshot,
            frame_id="camera_optical_frame",
            calibration_version="gazebo-camera",
            clock_domain="gazebo",
            image_topic="/orbo_sim/camera/image_raw",
            info_topic="/orbo_sim/camera/camera_info",
        )
        reasoner = InconclusiveVisionFixture()
        assistant = EmbodiedAssistant(
            store,
            Broker(store, adapter),
            memory,
            ObservationService(memory, frames),
            VerifiedVision(reasoner, memory),
        )
        owner, environment = "simulation-owner", "simulated-living-room"
        for scope, destination in (
            ("task.execute", environment),
            ("robot.command", adapter.robot_id),
            ("observation.capture", environment),
            ("observation.retain", environment),
            ("observation.infer", environment),
            ("cloud.reasoning", "near"),
            ("memory.read", environment),
            ("memory.write", environment),
            ("result.release", "local"),
        ):
            store.grant(owner, scope, destination, True, store.clock.utc() + 600)
        assistant.tasks.places.register(
            owner,
            environment,
            "living-room-map",
            "tv area",
            viewpoints=["tv"],
        )
        request = {
            "kind": "inspect",
            "environment": environment,
            "map_version": "living-room-map",
            "purpose": "synthetic pipeline qualification",
            "target": "object identity in the tv area",
            "places": ["tv area"],
            "allowed_actions": ["navigate", "capture", "infer", "report"],
            "budget": {"seconds": 180, "observations": 1, "provider_calls": 1},
            "destination": "local",
            "provenance": "synthetic",
        }
        await wait_for(lambda: bool(body))
        start = (body["x"], body["y"])
        task = await assistant.submit(owner, "inspect-tv", request)
        await asyncio.wait_for(assistant.jobs[task["id"]], 180)
        task = assistant.tasks.get(owner, task["id"])
        report["navigation_operations"] = [
            dict(row)
            for row in store.db.execute(
                "SELECT state,result FROM task_operations WHERE task_id=? AND capability='navigate'",
                (task["id"],),
            )
        ]
        report["task"] = {
            "id": task["id"],
            "state": task["state"],
            "result": task["result"],
        }
        release = assistant.release(owner, task, "local")
        report["release"] = release
        assert release and len(release["findings"]) == 1, report
        assert release["partial"] and release["coverage"] == ["tv"]
        assert task["state"] == "completed" and reasoner.calls == 1
        assert math.dist(start, (body["x"], body["y"])) > 1
        error = math.hypot(body["x"] - 1.0, body["y"] + 0.55)
        assert error < 0.15, error
        report["tv_position_error_m"] = error
        operation = store.db.execute(
            "SELECT * FROM task_operations WHERE task_id=? AND capability='navigate'",
            (task["id"],),
        ).fetchone()
        assert (
            operation["state"] == "completed"
            and json.loads(operation["result"])["evidence"] == "simulation"
        )
        observation = memory.observation(
            owner,
            environment,
            release["findings"][0]["observation_ids"][0],
        )
        report["observation"] = {
            k: observation[k]
            for k in (
                "id",
                "clock_domain",
                "frame_id",
                "viewpoint",
                "pose",
                "width",
                "height",
            )
        }
        assert observation["clock_domain"] == "gazebo" and (
            observation["width"],
            observation["height"],
        ) == (320, 240)
        # Save the actual captured view as local test evidence, never contribute it.
        png = memory.image(owner, environment, observation["id"])
        (target / "tv-view.png").write_bytes(png)
        # After task authority ends, even fresh collision-output samples must not
        # move the robot: the independent gate holds the stopped epoch fenced.
        commands = adapter.node.create_publisher(TwistStamped, "cmd_vel", 1)
        await asyncio.sleep(0.6)
        stopped = (body["x"], body["y"])
        for _ in range(40):
            sample = TwistStamped()
            sample.header.stamp = adapter.node.get_clock().now().to_msg()
            sample.twist.linear.x = 0.1
            commands.publish(sample)
            await asyncio.sleep(0.05)
        drift = math.dist(stopped, (body["x"], body["y"]))
        report["unauthorized_fresh_command_drift_m"] = drift
        assert drift < 0.01
        assert adapter.dispatches == 1
        report["passed"] = True
    except BaseException as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if assistant:
            await assistant.close()
        if adapter:
            await adapter.close()
        if store:
            store.close()
        transport.unsubscribe("/world/orbo_room/dynamic_pose/info")
        try:
            process.send_signal(signal.SIGINT)
            await asyncio.to_thread(process.wait, timeout=15)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.to_thread(process.wait, timeout=5)
        log.close()
        (target / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (target / "gazebo.log").write_text(log_path.read_text())
