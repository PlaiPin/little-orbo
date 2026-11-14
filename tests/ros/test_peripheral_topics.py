"""Real ROS topics/processes with synthetic input; no camera, gamepad or motors."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest
import yaml

from orbo_runtime.contracts import Action, Rejected, identity

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit Jazzy integration lane"
)


@pytest.fixture(autouse=True)
def isolated_domain():
    assert os.environ.get("ROS_DOMAIN_ID") == "96", "Never test on the robot domain"


async def wait_for(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def process(module, log, *args):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from orbo_runtime.{module} import main; main()",
            *args,
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def terminate(proc):
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


async def test_teleop_real_topics_gate_deadman_and_input_loss(tmp_path):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from sensor_msgs.msg import Joy
    from geometry_msgs.msg import TwistStamped
    from orbo_embedded_interfaces.msg import ControlSession
    from orbo_runtime.teleop import Teleoperation

    calibration = {
        key: 0.2
        for key in (
            "wheel_radius",
            "wheel_separation",
            "wheel_width",
            "base_length",
            "base_width",
            "base_height",
            "max_wheel_rad_s",
            "max_linear_m_s",
            "max_angular_rad_s",
        )
    }
    calibration.update(wheels_qualified=True, evidence_record="SYNTHETIC TEST ONLY")
    path = tmp_path / "calibration.yaml"
    path.write_text(yaml.safe_dump(calibration))
    rclpy.init()
    node = rclpy.create_node("teleop_adapter_test", namespace="/orbo")
    observer = rclpy.create_node("teleop_input_test", namespace="/orbo")
    # Match OrboAdapter's executor; a thread pool can backlog the 50 Hz
    # synthetic timer on the Mac and tests source starvation instead of teleop.
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    executor.add_node(observer)
    joy = observer.create_publisher(Joy, "teleop/joy", 1)
    nav_leases = observer.create_publisher(ControlSession, "control/lease", 1)
    unsolicited = observer.create_publisher(TwistStamped, "teleop/cmd_vel", 1)
    outputs, authorities, leases = [], [], []
    observer.create_subscription(
        TwistStamped, "drive_controller/cmd_vel", lambda m: outputs.append(m), 10
    )
    observer.create_subscription(
        ControlSession, "control/authority", lambda m: authorities.append(m), 10
    )
    observer.create_subscription(
        ControlSession, "control/teleop_lease", lambda m: leases.append(m), 10
    )
    state = {"held": False, "send": True}

    def publish():
        if state["send"]:
            msg = Joy()
            msg.header.stamp = observer.get_clock().now().to_msg()
            msg.axes = [1.0, 0.0] if state["held"] else [0.0, 0.0]
            msg.buttons = [int(state["held"])]
            joy.publish(msg)

    timer = observer.create_timer(0.02, publish)
    health = NS(boot_id=42, epoch=1, armed=False)

    class Adapter:
        session_type = ControlSession
        sequence = 0
        capabilities = {}
        stops = 0

        async def arm(self, epoch, wheels=False):
            assert wheels
            health.epoch, health.armed = epoch, True
            return health

        def _snapshot(self):
            return health

        async def stop_session(self):
            self.stops += 1
            health.armed = False

    adapter = Adapter()
    adapter.node = node
    teleop = Teleoperation(
        adapter,
        path,
        {
            "qualified": True,
            "evidence_record": "SYNTHETIC",
            "linear_axis": 0,
            "angular_axis": 1,
            "deadman_button": 0,
            "linear_scale": 1.0,
            "angular_scale": 1.0,
        },
    )
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    with (tmp_path / "gate.log").open("w") as log:
        gate = process(
            "velocity_gate",
            log,
            "--ros-args",
            "-p",
            "source:=teleop",
            "-p",
            "max_linear:=0.2",
            "-p",
            "max_angular:=0.2",
        )
        try:
            await wait_for(
                lambda: teleop.input.last is not None
                and node.count_subscribers("teleop/cmd_vel") >= 1
                and observer.count_publishers("drive_controller/cmd_vel") == 1
            )
            # A Nav2 lease must never authorize teleop even when stamped input arrives.
            for _ in range(5):
                lease = ControlSession(
                    interface_version=1,
                    operation=2,
                    enable_wheels=True,
                    boot_id=42,
                    epoch=1,
                    sequence=1,
                    issued_epoch_ms=time.time_ns() // 1_000_000,
                    valid_for_ms=200,
                )
                nav_leases.publish(lease)
                command = TwistStamped()
                command.header.stamp = observer.get_clock().now().to_msg()
                command.twist.linear.x = 0.02
                unsolicited.publish(command)
                await asyncio.sleep(0.02)
            assert not outputs and not authorities
            observer.destroy_publisher(unsolicited)

            def action(epoch):
                return Action(
                    identity(),
                    identity(),
                    "orbo",
                    "teleop.session",
                    {},
                    epoch,
                    time.time() + 2,
                    2,
                )

            work = asyncio.create_task(teleop.dispatch(action(2)))
            await wait_for(lambda: len(leases) >= 3 or work.done())
            if work.done():
                await work
            state["held"] = True
            await wait_for(lambda: any(m.twist.linear.x > 0 for m in outputs))
            assert max(m.twist.linear.x for m in outputs) <= 0.04 + 1e-6
            state["held"] = False
            count = len(outputs)
            await wait_for(lambda: any(m.twist.linear.x == 0 for m in outputs[count:]))
            result = await work
            assert (
                result.state == "completed"
                and not result.detail["physical_observation"]
            )
            assert adapter.stops == 1 and not health.armed
            assert all(m.valid_for_ms <= 200 for m in leases)
            await wait_for(
                lambda: teleop.input.last is not None and not teleop.input.last.held
            )
            work = asyncio.create_task(teleop.dispatch(action(3)))
            await wait_for(lambda: health.armed)
            state["held"] = True
            await asyncio.sleep(0.1)
            state["send"] = False
            with pytest.raises(Rejected, match="fresh teleop input"):
                await work
            assert not health.armed and adapter.stops == 2
            await wait_for(
                lambda: outputs and outputs[-1].twist.linear.x == 0, timeout=0.4
            )
            count = len(outputs)
            await asyncio.sleep(0.3)
            assert not any(m.twist.linear.x > 0 for m in outputs[count:])
        finally:
            terminate(gate)
            observer.destroy_timer(timer)
            executor.shutdown(timeout_sec=2)
            thread.join(timeout=2)
            node.destroy_node()
            observer.destroy_node()
            rclpy.shutdown()
        assert gate.returncode in (0, -signal.SIGINT), (
            tmp_path / "gate.log"
        ).read_text()


async def test_camera_monitor_real_image_info_and_disconnect(tmp_path):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, CameraInfo
    from diagnostic_msgs.msg import DiagnosticArray

    rclpy.init()
    node = rclpy.create_node("synthetic_camera_test", namespace="/orbo")
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    images = node.create_publisher(Image, "camera/image_raw", qos_profile_sensor_data)
    infos = node.create_publisher(
        CameraInfo, "camera/camera_info", qos_profile_sensor_data
    )
    received = []
    node.create_subscription(
        DiagnosticArray, "camera/diagnostics", lambda m: received.append(m), 10
    )
    send = {"enabled": True}

    def publish():
        if not send["enabled"]:
            return
        image = Image(height=2, width=2, encoding="rgb8", step=6, data=[0] * 12)
        image.header.frame_id = "camera_optical_frame"
        image.header.stamp = node.get_clock().now().to_msg()
        images.publish(image)
        info = CameraInfo(header=image.header, height=2, width=2)
        info.k = [100.0, 0.0, 1.0, 0.0, 100.0, 1.0, 0.0, 0.0, 1.0]
        infos.publish(info)

    timer = node.create_timer(0.05, publish)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    with (tmp_path / "camera.log").open("w") as log:
        monitor = process("camera", log)
        try:
            await wait_for(lambda: any(m.status[0].level == b"\x00" for m in received))
            send["enabled"] = False
            received.clear()
            await wait_for(
                lambda: any(
                    m.status[0].level == b"\x02" and "stale" in m.status[0].message
                    for m in received
                ),
                timeout=4,
            )
        finally:
            terminate(monitor)
            node.destroy_timer(timer)
            executor.shutdown(timeout_sec=2)
            thread.join(timeout=2)
            node.destroy_node()
            rclpy.shutdown()


async def test_keyboard_terminal_key_expiry_without_motion_authority(tmp_path):
    import pty
    import rclpy
    from sensor_msgs.msg import Joy
    from rclpy.executors import SingleThreadedExecutor

    rclpy.init()
    node = rclpy.create_node("keyboard_input_observer", namespace="/orbo")
    received = []
    node.create_subscription(Joy, "teleop/joy", lambda m: received.append(m), 10)
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    master, slave = pty.openpty()
    with (tmp_path / "keyboard.log").open("w") as log:
        keyboard = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from orbo_runtime.teleop_keyboard import main; main()",
            ],
            stdin=slave,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            await wait_for(lambda: received and list(received[-1].buttons) == [0])
            os.write(master, b"w")
            await wait_for(
                lambda: any(list(m.buttons) == [1] and m.axes[0] > 0 for m in received)
            )
            count = len(received)
            await wait_for(
                lambda: any(
                    list(m.buttons) == [0] and m.axes[0] == 0 for m in received[count:]
                ),
                timeout=0.5,
            )
            os.write(master, b"q")
            await asyncio.to_thread(keyboard.wait, timeout=3)
            assert keyboard.returncode == 0, (tmp_path / "keyboard.log").read_text()
        finally:
            if keyboard.poll() is None:
                terminate(keyboard)
            os.close(master)
            os.close(slave)
            executor.shutdown(timeout_sec=2)
            thread.join(timeout=2)
            node.destroy_node()
            rclpy.shutdown()
