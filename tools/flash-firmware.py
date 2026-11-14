#!/usr/bin/env python3
"""Flash the Orbo ESP32-S3 over native USB, including automatic ROM entry."""
import argparse
import importlib.util
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import serial
from serial.tools import list_ports

APP = (0x303A, 0x4002)
ROM = (0x303A, 0x1001)
ROOT = Path(__file__).resolve().parents[1]


def usb_ports():
    return [p for p in list_ports.comports() if (p.vid, p.pid) in (APP, ROM)]


def select_board(ports, port=None):
    if port:
        matches = [p for p in ports if p.device == port]
        if len(matches) != 1:
            raise RuntimeError(f"Not an Orbo application or ESP32-S3 ROM port: {port}")
        selected = matches[0]
        ports = [p for p in ports if p.location == selected.location]
    locations = {p.location for p in ports}
    if len(locations) != 1 or None in locations:
        raise RuntimeError("Connect one board, or select its logging/ROM port with --port.")
    rom = [p for p in ports if (p.vid, p.pid) == ROM]
    if len(rom) == 1:
        return rom[0], False
    # The checked-in dual CDC descriptor enumerates on macOS as ...1 / ...3.
    # Do not guess between the ROS and logging interfaces on other platforms.
    logs = [p for p in ports if (p.vid, p.pid) == APP and
            p.device.startswith('/dev/cu.usbmodem') and p.device.endswith('3')]
    if len(logs) != 1:
        raise RuntimeError("Cannot identify CDC1 logging port (automatic discovery supports macOS).")
    return logs[0], True


def wait_for(location, product, count, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        matches = [p for p in usb_ports() if p.location == location and
                   (p.vid, p.pid) == product]
        if len(matches) == count:
            return matches
        time.sleep(0.1)
    raise RuntimeError(f"USB {product[0]:04x}:{product[1]:04x} did not appear at {location}; "
                       "no other board was selected. B1–GND + reset remains available for recovery.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='Logging or ROM port when several boards are connected')
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build-locked')
    args = parser.parse_args()
    build = args.build_dir.resolve()
    flash_args = build / 'flash_args'
    tokens = shlex.split(flash_args.read_text())
    for i, token in enumerate(tokens[:-1]):
        if token.startswith('0x') and not (build / tokens[i + 1]).is_file():
            raise RuntimeError(f"Missing flash image: {tokens[i + 1]}")
    if importlib.util.find_spec('esptool') is None:
        raise RuntimeError('Source the locked ESP-IDF export.sh before running this command.')
    selected, application = select_board(usb_ports(), args.port)
    location = selected.location
    siblings = [p.device for p in usb_ports() if p.location == location]
    if shutil.which('lsof'):
        busy = subprocess.run(['lsof', '-t', *siblings], capture_output=True, text=True)
        if busy.stdout.strip():
            raise RuntimeError('Stop the XRCE agent and serial monitors before flashing. '
                               f'Port-owning PIDs: {busy.stdout.split()}')
    if application:
        print(f'Requesting ROM download through {selected.device} (CDC1, 1200 baud)', flush=True)
        # Start at normal logging speed, then change it explicitly so a retained
        # host line-coding setting cannot suppress the 1200-baud control request.
        with serial.Serial(selected.device, 115200, timeout=0, write_timeout=1) as connection:
            connection.baudrate = 1200
            time.sleep(0.05)
        selected = wait_for(location, ROM, 1)[0]
    print(f'Flashing {selected.device}, serial {selected.serial_number}, USB location {location}', flush=True)
    subprocess.run([sys.executable, '-m', 'esptool', '--chip', 'esp32s3',
                    '--port', selected.device, '--before', 'no-reset',
                    '--after', 'watchdog-reset', 'write-flash', '@flash_args'],
                   cwd=build, check=True, timeout=300)
    ports = wait_for(location, APP, 2)
    print('Flash verified; application USB returned: ' + ', '.join(p.device for p in ports), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError, serial.SerialException) as error:
        print(f'Flash failed: {error}', file=sys.stderr)
        sys.exit(1)
