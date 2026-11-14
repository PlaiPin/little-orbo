# Little Orbo target system

The target is Raspberry Pi 5 (8 GB), Ubuntu 24.04 ARM64 / ROS 2 Jazzy, and
ESP32-S3. Native Mac development uses the locked Pixi environment. The host Python
runtime and all five ROS package sources are versioned; deployment archives,
generated evidence and external service installations are separate.

Start with the [runtime guide](packages/orbo_runtime/README.md),
[ROS workspace guide](ros_ws/README.md), and [firmware README](firmware/orbo/README.md).
Software-only checks do not establish physical driving, Pi peripheral operation,
live-provider behavior or navigation acceptance. Existing CAD and
`Software/Teleoperate` remain intact; the ROS firmware does not use legacy UDP/text
motion handlers.

## Development commands

### MacBook ROS 2 / Pixi setup

The locked environment supports **Apple silicon, macOS 14 or newer**, and Pixi
0.84.2 or newer. Use a native ARM64 terminal, outside Rosetta, with no active
Conda, Python virtualenv, ESP-IDF, or other ROS environment. Intel Macs are not
covered by the current lockfile. Install Apple's command-line tools with
`xcode-select --install` if missing. Have either Homebrew or Pixi on `PATH`.

From the repository root:

```bash
./tools/install-ros-macos.sh
pixi run --locked ros-build-interfaces
```

The installer installs Pixi through Homebrew if needed, runs
`pixi install --locked`, and verifies a local ROS 2 C++ publisher/Python subscriber
exchange. It does not communicate with the ESP32. Rerun that check with
`pixi run --locked ros-check`. Use Pixi's Python for `rclpy`; the `uv` environment
below is separate. See the [Mac runbook](docs/engineering/runbooks/ros-macos.md)
for environment troubleshooting and `pixi run orbo-preview` for the RViz model.

### Quick start: Xbox joystick teleoperation

Connect the Xbox controller to the **Mac** by USB or Bluetooth. The Mac publishes
joystick input over the local network; the **Pi** runs Orbo's runtime and ROS
controllers. Left stick drives/steers, and **LB must be held to move**. Release LB
to command zero velocity. The initial mapping uses 25% of calibrated speed limits.

**One-time Mac build**, from this repository:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi install --locked
pixi run --locked ros-build-workspace
```

**Mac terminal 1 — Xbox input (leave running):**

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY
ros2 run joy joy_enumerate_devices
```

Copy the exact Xbox device name from the output into this command:

```bash
ros2 launch orbo_bringup joy.launch.py driver:=game_controller_node \
  device_name:="EXACT NAME FROM ENUMERATOR"
```

**Mac terminal 2 — verify input:** open another Pixi shell, source the same
workspace and set the same environment variables as terminal 1, then run:

```bash
ros2 topic echo /orbo/teleop/joy
```

Check that left-stick movement changes axes 0/1 and LB changes button 9 from 0 to
1 while held. Ctrl-C stops the observer. If enumeration lists no devices, connect
or pair the controller and rerun it before proceeding.

