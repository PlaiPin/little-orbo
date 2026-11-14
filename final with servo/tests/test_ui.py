import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pc"))
from PyQt6.QtCore import QEvent, QSettings, Qt
from PyQt6.QtGui import QKeyEvent, QFontDatabase, QFont
from PyQt6.QtWidgets import QApplication
import teleop

app = QApplication.instance() or QApplication([])
if Path("C:/Windows/Fonts/segoeui.ttf").exists():
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    app.setFont(QFont("Segoe UI", 9))


class FakeLink:
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(text)
    def close(self): pass
    def name(self): return "test robot"
    def no_reply_hint(self): return "no reply"


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_settings = teleop.QSettings
        teleop.QSettings = lambda *_: QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.window = teleop.MainWindow()
        self.window.recv_timer.stop()
        self.link = FakeLink()
        self.window.link = self.link
        self.window.link_ok = True
        self.panel = self.window.arms

    def tearDown(self):
        self.window.close()
        app.removeEventFilter(self.window)
        self.window.sock.close()
        teleop.QSettings = self.old_settings
        self.temp.cleanup()

    def enable_arms(self):
        self.panel.receive("ARMS", "0 0 0 0 0 1")
        self.panel.apply_config()
        command = self.panel.pending
        self.assertIsNotNone(command)
        self.assertFalse(self.panel.enabled.isEnabled())
        self.panel.receive("ARMCFG", command.partition(" ")[2])
        self.panel.pattern.setCurrentIndex(1)
        self.panel.enabled.setChecked(True)

    def test_wireless_default_and_keyboard_drive(self):
        self.assertTrue(self.window.wireless_radio.isChecked())
        self.window.held.add("fwd")
        self.window.tick()
        self.assertIn("DRIVE 25.0 -25.0", self.link.sent)

    def test_config_requires_matching_ack(self):
        self.panel.receive("ARMS", "0 0 0 0 0 1")
        self.panel.apply_config()
        self.panel.receive("ARMCFG", "wrong values")
        self.assertFalse(self.panel.confirmed)

    def test_patterns_do_not_change_wheel_command(self):
        self.enable_arms()
        self.window.held.add("fwd")
        self.window.tick()
        self.assertIn("DRIVE 25.0 -25.0", self.link.sent)
        self.assertIn("ARM 2 50 35 0 0", self.link.sent)

    def test_focus_loss_stops_wheels_and_arms(self):
        self.enable_arms()
        self.window.held.add("fwd")
        self.window.eventFilter(self.window, QEvent(QEvent.Type.ApplicationDeactivate))
        self.assertFalse(self.panel.enabled.isChecked())
        self.assertFalse(self.window.held)
        self.assertTrue(any(c.startswith("DRIVE ") and all(float(v) == 0 for v in c.split()[1:])
                            for c in self.link.sent))
        self.assertIn("ARM 0 50 0 0 0", self.link.sent)

    def test_lost_reply_clears_held_keys(self):
        self.enable_arms()
        self.window.held.add("fwd")
        self.window.last_state_t = time.monotonic() - 2
        self.window.check_link()
        self.assertFalse(self.window.held)
        self.assertFalse(self.panel.enabled.isChecked())

    def test_old_firmware_or_arm_timeout_cannot_keep_arms_enabled(self):
        self.enable_arms()
        self.panel.last_status = time.monotonic() - 2
        self.panel.tick()
        self.assertFalse(self.panel.enabled.isChecked())
        self.assertFalse(self.panel.confirmed)

    def test_invalid_calibration_not_sent(self):
        self.panel.receive("ARMS", "0 0 0 0 0 1")
        self.panel.values[0][0].setValue(1700)
        self.panel.apply_config()
        self.assertFalse(any(c.startswith("ARMCFG") for c in self.link.sent))

    def test_disconnect_sends_both_stops(self):
        self.enable_arms()
        self.window.disconnect("test")
        self.assertIn("DRIVE 0 0", self.link.sent)
        self.assertIn("ARM 0 50 0 0 0", self.link.sent)
        self.assertFalse(self.panel.confirmed)

    def test_render(self):
        self.window.link = None
        self.window.show()
        app.processEvents()
        output = ROOT / "tests" / "build"
        output.mkdir(exist_ok=True)
        self.assertTrue(self.window.grab().save(str(output / "ui-preview.png")))


if __name__ == "__main__": unittest.main()
