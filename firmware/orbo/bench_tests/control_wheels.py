#!/usr/bin/env python3
"""Publish live wheel commands: a timed drive or terminal WASD control."""
import argparse
from contextlib import contextmanager
import math
import os
import select
import signal
import sys
import termios
import time
import tty

import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
from orbo_embedded_interfaces.msg import ActuatorStatus, ControlSession, Health, WheelCommand, WheelState

LIMIT = 1.2
PERIOD = 0.05
KEY_TIMEOUT = 0.35
SESSION_TOPIC = '/orbo/embedded/control_session'
WHEEL_TOPIC = '/orbo/embedded/wheel_command'
FAULT_NAMES = {0: 'OK', 1: 'DISARMED', 2: 'EXPIRED', 3: 'STALE',
               4: 'BOUNDS', 5: 'FEEDBACK', 6: 'BUS', 7: 'CLOCK'}


class KeyboardCommand:
    def __init__(self, speed):
        self.speed = speed
        self.direction = (0, 0)
        self.last_key = -math.inf
        self.escape = 0

    def feed(self, data, now):
        for key in data.decode(errors='ignore'):
            if key == '\x1b':
                self.escape = 1
                continue
            if self.escape:
                if self.escape == 1 and key in '[O':
                    self.escape = 2
                elif self.escape == 1 or '@' <= key <= '~':
                    self.escape = 0
                continue
            if key.lower() == 'q':
                return False
            self.press(key, now)
        return True

    def press(self, key, now):
        directions = {'w': (1, 1), 's': (-1, -1),
                      'a': (-1, 1), 'd': (1, -1), ' ': (0, 0), 'x': (0, 0)}
        if key.lower() in directions:
            self.direction = directions[key.lower()]
            self.last_key = now

    def velocity(self, now):
        if now - self.last_key >= KEY_TIMEOUT:
            return [0.0, 0.0]
        return [self.speed * value for value in self.direction]


