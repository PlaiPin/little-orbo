"""Exercise the real bench controller over DDS on an isolated domain; no hardware."""

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit ROS integration lane"
)


@pytest.fixture
def bench():
    assert os.environ.get("ROS_DOMAIN_ID") == "96", "Never test on the board domain"
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from orbo_embedded_interfaces.msg import (
        ActuatorStatus,
        ControlSession,
        FlipperCommand,
        Health,
    )
    from orbo_runtime.bench.flippers import FlipperBench, PREFIX

    rclpy.init()
    firmware = rclpy.create_node("flipper_test_firmware")
    client = rclpy.create_node("flipper_test_client")
    controller = FlipperBench(client)
    state = SimpleNamespace(
        boot=42,
        epoch=0,
        armed=False,
        fault=1,
        commands=[],
        arms=[],
        stops=0,
        deadline=None,
        reject=False,
        mode="normal",
        seq=0,
    )
    hp = firmware.create_publisher(Health, PREFIX + "health", qos_profile_sensor_data)
    sp = firmware.create_publisher(
        ActuatorStatus, PREFIX + "actuator_status", qos_profile_sensor_data
    )

    def status(command, value, reason=0):
        state.seq += 1
        sp.publish(
            ActuatorStatus(
                boot_id=state.boot,
                epoch=state.epoch,
                command_id=command.command_id,
                sequence=state.seq,
                state=value,
                reason=reason,
                evidence=1,
            )
        )

    def session(msg):
        if msg.operation == 0:
            state.armed = False
            state.deadline = None
            state.stops += 1
        elif msg.operation == 1:
            state.arms.append(msg)
            state.epoch, state.armed, state.fault = msg.epoch, True, 0

    def command(msg):
        state.commands.append(msg)
        if state.reject:
            status(msg, 4, 4)
        elif state.mode == "reboot":
            state.boot, state.epoch, state.armed = 43, 0, False
        elif state.mode == "fault":
            state.armed, state.fault = False, 6
            status(msg, 6, 6)
        else:
            state.deadline = time.monotonic() + msg.duration_ms / 1000
            status(msg, 1)

    def tick():
        hp.publish(
            Health(
                interface_version=1,
                boot_id=state.boot,
                epoch=state.epoch,
                mono_us=time.monotonic_ns() // 1000,
                epoch_ms=time.time_ns() // 10**6,
                armed=state.armed,
                clock_synced=True,
                fault=state.fault,
            )
        )
        if state.deadline is not None and time.monotonic() >= state.deadline:
            state.deadline = None
            status(state.commands[-1], 3)

    firmware.create_subscription(
        ControlSession, PREFIX + "control_session", session, 10
    )
    firmware.create_subscription(
        FlipperCommand, PREFIX + "flipper_command", command, 10
    )
    firmware.create_timer(0.05, tick)
    executor = SingleThreadedExecutor()
    executor.add_node(firmware)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        yield controller, state, firmware
    finally:
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        client.destroy_node()
        firmware.destroy_node()
        rclpy.shutdown()


def test_bounded_flipper_command_and_stop(bench, capsys):
    controller, state, _ = bench
    try:
        controller.run([1450, 1550], 100)
    finally:
        controller.stop()
    assert len(state.arms) == len(state.commands) == 1
    assert not state.arms[0].enable_wheels
    command = state.commands[0]
    assert list(command.pulse_us) == [1450, 1550]
    assert command.duration_ms == 100
    assert command.boot_id == 42 and command.epoch == state.epoch
    assert not state.armed and state.stops == 1
    assert "physical movement is not measured" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mode,pattern",
    [
        ("reject", "command failed"),
        ("fault", "session lost|command failed"),
        ("reboot", "restarted"),
    ],
)
def test_rejection_fault_and_reboot_stop_without_rearming(bench, mode, pattern):
    controller, state, _ = bench
    state.reject = mode == "reject"
    state.mode = mode
    try:
        with pytest.raises(RuntimeError, match=pattern):
            controller.run([1450, 1550], 100)
    finally:
        controller.stop()
    assert len(state.arms) == len(state.commands) == 1
    assert not state.armed


def test_existing_armed_session_is_not_taken_over(bench):
    controller, state, _ = bench
    state.armed = True
    with pytest.raises(RuntimeError, match="already armed"):
        controller.run([1500, 1500], 100)
    controller.stop()
    assert state.armed and not state.arms and state.stops == 0


def test_stale_health_and_replayed_status_are_rejected(bench):
    controller, _, _ = bench
    from orbo_embedded_interfaces.msg import ActuatorStatus, Health

    controller.on_health(
        Health(
            interface_version=1,
            boot_id=42,
            mono_us=1,
            epoch_ms=time.time_ns() // 10**6 - 1000,
            clock_synced=True,
        )
    )
    with pytest.raises(RuntimeError, match="Fresh synchronized"):
        controller.check()
    controller.boot, controller.epoch = 42, 2
    controller.on_status(
        ActuatorStatus(
            boot_id=42,
            epoch=1,
            command_id=controller.command_id,
            sequence=99,
            state=3,
            evidence=1,
        )
    )
    assert controller.result is None
    controller.on_status(
        ActuatorStatus(
            boot_id=42,
            epoch=2,
            command_id=controller.command_id,
            sequence=2,
            state=4,
            evidence=1,
        )
    )
    controller.on_status(
        ActuatorStatus(
            boot_id=42,
            epoch=2,
            command_id=controller.command_id,
            sequence=1,
            state=3,
            evidence=1,
        )
    )
    assert controller.result.state == 4


def test_cli_sigterm_disarms_the_mcu(bench):
    controller, state, _ = bench
    # The executable owns the only command publishers for this case.
    controller.node.destroy_publisher(controller.sessions)
    controller.node.destroy_publisher(controller.commands)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "orbo_runtime.bench.flippers",
            "--left-us",
            "1450",
            "--right-us",
            "1550",
            "--duration-ms",
            "5000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 12
        while not state.commands and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            time.sleep(0.02)
        assert state.commands
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130, stdout + stderr
        assert "MCU disarmed" in stdout
        assert not state.armed and state.stops == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
