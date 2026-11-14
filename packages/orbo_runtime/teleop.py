"""Owner-authorized ROS Joy teleoperation; no direct model velocity interface."""

import asyncio
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time

from .commissioning import validate_calibration
from .contracts import ActionResult, Capability, Rejected


@dataclass(frozen=True)
class Input:
    stamp_ns: int
    received: float
    held: bool
    linear: float
    angular: float


class JoyInput:
    def __init__(self, config, calibration):
        self.config, self.calibration = config, calibration
        for key in ("linear_axis", "angular_axis", "deadman_button"):
            if type(config.get(key)) is not int or not 0 <= config[key] < 32:
                raise Rejected("explicit teleop axis/button mapping required")
        for key in ("linear_scale", "angular_scale"):
            value = config.get(key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < abs(value) <= 1
            ):
                raise Rejected("teleop scale must be nonzero and within [-1, 1]")
        self.last = None
        self.lock = threading.Lock()

    def accept(self, stamp, axes, buttons, now_ns, now_mono):
        with self.lock:
            try:
                if not 0 <= now_ns - stamp < 200_000_000 or (
                    self.last and stamp <= self.last.stamp_ns
                ):
                    raise Rejected(
                        "stale, future or reordered teleop input "
                        f"(age_ms={(now_ns - stamp) / 1_000_000:.3f}, "
                        f"delta_ns={stamp - self.last.stamp_ns if self.last else None})"
                    )
                if len(axes) > 32 or len(buttons) > 32:
                    raise Rejected("oversized teleop input")
                cfg = self.config
                x, z = axes[cfg["linear_axis"]], axes[cfg["angular_axis"]]
                held = buttons[cfg["deadman_button"]]
                if held not in (0, 1) or not all(
                    math.isfinite(v) and abs(v) <= 1 for v in (x, z)
                ):
                    raise Rejected("invalid teleop input")
                c = self.calibration
                v = x * cfg["linear_scale"] * c["max_linear_m_s"] if held else 0.0
                w = z * cfg["angular_scale"] * c["max_angular_rad_s"] if held else 0.0
                # Preserve curvature when combined demands exceed either wheel's bound.
                peak = max(
                    abs(v - w * c["wheel_separation"] / 2),
                    abs(v + w * c["wheel_separation"] / 2),
                )
                scale = (
                    min(1.0, c["wheel_radius"] * c["max_wheel_rad_s"] / peak)
                    if peak
                    else 1.0
                )
                self.last = Input(stamp, now_mono, bool(held), v * scale, w * scale)
            except Rejected:
                self.last = None
                raise
            except (IndexError, TypeError, ValueError):
                self.last = None
                raise Rejected("invalid teleop input") from None

    def current(self, now_ns=None, now_mono=None):
        with self.lock:
            # Read clocks after acquiring the input lock: a newer callback must
            # not appear to come from the future relative to a pre-lock clock.
            now_ns = time.time_ns() if now_ns is None else now_ns
            now_mono = time.monotonic() if now_mono is None else now_mono
            s = self.last
            if (
                not s
                or not 0 <= now_ns - s.stamp_ns < 200_000_000
                or not 0 <= now_mono - s.received < 0.2
            ):
                raise Rejected(
                    "fresh teleop input required "
                    f"(stamp_age_ms={(now_ns - s.stamp_ns) / 1_000_000 if s else None}, "
                    f"receive_age_ms={(now_mono - s.received) * 1000 if s else None})"
                )
            return s


