# Little Orbo — Wi-Fi keyboard drive + little hands

This standalone ESP-IDF project copies the running `Software/Teleoperate` project from
`C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\Software`, then adds the
two PWM arms using the driver approach in `firmware/orbo/main/flippers.c`.
The original projects are unchanged. No ROS or Raspberry Pi runtime is needed.

## Run

1. Join **PetBot** Wi-Fi, password **petbot123**.
2. Double-click **Start Teleop.cmd** in this folder.
3. Wireless is selected automatically. Connect to **192.168.4.1**.
4. Hold **W/A/S/D** or arrows to drive. **Space / Esc** stops wheels and disables arms.

If dependencies are missing, run `python -m pip install -r pc/requirements.txt`.
The existing wheel direction preferences are retained. On a fresh setup, Reverse right
defaults on, as documented for this robot. USB mode remains available for diagnostics.
Close/disconnect the old teleop app before controlling with this one.

## Arms and first calibration

| Signal | Board label | ESP32 GPIO |
|---|---|---|
| Left hand | D7 | 10 |
| Right hand | D10 | 21 |

Servo power comes from a suitable servo supply with common ground to the ESP32;
GPIO10/21 carry signal only. The user confirmed the signal pins, but mounted travel
limits have **not** been measured. The UI's 1400 / 1500 / 1600 microsecond starting
values are suggestions, not calibrated limits or guaranteed safe angles.

The firmware boots with **PWM off**. Calibration is independent for each hand:
minimum, center, maximum, and reverse direction. Applying limits does not move
either servo. The firmware acknowledges the exact values before Enable arms is
available. Settings are remembered on this PC, but must be applied again after
disconnect/reboot; arms never automatically enable on reconnect.

For the first physical check:

1. Keep the wheels stationary. Ensure the arms can move freely; if their neutral
   position is unknown, disengage the horns/linkages before first centering.
2. Start with a small window such as **1450 / 1500 / 1550 us**, select **Rest / center**,
   and apply the limits. These are still unverified starting values.
3. Enable briefly and observe the center, then use **Arms off**. Enabling initially
   commands center directly because ordinary PWM servos provide no position feedback.
4. Align the hands at rest. Start with **Manual pose**, low Travel, and move one slider
   a little at a time. If motion is reversed, disable arms, change Reverse, and reapply.
5. Expand limits only while observing clearance. Stop before binding, buzzing, or
   hitting the shell. The software cannot discover mechanical end stops automatically.

Once those limits are verified, choose a pattern:

| Pattern | Movement |
|---|---|
| Rest / center | Smoothly return to center while enabled |
| Sway together | Both hands swing together |
| Paddle / walking hands | Hands alternate forward/backward |
| Hello wave | Right hand waves; left stays centered |
| Happy wiggle | Both hands swing with a small phase offset |
| Manual pose | Independent left/right pose sliders |

**Speed** changes pattern tempo and limits the rate of pulse changes. **Travel**
scales motion within the applied limits (including manual poses). Begin with the
default 35% travel and 50% speed. Firmware clips each target to its individual limits.
Displayed pulse widths are **commanded PWM**, not measured servo positions.

**Arms off**, Space/Esc, disconnect, app close, or leaving the app disables arm PWM.
Entering a text/spin box also stops motion, so editing values cannot leave a drive key held.
The independent arm task expires commands after **500 ms**, even if the wheel bus is
busy. Disabling PWM does not disconnect servo power; behavior without pulses depends
on the servo. Wheels retain the existing 500 ms drive-command watchdog.

## Build / upload

On this PC, the helper uses the installed ESP-IDF 6.1 toolchain:

```powershell
cd 'C:\Users\sahlu\Desktop\little-orbo\final with servo'
.\build.ps1
.\build.ps1 -Flash -Port COM25
```

On another PC with an activated ESP-IDF environment, run `idf.py build` and
`idf.py -p YOUR_PORT flash` inside `firmware`. The checked-in sdkconfig preserves the
running reference: ESP32-S3, 16 MB flash, UART GPIO43/44, 1 Mbps, wheel IDs 1/2,
55 rpm maximum, PetBot AP, and UDP port 5005. It is distinct from the ROS firmware's
115200-baud profile. Do not substitute that profile here.

`menuconfig → Teleop robot` includes the two arm signal GPIOs. Servo motion uses
50 Hz, 14-bit LEDC PWM, following the existing flipper driver and
[Espressif's LEDC documentation](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/ledc.html).

To restore the previous application, build/flash the untouched reference project's
`Teleoperate/firmware` folder. It retains its original source and build output.

## Protocol additions

The original FIND, DRIVE, TORQUE, STATE and MSG commands remain supported over USB
and UDP. Arm commands use the same transport:

```text
ARMCFG left_min left_center left_max right_min right_center right_max left_sign right_sign
ARM mode speed travel left_pose right_pose
ARMS enabled mode left_commanded_us right_commanded_us configured pwm_ready
```

ARMCFG requires arms off, `500 <= min < center < max <= 2500`, and signs ±1.
Success echoes the exact ARMCFG command. This broad electrical validation does not
establish mechanical safety. ARM modes: 0 off, 1 rest, 2 sway, 3 paddle, 4 wave,
5 happy, 6 manual. Speed: 20–150, travel: 0–100%, poses: −100–100%.
The app sends ARM and DRIVE at 20 Hz; ARMS telemetry is sent at 10 Hz. An ARM
heartbeat does not keep wheels alive, and DRIVE does not keep arms alive.

The inherited SCAN diagnostic now stops wheels and disables wheel torque before
its blocking bus scan. Non-finite wheel commands and malformed arm commands are rejected.

## Checks

Run `python -m unittest discover -s tests -p 'test_*.py' -v` for the UI regression suite.
It covers mixed drive/arm commands, acknowledgement matching, invalid limits, stop,
focus loss, telemetry timeout, disconnect, and rendering. Render output is in
`tests/build/ui-preview.png`.

`tests/test_motion.c` exercises the actual firmware motion engine: bounds for every
pattern, slew limits, atomic calibration validation, direction, timeout, and off.
For a host with a C compiler: `cc -Ifirmware/main tests/test_motion.c firmware/main/arm_motion.c -lm -o test_motion`.
On this Windows PC it compiled, but Smart App Control blocked execution of the new
test executable. No security settings were changed. See `VALIDATION.md` for hardware checks.
