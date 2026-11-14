"""Run in the Pixi ROS overlay; mocks only, no board/agent or motion."""
import importlib.util
import io
import math
import os
from pathlib import Path
import pty
import termios
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('control_wheels',
    Path(__file__).resolve().parents[1] / 'bench_tests/control_wheels.py')
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


class KeyboardTests(unittest.TestCase):
    def test_directions_zero_and_timeout(self):
        keys = manual.KeyboardCommand(1.2)
        self.assertEqual(keys.velocity(0), [0, 0])
        for key, velocity in [('w', [1.2, 1.2]), ('s', [-1.2, -1.2]),
                              ('a', [-1.2, 1.2]), ('d', [1.2, -1.2])]:
            self.assertTrue(keys.feed(key.encode(), 1))
            self.assertEqual(keys.velocity(1.1), velocity)
            self.assertEqual(keys.velocity(1.36), [0, 0])
        keys.feed(b' ', 2)
        self.assertEqual(keys.velocity(2), [0, 0])
        self.assertFalse(keys.feed(b'q', 2))

    def test_fragmented_arrow_sequences_do_not_become_wasd(self):
        keys = manual.KeyboardCommand(1.2)
        for part in [b'\x1b', b'[', b'A', b'\x1bOB']:
            keys.feed(part, 1)
        self.assertEqual(keys.velocity(1), [0, 0])

    def test_terminal_restored_after_exception(self):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
            with patch.object(manual.sys, 'stdin', Mock(fileno=lambda: slave)):
                with self.assertRaises(RuntimeError):
                    with manual.keyboard_terminal():
                        raise RuntimeError('test exit')
            restored = termios.tcgetattr(slave)
            # macOS may set PENDIN while restoring canonical input; it is a
            # pending-input state bit, not a changed terminal mode.
            restored[3] &= ~getattr(termios, 'PENDIN', 0)
            original[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(restored, original)
        finally:
            os.close(master)
            os.close(slave)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.node = Mock()
        self.sessions, self.wheels = Mock(), Mock()
        self.node.create_publisher.side_effect = [self.sessions, self.wheels]
        self.node.count_publishers.return_value = 1
        self.sessions.get_subscription_count.return_value = 1
        self.wheels.get_subscription_count.return_value = 1
        self.controller = manual.WheelPublisher(self.node)
        self.wheel = manual.WheelState(boot_id=7, epoch=3, armed=True,
            valid=[True, True], hardware_error=[0, 0], sample_mono_us=1000000,
            motor_sample_mono_us=[999000, 999500], position_rad=[0.0, 0.0],
            velocity_rad_s=[0.0, 0.0])
        self.controller.on_wheel(self.wheel)
        self.controller.on_health(manual.Health(boot_id=7, clock_synced=True,
            epoch_ms=int(time.time()*1000)))
        self.controller.boot, self.controller.epoch = 7, 3

    def test_live_messages_increase_sequence(self):
        for velocity in ([1.2, 1.2], [-1.2, 1.2], [0.0, 0.0]):
            self.controller.publish(velocity)
        messages = [call.args[0] for call in self.wheels.publish.call_args_list]
        self.assertEqual([m.sequence for m in messages], [1, 2, 3])
        for m in messages:
            self.assertEqual((m.boot_id, m.epoch, m.valid_for_ms), (7, 3, 300))
            self.assertLess(abs(time.time()*1000-m.issued_epoch_ms), 100)

    def test_stale_feedback_disarm_reboot_and_contending_publishers_abort(self):
        cases = [lambda: setattr(self.controller, 'wheel_received', -math.inf),
                 lambda: setattr(self.wheel, 'armed', False),
                 lambda: setattr(self.wheel, 'boot_id', 8),
                 lambda: setattr(self.wheel, 'valid', [False, True]),
                 lambda: setattr(self.wheel, 'motor_sample_mono_us', [1000001, 999000]),
                 lambda: setattr(self.controller, 'health_delivery_ms', 150),
                 lambda: setattr(self.node.count_publishers, 'return_value', 2)]
        for change in cases:
            self.setUp()
            change()
            with self.assertRaises(RuntimeError):
                self.controller.publish([1.2, 1.2])
            self.wheels.publish.assert_not_called()
            self.sessions.publish.assert_not_called()  # No automatic rearm.

    def test_out_of_bounds_not_published(self):
        for velocity in ([math.nan, 0], [1.21, 0], [0]):
            with self.assertRaises(ValueError):
                self.controller.publish(velocity)
        self.wheels.publish.assert_not_called()

    def test_arm_uses_fresh_epoch_and_stop_is_confirmed(self):
        self.wheel.armed = False
        self.controller.boot = self.controller.epoch = None
        self.controller.spin = lambda seconds: self.controller.on_wheel(self.wheel)
        def receive(message):
            self.wheel.armed = bool(message.operation)
            if message.operation:
                self.wheel.epoch = message.epoch
        self.sessions.publish.side_effect = receive
        self.controller.connect_and_arm()
        arm = self.sessions.publish.call_args.args[0]
        self.assertEqual((arm.boot_id, arm.epoch, arm.sequence), (7, 4, 1))
        self.assertTrue(arm.enable_wheels)
        self.assertTrue(self.controller.stop())
        self.assertFalse(self.wheel.armed)

    def test_original_fault_survives_cleanup_stop_and_ignores_other_sessions(self):
        self.controller.arm_requested = True
        self.controller.on_status(manual.ActuatorStatus(boot_id=8, epoch=3, state=6, reason=2))
        self.assertIsNone(self.controller.fault_event)
        self.controller.on_status(manual.ActuatorStatus(boot_id=7, epoch=2, state=6, reason=6))
        self.assertIsNone(self.controller.fault_event)
        self.controller.on_status(manual.ActuatorStatus(boot_id=7, epoch=3, state=6, reason=5))
        self.controller.on_status(manual.ActuatorStatus(boot_id=7, epoch=3, state=5, reason=1))
        self.controller.on_status(manual.ActuatorStatus(boot_id=7, epoch=3, state=6, reason=1))
        self.controller.on_health(manual.Health(boot_id=7, epoch=3, fault=1,
            clock_synced=True, epoch_ms=int(time.time()*1000)))
        output = io.StringIO()
        with patch.object(manual.sys, 'stderr', output):
            self.controller.describe_failure()
        self.assertIn('Firmware fault: FEEDBACK (5)', output.getvalue())

    def test_cleanup_stop_is_not_mislabeled_as_original_failure(self):
        self.controller.arm_requested = True
        self.controller.cleanup_started = True
        self.controller.on_status(manual.ActuatorStatus(boot_id=7, epoch=3, state=6, reason=1))
        self.assertIsNone(self.controller.fault_event)


if __name__ == '__main__':
    unittest.main()
