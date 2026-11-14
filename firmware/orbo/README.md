# Orbo ESP32-S3 micro-ROS firmware

This firmware publishes ROS 2 telemetry over native USB CDC0 and accepts bounded
control messages. CDC1 carries text logs. It boots disarmed. The checked-in
`sdkconfig` now enables wheel arming for the lifted-base XL430 bench, with a
1.2 rad/s maximum, left ID 1/right ID 2, 115200 baud, and signs +1/-1. Flippers remain
disabled. The generic Kconfig defaults still disable both commissioning flags.
Enabling wheel arming does not arm at boot or on reconnection and does not qualify
ground driving, geometry, or navigation.

The commands below target the Waveshare ESP32-S3-Nano on an Apple silicon Mac.
**Run commands from the repository root**, including commands shown in this file.
If currently in `firmware/orbo`, run `cd ../..` first. The checkout must include
the root `pixi.toml`, `pixi.lock`, `tools/`, `deploy/`, `ros_ws/`, and the referenced
bench scripts under `artifacts/test-evidence/`.

## Current lifted-wheel test

After the Mac/agent setup below, use the maintained wheel check for the current
wheel-enabled image. Keep the base lifted and the motor supply on. The motors
must report valid feedback with zero hardware errors before motion is attempted.

From the repository root, with the agent running in another terminal:

```bash
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
# Default: each wheel in both directions, then both wheels and expiry.
python firmware/orbo/bench_tests/check_wheels.py
# Optional read-only feedback and timestamp check:
python firmware/orbo/bench_tests/check_wheels.py --observe-only
```

The script uses domain 42, fresh arm epochs and timestamps, increasing command
sequences, +/-1.2 rad/s setpoints held for 8 seconds per direction, and 300 ms
command validity. Motion is the default; `--observe-only` skips arming and motion. It verifies
encoder direction/velocity and stopping, aborts on invalid or stale feedback,
and requests stop on completion/failure. It prints an automatically created JSON
result path; an explicit output path can optionally be passed as the first argument.
It refuses to overwrite existing output.
Physical chassis direction and independent stopping latency still need measurement.

Physical encoder tests on 13 November 2026 passed individual positive/negative
wheel commands and simultaneous positive commands, explicit stop, and command
expiry. At 0.3 rad/s, settled measured speeds in the final run were approximately
0.299 rad/s; each 8-second forward phase moved about 140 degrees. The timestamp
fix stamps the completed feedback snapshot after any agent ping, eliminating the
previous enclosing/motor timestamp inversion in the captured tests.

The earlier 0.3 rad/s test's first long run aborted during simultaneous motion after one right
motor feedback error (`ORBO_FEEDBACK`); the following full run passed. A separate
read-only interval retained a health delivery age of 185 ms, so the existing
100 ms delivery qualification remains unresolved. These failures are retained,
not overwritten. See the [wheel-control evidence](../../artifacts/test-evidence/esp32s3-wheel-control-2026-11-13/README.md).

The subsequent 1.2 rad/s test (four times the previous command speed) passed all
five motion phases on its second run, with settled speeds of 1.184–1.187 rad/s.
Explicit stop and command expiry both stopped and disarmed the wheels. Its first
run stopped on one left motor-feedback error during reverse; the repeat added no
read errors. These runs used the former 1 Mbps motor profile. They also recorded
a 329 ms health-delivery age; broader delivery qualification remains unresolved.
The reproduced motor CRC issue is addressed by the 115200-baud validation below. See the
[1.2 rad/s evidence](../../artifacts/test-evidence/esp32s3-wheel-speed-1p2-2026-11-13/README.md).

Do not run the old bare-board `check-rejections.py` against this wheel-enabled
image: its wheel-arm rejection case can now enable torque. That script belongs
to the wheel-disabled profile described in section 5.

## Motor bus configuration and diagnostics

The checked-in profile uses `CONFIG_ORBO_DXL_BAUD=115200`. At startup it checks
configured motor IDs at that rate. If a known XL430 (model 1060) is still at the
former 1 Mbps setting, it disables torque, reads back torque-off, requests baud
register 8 = 2, and verifies model and saved baud at 115200. A partial migration
can resume on the next boot. Motors already at 115200 receive no baud EEPROM
writes. Other models are refused. A failed initialization does not permit arming.
The motor baud setting persists across power cycles: older 1 Mbps firmware or
raw diagnostic tools must be configured accordingly before they can communicate.
The GPIOs, IDs, direction signs, 1.2 rad/s speed bound and feedback fault policy
are unchanged. This baud is independent of the USB logging port's line coding.

