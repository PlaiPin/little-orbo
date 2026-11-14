# Little Orbo target system

**Software handoff complete (13 November 2026):** [handoff and reproducible checks](docs/engineering/software-handoff.md). The earlier full distribution is `artifacts/release/orbo-software-handoff.tar.gz`; the latest [Commons patch separation](integrations/trace_commons/README.md) is delivered in the repository as a **source-only handoff**, with **binary packaging and release-archive refresh pending separately**; [hardware commissioning and live-service validation](docs/engineering/pending-validation.md) remain separate pending steps.

The target is Raspberry Pi 5 (8 GB), Ubuntu 24.04 ARM64 / ROS 2 Jazzy, and ESP32-S3. Uncommissioned target firmware, ARM64 ROS/Agent builds, a Python wheel and a complete deployment source bundle are available. The ARM64 staging installation and owner-IPC smoke test pass; device/live acceptance remains pending. Host tests and synthetic scenarios do **not** establish hardware, live-provider, Commons delivery or navigation acceptance.

The [master execution plan](master-implementation-phases.md) and [implementation record](docs/engineering/implementation-status.md) distinguish implemented, deployed, verified and externally pending work. Existing CAD and `Software/Teleoperate` remain intact; production firmware does not link legacy UDP/text motion handlers.

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
See the [recorded results](artifacts/test-evidence/esp32s3-usb-fix-2026-11-13/final/board-validation.json).

### Python application development

The Orbo Python application workflow requires Python 3.12 and `uv`. Run from this repository:

```sh
uv sync --frozen --all-extras --group dev
uv run --all-extras pytest -q
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
| `integrations/booster_k1` | On-K1 official SDK worker, dependency lock, lease/stop/recovery and commissioning |
| `integrations/trace_commons` | Two generic fixes plus three Orbo client/server patches, durable revocation/recovery, minimized exports and targeted evidence retrieval |
| `artifacts/test-evidence` | Recorded local checks, explicitly distinct from device/live results |
| `docs/engineering` | Architecture contracts, status and operator instructions |

Credentials load from systemd's credential directory or injected `NEARAI_API_KEY` / `FISH_API_KEY` environment variables. Never commit keys. Fish is owner-prepaid; the runtime contains no purchase operation. Cloud processing, local execution, speech and contribution permissions are distinct and default to absent.

Firmware boots disarmed. Generic Kconfig defaults disable wheel and flipper commissioning; the checked-in `sdkconfig` selects the wheel-enabled lifted-base bench profile described above. Flipper pins and pulse limits remain unset and flippers stay disabled. Ground driving and navigation still require physical qualification. The owner identified the humanoid as Booster K1; its [official SDK bridge](integrations/booster_k1/README.md) is implemented with motion disabled pending physical commissioning. The [hardware inventory](docs/engineering/hardware-inventory.md) records the repository specifications.

Build and operate using the [operator runbook](docs/engineering/runbooks/operator.md). Flashable uncommissioned ESP32-S3 artifacts and ARM64 development packages are under `artifacts/`, with hashes and explicit deployment limitations.

Rebuild the artifact index with `python3 tools/package-release.py` after building the runtime (`uv build --wheel --sdist --out-dir artifacts/runtime`). The explicit deployment archive excludes dependency checkouts, build directories, databases, secrets and unrelated CAD. Current patch-separation results and exact validation limits are recorded in [the Commons review packet](integrations/trace_commons/upstream/validation.md). Earlier target-device software checks remain in the implementation record; no new hardware or live-service acceptance is claimed.

ROS keyboard/gamepad teleop, the ArduCam B0033+B0087 **OV5647** camera, and WaveShare **USB TO AUDIO** microphone/speaker are covered by the [Pi peripherals runbook](docs/engineering/runbooks/pi-peripherals.md) and [architecture diagram](docs/engineering/diagrams/pi-peripherals.svg). Camera capture and motion remain opt-in; physical qualification is pending.

## Private cloud validation

Use the [NEAR/Fish live-validation runner](docs/engineering/runbooks/live-validation.md) to initialize owner-only credential templates in the gitignored `.live-validation/` directory and run network-free preflight. Live execution requires an explicit `--execute` batch. Commons remains separate until its compatible server is available.

Credential sample: [`deploy/live-validation/credentials.env.example`](deploy/live-validation/credentials.env.example). Edit the ignored `.live-validation/credentials.env` with the NEAR key, Fish key and Fish voice ID.
