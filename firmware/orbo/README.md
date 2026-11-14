# Orbo ESP32-S3 micro-ROS firmware

This firmware publishes ROS 2 telemetry over native USB CDC0 and accepts bounded
control messages. CDC1 carries text logs. It boots disarmed; wheel and flipper
commissioning flags are disabled in the bare-board image.

The commands below target the Waveshare ESP32-S3-Nano on an Apple silicon Mac.
**Run commands from the repository root**, including commands shown in this file.
If currently in `firmware/orbo`, run `cd ../..` first. The checkout must include
the root `pixi.toml`, `pixi.lock`, `tools/`, `deploy/`, `ros_ws/`, and the referenced
bench scripts under `artifacts/test-evidence/`.

## 1. Set up Pixi and ROS 2 on the Mac

Use macOS 14 or newer, a native ARM64 terminal, Apple command-line tools, and
Homebrew or Pixi >=0.84.2 on `PATH`. Run `xcode-select --install` if the Apple tools
are missing. The current Pixi lock does not support Intel Macs. Start in a fresh
terminal without an active Conda, virtualenv, ESP-IDF, or other ROS environment.

```bash
./tools/install-ros-macos.sh
pixi run --locked ros-build-interfaces
pixi run --locked ros-check
```

The installer installs Pixi via Homebrew when needed and restores dependencies
with `pixi install --locked`. `ros-check` tests local C++/Python ROS messaging;
it does not contact the board. The interface task builds the six Orbo message
types into `ros_ws/install/macos`. Use Pixi's Python for ROS tests, not the
application's separate `uv` environment. More setup details are in the
[Mac runbook](../../docs/engineering/runbooks/ros-macos.md).

## 2. Build and start the native agent

Build once, from a normal terminal at the repository root:

```bash
./tools/build-xrce-agent-macos.sh
ls /dev/cu.usbmodem*
```

The builder enters the locked Pixi environment, fetches the agent source lock,
builds compatible spdlog and the standalone Micro XRCE-DDS Agent, and applies
`deploy/patches/xrce-agent-system-clock.patch`. This patch makes time sync use
Unix epoch time instead of Mac uptime. The runner supplies dynamic-library paths.
This is a serial/DDS bench bridge; the Pi deployment uses the full micro-ROS
Agent. Observe topics directly rather than relying on ROS node-graph discovery.

| Mode/interface | Port on the tested Mac | Purpose |
| --- | --- | --- |
| ROM bootloader, USB `303a:1001` | `/dev/cu.usbmodem1301` | Flashing only |
| Application CDC0, USB `303a:4002` | `/dev/cu.usbmodem1234561` | XRCE agent |
| Application CDC1, USB `303a:4002` | `/dev/cu.usbmodem1234563` | Text logs |

These names are examples, not stable device identifiers. Re-identify ports after
reconnecting. Only one process can own a given port; do not open a serial monitor
on CDC0 while the agent is running.

**Terminal A:** leave this running during the manual and basic automated tests:

```bash
./tools/run-xrce-agent-macos.sh /dev/cu.usbmodem1234561
```

## 3. Observe live ROS messages

In **each** additional ROS terminal, from the repository root:

```bash
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
ros2 interface show orbo_embedded_interfaces/msg/ControlSession
```

Using Bash explicitly makes the setup command the same even if your login shell
is zsh. Run one observer per terminal, or Ctrl-C before switching topics:

```bash
ros2 topic echo /orbo/embedded/health orbo_embedded_interfaces/msg/Health \
  --qos-reliability best_effort
ros2 topic echo /orbo/embedded/wheel_state orbo_embedded_interfaces/msg/WheelState \
  --qos-reliability best_effort
ros2 topic echo /orbo/embedded/actuator_status orbo_embedded_interfaces/msg/ActuatorStatus \
  --qos-reliability best_effort
```

