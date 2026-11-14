"""Real ROS action protocol against synthetic Nav2 server, never physical acceptance."""

import asyncio
import os
import threading
import time
from types import SimpleNamespace as NS

import pytest

from orbo_runtime.contracts import Action, Rejected, identity

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit Jazzy integration lane"
)


@pytest.fixture(autouse=True)
def isolated_domain():
    assert os.environ.get("ROS_DOMAIN_ID") == "96", "Never test on the robot domain"


async def test_nav2_goal_lease_and_cancel_protocol(tmp_path):
    import rclpy
    from rclpy.action import ActionServer, CancelResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from geometry_msgs.msg import TwistStamped
    from nav2_msgs.action import NavigateToPose
    from orbo_embedded_interfaces.msg import ControlSession
    import yaml
    from orbo_runtime.navigation import Navigation

    calibration = {
        key: 0.2
        for key in [
            "wheel_radius",
            "wheel_separation",
            "wheel_width",
            "base_length",
            "base_width",
            "base_height",
            "max_wheel_rad_s",
            "max_linear_m_s",
            "max_angular_rad_s",
        ]
    }
    calibration.update(
        wheels_qualified=True,
        evidence_record="SYNTHETIC TEST ONLY",
        navigation={
            "qualified": True,
            "operating_surface": "floor",
            "sensor_type": "LaserScan",
            "scan_topic": "/synthetic_scan",
            "sensor_frame": "test_laser",
            "max_linear_accel": 0.1,
            "max_angular_accel": 0.1,
            "goal_xy_tolerance": 0.05,
            "goal_yaw_tolerance": 0.1,
            "scan_timeout": 0.2,
            "collision_time_horizon": 1.0,
            "range_max": 2,
            "obstacle_max": 1,
            "raytrace_max": 1.5,
            "inflation_radius": 0.2,
            "waypoints": {"test": {"x": 0.3, "y": 0.0, "yaw": 0.0}},
        },
    )
    path = tmp_path / "synthetic.yaml"
    path.write_text(yaml.safe_dump(calibration))
    rclpy.init()
    node = rclpy.create_node("nav_adapter_test", namespace="/orbo")
    server_node = rclpy.create_node("nav_server_test", namespace="/orbo")
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    executor.add_node(server_node)
    received, leases, cancelled = [], [], []
    commands = server_node.create_publisher(TwistStamped, "cmd_vel", 1)
    planning = {"finished": False}

    def execute(goal_handle):
        received.append(goal_handle.request)
        planning["finished"] = False
        started = time.monotonic()
        end = started + 1.0
        while time.monotonic() < end:
            if goal_handle.is_cancel_requested:
                cancelled.append(True)
                goal_handle.canceled()
                return NavigateToPose.Result()
            if time.monotonic() - started > 0.65:
                planning["finished"] = True
                command = TwistStamped()
                command.header.stamp = server_node.get_clock().now().to_msg()
                commands.publish(command)
                feedback = NavigateToPose.Feedback()
                feedback.current_pose = goal_handle.request.pose
                feedback.distance_remaining = 0.1
                goal_handle.publish_feedback(feedback)
            time.sleep(0.01)
        goal_handle.succeed()
        return NavigateToPose.Result()

    server = ActionServer(
        server_node,
        NavigateToPose,
        "navigate_to_pose",
        execute_callback=execute,
        cancel_callback=lambda _: CancelResponse.ACCEPT,
        callback_group=ReentrantCallbackGroup(),
    )
    server_node.create_subscription(
        ControlSession, "control/lease", lambda m: leases.append(m), 10
    )
    health = NS(boot_id=42, epoch=1, armed=True)

    class Adapter:
        session_type = ControlSession
        capabilities = {}
        sequence = 0
        stops = 0

        async def arm(self, epoch, wheels=False):
            assert wheels and planning["finished"], (
                "must not arm while planner is delayed"
            )
            health.epoch = epoch
            health.armed = True
            return health

        def _snapshot(self):
            return health

        async def stop_session(self):
            self.stops += 1
            health.armed = False

    adapter = Adapter()
    adapter.node = node
    nav = Navigation(adapter, str(path))
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(5):
            while not nav.client.server_is_ready():
                await asyncio.sleep(0.02)

        def action(epoch):
            return Action(
                identity(),
                identity(),
                "orbo",
                "navigation.named_goal",
                {"waypoint": "test"},
                epoch,
                time.time() + 2,
                2,
            )

        result = await nav.dispatch(action(2))
        assert (
            result.state == "completed"
            and result.detail["physical_observation"] is False
        )
        assert received[0].pose.header.frame_id == "map"
        assert result.detail["motion_armed"] and result.detail["navigation_feedback"]
        assert (
            result.detail["navigation_feedback"][0]["source"]
            == "nav2_localization_feedback"
        )
        assert leases and all(m.enable_wheels and m.valid_for_ms <= 250 for m in leases)
        task = asyncio.create_task(nav.dispatch(action(3)))
        async with asyncio.timeout(2):
            while len(received) < 2:
                await asyncio.sleep(0.01)
        nav.stop()
        with pytest.raises(Rejected):
            await task
        async with asyncio.timeout(2):
            while not cancelled:
                await asyncio.sleep(0.01)
        assert adapter.stops == 2
    finally:
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        server.destroy()
        node.destroy_node()
        server_node.destroy_node()
        rclpy.shutdown()