Warning logs on CDC1 now identify the motor ID and read register for bus errors,
and distinguish hardware/watchdog/torque checks from encoder discontinuities.
DYNAMIXEL status errors include their actual protocol error byte. During diagnosis
we captured `ff ff fd 00 01 04 00 55 03 ab 0c`: a valid motor reply reporting
incoming-request CRC error 0x03. This is distinct from a corrupted reply detected
by the host's CRC checker. A 100 us inter-transaction gap did not eliminate the
failure and was removed; it is not part of the final configuration.

Protocol and baud definitions: [ROBOTIS Protocol 2.0](https://emanual.robotis.com/docs/en/dxl/protocol2/#error)
and [XL430 baud register](https://emanual.robotis.com/docs/en/dxl/x/xl430-w250/#baud-rate8).
A CRC error identifies a communication failure; it does not establish ESP32 CPU
exhaustion or identify a specific electrical component without waveform capture.

After rebuilding and automatic USB flashing on 13 November 2026, the 115200-baud
profile completed the requested **30.02-second drive at [1.2, 1.2] rad/s**:
585 commands, 1,912 captured wheel samples, no unexpected disarms, both read-error
counters zero, and all feedback valid with zero hardware errors. Settled measured
speeds averaged 1.1864/1.1861 rad/s. A separate terminal/PTY keyboard test supplied
W/S/A/D, zero, and key-release timeout for 30 seconds; the session stayed armed
for 31.29 seconds until intentional quit, with 1,825 wheel samples and zero read
errors. Both runs ended with encoder-confirmed zero speed and disarm. Host
sanitizer, baud-migration, and nine manual-publisher regression tests passed.
The motors were raised off the ground; this does not qualify ground driving.
Generated captures remain local and ignored by Git.

For the full `ros2_control` path with measured wheel odometry and v9 RViz meshes,
see the [Mac control-stack runbook](../../docs/engineering/runbooks/ros-control-macos.md).
That path uses `tools/control-base.py`; do not run it alongside the direct helper below.

## Manual CLI and keyboard wheel control

The installed firmware accepts `orbo_embedded_interfaces/msg/WheelCommand` on
`/orbo/embedded/wheel_command`, with `velocity_rad_s: [left, right]` in rad/s.
It is not a `geometry_msgs/Twist` or `/cmd_vel` subscriber. Each message must
include the current boot ID, an explicitly armed epoch, a strictly increasing
sequence, and a fresh Unix-millisecond timestamp with bounded validity. Repeating
one static YAML message with `ros2 topic pub -r` will not maintain motion.

For the already-flashed updated firmware, no rebuild or B1–GND bridge is needed.
Keep USB connected, the base lifted, and motor power on. Use two fresh terminals
outside ESP-IDF and stop any other wheel-command publisher. In terminal A:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
./tools/run-xrce-agent-macos.sh /dev/cu.usbmodem1234561
```

Leave the agent running. Use the actual application CDC0 port if its name has
changed (`ls /dev/cu.usbmodem*`); reuse an already-running agent on that port.
The agent helper enters Pixi itself. In terminal B:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
python firmware/orbo/bench_tests/control_wheels.py keyboard
```

Wait for `Armed:` before using the keys below. Q or Ctrl-C exits with stop and
disarm; expect `Stopped and disarmed (encoder feedback).` Stop the agent only
after the controller exits. For a timed command instead, exit keyboard mode and
run either command in the same configured terminal B:

```bash
python firmware/orbo/bench_tests/control_wheels.py drive --left 1.2 --right 1.2 --seconds 2
# Or repeat the full 30-second regression:
python firmware/orbo/bench_tests/control_wheels.py drive --left 1.2 --right 1.2 --seconds 30
```

Use negative values for reverse, opposite signs for opposite wheel rotation, or
zero for an idle wheel. The current limit is +/-1.2 rad/s per wheel; duration is
bounded to 60 seconds. The helper discovers the board, checks feedback, requests
one fresh arm, streams live WheelCommand messages at approximately 20 Hz with
300 ms validity, then sends stop and checks disarmed/stopped feedback. A new
helper invocation requests a new epoch. Do not run it alongside the test sequence
or another command publisher. Ground/chassis direction has not been qualified.

For manual keys instead of a timed drive:

```bash
python firmware/orbo/bench_tests/control_wheels.py keyboard
```

| Key | Wheel command |
| --- | --- |
| W | Both positive |
| S | Both negative |
| A | Left negative, right positive |
| D | Left positive, right negative |
| Space or X | Zero velocity; remain armed |
| Q or Ctrl-C | Stop, disarm and exit |

Keyboard mode arms once on startup and sends zero until a movement key arrives.
Speed defaults to 1.2 rad/s; use `keyboard --speed 0.6` for half speed. Terminal
input has no key-release events: tap/hold WASD, and after 350 ms without a movement
key/repeat the requested velocity returns to zero. The initial keyboard-repeat
delay can produce a brief pause when holding a key. Commands retain their own
300 ms firmware expiry. Faults, stale feedback, conflicting publishers, or a
changed boot/session stop the controller; it never automatically rearms after
such a failure. Ctrl-Z also exits with a stop rather than suspending publication.

For an explicit stop from a configured ROS terminal:

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 1, operation: 0}'
```

The manual helper's message sequencing, guards, key timeout, escape-sequence
handling and terminal restoration are covered by host mocks using real ROS
message classes. The earlier 1 Mbps timed drive reproduced a firmware `FEEDBACK (5)` stop after
the left-wheel read-error counter rose from 0 to 1. The publisher's maximum send
gap was 52.6 ms and the boot/session stayed unchanged. The subsequent diagnostic
build identified incoming-request CRC errors. Both timed drive and live keyboard
input passed after the 115200-baud change; see the motor-bus results above.

On failure, the helper now captures `/orbo/embedded/actuator_status` and prints
the original firmware fault, feedback counters/voltage, last accepted sequence,
and maximum publication gap. The cleanup stop can change `health.fault` to
`DISARMED (1)`; that later value does not identify the original cause. A transient
invalid motor sample may have recovered by the next published WheelState while
the controller remains disarmed. `EXPIRED (2)` means command validity elapsed,
`FEEDBACK (5)` means feedback failed validation or freshness, and `BUS (6)` covers
bus/transport faults. For the exact low-level cause, inspect CDC1 warning logs: the firmware now
reports read register/error code, motor protocol errors, hardware/watchdog/torque
validation failures and encoder discontinuities.

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

These historical bare-board checks require both commissioning flags disabled;
they are not the test sequence for the currently selected wheel-enabled image.
Use the lifted-wheel test above for that image.

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

Stop the agent and serial monitors before flashing. With this firmware installed,
leave B1 disconnected and run this from the repository root in the IDF terminal:

```bash
./tools/flash-firmware.sh
```

The Mac helper sets the logging interface (CDC1) to 1200 baud. Firmware latches
a disarm request, rejects further arms, and lets the motor task send zero velocity
and torque-off before rebooting into ROM download mode. It leaves 150 ms without
motor traffic for the configured 100 ms motor watchdog, then detaches USB and
switches the internal PHY to hardware USB Serial/JTAG. CDC0 ROS traffic and normal
115200-baud logging do not trigger a reboot. Do not open CDC1 at 1200 baud for logs.

The helper follows the same physical USB location into the bootloader, flashes
the three images, verifies them, restarts via watchdog, and waits for both
application ports. It refuses ambiguous boards and ports held by an agent or
monitor. For multiple boards, select one with `--port /dev/cu.usbmodem1234563`.
Automatic application-port discovery currently targets macOS. Direct `idf.py flash`
does not send this CDC1 trigger; use the helper for the complete automatic cycle.
The helper flashes the existing `build-locked` output; it does not rebuild it.
Use `--build-dir /absolute/path/to/build` for a different build directory. Stop on
a failed hash verification or missing application ports and inspect the error
before retrying. The bridge procedure below remains available for recovery.

For the **first installation**, or recovery from firmware that cannot process USB,
connect B1 to GND and press/release RST. Keep the bridge connected while flashing
(replace the example bootloader port with the actual one):

```bash
cd build-locked
python -m esptool --chip esp32s3 --port /dev/cu.usbmodem1301 \
  --before no-reset --after no-reset write-flash @flash_args
cd ..
```

After three verified hashes, remove B1–GND and press/release RST once. Subsequent
updates use the helper without a bridge or button press. The current `sdkconfig`
permits wheel arming; disable
`CONFIG_ORBO_WHEELS_CALIBRATED` explicitly when building an actual bare-board test
image. Do not confuse either setting with an automatic arm command.

## Results and troubleshooting

Automatic bootloader entry was tested on 13 November 2026: after the initial
bridge-assisted install, the helper reflashed all three images and restarted the
application with B1 disconnected and no button presses. Normal logging and the
busy-port refusal passed. ROS feedback passed after an initial discovery timeout;
both observations are retained locally under
`artifacts/test-evidence/esp32s3-auto-bootloader-2026-11-13/`. Generated reports
and hardware captures are ignored by Git; they are not repository deliverables.

Earlier on 13 November 2026, the wheel-disabled image passed flash verification, live ROS message
exchange, rejection/software-session tests, three agent restarts, and power-cycle
recovery. USB logs were clean. A subsequent 30-second telemetry run **failed the
100 ms delivery budget**: 3 of 129 health messages were late, up to 326 ms, with
two session recreations and `ORBO_BUS` (6). The board stayed disarmed and its
sequence numbers remained increasing. Timing/connection stability remains
unresolved; do not interpret the suite as entirely passing or increase safety
timeouts to hide the failure. Later raised-wheel tests are recorded above; ground
driving and full ros2_control/Nav2 operation have not been qualified. See the
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
