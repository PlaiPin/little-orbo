"""Real Gazebo/DART + ros2_control: physical motion, contact, and command expiry."""

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ORBO_GAZEBO_TESTS") != "1",
    reason="explicit native Gazebo integration lane",
)
ROOT = Path(__file__).resolve().parents[2]


def yaw(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def test_physics_driving_and_wall_contact(tmp_path):
    assert os.environ.get("ROS_DOMAIN_ID") == "99"
    assert os.environ.get("GZ_PARTITION") == "little-orbo-sim-test"
    import rclpy
    from geometry_msgs.msg import TwistStamped
    from nav_msgs.msg import Odometry
    from rclpy.parameter import Parameter
    from gz.transport13 import Node as GzNode
    from gz.msgs10.pose_v_pb2 import Pose_V

    log_path = tmp_path / "gazebo.log"
    report = {"capture_kind": "simulation", "physics": "DART", "checks": {}}
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["ros2", "launch", "orbo_simulation", "room.launch.py", "gui:=false"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        rclpy.init()
        node = rclpy.create_node(
            "physics_acceptance",
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        pub = node.create_publisher(
            TwistStamped, "/orbo_sim/drive_controller/cmd_vel", 1
        )
        state = {"pose": None, "odom": None, "pose_at": 0.0, "odom_at": 0.0}
        transport = GzNode()

        def pose_received(msg):
            for pose in msg.pose:
                if pose.name == "orbo":
                    state.update(pose=pose, pose_at=time.monotonic())

        def odom_received(msg):
            state.update(odom=msg, odom_at=time.monotonic())

        transport.subscribe(Pose_V, "/world/orbo_room/dynamic_pose/info", pose_received)
        node.create_subscription(
            Odometry, "/orbo_sim/drive_controller/odom", odom_received, 10
        )

        def command(v=0.0, w=0.0):
            msg = TwistStamped()
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.twist.linear.x, msg.twist.angular.z = v, w
            pub.publish(msg)

        def run(seconds, velocity=None):
            start = node.get_clock().now().nanoseconds
            deadline = time.monotonic() + max(15, seconds * 5)
            while (node.get_clock().now().nanoseconds - start) / 1e9 < seconds:
                assert process.poll() is None, log_path.read_text()[-8000:]
                assert time.monotonic() < deadline, "Simulation clock stopped"
                rclpy.spin_once(node, timeout_sec=0.01)
                if velocity is not None:
                    command(*velocity)
                time.sleep(0.01)
            assert time.monotonic() - state["pose_at"] < 1, "No fresh Gazebo body pose"
            assert time.monotonic() - state["odom_at"] < 1, "No fresh encoder odometry"

        def position():
            p = state["pose"].position
            return p.x, p.y, p.z

        def displacement(a, b):
            return math.hypot(b[0] - a[0], b[1] - a[1])

        try:
            deadline = time.monotonic() + 60
            while (
                state["odom"] is None
                or state["pose"] is None
                or pub.get_subscription_count() == 0
            ):
                rclpy.spin_once(node, timeout_sec=0.05)
                assert process.poll() is None, log_path.read_text()[-8000:]
                assert time.monotonic() < deadline, log_path.read_text()[-8000:]
            run(0.6, (0.0, 0.0))
            settled = position()
            assert abs(settled[2] - 0.033) < 0.003, settled
            assert abs(state["pose"].orientation.x) < 0.02
            assert abs(state["pose"].orientation.y) < 0.02
            report["checks"]["resting_axle_height_m"] = settled[2]

            start = position()
            odom_start = state["odom"].pose.pose.position.x
            run(2.5, (0.12, 0.0))
            command()
            run(0.7, (0.0, 0.0))
            forward = position()
            distance = forward[0] - start[0]
            odom_distance = state["odom"].pose.pose.position.x - odom_start
            assert 0.20 < distance < 0.38, (distance, odom_distance)
            assert abs(forward[1] - start[1]) < 0.02
            assert abs(distance - odom_distance) < 0.025
            report["checks"].update(
                forward_body_m=distance, forward_encoder_m=odom_distance
            )

            run(1.5, (-0.10, 0.0))
            run(0.7, (0.0, 0.0))
            reverse = position()[0] - forward[0]
            assert reverse < -0.08, reverse
            report["checks"]["reverse_body_m"] = reverse

            start_yaw = yaw(state["pose"].orientation)
            start = position()
            run(1.5, (0.0, 0.7))
            run(0.7, (0.0, 0.0))
            angle = yaw(state["pose"].orientation) - start_yaw
            assert 0.7 < angle < 1.3, angle
            assert displacement(start, position()) < 0.035
            report["checks"]["turn_body_rad"] = angle

            # Stop publishing entirely: the controller timeout must stop the physical body.
            run(0.8, (0.1, 0.0))
            run(1.2)
            stopped = position()
            run(0.6)
            drift = displacement(stopped, position())
            assert drift < 0.004, drift
            report["checks"]["command_loss_drift_m"] = drift

            # Move the stopped robot near the east wall, then drive into it.
            # Ground truth must stop even if the wheels slip and encoder odometry advances.
            # Keep the blocking service client out of the Python process receiving
            # high-rate transport callbacks; the binding can hold the GIL while waiting.
            moved = subprocess.run(
                [
                    "gz",
                    "service",
                    "-s",
                    "/world/orbo_room/set_pose",
                    "--reqtype",
                    "gz.msgs.Pose",
                    "--reptype",
                    "gz.msgs.Boolean",
                    "--timeout",
                    "5000",
                    "--req",
                    'name: "orbo", position: {x: 1.70, y: -1, z: 0.034}, orientation: {w: 1}',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert moved.returncode == 0 and "data: true" in moved.stdout, moved
            run(0.8, (0.0, 0.0))
            run(3.0, (0.12, 0.0))
            contact = position()
            assert 1.84 < contact[0] < 1.94, contact
            run(1.0, (0.12, 0.0))
            assert abs(position()[0] - contact[0]) < 0.02, position()
            assert 0.02 < position()[2] < 0.05, position()
            run(0.6, (0.0, 0.0))
            report["checks"]["wall_stop_x_m"] = contact[0]
            report["passed"] = True
            target = ROOT / "artifacts/test-evidence/gazebo-macos"
            target.mkdir(parents=True, exist_ok=True)
            (target / "physics-report.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report, indent=2))
        finally:
            if rclpy.ok():
                command()
            transport.unsubscribe("/world/orbo_room/dynamic_pose/info")
            node.destroy_node()
            rclpy.shutdown()
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
