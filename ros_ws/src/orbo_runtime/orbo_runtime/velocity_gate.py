"""Exclusive stamped velocity source. The model never writes this interface."""

import math
from dataclasses import dataclass

from .contracts import Rejected


@dataclass(frozen=True)
class Velocity:
    source: str
    stamp_ns: int
    linear: float
    angular: float


class VelocityGate:
    def __init__(self, *, max_linear, max_angular, max_age_ms=200):
        if (
            not all(math.isfinite(v) and v > 0 for v in (max_linear, max_angular))
            or not 1 <= max_age_ms <= 500
        ):
            raise Rejected("invalid velocity bounds")
        self.max_linear, self.max_angular = max_linear, max_angular
        self.max_age_ns = max_age_ms * 1_000_000
        self.source = None
        self.epoch = 0
        self.stopped_epoch = -1
        self.lease_until = 0.0
        self.last = None

    def authorize(self, source, epoch, until_mono, now_mono):
        if (
            source not in {"teleop", "nav2"}
            or epoch <= self.stopped_epoch
            or epoch < self.epoch
            or not now_mono < until_mono <= now_mono + 0.5
        ):
            raise Rejected("invalid velocity authority")
        if epoch == self.epoch and self.source != source:
            raise Rejected("source switch requires a fresh lease epoch")
        if epoch > self.epoch:
            self.last = None
        self.source, self.epoch, self.lease_until = source, epoch, until_mono

    def accept(self, sample, now_ns, now_mono):
        if sample.source != self.source or now_mono >= self.lease_until:
            raise Rejected("velocity source has no authority")
        if sample.stamp_ns > now_ns or now_ns - sample.stamp_ns >= self.max_age_ns:
            raise Rejected("stale or future velocity command")
        if self.last and sample.stamp_ns <= self.last.stamp_ns:
            raise Rejected("reordered velocity command")
        if (
            not math.isfinite(sample.linear)
            or not math.isfinite(sample.angular)
            or abs(sample.linear) > self.max_linear
            or abs(sample.angular) > self.max_angular
        ):
            raise Rejected("velocity outside commissioned bounds")
        self.last = sample

    def current(self, now_ns, now_mono):
        if (
            now_mono >= self.lease_until
            or self.last is None
            or not 0 <= now_ns - self.last.stamp_ns < self.max_age_ns
        ):
            self.last = None
            return None
        return self.last

    def stop(self):
        self.stopped_epoch = max(self.stopped_epoch, self.epoch)
        self.lease_until = 0
        self.last = None


class ClockAlignedSlot:
    """One packet awaiting a slightly lagging ROS clock, without timestamp renewal.

    Distributed /clock and data topics can arrive in either order. This buffer
    never authorizes a future packet: it waits <=50 ms for local clock alignment.
    Both its wall wait and its source-clock offset are bounded.
    """

    def __init__(self):
        self.pending = None

    def offer(self, value, stamp_ns, now_ns, now_mono):
        if not 0 < stamp_ns - now_ns <= 50_000_000:
            return False
        if self.pending and stamp_ns <= self.pending[1]:
            return False
        self.pending = (value, stamp_ns, now_mono)
        return True

    def pop(self, now_ns, now_mono):
        if self.pending is None:
            return None
        value, stamp_ns, received = self.pending
        if not 0 <= now_mono - received <= 0.05 or abs(stamp_ns - now_ns) > 50_000_000:
            self.pending = None
            return None
        if stamp_ns > now_ns:
            return None
        self.pending = None
        return value


