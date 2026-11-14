# Orbo prototypes

Standalone ESP32-S3 firmware and desktop applications for USB/Wi-Fi robot
teleoperation. Each project contains its own firmware and Python UI.

| Project | Purpose |
|---|---|
| [teleop](teleop/README.md) | Keyboard wheel control over USB or Wi-Fi |
| [teleop-servo](teleop-servo/README.md) | Wheel control with independently calibrated PWM arms |

These projects use ESP-IDF 6.1 and a 1 Mbps DYNAMIXEL bus profile. The production
ROS firmware lives in [firmware/orbo](../firmware/orbo/README.md) and uses its own
bus driver and configuration. Do not interchange firmware configurations.

## Build

Activate ESP-IDF 6.1, then run from the repository root:

```sh
idf.py -C prototypes/teleop/firmware build
idf.py -C prototypes/teleop-servo/firmware build
```

These commands only build. Follow each project's instructions to configure,
flash and operate the hardware.

## Desktop applications

Install the selected project's `pc/requirements.txt` in a Python environment,
then launch its `pc/teleop.py`. The applications require PyQt6 and pyserial;
neither requires ROS or a Raspberry Pi runtime.

## Servo tests

From `prototypes/teleop-servo`, with its Python dependencies installed:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
cc -std=c11 -Wall -Wextra -Werror -Ifirmware/main \
  tests/test_motion.c firmware/main/arm_motion.c -lm -o /tmp/orbo-test-motion
/tmp/orbo-test-motion
```

The UI tests use a simulated link and offscreen rendering. The C tests check
motion bounds, calibration, direction, command expiry and stopping without
connecting to hardware. Generated UI captures are written to `tests/build/`.
