"""Synthetic MCU in its own process, with the same independent expiry checks."""

import multiprocessing
import os
import time


def serve(connection):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from orbo_embedded_interfaces.msg import (
        WheelState,
        WheelCommand,
        Health,
        ControlSession,
        FlipperCommand,
        ActuatorStatus,
    )

    assert os.environ.get("ROS_DOMAIN_ID") == os.environ.get("ORBO_TEST_DOMAIN", "96")
    assert os.environ.get("ROS_DOMAIN_ID") in {"96", "97", "98", "99"}
    assert os.environ.get("ORBO_ROS_TESTS") == "1"
    rclpy.init()
    firmware = rclpy.create_node("synthetic_firmware")
    wp = firmware.create_publisher(
        WheelState, "/orbo/embedded/wheel_state", qos_profile_sensor_data
    )
    hp = firmware.create_publisher(
        Health, "/orbo/embedded/health", qos_profile_sensor_data
    )
    sp = firmware.create_publisher(
        ActuatorStatus, "/orbo/embedded/actuator_status", qos_profile_sensor_data
    )
    state = dict(
        boot=42,
        epoch=0,
        armed=False,
        arms=0,
        sequence=0,
        position=[100.0, 150.0],
        measured_velocity=[0.4, 0.4],
        started=None,
        received=[],
        deadline=None,
        feedback=True,
        stops=0,
        fault=0,
        wheels_enabled=False,
        flipper_active=False,
        flipper_commands=[],
        flipper_finishes=[],
        flipper_deadline=None,
        reject_flippers=False,
        status_sequence=0,
    )

    timing = {"feedback": [], "command": [], "session": []}

    def session(msg):
        timing["session"].append([time.monotonic(), msg.operation])
        if msg.operation == 0:
            state["armed"] = False
            state["flipper_active"] = False
            state["flipper_deadline"] = None
            state["stops"] += 1
        elif msg.operation == 1:
            state.update(
                epoch=msg.epoch,
                armed=True,
                started=time.monotonic(),
                deadline=None,
                wheels_enabled=msg.enable_wheels,
                fault=0,
            )
            state["arms"] += 1

    def command(msg):
        timing["command"].append(
            [time.monotonic(), time.time_ns() // 10**6 - msg.issued_epoch_ms]
        )
        state["received"].append((time.monotonic(), msg))
        if (
            msg.boot_id == state["boot"]
            and msg.epoch == state["epoch"]
            and state["armed"]
        ):
            state["deadline"] = (msg.issued_epoch_ms + msg.valid_for_ms) / 1000

    def status(msg, value, reason=0):
        state["status_sequence"] += 1
        sp.publish(
            ActuatorStatus(
                boot_id=state["boot"],
                epoch=state["epoch"],
                command_id=msg.command_id,
                sequence=state["status_sequence"],
                mono_us=time.monotonic_ns() // 1000,
                state=value,
                reason=reason,
                evidence=1,
            )
        )

    def flipper(msg):
        state["flipper_commands"].append((time.monotonic(), msg))
        age = time.time_ns() // 10**6 - msg.issued_epoch_ms
        if (
            state["reject_flippers"]
            or not state["armed"]
            or state["flipper_active"]
            or msg.boot_id != state["boot"]
            or msg.epoch != state["epoch"]
            or not 0 <= age < msg.valid_for_ms
            or not 0 < msg.duration_ms <= 5000
            or not all(1400 <= p <= 1600 for p in msg.pulse_us)
        ):
            status(msg, 4, 4)
            return
        state["flipper_active"] = True
        state["flipper_deadline"] = time.monotonic() + msg.duration_ms / 1000
        status(msg, 1)

    # The node retains its subscriptions for the process lifetime.
    firmware.create_subscription(
        ControlSession, "/orbo/embedded/control_session", session, 10
    )
    firmware.create_subscription(
        WheelCommand, "/orbo/embedded/wheel_command", command, 10
    )
    firmware.create_subscription(
        FlipperCommand, "/orbo/embedded/flipper_command", flipper, 10
    )
    previous_tick = time.monotonic()

    def feedback():
        nonlocal previous_tick
        now_s = time.monotonic()
        dt, previous_tick = now_s - previous_tick, now_s
        if state["deadline"] is not None and time.time() >= state["deadline"]:
            state["armed"] = False
        if not state["armed"]:
            state["flipper_active"] = False
            state["flipper_deadline"] = None
        elif (
            state["flipper_deadline"] is not None and now_s >= state["flipper_deadline"]
        ):
            state["flipper_active"] = False
            state["flipper_deadline"] = None
            msg = state["flipper_commands"][-1][1]
            state["flipper_finishes"].append(now_s)
            status(msg, 3)
        moving = (
            state["armed"] and state["wheels_enabled"] and now_s - state["started"] > 1
        )
        if moving:
            for i in range(2):
                state["position"][i] += state["measured_velocity"][i] * dt
        if not state["feedback"]:
            return
        timing["feedback"].append([time.monotonic(), state["armed"]])
        state["sequence"] += 1
        now = time.monotonic_ns() // 1000
        wp.publish(
            WheelState(
                boot_id=state["boot"],
                epoch=state["epoch"],
                sequence=state["sequence"],
                sample_mono_us=now,
                motor_sample_mono_us=[now, now],
                epoch_ms=time.time_ns() // 10**6,
                armed=state["armed"],
                valid=[True, True],
                hardware_error=[0, 0],
                position_rad=list(state["position"]),
                velocity_rad_s=state["measured_velocity"] if moving else [0.0, 0.0],
            )
        )
        if state["sequence"] % 10 == 0:
            hp.publish(
                Health(
                    interface_version=1,
                    boot_id=state["boot"],
                    epoch=state["epoch"],
                    armed=state["armed"],
                    clock_synced=True,
                    mono_us=now,
                    fault=state["fault"],
                    epoch_ms=time.time_ns() // 10**6,
                )
            )

    firmware.create_timer(0.02, feedback)
    executor = SingleThreadedExecutor()
    executor.add_node(firmware)
    connection.send("ready")
    try:
        running = True
        while running:
            executor.spin_once(timeout_sec=0.002)
            while connection.poll():
                operation, payload = connection.recv()
                if operation == "get":
                    connection.send(state[payload])
                elif operation == "update":
                    state.update(payload)
                    connection.send(None)
                elif operation == "timing":
                    connection.send(timing)
                elif operation == "close":
                    connection.send(None)
                    running = False
    finally:
        executor.shutdown(timeout_sec=2)
        firmware.destroy_node()
        rclpy.shutdown()
        connection.close()


class SyntheticFirmware:
    """Test-only RPC; never used to carry the ROS command or feedback stream."""

    def __init__(self):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=serve, args=(child,), daemon=True)
        self.process.start()
        child.close()
        if not self.connection.poll(10) or self.connection.recv() != "ready":
            self.process.terminate()
            self.process.join(timeout=5)
            raise RuntimeError("synthetic firmware did not start")

    def request(self, operation, payload=None):
        self.connection.send((operation, payload))
        if not self.connection.poll(5):
            raise RuntimeError("synthetic firmware stopped responding")
        return self.connection.recv()

    def __getitem__(self, name):
        return self.request("get", name)

    def __setitem__(self, name, value):
        self.update(**{name: value})

    def update(self, **values):
        self.request("update", values)

    def close(self):
        try:
            if self.process.is_alive():
                self.request("close")
                self.process.join(timeout=5)
        finally:
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.connection.close()
