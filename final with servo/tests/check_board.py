"""Non-moving protocol smoke test. Never sends a nonzero DRIVE or enables calibrated arms."""
import argparse
import json
import socket
import time
from pathlib import Path
import serial

parser = argparse.ArgumentParser()
parser.add_argument("--port")
parser.add_argument("--udp")
args = parser.parse_args()
if bool(args.port) == bool(args.udp):
    parser.error("choose --port COM25 or --udp 192.168.4.1")

if args.port:
    link = serial.Serial()
    link.port, link.baudrate, link.timeout = args.port, 115200, 0.05
    link.dtr = link.rts = False
    link.open()
    link.reset_input_buffer()
    def send(line): link.write((line + "\n").encode())
    def read(): return link.readline().decode(errors="replace").strip()
else:
    link = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    link.connect((args.udp, 5005))
    link.settimeout(0.05)
    def send(line): link.send(line.encode())
    def read():
        try: return link.recv(512).decode(errors="replace").strip()
        except socket.timeout: return ""

observations = []
def collect(seconds=0.5):
    lines = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = read()
        if line:
            lines.append(line)
            observations.append(line)
    return lines

try:
    send("DRIVE 0 0")
    send("ARM 0 50 0 0 0")
    send("FIND")
    lines = collect(1)
    assert "HERE PetBot" in lines, lines
    assert any(l.startswith("ARMS 0 0 0 0") and l.endswith(" 1") for l in lines), lines
    assert any(l.startswith("STATE ") and l.split()[-1] == "1" for l in lines), lines
    send("ARMCFG 1600 1500 1400 1450 1500 1550 1 -1")
    assert any("rejected" in l for l in collect()), "Invalid limits accepted"
    config = "ARMCFG 1450 1500 1550 1450 1500 1550 1 -1"
    send(config)
    assert config in collect(), "No calibration acknowledgement"
    send("ARM 9 50 35 0 0")
    assert any("rejected" in l for l in collect()), "Invalid mode accepted"
    send("ARM 2 50 101 0 0")
    assert any("rejected" in l for l in collect()), "Out-of-range travel accepted"
    send("ARM 2 50 35 0 0 trailing")
    assert any("rejected" in l for l in collect()), "Trailing garbage accepted"
    send("ARM 0 50 0 0 0")
    lines = collect()
    assert any(l == "ARMS 0 0 0 0 1 1" for l in lines), lines
    result = {"transport": args.port or args.udp, "passed": True,
              "motion_commanded": False, "observations": observations}
    output = Path(__file__).parent / "build"
    output.mkdir(exist_ok=True)
    (output / ("board-usb.json" if args.port else "board-wifi.json")).write_text(json.dumps(result, indent=2))
    print("PASS", args.port or args.udp, "discovery, wheel readiness, PWM readiness, limits ACK, malformed command rejection, arms off")
    print(next(l for l in reversed(lines) if l.startswith("STATE ")))
finally:
    try:
        send("DRIVE 0 0")
        send("ARM 0 50 0 0 0")
    finally:
        link.close()
