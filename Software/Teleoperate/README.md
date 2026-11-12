# Teleoperate

Drive the robot with the keyboard, **wired (USB-C cable) or wireless (Wi-Fi)**, from one app. The hardware is **ESP32-S3 + 2 × DYNAMIXEL XL430-W250-T**, the same circuit as the Two Wheel robot: single-wire bus on GPIO43/44 (TX and RX jumpered, open-drain TX) through a BSS138 level shifter, 1 Mbps, **left ID 1, right ID 2**. Teleop only: no balancing, no tuning.

```
                 ┌── USB-C cable ───────────────┐
PC (pc/teleop.py)┤                              ├─► ESP32-S3 (firmware/) ──single-wire bus──► XL430 × 2
                 └── Wi-Fi "PetBot", UDP 5005 ──┘
```

The firmware listens on **both** links at the same time. You pick one in the app; there's no reflashing to switch.

| Path | What it is |
|---|---|
| `firmware/` | ESP-IDF project: `main.c` (USB + Wi-Fi), `teleop.c` (commands, watchdog, status), `wheels.c` (motor setup and driving), `dxl_bus.c` (single-wire DYNAMIXEL driver) |
| `pc/teleop.py` | The app: choose Wired / Wireless, connect, drive |

## 1. Flash the firmware (once)

ESP-IDF v6.1, in PowerShell:
```powershell
. C:\Espressif\tools\Microsoft.v6.1.PowerShell_profile.ps1
cd firmware
idf.py -p COM25 flash          # your ESP32's port (Device Manager)
```
Settings in `idf.py menuconfig` → **Teleop robot**:

| Setting | Default |
|---|---|
| Pins (TX / RX), baud | 43 / 44, 1 Mbps |
| Motor IDs | left 1, right 2 |
| Max speed, ramp, watchdog | 55 rpm, 40, 500 ms |
| Wi-Fi network / password, UDP port | `PetBot` / `petbot123`, 5005 |

## 2. Drive

```powershell
pip install -r pc/requirements.txt
python pc/teleop.py
```
Switch the robot battery **on** first.

| | Wired | Wireless |
|---|---|---|
| Connect | USB-C cable from the PC to the ESP32 | Join the Wi-Fi **`PetBot`** (password `petbot123`) on the PC. It has no internet. |
| In the app | Pick **Wired**, choose the port marked **(ESP32)** | Pick **Wireless**, address `192.168.4.1` (or press **Find robot**) |

Press **Connect**. The line under it should say "Connected … Motors: OK". Then click the window and drive:

| Key | Action |
|---|---|
| Hold **W / S** (or ↑ / ↓) | forward / backward |
| Hold **A / D** (or ← / →) | turn left / right; combine with W / S to curve |
| **Space** / **Esc** | stop |
| Speed / Turn sliders | wheel rpm for the keys (max 55 rpm) |
| Reverse left / right | flip a wheel that turns the wrong way on W. This robot: **Reverse right** on (saved) |
| Torque on | untick to let the wheels turn freely |

**Safety:** the app sends a command 20 times a second. If the robot gets nothing for **0.5 s** (cable pulled, Wi-Fi lost, app closed), it stops the wheels. It also stops when the app window loses focus.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Wired: "Cannot connect … cannot open COMx" | `idf.py monitor` or another program has the port. Close it. |
| Wired: "No answer on COMx" | Wrong port, or the teleop firmware isn't flashed. |
| Wireless: "No reply from 192.168.4.1" | PC not on `PetBot`, or battery off. On Windows, allow Python through the firewall (private networks). |
| "Motors: NOT READY" | The log line `Motors not ready: …` says why: battery, motor cable, ID/baud. |
| `… bus wiring fault` | Our own packet didn't come back: check the TX-RX jumper and the level shifter's power. |
| Battery shows ~4-5 V | The battery is off and the motors run from USB power. |
| A/D turn the wrong way | Left and right IDs are swapped: change them in menuconfig. |

## Protocol

The same text protocol runs on both links (one line over USB, one packet over UDP); see `firmware/main/teleop.h`.
- PC → robot: `FIND`, `DRIVE <left_rpm> <right_rpm>`, `TORQUE <0|1>`
- robot → PC: `HERE <name>`, `STATE <l> <r> <volts> <torque> <ok>`, `MSG <text>`
