"""Run with the ESP-IDF Python (pyserial required); no hardware access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('flash_firmware',
    Path(__file__).resolve().parents[3] / 'tools/flash-firmware.py')
flash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flash)


def port(device, product=flash.APP, location='1-1.3'):
    return SimpleNamespace(device=device, vid=product[0], pid=product[1], location=location)


class FlashDiscovery(unittest.TestCase):
    def setUp(self):
        self.ros = port('/dev/cu.usbmodem1234561')
        self.logs = port('/dev/cu.usbmodem1234563')
        self.rom = port('/dev/cu.usbmodem11301', flash.ROM)

    def test_log_interface_selected_not_ros(self):
        self.assertEqual(flash.select_board([self.logs, self.ros]), (self.logs, True))

    def test_already_in_rom(self):
        self.assertEqual(flash.select_board([self.rom]), (self.rom, False))

    def test_ambiguous_and_unknown_ports_refused(self):
        other = port('/dev/cu.usbmodem9993', location='2-1')
        for ports in [[], [self.ros], [self.logs, other], [port('unknown')]]:
            with self.assertRaises(RuntimeError):
                flash.select_board(ports)
        self.assertEqual(flash.select_board([self.logs, other], self.logs.device), (self.logs, True))

    def test_reenumeration_only_accepts_same_physical_location(self):
        other = port('/dev/cu.usbmodem99901', flash.ROM, '2-1')
        with patch.object(flash, 'usb_ports', side_effect=[[other], [self.rom]]), \
             patch.object(flash.time, 'sleep'):
            self.assertEqual(flash.wait_for('1-1.3', flash.ROM, 1), [self.rom])
        with patch.object(flash, 'usb_ports', return_value=[other]):
            with self.assertRaises(RuntimeError):
                flash.wait_for('1-1.3', flash.ROM, 1, timeout=0)


if __name__ == '__main__':
    unittest.main()