def main():
    import time
    import rclpy
    from rclpy.node import Node
    from geometry_msgs.msg import TwistStamped
    from orbo_embedded_interfaces.msg import ControlSession

    class GateNode(Node):
        def __init__(self):
            super().__init__("orbo_velocity_gate", namespace="/orbo")
            self.declare_parameter("max_linear", 0.0)
            self.declare_parameter("max_angular", 0.0)
            self.declare_parameter("source", "nav2")
            self.source = self.get_parameter("source").value
            self.gate = VelocityGate(
                max_linear=self.get_parameter("max_linear").value,
                max_angular=self.get_parameter("max_angular").value,
            )
            self.authority = None
            self.lease_debug = {"received": 0, "accepted": 0, "last_rejection": None}
            self.pending_lease = ClockAlignedSlot()
            self.pending_command = ClockAlignedSlot()
            self.sequence = 0
            self.had_command = False
            self.output = self.create_publisher(
                TwistStamped, "drive_controller/cmd_vel", 1
            )
            self.auth_output = self.create_publisher(
                ControlSession, "control/authority", 1
            )
            self.stop_output = self.create_publisher(
                ControlSession, "embedded/control_session", 1
            )
            if self.source not in {"nav2", "teleop"}:
                raise Rejected("unknown velocity source")
            topic = (
                "control/teleop_lease" if self.source == "teleop" else "control/lease"
            )
            self.create_subscription(ControlSession, topic, self.lease, 1)
            self.create_subscription(
                TwistStamped, "cmd_vel", lambda m: self.command("nav2", m), 1
            )
            self.create_subscription(
                TwistStamped, "teleop/cmd_vel", lambda m: self.command("teleop", m), 1
            )
            self.create_subscription(
                ControlSession, "embedded/control_session", self.external_stop, 1
            )
            self.create_timer(0.02, self.tick)

        def external_stop(self, msg):
            if msg.interface_version == 1 and msg.operation == 0:
                self.gate.stop()

        def lease(self, msg, received_mono=None):
            self.lease_debug["received"] += 1
            now_mono = time.monotonic()
            if received_mono is None:
                received_mono = now_mono
            now_ms = self.get_clock().now().nanoseconds // 1_000_000
            remaining = min(
                msg.valid_for_ms - (now_ms - msg.issued_epoch_ms),
                msg.valid_for_ms - (now_mono - received_mono) * 1000,
            )
            if msg.issued_epoch_ms > now_ms:
                self.lease_debug["last_rejection"] = (
                    f"future {msg.issued_epoch_ms - now_ms} ms"
                )
                self.pending_lease.offer(
                    (msg, received_mono),
                    msg.issued_epoch_ms * 1_000_000,
                    now_ms * 1_000_000,
                    time.monotonic(),
                )
                return
            if (
                msg.interface_version != 1
                or not msg.enable_wheels
                or msg.issued_epoch_ms > now_ms
                or not 0 < remaining <= 500
            ):
                self.lease_debug["last_rejection"] = f"invalid remaining {remaining}"
                return
            if (
                self.authority
                and msg.epoch == self.authority.epoch
                and msg.sequence <= self.authority.sequence
            ):
                return
            try:
                self.gate.authorize(
                    self.source,
                    msg.epoch,
                    time.monotonic() + remaining / 1000,
                    time.monotonic(),
                )
            except Rejected:
                return
            self.authority = msg
            self.lease_debug["accepted"] += 1

        def command(self, source, msg):
            if source != self.source:
                return
            if msg.header.frame_id not in {"", "base_link"} or any(
                v != 0
                for v in [
                    msg.twist.linear.y,
                    msg.twist.linear.z,
                    msg.twist.angular.x,
                    msg.twist.angular.y,
                ]
            ):
                self.gate.stop()
                return
            stamp = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
            now_ns = self.get_clock().now().nanoseconds
            if stamp > now_ns:
                if not self.pending_command.offer(
                    (source, msg), stamp, now_ns, time.monotonic()
                ):
                    self.gate.stop()
                return
            if (
                self.gate.last
                and stamp == self.gate.last.stamp_ns
                and msg.twist.linear.x == self.gate.last.linear
                and msg.twist.angular.z == self.gate.last.angular
            ):
                return
            try:
                self.gate.accept(
                    Velocity(
                        source,
                        msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec,
                        msg.twist.linear.x,
                        msg.twist.angular.z,
                    ),
                    self.get_clock().now().nanoseconds,
                    time.monotonic(),
                )
            except Rejected as error:
                # Invalid selected-source input removes authority, never refreshes a stale command.
                if source == self.gate.source:
                    if self.gate.lease_until:
                        self.get_logger().warning(
                            "Velocity authority stopped: " + str(error)
                        )
                    self.gate.stop()

        def tick(self):
            now = self.get_clock().now()
            now_mono = time.monotonic()
            pending_lease = self.pending_lease.pop(now.nanoseconds, now_mono)
            if pending_lease is not None:
                self.lease(*pending_lease)
            pending_command = self.pending_command.pop(now.nanoseconds, now_mono)
            if pending_command is not None:
                self.command(*pending_command)
            previous = self.gate.last
            source_age_ms = (
                (now.nanoseconds - previous.stamp_ns) / 1_000_000 if previous else None
            )
            lease_remaining_ms = (self.gate.lease_until - now_mono) * 1000
            current = self.gate.current(now.nanoseconds, now_mono)
            if current is None:
                if self.had_command:
                    self.get_logger().warning(
                        f"Velocity authority stopped: source age {source_age_ms} ms; "
                        f"lease remaining {lease_remaining_ms:.1f} ms; {self.lease_debug}"
                    )
                    self.gate.stop()
                    zero = TwistStamped()
                    zero.header.stamp = now.to_msg()
                    self.output.publish(zero)
                    stop = ControlSession()
                    stop.interface_version = 1
                    stop.operation = 0
                    self.stop_output.publish(stop)
                    self.had_command = False
                return
            command = TwistStamped()
            command.header.frame_id = "base_link"
            command.header.stamp.sec = current.stamp_ns // 1_000_000_000
            command.header.stamp.nanosec = current.stamp_ns % 1_000_000_000
            command.twist.linear.x = current.linear
            command.twist.angular.z = current.angular
            self.output.publish(command)
            self.sequence += 1
            auth = ControlSession()
            auth.interface_version = 1
            auth.operation = 2
            auth.enable_wheels = True
            auth.boot_id = self.authority.boot_id
            auth.epoch = self.authority.epoch
            auth.sequence = self.sequence
            auth.issued_epoch_ms = current.stamp_ns // 1_000_000
            # The hardware sees the earlier of source expiry and owner lease expiry.
            lease_end = self.authority.issued_epoch_ms + self.authority.valid_for_ms
            auth.valid_for_ms = max(1, min(200, lease_end - auth.issued_epoch_ms))
            self.auth_output.publish(auth)
            self.had_command = True

    rclpy.init()
    node = GateNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.gate.stop()
        if rclpy.ok():
            node.tick()
        node.destroy_node()
        rclpy.try_shutdown()