Expect health `armed: false`, `clock_synced: true`, increasing timestamps, and a
stable `boot_id` until a physical reboot. Wheel-state sequence numbers should
increase across agent reconnects. A bare board has `valid: [false, false]` motor
feedback; it must not claim measured motion. `clock_synced` alone does not prove
accurate epoch time; the automated telemetry check also compares delivery age.

## 4. Publish test commands

Use a **bare board with both commissioning flags disabled** for these tests.
Keep the actuator-status observer running before publishing from another ROS
terminal configured as above. A successful CLI publish does not prove receipt;
check the status event and health. All examples publish once and bound discovery
waits to ten seconds.

Stop (accepted even without an arm session):

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 1, operation: 0}'
```

Expect status `state: 5` (stopped), and health `armed: false`.

Invalid interface version (expect `state: 4`, rejected):

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 99, operation: 1, enable_wheels: false}'
```

Deliberately invalid wheel and flipper commands (expect `state: 4` with the
corresponding `command_id`). Their zero validity and boot ID make them invalid;
these examples do not request accepted movement:

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/wheel_command orbo_embedded_interfaces/msg/WheelCommand \
  '{boot_id: 0, epoch: 0, sequence: 1, command_id: 920001, issued_epoch_ms: 0, valid_for_ms: 0, velocity_rad_s: [0.0, 0.0]}'
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/flipper_command orbo_embedded_interfaces/msg/FlipperCommand \
  '{boot_id: 0, epoch: 0, sequence: 1, command_id: 920002, issued_epoch_ms: 0, valid_for_ms: 0, pulse_us: [0, 0], duration_ms: 0}'
```

For a **valid software-only arm**, use the automated test below: it reads the
current boot ID/epoch and generates fresh timestamps immediately before sending.
A copied static arm payload expires. Arming requires a newer epoch, synchronized
time, and a validity window of at most 500 ms. The test uses `enable_wheels=False`,
observes arming, publishes stop, and verifies that reusing the epoch is rejected.
Status values are 1 accepted, 3 controller-finished, 4 rejected, 5 stopped, and
6 fault. An acknowledgment is not independent evidence of physical movement.

## 5. Run the automated bare-board checks

With Terminal A's agent running, execute in a configured ROS terminal. Use a new
output directory for every run to preserve failures as well as passes:

```bash
ORBO_RUN_DIR="$(mktemp -d /tmp/orbo-bench.XXXXXX)"
ORBO_BASE_TESTS=artifacts/test-evidence/esp32s3-nano-preflight-2026-11-13
python "$ORBO_BASE_TESTS/check-board.py" "$ORBO_RUN_DIR/telemetry.json" 30
python "$ORBO_BASE_TESTS/check-rejections.py" "$ORBO_RUN_DIR/rejections.json"
python "$ORBO_BASE_TESTS/check-software-session.py" "$ORBO_RUN_DIR/software-session.json"
printf 'Bench results: %s\n' "$ORBO_RUN_DIR"
```

Check each command's exit status and JSON `checks`; any `false` is a failure.
The telemetry check requires health/wheel messages, an unarmed state, valid clock
sync, increasing sequences, and all health delivery ages within 100 ms. The
rejection test uses fresh messages to test disarmed commands, an uncommissioned
wheel-arm request, and an invalid interface. The software-session test also checks
expired/future timestamps and the wrong boot ID. These scripts publish messages;
they are not read-only except for `check-board.py`.

For three automatic agent restarts, **first Ctrl-C Terminal A's agent**. This test
starts and stops its own agent; keep the same ROS shell and output directory:

```bash
python artifacts/test-evidence/esp32s3-usb-fix-2026-11-13/check-reconnects-persistent.py \
  "$ORBO_RUN_DIR" /dev/cu.usbmodem1234561
