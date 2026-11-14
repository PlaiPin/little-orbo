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

        def lease(self, msg):
            now_ms = self.get_clock().now().nanoseconds // 1_000_000
            remaining = msg.valid_for_ms - (now_ms - msg.issued_epoch_ms)
            if (
                msg.interface_version != 1
                or not msg.enable_wheels
                or msg.issued_epoch_ms > now_ms
                or not 0 < remaining <= 500
            ):
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
            except Rejected:
                # Invalid selected-source input removes authority, never refreshes a stale command.
                if source == self.gate.source:
                    self.gate.stop()

        def tick(self):
            now = self.get_clock().now()
            current = self.gate.current(now.nanoseconds, time.monotonic())
            if current is None:
                if self.had_command:
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
