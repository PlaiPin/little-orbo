"""Send one bounded pair of PWM targets through ROS, with wheel torque disabled.

Requires commissioned firmware limits. Completion is a controller acknowledgment,
not measured servo position. Run with only the micro-ROS Agent, no other owner.
"""

import argparse
import os
import secrets
import signal
import sys
import time

PREFIX = "/orbo/embedded/"


class FlipperBench:
    def __init__(self, node):
        import rclpy
        from rclpy.qos import qos_profile_sensor_data
        from orbo_embedded_interfaces.msg import (
            ActuatorStatus,
            ControlSession,
            FlipperCommand,
            Health,
        )

        self.node, self.rclpy = node, rclpy
        self.session_type, self.command_type = ControlSession, FlipperCommand
        self.sessions = node.create_publisher(
            ControlSession, PREFIX + "control_session", 1
        )
        self.commands = node.create_publisher(
            FlipperCommand, PREFIX + "flipper_command", 1
        )
        self.health = None
        self.health_at = 0.0
        self.boot = self.epoch = None
        self.command_id = secrets.randbits(64) or 1
        self.result = None
        self.arm_sent = False
        node.create_subscription(
            Health, PREFIX + "health", self.on_health, qos_profile_sensor_data
        )
        node.create_subscription(
            ActuatorStatus,
            PREFIX + "actuator_status",
            self.on_status,
            qos_profile_sensor_data,
        )

    def on_health(self, msg):
        if msg.interface_version != 1:
            return
        if self.health and msg.boot_id == self.health.boot_id:
            if msg.mono_us <= self.health.mono_us or msg.epoch < self.health.epoch:
                return
        self.health, self.health_at = msg, time.monotonic()

    def on_status(self, msg):
        if (msg.boot_id, msg.epoch, msg.command_id) != (
            self.boot,
            self.epoch,
            self.command_id,
        ):
            return
        if self.result is None or msg.sequence > self.result.sequence:
            self.result = msg

    def spin(self):
        self.rclpy.spin_once(self.node, timeout_sec=0.02)

    def check(self, *, armed=False):
        h = self.health
        if (
            h is None
            or time.monotonic() - self.health_at > 0.6
            or not h.clock_synced
            or not 0 <= time.time_ns() // 1_000_000 - h.epoch_ms <= 600
        ):
            raise RuntimeError("Fresh synchronized MCU health required")
        if self.boot is not None and h.boot_id != self.boot:
            raise RuntimeError("MCU restarted; no automatic rearm")
        if armed and (not h.armed or h.epoch != self.epoch or h.fault != 0):
            raise RuntimeError(
                f"Flipper session lost: epoch={h.epoch}, fault={h.fault}"
            )
        return h

    def check_graph(self):
        for topic in ("control_session", "flipper_command"):
            if self.node.count_publishers(PREFIX + topic) != 1:
                raise RuntimeError(
                    "Stop other runtime/teleop/bench owners before flipper control"
                )
        if self.node.count_publishers(PREFIX + "wheel_command"):
            raise RuntimeError(
                "Stop wheel controllers before flipper-only bench control"
            )
        if (
            self.sessions.get_subscription_count() != 1
            or self.commands.get_subscription_count() != 1
        ):
            raise RuntimeError("Expected one connected MCU")

    def session(self, operation):
        # Allow stop even after stale health, disconnect or reset.
        self.sessions.publish(
            self.session_type(
                interface_version=1,
                boot_id=self.boot or 0,
                epoch=self.epoch or 0,
                sequence=1 if operation else 2,
                operation=operation,
                enable_wheels=False,
                issued_epoch_ms=time.time_ns() // 1_000_000 - 20,
                valid_for_ms=500,
            )
        )

    def run(self, pulses, duration_ms):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.spin()
            try:
                self.check()
                self.check_graph()
                break
            except RuntimeError:
                continue
        h = self.check()
        self.check_graph()
        if h.armed:
            raise RuntimeError("MCU already armed; end the existing session first")
        self.boot, self.epoch = h.boot_id, h.epoch + 1
        self.arm_sent = True
        self.session(1)
        deadline = time.monotonic() + 2
        while True:
            self.spin()
            h = self.check()
            if h.armed and h.epoch == self.epoch:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"MCU refused flipper session (fault={h.fault})")
        self.check(armed=True)
        self.check_graph()
        self.commands.publish(
            self.command_type(
                boot_id=self.boot,
                epoch=self.epoch,
                sequence=1,
                command_id=self.command_id,
                issued_epoch_ms=time.time_ns() // 1_000_000 - 20,
                valid_for_ms=300,
                pulse_us=pulses,
                duration_ms=duration_ms,
            )
        )
        deadline = time.monotonic() + duration_ms / 1000 + 2
        while time.monotonic() < deadline:
            self.spin()
            self.check(armed=True)
            self.check_graph()
            if self.result and self.result.state in {3, 4, 5, 6}:
                if (
                    self.result.state != 3
                    or self.result.reason != 0
                    or self.result.evidence != 1
                ):
                    raise RuntimeError(
                        f"Flipper command failed: state={self.result.state}, reason={self.result.reason}; "
                        "check firmware commissioning and pulse limits"
                    )
                print("PWM duration completed; physical movement is not measured.")
                return
        raise RuntimeError("No controller completion received")

    def stop(self):
        if not self.arm_sent:
            return
        # A new health sample must confirm disarm after the stop request.
        stopped_at = time.monotonic()
        self.session(0)
        deadline = stopped_at + 2
        while time.monotonic() < deadline:
            self.spin()
            if self.health_at > stopped_at and not self.health.armed:
                print("MCU disarmed; PWM outputs disabled.")
                return
        raise RuntimeError("Stop sent, but MCU disarm was not confirmed")


def main():
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from rclpy.utilities import remove_ros_args

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--left-us", type=int, required=True, help="D7 / GPIO10 pulse width"
    )
    parser.add_argument(
        "--right-us", type=int, required=True, help="D10 / GPIO21 pulse width"
    )
    parser.add_argument("--duration-ms", type=int, default=500)
    args = parser.parse_args(remove_ros_args()[1:])
    if not all(0 < p < 20000 for p in (args.left_us, args.right_us)):
        parser.error(
            "Pulse widths must be between 1 and 19999 us; firmware enforces calibrated limits"
        )
    if not 1 <= args.duration_ms <= 5000:
        parser.error("--duration-ms must be between 1 and 5000")
    rclpy.init(
        domain_id=int(os.environ.get("ROS_DOMAIN_ID", "42")),
        signal_handler_options=SignalHandlerOptions.NO,
    )
    node = rclpy.create_node("orbo_flipper_bench")
    controller = FlipperBench(node)

    def interrupt(*_):
        raise KeyboardInterrupt

    old = {
        s: signal.signal(s, interrupt)
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGTSTP)
    }
    result = 0
    try:
        controller.run([args.left_us, args.right_us], args.duration_ms)
    except KeyboardInterrupt:
        print("Stopping flippers...")
        result = 130
    except Exception as exc:
        print(f"Flipper control failed: {exc}", file=sys.stderr)
        result = 1
    finally:
        try:
            controller.stop()
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            result = 1
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            node.destroy_node()
            rclpy.shutdown()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
