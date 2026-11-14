"""Run with ORBO_ROS_TESTS=1 under Jazzy; synthetic DDS only, no devices."""

import os
import subprocess
import threading
import time

import pytest

from process_cleanup import stop_process_group

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit Jazzy integration lane"
)


def test_real_plugin_load_activation_and_stale_feedback_fault(tmp_path):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
    from std_msgs.msg import String
    from controller_manager_msgs.srv import (
        ListHardwareComponents,
        SetHardwareComponentState,
    )
    from orbo_embedded_interfaces.msg import WheelState
    import yaml

    assert os.environ.get("ROS_DOMAIN_ID") == os.environ.get("ORBO_TEST_DOMAIN", "96")
    assert os.environ.get("ROS_DOMAIN_ID") in {"96", "97", "98", "99"}

    # All geometry is explicitly synthetic and never installed as calibration.
    urdf = """<robot name="synthetic_orbo"><link name="base_link"/>
    <link name="left_wheel"/><link name="right_wheel"/>
    <joint name="left_wheel_joint" type="continuous"><parent link="base_link"/><child link="left_wheel"/><axis xyz="0 1 0"/></joint>
    <joint name="right_wheel_joint" type="continuous"><parent link="base_link"/><child link="right_wheel"/><axis xyz="0 1 0"/></joint>
    <ros2_control name="OrboWheels" type="system"><hardware><plugin>orbo_hardware/System</plugin><param name="calibrated">true</param><param name="max_velocity_rad_s">2.0</param></hardware>
    <joint name="left_wheel_joint"><command_interface name="velocity"/><state_interface name="position"/><state_interface name="velocity"/></joint>
    <joint name="right_wheel_joint"><command_interface name="velocity"/><state_interface name="position"/><state_interface name="velocity"/></joint>
    </ros2_control></robot>"""
    params = tmp_path / "controller.yaml"
    params.write_text(
        yaml.safe_dump(
            {
                "/orbo/controller_manager": {
                    "ros__parameters": {
                        "update_rate": 50,
                        "hardware_components_initial_state.inactive": ["OrboWheels"],
                    }
                }
            }
        )
    )
    rclpy.init()
    node = rclpy.create_node("synthetic_hardware_fixture")
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    qos = QoSProfile(
        depth=1,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        reliability=ReliabilityPolicy.RELIABLE,
    )
    description = node.create_publisher(String, "/orbo/robot_description", qos)
    publisher = node.create_publisher(WheelState, "/orbo/embedded/wheel_state", 10)
    enabled = True
    sequence = 0

    def publish():
        nonlocal sequence
        if not enabled:
            return
        sequence += 1
        msg = WheelState()
        msg.boot_id = 42
        msg.epoch = 1
        msg.sequence = sequence
        msg.sample_mono_us = time.monotonic_ns() // 1000
        msg.motor_sample_mono_us = [msg.sample_mono_us, msg.sample_mono_us]
        msg.valid = [True, True]
        msg.position_rad = [0.1, 0.2]
        msg.velocity_rad_s = [0.0, 0.0]
        publisher.publish(msg)

    node.create_timer(0.02, publish)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    output = (tmp_path / "controller.log").open("w")
    process = subprocess.Popen(
        [
            "ros2",
            "run",
            "controller_manager",
            "ros2_control_node",
            "--ros-args",
            "-r",
            "__ns:=/orbo",
            "--params-file",
            str(params),
        ],
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        description.publish(String(data=urdf))
        client = node.create_client(
            SetHardwareComponentState,
            "/orbo/controller_manager/set_hardware_component_state",
        )
        assert client.wait_for_service(timeout_sec=15), (
            tmp_path / "controller.log"
        ).read_text()
        # Service discovery precedes robot-description delivery/plugin loading.
        # Wait for the actual inactive hardware, not an arbitrary startup delay.
        listing = node.create_client(
            ListHardwareComponents, "/orbo/controller_manager/list_hardware_components"
        )
        assert listing.wait_for_service(timeout_sec=3)
        ready = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            description.publish(String(data=urdf))
            query = listing.call_async(ListHardwareComponents.Request())
            query_deadline = min(deadline, time.monotonic() + 2)
            while not query.done() and time.monotonic() < query_deadline:
                time.sleep(0.01)
            if query.done() and any(
                c.name == "OrboWheels" and c.state.id == 2
                for c in query.result().component
            ):
                ready = True
                break
            time.sleep(0.05)
        assert ready, (tmp_path / "controller.log").read_text()
        first_sample = sequence
        deadline = time.monotonic() + 3
        while (
            publisher.get_subscription_count() == 0 or sequence < first_sample + 5
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.get_subscription_count() > 0
        request = SetHardwareComponentState.Request()
        request.name = "OrboWheels"
        request.target_state.id = 3
        request.target_state.label = "active"
        future = client.call_async(request)
        deadline = time.monotonic() + 5
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert future.done() and future.result().ok, (
            tmp_path / "controller.log"
        ).read_text()
        enabled = False
        time.sleep(0.5)
        listing = node.create_client(
            ListHardwareComponents, "/orbo/controller_manager/list_hardware_components"
        )
        assert listing.wait_for_service(timeout_sec=3)
        future = listing.call_async(ListHardwareComponents.Request())
        deadline = time.monotonic() + 3
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert future.done()
        component = next(c for c in future.result().component if c.name == "OrboWheels")
        assert component.state.id != 3, "stale measured feedback must leave ACTIVE"
    finally:
        stop_process_group(process, timeout=5)
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        node.destroy_node()
        rclpy.shutdown()
        output.close()