```

For physical power-cycle recovery, restart Terminal A's agent, then run:

```bash
ORBO_OLD_BOOT="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["samples"]["health"][-1]["boot_id"])' "$ORBO_RUN_DIR/telemetry.json")"
python "$ORBO_BASE_TESTS/check-power-cycle.py" "$ORBO_OLD_BOOT" "$ORBO_RUN_DIR/power-cycle.json"
```

While that watcher runs (up to 120 seconds), unplug USB, wait two seconds, then
reconnect normally without a B1–GND bridge. Start from a passing telemetry result
from the current boot; rerun `check-board.py` first if the board has since rebooted.
Expect a new boot ID, epoch zero, unarmed state, and automatic telemetry recovery.
Finish by stopping the agent with Ctrl-C and leaving Pixi shells with `exit`.

## 6. Firmware build, flash, and host regression tests

The C regression tests run without hardware or ROS:

```bash
firmware/orbo/host_tests/run.sh
```

They use address/undefined-behavior sanitizers, injected USB failures, 1 ms and
10 ms scheduler ticks, and 100 logical reconnect cycles. See
[host test coverage](host_tests/README.md).

Build firmware in a **separate ESP-IDF terminal**, outside Pixi. Install the
locked ESP-IDF v6.0.3 release and ESP32-S3 tools as described in the
[operator runbook](../../docs/engineering/runbooks/operator.md#2-build-firmware-without-flashing).
With IDF installed at the local path used for this bench:

```bash
source .sdk/esp-idf-v6.0.3/export.sh
python -m pip install --require-hashes -r firmware/orbo/host-build-requirements.lock
./tools/build-firmware.sh
python -m serial.tools.list_ports -v
```

Stop the agent and serial monitors before flashing. Enter ROM download mode by
connecting B1 to GND while resetting or reconnecting USB. Once the bootloader
enumerates, remove the bridge without resetting. Identify the actual bootloader
port and back up existing flash using the
[board procedure](../../docs/engineering/runbooks/esp32s3-nano.md#build-and-connect).
Then flash all three images (replace the example port if needed):

```bash
cd build-locked
python -m esptool --chip esp32s3 --port /dev/cu.usbmodem1301 \
  --before no-reset --after watchdog-reset write-flash @flash_args
cd ..
python -m serial.tools.list_ports -v
```

Expect three verified hashes and both application CDC ports after reset. The
watchdog reset was needed on this Mac because the default RTS reset left the
flasher stub running. Keep both commissioning flags disabled for bare-board tests.

## Results and troubleshooting

On 13 November 2026, the rebuilt image passed flash verification, live ROS message
exchange, rejection/software-session tests, three agent restarts, and power-cycle
recovery. USB logs were clean. A subsequent 30-second telemetry run **failed the
100 ms delivery budget**: 3 of 129 health messages were late, up to 326 ms, with
two session recreations and `ORBO_BUS` (6). The board stayed disarmed and its
sequence numbers remained increasing. Timing/connection stability remains
unresolved; do not interpret the suite as entirely passing or increase safety
timeouts to hide the failure. Physical actuators and full ros2_control/Nav2
operation have not been qualified. See the
[final report](../../artifacts/test-evidence/esp32s3-usb-fix-2026-11-13/final/board-validation.json).

- Missing `rclpy` or message types: use Pixi, build interfaces, and source the
  overlay in every ROS terminal. Do not combine the IDF and Pixi environments.
- No messages: check domain 42, best-effort subscriber QoS, CDC0 selection, the
  agent log, and serial-port ownership. A bootloader port cannot carry app topics.
- `clock_synced: true` with wildly wrong `epoch_ms`: rebuild with the agent helper
  so the macOS clock patch is present; compare against Unix time, not host uptime.
- Commands rejected: check boot ID, newer arm epoch, timestamps, validity, and
  calibration. Reconnection disarms; it does not authorize automatic rearming.
- No actuator-status event: start the best-effort observer before publishing;
  acknowledgments are not retained for a subscriber that joins afterward.
