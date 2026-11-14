"""Native Gazebo sensors, AMCL/Nav2 goals, and physical stopping on cancellation."""

import json
import math
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ORBO_GAZEBO_TESTS") != "1",
    reason="explicit native Gazebo integration lane",
)
ROOT = Path(__file__).resolve().parents[2]


def test_living_room_sensors_and_nav2(tmp_path):
    assert os.environ.get("ROS_DOMAIN_ID") == "99"
    assert os.environ.get("GZ_PARTITION") == "little-orbo-sim-test"
    import rclpy
    import yaml
    from action_msgs.msg import GoalStatus
    from gz.msgs10.pose_v_pb2 import Pose_V
    from gz.transport13 import Node as GzNode
    from nav2_msgs.action import NavigateToPose
    from PIL import Image as PillowImage
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CameraInfo, Image, LaserScan

    target = ROOT / "artifacts/test-evidence/gazebo-macos"
    target.mkdir(parents=True, exist_ok=True)
    report = {"capture_kind": "simulation", "passed": False, "goals": []}
    report_path = target / "navigation-report.json"
    report_path.write_text(json.dumps(report) + "\n")
    log_path = tmp_path / "navigation.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["ros2", "launch", "orbo_simulation", "room.launch.py", "gui:=false"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        rclpy.init()
        navigator_type = runpy.run_path(
            str(ROOT / "ros_ws/src/orbo_simulation/scripts/navigate.py")
        )["Navigator"]
        nav = navigator_type()
        transport = GzNode()
        state = {}
        stamps = {key: [] for key in ("scan", "image", "info")}
        trajectory = []

        def body_received(msg):
            for pose in msg.pose:
                if pose.name == "orbo":
                    state["body"] = pose
                    state["body_at"] = time.monotonic()
                    trajectory.append(
                        [pose.position.x, pose.position.y, pose.position.z]
                    )

        transport.subscribe(Pose_V, "/world/orbo_room/dynamic_pose/info", body_received)

        def received(key, msg):
            state[key] = msg
            stamps[key].append(msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9)

        for key, message, topic in (
            ("scan", LaserScan, "scan"),
            ("image", Image, "camera/image_raw"),
            ("info", CameraInfo, "camera/camera_info"),
        ):
            nav.node.create_subscription(
                message,
                f"/orbo_sim/{topic}",
                lambda msg, key=key: received(key, msg),
                qos_profile_sensor_data,
            )

        def spin_until(predicate, timeout=60):
            deadline = time.monotonic() + timeout
            while not predicate():
                assert process.poll() is None, log_path.read_text()[-6000:]
                assert time.monotonic() < deadline, log_path.read_text()[-6000:]
                rclpy.spin_once(nav.node, timeout_sec=0.05)

        def run(seconds):
            start = nav.node.get_clock().now().nanoseconds
            spin_until(
                lambda: (nav.node.get_clock().now().nanoseconds - start) / 1e9
                >= seconds,
                20,
            )
            assert time.monotonic() - state["body_at"] < 1

        def position():
            p = state["body"].position
            return p.x, p.y

        try:
            spin_until(
                lambda: "body" in state and all(len(v) >= 20 for v in stamps.values())
            )
            # Measure steady-state rates after Nav2 and renderer startup.
            nav.wait_until_active()
            for samples in stamps.values():
                samples.clear()
            run(2.5)
            scan = state["scan"]
            assert scan.header.frame_id == "lidar_link"
            assert len(scan.ranges) == 360
            finite = [r for r in scan.ranges if math.isfinite(r)]
            assert len(finite) > 300 and min(finite) > 0.06 and max(finite) < 8
            frame, info = state["image"], state["info"]
            assert (
                frame.header.frame_id == info.header.frame_id == "camera_optical_frame"
            )
            assert (frame.width, frame.height, frame.encoding) == (320, 240, "rgb8")
            assert (info.width, info.height) == (320, 240) and info.k[0] > 0
            assert (
                len(frame.data) == 320 * 240 * 3
                and max(frame.data) - min(frame.data) > 50
            )
            PillowImage.frombytes("RGB", (320, 240), bytes(frame.data)).save(
                target / "camera.png"
            )
            rates = {k: (len(v) - 1) / (v[-1] - v[0]) for k, v in stamps.items()}
            assert all(8 < rate < 12 for rate in rates.values()), rates
            report["sensors"] = {
                "rate_sim_hz": rates,
                "lidar_rays": 360,
                "camera_size": [320, 240],
            }

            goals = yaml.safe_load(
                (ROOT / "ros_ws/src/orbo_simulation/config/goals.yaml").read_text()
            )
            for name in ("tv", "sofa", "reading", "entry"):
                goal = goals[name]
                nav.go(*goal, timeout=180)
                run(0.5)
                x, y = position()
                error = math.hypot(x - goal[0], y - goal[1])
                q = state["body"].orientation
                yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y**2 + q.z**2))
                yaw_error = abs(
                    math.atan2(math.sin(yaw - goal[2]), math.cos(yaw - goal[2]))
                )
                report["goals"].append(
                    {
                        "name": name,
                        "goal": goal,
                        "body": [x, y, yaw],
                        "position_error_m": error,
                        "yaw_error_rad": yaw_error,
                    }
                )
                assert error < 0.15 and yaw_error < 0.3, report["goals"][-1]
                print(f"{name}: physical position error {error:.3f} m", flush=True)

            # Cancellation must stop the real simulated body, not only the action.
            msg = NavigateToPose.Goal()
            msg.pose.header.frame_id = "map"
            msg.pose.header.stamp = nav.node.get_clock().now().to_msg()
            msg.pose.pose.position.x, msg.pose.pose.position.y = goals["tv"][:2]
            msg.pose.pose.orientation.w = 1.0
            nav.handle = nav.wait(nav.client.send_goal_async(msg), 10)
            assert nav.handle.accepted
            result = nav.handle.get_result_async()
            start = position()
            spin_until(lambda: math.dist(start, position()) > 0.15, 20)
            assert nav.cancel().goals_canceling
            assert nav.wait(result, 10).status == GoalStatus.STATUS_CANCELED
            nav.handle = None
            run(1.0)
            stopped = position()
            run(1.0)
            drift = math.dist(stopped, position())
            assert drift < 0.01, drift
            report["cancel_stop_drift_m"] = drift
            # Exercise the actual CLI's Ctrl-C handler while ROS is still alive.
            client_log = tmp_path / "goal-client.log"
            with client_log.open("w") as output:
                client = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "ros_ws/src/orbo_simulation/scripts/navigate.py"),
                        "named",
                        "sofa",
                    ],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                try:
                    start = position()
                    spin_until(lambda: math.dist(start, position()) > 0.15, 20)
                    client.send_signal(signal.SIGINT)
                    spin_until(lambda: client.poll() is not None, 10)
                    assert client.returncode == 130, client_log.read_text()
                    assert "1 goal(s) canceling" in client_log.read_text(), (
                        client_log.read_text()
                    )
                    run(1.0)
                    stopped = position()
                    run(1.0)
                    drift = math.dist(stopped, position())
                    assert drift < 0.01, drift
                    report["interrupt_stop_drift_m"] = drift
                finally:
                    if client.poll() is None:
                        client.kill()
                    client.wait(timeout=5)
            assert all(0.025 < p[2] < 0.045 for p in trajectory)
            report["passed"] = True
        except BaseException as error:
            report["failure"] = str(error)
            raise
        finally:
            if nav.handle is not None:
                try:
                    nav.cancel()
                except Exception:
                    pass
            transport.unsubscribe("/world/orbo_room/dynamic_pose/info")
            nav.close()
            rclpy.shutdown()
            try:
                # Launch forwards SIGINT to children. Signaling the group here
                # interrupts Python cleanup twice and can strand native nodes.
                process.send_signal(signal.SIGINT)
                process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            (target / "trajectory.json").write_text(json.dumps(trajectory) + "\n")
            (target / "navigation.log").write_text(log_path.read_text())