**Pi — prepare and authorize motion:** follow the
[robot startup and session commands](ros_ws/README.md#2-pi-configure-and-start-the-robot)
to configure the Xbox mapping in `/etc/orbo/runtime.toml`, start the agent,
controllers, runtime and one teleop gate, activate the drive controller, and
request a bounded owner session. This requires measured wheel calibration and a
qualified controller mapping. The joystick launch alone publishes input; it does
not activate controllers or arm motors. Keep LB released while requesting the
session, then hold it to drive. Sessions last at most 30 seconds.

**Stop**, from a Pi owner terminal:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request stop
```

Then Ctrl-C the Mac joystick launch. For network troubleshooting, Pi-connected
controllers, and qualification steps, see the full
[Xbox launch guide](ros_ws/README.md#remote-xbox-joystick-control).
The existing lifted-bench keyboard helper below is a separate control path;
stop it before using application teleop.

### Quick start: keyboard control with the updated firmware

With the updated firmware already flashed, keep USB connected, motor power on,
and the base lifted. No rebuild, flash, or B1–GND bridge is needed for this test.
Use two fresh terminals outside ESP-IDF; stop any other wheel-command publisher.

**Terminal 1 — agent (leave running):**

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
./tools/run-xrce-agent-macos.sh /dev/cu.usbmodem1234561
```

Use the application CDC0 port; check `ls /dev/cu.usbmodem*` if its name changed.
The agent helper activates Pixi itself. If an agent is already using that port,
keep it running instead of starting a second copy.

**Terminal 2 — keyboard controller:**

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
python firmware/orbo/bench_tests/control_wheels.py keyboard
```

Wait for `Armed:`. W/S command both wheels positive/negative; A/D command
opposite wheel directions. Space/X requests zero while staying armed. Q or
Ctrl-C stops, disarms, and exits. Default speed is 1.2 rad/s; add `--speed 0.6`
for half speed. After 350 ms without a movement key/repeat, velocity returns to
zero; the operating system's initial key-repeat delay can cause a brief pause.
Expect `Stopped and disarmed (encoder feedback).` on exit, then stop the agent
with Ctrl-C. The helper does not automatically rearm after a fault.

For the 30-second straight-wheel regression, run this in terminal 2 after the
keyboard controller has exited:

```bash
python firmware/orbo/bench_tests/control_wheels.py drive --left 1.2 --right 1.2 --seconds 30
```

See the [firmware instructions](firmware/orbo/README.md#manual-cli-and-keyboard-wheel-control)
for feedback diagnostics and an independent ROS stop command. First-time setup
is above; agent build and additional ROS observers are below.

### Connect the ESP32 micro-ROS firmware to ROS 2

The current firmware `sdkconfig` enables wheel arming for a lifted-base bench
at a 1.2 rad/s maximum; it still boots disarmed and keeps flippers disabled.
The motor bus now runs at 115200 baud, with automatic torque-off migration of
known XL430s from the former 1 Mbps profile. A 30-second drive and 30-second
keyboard-input run passed without feedback errors or unexpected disarming.
Use the [lifted-wheel test](firmware/orbo/README.md#current-lifted-wheel-test)
for that image, or use [manual CLI/keyboard control](firmware/orbo/README.md#manual-cli-and-keyboard-wheel-control).
Do not run the historical bare-board wheel-arm rejection test
with wheel arming enabled, because that request can now enable torque.

For the flashed board, build the native Mac XRCE agent once:

```bash
./tools/build-xrce-agent-macos.sh
ls /dev/cu.usbmodem*
```

This helper enters Pixi itself, fetches locked agent sources, and applies the
macOS Unix-clock synchronization patch. The native bridge is the standalone
**Micro XRCE-DDS Agent**, not the full micro-ROS graph manager used on the Pi.
Test topics directly; `ros2 node list` is not the acceptance check.

Terminal A, from the repository root, starts the agent and stays running:

```bash
# Replace with the actual application CDC0 port on your Mac.
./tools/run-xrce-agent-macos.sh /dev/cu.usbmodem1234561
```

The tested board exposes CDC0 as `/dev/cu.usbmodem1234561` for XRCE traffic and
CDC1 as `/dev/cu.usbmodem1234563` for text logs. Port names can change. The ROM
bootloader port `/dev/cu.usbmodem1301` is for flashing, not the agent. Only one
process may open each serial port.

In **each ROS terminal**, start at the repository root and activate the messages:

```bash
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
```

Terminal B observes health, or actuator acknowledgments when publishing tests:

```bash
ros2 topic echo /orbo/embedded/health orbo_embedded_interfaces/msg/Health \
  --qos-reliability best_effort
# Ctrl-C before switching to the next observer:
ros2 topic echo /orbo/embedded/actuator_status orbo_embedded_interfaces/msg/ActuatorStatus \
  --qos-reliability best_effort
```

Terminal C, with the same ROS environment, can publish a stop:

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 1, operation: 0}'
```

Expect actuator status `state: 5` and health `armed: false`. A CLI "publishing"
message alone is not an acknowledgment. For a deliberately invalid arm request:

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 99, operation: 1, enable_wheels: false}'
```

Expect `state: 4` (rejected). The [firmware README](firmware/orbo/README.md)
includes wheel/flipper rejection examples, fresh software-only arm/stop tests,
bounded telemetry checks, firmware build/flash instructions, and recovery tests.
Stop the agent with Ctrl-C when finished; `exit` leaves each Pixi Bash shell.

For subsequent firmware updates on the Mac, stop the agent and serial monitors,
then use a separate ESP-IDF terminal:

```bash
source .sdk/esp-idf-v6.0.3/export.sh
./tools/build-firmware.sh
./tools/flash-firmware.sh
```

The flash helper requests bootloader entry through the logging USB port, follows
the board into ROM, flashes and verifies the images, and restarts the application.
No B1–GND bridge is needed once the automatic-entry firmware is installed. The
[firmware README](firmware/orbo/README.md#6-firmware-build-flash-and-host-regression-tests)
also covers initial installation and manual recovery. Opening CDC1 at 1200 baud
is reserved for bootloader entry; use 115200 for normal logs.
The automatic reflash was verified with B1 disconnected and no button presses;
all three hashes matched and the application USB ports returned. Generated test
reports and hardware captures remain local and are excluded from Git. See the
local automatic flashing results in `artifacts/test-evidence/esp32s3-auto-bootloader-2026-11-13/`.

**Bench status, 13 November 2026:** live ROS publishing/subscription, command
rejections, software-only arm/stop, three agent restarts, and power-cycle recovery
were tested. A subsequent timing check failed: three health samples exceeded
100 ms, reaching 326 ms, with two session recreations. This remains unresolved;
later raised-wheel tests passed at 1.2 rad/s. The reproduced motor-feedback
CRC errors at 1 Mbps were addressed by the tested 115200-baud profile. Broader
telemetry timing, ground driving, and full ros2_control/Nav2 qualification remain open.
Generated validation captures remain local and are ignored by Git.

### Integrated ROS 2 base control on the Mac

The native Pixi environment now includes the controller manager, differential-drive
controller and joint-state broadcaster. See the [Mac control-stack runbook](docs/engineering/runbooks/ros-control-macos.md)
for the build, agent, controller, keyboard and live-RViz commands. This path uses
measured wheel positions for odometry and nominal CAD dimensions for a lifted bench.
The 30-second physical test of this new path is still pending; initial runs expired,
and the battery is charging before the revised command-rate test. Software-only
validation can continue with `pixi run --locked ros-test-control` in isolated domain 96.

The live description and preview now use the complete **v9 assembly**, including
the v8 chassis, flippers and shells plus the v9 display mount. Use the runbook's
internal preview to inspect beneath the shells. Flippers are fixed CAD references;
their physical PWM configuration remains unresolved.

Next: finish the sustained encoder-odometry and fault/recovery tests on the Mac,
measure the physical geometry and ground odometry, then deploy the same controller
stack on the Pi. Sensor integration, localization and Nav2 follow that validation.

### Runtime and ROS source

The [host runtime guide](packages/orbo_runtime/README.md) explains application
coordination, robot adapters, optional services, and the different keyboard paths.
The [ROS workspace guide](ros_ws/README.md) documents all five packages, global
TF topics, camera/Nav2 launch boundaries, and fresh-checkout setup:

```bash
pixi install --locked
pixi run --locked ros-build-workspace
pixi run --locked ros-test-integrations
```

Pixi installs `orbo-runtime` directly; no `PYTHONPATH` workaround is needed.
The integration tests use synthetic inputs in domain 96. Physical driving and
Pi peripheral/navigation acceptance remain separate. Old v05 meshes and generated
evidence are excluded from source commits.

For standalone OV5647 camera setup on the Pi, use the
[ROS 2 camera setup guide](deploy/ros2-camera/README.md). Its script checks the
working libcamera installation, builds `camera_ros`, and publishes `/camera/*`
topics for an initial image test.

### Python application development

The Orbo Python application workflow requires Python 3.12 and `uv`. Run from this repository:

```sh
uv sync --locked --all-extras --group dev
uv run --locked --all-extras pytest -q
uv run ruff check packages tests
uv run --all-extras orbo diagnostics
uv run --all-extras orbo demo --database /tmp/orbo-synthetic.db
cc -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined \
  -Ifirmware/orbo/main firmware/orbo/main/control.c \
  firmware/orbo/host_tests/test_control.c -o /tmp/orbo-control-test
/tmp/orbo-control-test
```

The demo makes no network or hardware calls. It labels simulated action/playback and preserves its episode and export in SQLite. `uv.lock` pins Python dependencies and package hashes; Pi installation and audio verification remain separate checks.

## Components

| Location | Responsibility |
|---|---|
| `packages/orbo_runtime` | SQLite journal, reminders/preferences, broker, conversation/tools, NEAR/Fish/audio, ROS adapter, exports and owner CLI |
| `firmware/orbo` | Production micro-ROS application, local command expiry, XL430 feedback/watchdogs and calibrated PWM |
| `ros_ws/src` | Bounded shared embedded interfaces and Pi ROS packages |
| `artifacts/test-evidence` | Ignored local captures; not part of the source distribution |
| `docs/engineering` | Architecture contracts, status and operator instructions |

Credentials load from systemd's credential directory or injected `NEARAI_API_KEY` / `FISH_API_KEY` environment variables. Never commit keys. Fish is owner-prepaid; the runtime contains no purchase operation. Cloud processing, local execution, speech and contribution permissions are distinct and default to absent.

Firmware boots disarmed. Generic Kconfig defaults disable wheel and flipper commissioning; the checked-in `sdkconfig` selects the wheel-enabled lifted-base bench profile described above. Flipper pins and pulse limits remain unset and flippers stay disabled. Ground driving and navigation still require physical qualification. The runtime includes clients for a separate Booster K1 worker and Commons service;
those services and their deployment assets are not installed by the Python or
Pixi setup above.

Build the Python distribution with `uv build`; local outputs go to ignored
`dist/`. Use the [workspace guide](ros_ws/README.md) for camera/teleop/navigation
launch integration and its Pi validation boundary. The current Mac bench setup
is documented in the [control runbook](docs/engineering/runbooks/ros-control-macos.md).

## Private cloud validation

The installed runtime can create private local templates and perform network-free
preflight without any deployment wrapper:

```bash
uv run --locked --all-extras python -m orbo_runtime.live_validation init
# Fill the ignored .live-validation/credentials.env privately, then:
uv run --locked --all-extras python -m orbo_runtime.live_validation check
```

The default directory is gitignored `.live-validation/`. Preflight reports missing
configuration without making provider calls. Live execution is a separate
explicit `run --execute` operation and may consume credit; it is not part of the
runtime or ROS test commands. Keep credentials and generated reports local.