class Teleoperation:
    def __init__(self, adapter, path, config):
        import yaml
        from geometry_msgs.msg import TwistStamped
        from sensor_msgs.msg import Joy
        from rclpy.qos import qos_profile_sensor_data

        calibration = validate_calibration(yaml.safe_load(Path(path).read_text()))
        if config.get("qualified") is not True or not config.get("evidence_record"):
            raise Rejected("teleop mapping and stop qualification required")
        self.input = JoyInput(config, calibration)
        self.adapter, self.twist_type = adapter, TwistStamped
        self.generation = 0
        self.stop_reason = "owner stop"
        self.results = {}
        self.commands = adapter.node.create_publisher(TwistStamped, "teleop/cmd_vel", 1)
        self.leases = adapter.node.create_publisher(
            adapter.session_type, "control/teleop_lease", 1
        )
        adapter.node.create_subscription(
            Joy, "teleop/joy", self.receive, qos_profile_sensor_data
        )
        adapter.capabilities["teleop.session"] = Capability(
            "teleop.session", ("wheels", "flippers"), 30, ("controller_ack",), True
        )

    def receive(self, message):
        stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
        try:
            self.input.accept(
                stamp,
                message.axes,
                message.buttons,
                self.adapter.node.get_clock().now().nanoseconds,
                time.monotonic(),
            )
        except Rejected as error:
            # A malformed input ends the active session even if a good one follows.
            self.stop(str(error))

    def stop(self, reason="owner stop"):
        self.stop_reason = reason
        self.generation += 1

    async def dispatch(self, action):
        if action.parameters or action.duration < 1:
            raise Rejected(
                "teleop session requires 1–30 seconds and no velocity arguments"
            )
        if self.adapter.node.count_publishers("teleop/joy") != 1:
            raise Rejected("exactly one teleop input publisher required")
        generation = self.generation
        sample = self.input.current()
        if sample.held:
            raise Rejected("release deadman before starting a new teleop session")
        deadline = (
            time.monotonic() + min(action.duration, action.deadline - time.time()) - 0.4
        )
        if deadline <= time.monotonic():
            raise Rejected("teleop deadline expired before admission")
        try:
            health = await self.adapter.arm(action.epoch, wheels=True)
            boot = health.boot_id
            while time.monotonic() < deadline:
                if (
                    self.generation != generation
                    or self.adapter.node.count_publishers("teleop/joy") != 1
                ):
                    raise Rejected("teleop stopped: " + self.stop_reason)
                health = self.adapter._snapshot()
                if (
                    health.boot_id != boot
                    or health.epoch != action.epoch
                    or not health.armed
                ):
                    raise Rejected("teleop controller changed or disarmed")
                sample = self.input.current()
                twist = self.twist_type()
                twist.header.frame_id = "base_link"
                twist.header.stamp.sec, twist.header.stamp.nanosec = divmod(
                    sample.stamp_ns, 1_000_000_000
                )
                twist.twist.linear.x, twist.twist.angular.z = (
                    sample.linear,
                    sample.angular,
                )
                self.commands.publish(twist)
                self.adapter.sequence += 1
                lease = self.adapter.session_type()
                lease.interface_version, lease.operation, lease.enable_wheels = (
                    1,
                    2,
                    True,
                )
                lease.boot_id, lease.epoch, lease.sequence = (
                    boot,
                    action.epoch,
                    self.adapter.sequence,
                )
                lease.issued_epoch_ms = sample.stamp_ns // 1_000_000
                lease.valid_for_ms = min(
                    200, max(1, int((deadline - time.monotonic()) * 1000))
                )
                self.leases.publish(lease)
                await asyncio.sleep(0.02)
        finally:
            await self.adapter.stop_session()
        # Only fresh MCU disarmed acknowledgment may close the session normally.
        async with asyncio.timeout(0.3):
            while True:
                final = self.adapter._snapshot()
                if final.boot_id != boot or final.epoch != action.epoch:
                    raise Rejected("controller changed before disarm acknowledgment")
                if not final.armed:
                    break
                await asyncio.sleep(0.01)
        result = ActionResult(
            action.command_id,
            "completed",
            "controller_ack",
            {
                "session_ended": True,
                "controller_disarmed": True,
                "physical_observation": False,
            },
        )
        self.results[action.command_id] = result
        if len(self.results) > 128:
            self.results.pop(next(iter(self.results)))
        return result