class WheelPublisher:
    def __init__(self, node, *, direct_wheels=True):
        self.node = node
        self.wheel = self.health = None
        self.wheel_received = self.health_received = -math.inf
        self.health_delivery_ms = math.inf
        self.boot = self.epoch = None
        self.sequence = 0
        self.arm_requested = False
        self.cleanup_started = False
        self.fault_event = self.fault_health = None
        self.read_errors_at_arm = None
        self.failure_wheel = None
        self.last_publish_time = None
        self.max_publish_gap = 0.0
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.subscriptions = [
            node.create_subscription(WheelState, '/orbo/embedded/wheel_state', self.on_wheel, qos),
            node.create_subscription(Health, '/orbo/embedded/health', self.on_health, qos),
            node.create_subscription(ActuatorStatus, '/orbo/embedded/actuator_status',
                self.on_status, QoSProfile(depth=20, reliability=ReliabilityPolicy.BEST_EFFORT)),
        ]
        self.sessions = node.create_publisher(ControlSession, SESSION_TOPIC, 10)
        self.wheels = node.create_publisher(WheelCommand, WHEEL_TOPIC, 10) if direct_wheels else None

    def on_wheel(self, message):
        self.wheel, self.wheel_received = message, time.monotonic()

    def on_health(self, message):
        self.health, self.health_received = message, time.monotonic()
        self.health_delivery_ms = time.time() * 1000 - message.epoch_ms
        if (self.arm_requested and message.boot_id == self.boot and message.epoch == self.epoch
                and not message.armed and message.fault not in (0, 1) and self.fault_health is None):
            self.fault_health = message.fault

    def on_status(self, message):
        if (self.arm_requested and message.boot_id == self.boot and message.epoch == self.epoch
                and message.state == 6 and self.fault_event is None
                and not (self.cleanup_started and message.reason == 1)):
            # Preserve the first fault even if our final stop changes health to DISARMED.
            self.fault_event = message

    def describe_failure(self):
        if self.fault_event is not None:
            reason = self.fault_event.reason
            print(f'Firmware fault: {FAULT_NAMES.get(reason, "UNKNOWN")} ({reason}), '
                  f'epoch={self.fault_event.epoch}, MCU time={self.fault_event.mono_us} us', file=sys.stderr)
        elif self.fault_health is not None:
            print(f'Firmware health fault: {FAULT_NAMES.get(self.fault_health, "UNKNOWN")} '
                  f'({self.fault_health})', file=sys.stderr)
        else:
            print('Original firmware fault not captured; the cleanup stop is not the cause.', file=sys.stderr)
        wheel = self.failure_wheel
        if wheel is not None:
            print(f'Feedback at failure: boot_id={wheel.boot_id}, epoch={wheel.epoch}, '
                  f'armed={wheel.armed}, valid={list(wheel.valid)}, '
                  f'read_errors={[int(v) for v in wheel.read_errors]}, '
                  f'hardware_error={[int(v) for v in wheel.hardware_error]}, '
                  f'voltage={[round(float(v), 2) for v in wheel.voltage]}, '
                  f'last_accepted_sequence={wheel.last_command_sequence}', file=sys.stderr)
        print(f'Publisher: sent={self.sequence}, max_publish_gap_ms={self.max_publish_gap * 1000:.1f}, '
              f'read_errors_at_arm={self.read_errors_at_arm}', file=sys.stderr)

    def spin(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            rclpy.spin_once(self.node, timeout_sec=min(0.01, max(0.0, until - time.monotonic())))

    def check(self, armed=False):
        now = time.monotonic()
        wheel, health = self.wheel, self.health
        if wheel is None or now - self.wheel_received > 0.15:
            raise RuntimeError('Wheel feedback missing or older than 150 ms')
        if health is None or now - self.health_received > 0.75 or not health.clock_synced:
            raise RuntimeError('Synchronized health telemetry missing or stale')
        if abs(self.health_delivery_ms) >= 100:
            raise RuntimeError('Firmware clock/health delivery differs from host by at least 100 ms')
        if health.boot_id != wheel.boot_id or (self.boot is not None and wheel.boot_id != self.boot):
            raise RuntimeError('Board reboot/session changed; restart the controller explicitly')
        ages = [int(wheel.sample_mono_us) - int(stamp) for stamp in wheel.motor_sample_mono_us]
        if (not all(wheel.valid) or any(wheel.hardware_error) or
                not all(0 <= age < 150000 for age in ages) or
                not all(math.isfinite(v) for v in list(wheel.position_rad) + list(wheel.velocity_rad_s))):
            raise RuntimeError('Invalid motor feedback; inspect wheel_state')
        self.check_graph()
        if armed and (not wheel.armed or wheel.epoch != self.epoch):
            raise RuntimeError(f'Firmware disarmed or session changed '
                               f'(armed={wheel.armed}, expected epoch={self.epoch}, '
                               f'observed epoch={wheel.epoch}); no automatic rearm')

    def check_graph(self):
        if self.sessions.get_subscription_count() != 1 or self.wheels.get_subscription_count() != 1:
            raise RuntimeError('Expected exactly one firmware command subscriber')
        if self.node.count_publishers(SESSION_TOPIC) != 1 or self.node.count_publishers(WHEEL_TOPIC) != 1:
            raise RuntimeError('Another command publisher is running; stop it first')

    def connect_and_arm(self):
        until = time.monotonic() + 15
        print('Waiting for firmware telemetry and command subscriptions...', flush=True)
        while time.monotonic() < until:
            self.spin(0.05)
            if self.wheel and self.health:
                try:
                    self.check_graph()
                    break
                except RuntimeError:
                    pass
        self.check()
        if self.wheel.armed:
            raise RuntimeError('Firmware is already armed; stop the current controller first')
        self.boot, self.epoch = self.wheel.boot_id, self.wheel.epoch + 1
        # Latch before publishing so even an exception during publication stops
        # a potentially accepted arm in main's finally block.
        self.arm_requested = True
        self.sessions.publish(ControlSession(interface_version=1, operation=1,
            boot_id=self.boot, epoch=self.epoch, sequence=1,
            issued_epoch_ms=int(time.time() * 1000) - 20, valid_for_ms=500, enable_wheels=True))
        until = time.monotonic() + 2
        while time.monotonic() < until:
            self.spin(0.02)
            self.check()
            if self.wheel.armed and self.wheel.epoch == self.epoch:
                self.read_errors_at_arm = [int(v) for v in self.wheel.read_errors]
                print(f'Armed: boot_id={self.boot}, epoch={self.epoch}', flush=True)
                return
        raise RuntimeError('Wheel arm request was not accepted')

    def publish(self, velocity):
        self.check(armed=True)
        if len(velocity) != 2 or not all(math.isfinite(v) and abs(v) <= LIMIT for v in velocity):
            raise ValueError('Each wheel velocity must be finite and within +/-1.2 rad/s')
        self.sequence += 1
        now = time.monotonic()
        if self.last_publish_time is not None:
            self.max_publish_gap = max(self.max_publish_gap, now - self.last_publish_time)
        self.last_publish_time = now
        self.wheels.publish(WheelCommand(boot_id=self.boot, epoch=self.epoch,
            sequence=self.sequence, command_id=self.sequence,
            issued_epoch_ms=int(time.time() * 1000) - 20, valid_for_ms=300,
            velocity_rad_s=list(velocity)))

    def stop(self):
        if self.arm_requested:
            self.cleanup_started = True
            for _ in range(3):
                self.sessions.publish(ControlSession(interface_version=1, operation=0))
                self.spin(0.05)
            until = time.monotonic() + 2
            while time.monotonic() < until:
                self.spin(0.05)
                if (self.wheel and time.monotonic() - self.wheel_received < 0.15 and
                        self.wheel.boot_id == self.boot and not self.wheel.armed and
                        all(self.wheel.valid) and all(abs(v) < 0.05 for v in self.wheel.velocity_rad_s)):
                    print('Stopped and disarmed (encoder feedback).', flush=True)
                    return True
            print('Stop requested; stopped/disarmed feedback was not confirmed.', file=sys.stderr)
            return False
        return True


@contextmanager
def keyboard_terminal():
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        raise RuntimeError('Keyboard mode requires an interactive terminal')
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def keyboard_loop(controller, speed):
    keys = KeyboardCommand(speed)
    with keyboard_terminal() as fd:
        print('W/S: both +/-  A/D: opposite wheels  Space/X: zero  Q/Ctrl-C: disarm and quit\r')
        print('Tap or hold WASD. No key-repeat for 0.35 s returns to zero.\r')
        controller.connect_and_arm()
        # Discard keys typed during discovery/arming; movement requires fresh input.
        termios.tcflush(fd, termios.TCIFLUSH)
        while True:
            if select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 64)
                if not data or not keys.feed(data, time.monotonic()):
                    break
            controller.publish(keys.velocity(time.monotonic()))
            controller.spin(PERIOD)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    keyboard = commands.add_parser('keyboard', help='Interactive WASD control')
    keyboard.add_argument('--speed', type=float, default=LIMIT, help='Wheel speed in rad/s (default 1.2)')
    drive = commands.add_parser('drive', help='Publish a bounded left/right velocity stream')
    drive.add_argument('--left', type=float, required=True)
    drive.add_argument('--right', type=float, required=True)
    drive.add_argument('--seconds', type=float, default=2.0)
    args = parser.parse_args()
    speeds = [args.speed] if args.mode == 'keyboard' else [args.left, args.right]
    if not all(math.isfinite(v) and abs(v) <= LIMIT for v in speeds):
        parser.error('Wheel speeds must be finite and within +/-1.2 rad/s')
    if args.mode == 'keyboard' and args.speed <= 0:
        parser.error('--speed must be positive')
    if args.mode == 'drive' and (not math.isfinite(args.seconds) or not 0 < args.seconds <= 60):
        parser.error('--seconds must be greater than zero and at most 60')
    rclpy.init(domain_id=42, signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('orbo_manual_wheel_control')
    controller = WheelPublisher(node)
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    old_handlers = {s: signal.signal(s, interrupt) for s in (signal.SIGTERM, signal.SIGTSTP)}
    result = 0
    try:
        if args.mode == 'keyboard':
            keyboard_loop(controller, args.speed)
        else:
            controller.connect_and_arm()
            started = time.monotonic()
            until = started + args.seconds
            while time.monotonic() < until:
                controller.publish([args.left, args.right])
                controller.spin(PERIOD)
            print(f'Drive completed: {time.monotonic()-started:.2f} s, '
                  f'{controller.sequence} commands published.', flush=True)
    except KeyboardInterrupt:
        print('\nStopping...', flush=True)
    except Exception as error:
        controller.failure_wheel = controller.wheel
        print(f'Controller stopped: {error}', file=sys.stderr)
        result = 1
    finally:
        try:
            if not controller.stop():
                result = 1
            if result:
                controller.describe_failure()
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
            node.destroy_node()
            rclpy.shutdown()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
