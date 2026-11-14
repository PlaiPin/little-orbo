# Raspberry Pi 5: native ROS 2 installation and testing

Target: **Ubuntu 24.04 ARM64, ROS 2 Jazzy already installed, and an OV5647 camera
working with `rpicam-hello`**. The primary workflow is `ros2 run` / `ros2 launch`.
Systemd is optional. Pixi is only used for Mac development, never on the Pi.

Run one block at a time in Bash. Initial movement tests require the current
wheel-enabled ESP32 firmware, motor power, and the wheels lifted. Keep the power
cutoff within reach. Run only one motion owner and one velocity gate at a time.

## Contents

1. [Upgrade an existing installation](#1-upgrade-an-existing-installation)
2. [First installation](#2-first-installation)
3. [Open ROS terminals](#3-open-ros-terminals)
4. [Check installed executables](#4-check-installed-executables)
5. [Agent and direct-wheel checks](#5-agent-and-direct-wheel-checks)
6. [Complete lifted-wheel base stack](#6-complete-lifted-wheel-base-stack)
7. [Camera](#7-camera)
8. [Microphone and speaker](#8-microphone-and-speaker)
9. [Full application teleop](#9-full-application-teleop)
10. [Software tests](#10-software-tests)
11. [Optional systemd deployment](#11-optional-systemd-deployment)
12. [Remaining commissioning](#12-remaining-commissioning)

## 1. Upgrade an existing installation

If you already ran `sudo ./deploy/install-pi.sh --camera-stack existing`, **do not
delete the installation**. Keep `/etc/orbo` (configuration, credentials,
calibration), `/var/lib/orbo` (journal/data), `/opt/orbo/.venv`, and
`/opt/orbo-camera` (working camera overlay). No manual build-directory cleanup is
required for this refactor. The installer rebuilds the workspace in place.

First stop motion through the running owner/helper, confirm disarming, and close
any manual ROS launches. Then, in your **normal-user terminal**, stop and disable
the old services so they cannot compete with foreground ROS commands:

```bash
sudo systemctl disable --now \
  orbo-teleop orbo-navigation orbo-control orbo-runtime \
  orbo-k1-runtime orbo-camera orbo-micro-ros-agent
```

If the old installer only installed the units, they are already disabled; this
also stops any that you subsequently started. The unit files may remain on disk.
They are harmless while disabled and stopped. This command does not erase data.

Update and reinstall from a fresh normal-user shell without an old workspace or
virtualenv activated:

```bash
cd ~/little-orbo
git pull --ff-only
sudo ./deploy/install-pi.sh --camera-stack existing
```

The installer refuses to overwrite an installation with active Orbo services.
It preserves configuration, rebuilds the new Python ROS package, and does not
install/start/enable systemd units by default. Old source copies can remain;
installed ROS executables use the rebuilt workspace. Close old ROS shells after
upgrading and open fresh ones using section 3. Continue with section 4.

## 2. First installation

Skip this section if you followed the upgrade above. In your normal-user terminal:

```bash
git clone https://github.com/PlaiPin/little-orbo.git ~/little-orbo
cd ~/little-orbo
sudo ./deploy/install-pi.sh --camera-stack existing
```

This installs the native ROS workspace under `/opt/orbo/ros_ws`, the service
account `orbo`, and the Python dependencies under `/opt/orbo/.venv`. Colcon
installs ROS executables with the appropriate Python interpreter in their
shebangs; you do not need to activate that virtualenv to use `ros2 run`.

The working camera packages are preserved. If APT proposes a camera-stack
change, resolve the reported conflict instead of switching modes to bypass it.
The installer does not flash firmware, start nodes, or grant motion authority.

## 3. Open ROS terminals

Use a separate terminal for each long-running ROS command. To reuse the existing
private `/etc/orbo` configuration and journal ownership without changing their
permissions, run the robot processes as the existing `orbo` user.

In **each new ROS terminal**, paste:

```bash
sudo -u orbo bash
```

Then, inside that shell:

```bash
cd /opt/orbo
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
export ROS_LOG_DIR=/var/lib/orbo/ros-logs
unset ROS_LOCALHOST_ONLY
```

Run the `ros2` commands below in these shells. Use `exit` to return to your normal
user. Perform installation and `sudoedit` configuration steps from a separate
**normal-user terminal**, not from the `orbo` shell. This user choice provides
device/configuration access; it does not require a background service.

## 4. Check installed executables

In a ROS terminal:

```bash
ros2 pkg executables orbo_runtime
ros2 run orbo_runtime runtime diagnostics
ros2 run orbo_runtime base_bench --help
ros2 run orbo_runtime wheel_bench --help
ros2 run orbo_runtime installed_smoke
```

Expect `base_bench`, `wheel_bench`, `teleop_keyboard`, `velocity_gate`,
`camera_monitor`, `runtime`, `audio_check`, and `installed_smoke`.

The smoke check uses a temporary journal/socket in isolated domain 94. It checks
the real runtime/ROS adapter, owner IPC and shutdown without arming or provider
calls. Diagnostics inventories dependencies; it is not a hardware-health check.

Optional synthetic application demo:

```bash
orbo_demo_dir=$(mktemp -d /tmp/orbo-demo-XXXXXX)
ros2 run orbo_runtime runtime demo --database "$orbo_demo_dir/demo.db"
```

## 5. Agent and direct-wheel checks

### Startup order: power, reset, one agent, feedback, motion

The repeatable order is:

1. Stop motion helpers and confirm disarming. Close old control/runtime launches.
2. Stop old agents on **both the Mac and Pi**. A Pi agent and a Mac agent on
   domain 42 can expose two MCU command subscribers, even after moving the USB
   cable. The helper correctly refuses to arm in that situation.
3. Keep the wheels lifted. Switch on the motor battery, connect ESP32 USB to the
   Pi, then press **RESET/RST once with motor power already on**. Do not press BOOT.
4. Start exactly one foreground agent on the Pi's CDC0 interface.
5. Check DDS endpoint counts, synchronized health and valid wheel feedback.
6. Run the direct-wheel test. Confirm stopped/disarmed feedback before starting
   the base stack, then run its short and long tests sequentially.

Motor initialization runs at ESP32 startup. If it fails, turning on motor power
later does not retry initialization in this firmware. Reset with motor power on
before diagnosing all-zero motor timestamps/counters. No reflash is needed for
these tests. This does not mean every motor fault is fixed by resetting.

### Stop competing processes before starting

On the **Mac**, Ctrl-C any running agent or robot launch, then verify:

```bash
pgrep -fl '[M]icroXRCEAgent|[m]icro_ros_agent' || true
```

No agent should remain on the Mac. The earlier local test agent was stopped when
that test finished; this check protects against starting another one afterward.

On the **Pi, in your normal-user terminal**, stop/disable any previously installed
Orbo services so they cannot restart alongside the foreground tests:

```bash
for unit in orbo-teleop orbo-navigation orbo-control orbo-runtime \
            orbo-k1-runtime orbo-camera orbo-micro-ros-agent; do
  if systemctl cat "$unit.service" >/dev/null 2>&1; then
    sudo systemctl disable --now "$unit.service"
  fi
done
pgrep -af '[m]icro_ros_agent|[M]icroXRCEAgent' || true
```

If an agent remains, stop it with Ctrl-C in its owning terminal and repeat the
check. Do not start another copy. This procedure uses foreground ROS commands;
there is no need to install or enable systemd units.

### Terminal A: the single Pi agent

Complete the battery/USB/reset step above, then open a ROS terminal using section 3.
Identify the actual application **CDC0 transport** device:

```bash
ls -l /dev/serial/by-id/
```

For the board already identified in this setup, `if00` is CDC0 and `if02` is CDC1.
Use the stable `if00` link below; update it if your listing differs. Leave this
command running throughout both direct-wheel and base-stack tests:

```bash
ORBO_CDC0=/dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_123456-if00
if pgrep -af '[m]icro_ros_agent|[M]icroXRCEAgent'; then
  echo 'An agent is already running. Stop it before starting this one.'
elif [ ! -c "$ORBO_CDC0" ]; then
  echo 'CDC0 not found. Check the by-id listing and application USB mode.'
else
  ros2 run micro_ros_agent micro_ros_agent serial --dev "$ORBO_CDC0" -b 115200
fi
```

CDC1 is for logs; the ROM bootloader device is for flashing. Only one agent may
open CDC0. `/etc/orbo/agent.env` is used by the optional service wrapper; direct
`ros2 run` selects its device with `--dev`.

### Terminal B: verify one MCU endpoint before motion

Open another ROS terminal using section 3. Run these checks **before** starting
any wheel helper or base/control launch:

```bash
ros2 topic info /orbo/embedded/control_session --verbose --no-daemon --spin-time 3
ros2 topic info /orbo/embedded/wheel_command --verbose --no-daemon --spin-time 3
ros2 topic info /orbo/embedded/wheel_state --verbose --no-daemon --spin-time 3
```

Required counts at this point:

| Topic | Publisher count | Subscription count |
|---|---:|---:|
| `/orbo/embedded/control_session` | 0 | 1 |
| `/orbo/embedded/wheel_command` | 0 | 1 |
| `/orbo/embedded/wheel_state` | 1 | Any observers |

A `ros2 run` wrapper and its agent child can appear as two OS processes; the DDS
endpoint counts above verify that only one MCU bridge is exposed. If there are
two MCU endpoints, stop the other agent on the Pi/Mac/network and repeat the
checks. Do not weaken the helper's single-subscriber check. Zero endpoints mean
the agent/device/domain/discovery needs attention. `--no-daemon` avoids relying
on a previously cached ROS graph. Counts change normally after the base stack
starts; do not apply this pre-motion table to the running base stack.

Then inspect current feedback:

```bash
ros2 topic echo /orbo/embedded/health \
  --once --qos-reliability best_effort
ros2 topic echo /orbo/embedded/wheel_state \
  --once --qos-reliability best_effort
```

Require `clock_synced: true`, `armed: false`, `valid: [true, true]`,
`hardware_error: [0, 0]`, advancing motor timestamps and a plausible voltage.
Record the starting `read_errors` counters; they must not increase during testing.
`epoch: 0` and `last_command_sequence: 0` are normal before the first arm.
`health.fault: 1` means disarmed; `fault: 6` can reflect a previous agent disconnect
and is not, by itself, a motor-voltage diagnosis.

If `hardware_error: [1, 1]` returns, stop the test: both motors report input-voltage
errors. With invalid feedback, the existing `wheel_state.voltage` can be zero or
retained rather than a fresh measurement. Do not interpret that as measured zero
volts or repeatedly attempt to arm.

### Terminal D: observe voltage and errors during both tests

Open another ROS terminal using section 3 and leave this observer running:

```bash
ORBO_TEST_DIR=$(mktemp -d /tmp/orbo-lifted-tests-XXXXXX)
echo "Telemetry saved to $ORBO_TEST_DIR/wheel-state.yaml"
ros2 topic echo /orbo/embedded/wheel_state \
  --qos-reliability best_effort --flow-style | tee "$ORBO_TEST_DIR/wheel-state.yaml"
```

Watch `voltage`, `valid`, `hardware_error`, `read_errors`, `armed` and measured
`velocity_rad_s`. This observer sends no motion commands. Keep the printed file
path for inspection afterward. The maintained helpers perform the admission,
freshness and stop checks; an observer alone is not a motion safety controller.

### Terminal B: the short fast direct-wheel test

With all preflight checks passing and wheels lifted:

```bash
ros2 run orbo_runtime wheel_bench drive --left 1.2 --right 1.2 --seconds 3
```

This uses the existing 1.2 rad/s bench bound. Expect a completed drive and
`Stopped and disarmed (encoder feedback).` Confirm valid feedback, zero measured
speed, no hardware errors and unchanged read-error counters before proceeding.
If the helper exits with a failure or stop is unconfirmed, do not continue to
the next test. Do not run this direct-wheel owner alongside the base controller,
application runtime, or legacy `Software/Teleoperate` program.

## 6. Complete lifted-wheel base stack

Keep **the same agent in terminal A** and the observer in terminal D running.
The direct-wheel helper must have exited and disarmed. Do not start another agent.
No systemd control, teleop, navigation, or runtime services may be active.

**Terminal B:** start the bench stack, leaving it running:

```bash
ros2 launch orbo_bringup bench.launch.py
```

This resolves the installed CAD bench profile and starts the controllers and one
velocity gate. It does not arm the MCU. The profile is for lifted wheels only;
its nominal dimensions do not establish ground calibration.

**Terminal C:** open another ROS terminal using section 3, then run the short test:

```bash
ros2 run orbo_runtime base_bench drive --linear 0.02 --seconds 3
```

Only after it succeeds, confirms stopped/disarmed feedback, and adds no errors:

```bash
ros2 run orbo_runtime base_bench drive --linear 0.02 --seconds 30
```

The helper activates the required hardware/controllers and reports measured
odometry. `0.02` is metres per second at the base command, approximately
0.606 rad/s per wheel using this CAD profile. It is distinct from the direct
helper's wheel-speed arguments in rad/s.

The packaged helper preserves controller activation, fresh arm epochs, expiring
leases, velocity bounds, competing-publisher rejection, measured feedback and
odometry checks, and stop/disarm acknowledgment. It publishes through the gate
and ros2_control. It does not replace these checks with a raw velocity publisher.

### Shutdown and reference results

After the final helper exits, verify in terminal C:

```bash
ros2 topic echo /orbo/embedded/wheel_state \
  --once --qos-reliability best_effort
ros2 topic echo /orbo/embedded/health \
  --once --qos-reliability best_effort
```

Confirm `armed: false`, valid feedback, near-zero wheel velocities, no hardware
errors and unchanged read-error counters. Then Ctrl-C the bench launch in B,
the observer in D, and **finally the agent in A**. Do not reset/unplug the MCU or
stop the agent as a substitute for an orderly stop while a motion helper runs.
For unexpected motion use the power cutoff. An independent software stop is:

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session \
  orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 1, operation: 0}'
```

Check acknowledgment/health afterward; publishing alone does not prove physical
cessation.

The same lifted-wheel sequence passed locally through Pixi on 14 November 2026:

| Test | Lowest left / right voltage | New read / hardware errors |
|---|---|---|
| Direct wheels, 1.2 rad/s, 3 s | 11.8 / 11.7 V | 0 / 0 |
| Base stack, 0.02 m/s, 3 s | 11.8 / 11.9 V | 0 / 0 |
| Base stack, 0.02 m/s, 30 s | 11.7 / 11.6 V | 0 / 0 |

All samples were valid and all tests confirmed stopping/disarming. The long run's
encoder-derived travel and odometry agreed at approximately 0.591 m. These are
reference measurements from the Mac bench run, not voltage acceptance limits or
a claim that the Pi repeat has passed. No firmware change was used. On the Mac,
a temporary loopback-only DDS profile isolated the USB agent from a second MCU
endpoint on the LAN. On the Pi, stop other agents and verify the endpoint counts
above; do not assume moving the USB cable removes the other agent's DDS entities.

## 7. Camera

### Build or reuse the camera overlay

If `/opt/orbo-camera/install/local_setup.bash` already exists and was built with
the working camera setup script, reuse it; this refactor does not require a
camera rebuild. Otherwise, stop all capture programs and run as your normal user:

```bash
cd ~/little-orbo
sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" /opt/orbo-camera
(
  umask 022
  bash deploy/ros2-camera/setup-camera.sh \
    --build-only --workspace /opt/orbo-camera
)
```

Matching headers and pkg-config metadata must come from the working libcamera
installation. See the [camera setup guide](deploy/ros2-camera/README.md) if the
script reports missing or mismatched development files.

### Initial camera selection

In a ROS terminal, add the camera overlay and verify its prefix:

```bash
source /opt/orbo-camera/install/local_setup.bash
ros2 pkg prefix camera_ros
ros2 run camera_ros camera_node --ros-args -p camera:=0
```

Expect the prefix `/opt/orbo-camera/install/camera_ros`. Record the exact camera
identifier printed by the driver, then Ctrl-C before starting another publisher.

In a **normal-user terminal**:

```bash
sudoedit /etc/orbo/camera.yaml
```

Edit existing fields (do not erase measured intrinsics/mount on an upgrade):

```yaml
enabled: true
driver: libcamera
camera: "ACTUAL_ENUMERATED_OV5647_IDENTIFIER"
width: 640
height: 480
frame_id: camera_optical_frame
calibration_file: ""
```

### Launch and observe

In the ROS terminal with the camera overlay sourced:

```bash
ros2 launch orbo_bringup camera.launch.py config:=/etc/orbo/camera.yaml
```

In a second ROS terminal:

```bash
ros2 topic hz /orbo/camera/image_raw
```

Ctrl-C the observer, then:

```bash
ros2 topic echo /orbo/camera/diagnostics --once
```

An intrinsics warning is expected before optical calibration. Ctrl-C the launch
when done. `/etc/orbo/camera.env` is only needed for the optional systemd camera
service; foreground launches use the sourced overlay.

## 8. Microphone and speaker

In a ROS terminal:

```bash
ros2 run orbo_runtime audio_check list
```

Set the actual input/output names or indices and check format support:

```bash
ORBO_AUDIO_IN='ACTUAL_INPUT_NAME_OR_INDEX'
ORBO_AUDIO_OUT='ACTUAL_OUTPUT_NAME_OR_INDEX'
ros2 run orbo_runtime audio_check check \
  --input "$ORBO_AUDIO_IN" --output "$ORBO_AUDIO_OUT"
```

Start at low speaker volume:

```bash
ros2 run orbo_runtime audio_check tone --output "$ORBO_AUDIO_OUT" --seconds 2
```

Speak during the microphone check:

```bash
ros2 run orbo_runtime audio_check record --input "$ORBO_AUDIO_IN" --seconds 3
```

The recording check reports levels/clipping and discards the audio. These checks
need no cloud credentials and do not save or upload audio.

## 9. Full application teleop

This adds the application permission broker and bounded owner sessions. It
requires measured wheel calibration and a qualified input mapping. Complete the
lifted checks first. Do not relabel the CAD profile as measured calibration.

### Configuration

Stop the bench helper and `bench.launch.py`; only the agent may remain running.
In a **normal-user terminal**, preserve existing files and create a template only
if calibration is missing:

```bash
sudo test -e /etc/orbo/calibration.yaml || \
  sudo install -o orbo -g orbo -m 0600 \
    /opt/orbo/ros_ws/src/orbo_description/config/calibration.template.yaml \
    /etc/orbo/calibration.yaml
sudoedit /etc/orbo/calibration.yaml
sudoedit /etc/orbo/runtime.toml
sudo install -d -o orbo -g orbo -m 0700 /run/orbo
```

`/run/orbo` is temporary: recreate it after reboot for foreground runtime use.
The optional systemd runtime creates it automatically. Keep the existing journal
path and private ownership; no database migration/deletion is needed here.

Supply measured geometry, firmware-compatible speed limits and qualification
records. For keyboard teleop, edit the existing TOML sections without duplicating
section headers or removing other fields:

```toml
[robot]
# Retain id, calibration, and flipper fields.
teleop_enabled = true
navigation_enabled = false

[robot.teleop]
qualified = true # Only after physical mapping/stop checks are recorded.
linear_axis = 0
angular_axis = 1
deadman_button = 0
linear_scale = 0.25
angular_scale = 0.25
evidence_record = "YOUR_MEASURED_KEYBOARD_TELEOP_QUALIFICATION_RECORD"
```

Keep voice and cloud reasoning disabled for this first application test. For
Xbox input, use the [joystick mapping](ros_ws/README.md#remote-xbox-joystick-control)
and run only that input publisher instead of the keyboard.

### Launch the application components

Use section 3 for **each ROS terminal**. Keep the agent from section 5 running.

**Control terminal:**

```bash
ros2 launch orbo_bringup control.launch.py calibration:=/etc/orbo/calibration.yaml
```

**Runtime terminal:**

```bash
ros2 launch orbo_bringup runtime.launch.py config:=/etc/orbo/runtime.toml
```

**Teleop gate terminal:**

```bash
ros2 launch orbo_bringup teleop.launch.py \
  calibration:=/etc/orbo/calibration.yaml input:=keyboard
```

Camera can run concurrently in its own terminal as in section 7. These launches
are foreground processes; errors and logs appear in their terminals.

**Owner terminal:** wait for hardware/controllers, then activate them:

```bash
ros2 control list_hardware_components -c /orbo/controller_manager
ros2 control list_controllers -c /orbo/controller_manager
ros2 control set_hardware_component_state OrboWheels active -c /orbo/controller_manager
ros2 control set_controller_state drive_controller active -c /orbo/controller_manager
ros2 control list_controllers -c /orbo/controller_manager
```

Confirm `joint_state_broadcaster` and `drive_controller` are active. Activation
alone does not arm.

**Keyboard terminal** (interactive TTY):

```bash
ros2 run orbo_runtime teleop_keyboard
```

### Authorize a session

In the owner terminal:

```bash
orbo_grant_expires=$(date -u -d '+5 minutes' +%s)
ros2 run orbo_runtime runtime request permission.set \
  --args "{\"scope\":\"robot.command\",\"destination\":\"orbo\",\"enabled\":true,\"expires\":$orbo_grant_expires}"
ros2 run orbo_runtime runtime request episode.start \
  --args '{"profile":"physical_task.v1"}'
```

Copy the returned episode ID. With input neutral, request a session, then use
W/S/A/D in the keyboard terminal:

```bash
ros2 run orbo_runtime runtime request robot.teleop \
  --args '{"episode_id":"YOUR_EPISODE_ID","key":"pi-test-001","duration":20}'
```

Use a new key each time. Space commands neutral; Q exits keyboard input. Sessions
last at most 30 seconds. From another owner terminal, stop early with:

```bash
ros2 run orbo_runtime runtime request stop
```

After disarming, close the episode with the actual outcome (`completed`,
`cancelled`, `failed`, or `unknown`):

```bash
ros2 run orbo_runtime runtime request episode.end \
  --args '{"episode_id":"YOUR_EPISODE_ID","outcome":"completed"}'
```

On shutdown, stop the session first, exit input, and confirm disarming. Then
Ctrl-C the gate, runtime, control and camera launches, and finally the agent.
A runtime restart requires new motion grants; it never restores old authority.

## 10. Software tests

These are optional software-only checks, separate from physical acceptance.
Stop physical tests to avoid CPU contention. In a fresh **normal-user terminal**:

```bash
cd ~/little-orbo
python3.12 -m venv --system-site-packages ~/orbo-test-venv
/opt/orbo/.venv/bin/uv export --frozen --all-extras --group dev \
  --no-emit-project -o /tmp/orbo-test-requirements.txt
/opt/orbo/.venv/bin/uv pip install --python ~/orbo-test-venv/bin/python \
  --require-hashes -r /tmp/orbo-test-requirements.txt
/opt/orbo/.venv/bin/uv pip install --python ~/orbo-test-venv/bin/python --no-deps .
source ~/orbo-test-venv/bin/activate
python -m pytest -q tests --ignore=tests/ros --ignore=tests/k1
```

After the camera overlay is built, source the workspace and run synthetic ROS
checks in domain 96 (never domain 42):

```bash
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
source /opt/orbo-camera/install/local_setup.bash
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY
ROS_DOMAIN_ID=96 ORBO_ROS_TESTS=1 python -m pytest -q tests/ros
bash firmware/orbo/host_tests/run.sh
deactivate
```

This includes installed executable discovery, launches from outside the source
checkout, synthetic drive/stop/fault checks, keyboard expiry and camera stream
health. No test opens the physical USB device. Use a fresh ROS terminal before
returning to hardware tests.

## 11. Optional systemd deployment

Once foreground operation is qualified, systemd can supervise the same ROS
commands for unattended use. It is not needed for any foreground test above.
Do not run service and foreground copies together.

Stop all robot processes, then install/update the optional units from a fresh
normal-user terminal:

```bash
cd ~/little-orbo
sudo ./deploy/install-pi.sh --camera-stack existing --install-systemd
```

Configure `/etc/orbo/agent.env` with the actual CDC0 path, root-owned mode 0600.
For the separate camera overlay, preserve or create `/etc/orbo/camera.env`:

```text
ORBO_CAMERA_OVERLAY=/opt/orbo-camera/install
```

Start only commissioned components. For example, to supervise just the agent:

```bash
sudo systemctl start orbo-micro-ros-agent
sudo journalctl -u orbo-micro-ros-agent -f
```

Ctrl-C exits the log viewer, not the service. Stop it before foreground agent use:

```bash
sudo systemctl stop orbo-micro-ros-agent
```

Installation never enables boot startup. Service status describes process
supervision, not ROS readiness, controller activation, or physical qualification.
See the [operator runbook](docs/engineering/runbooks/operator.md) for application
services, credentials and recovery.

## 12. Remaining commissioning

Cloud voice needs tested audio devices, provider credentials, an owner-selected
Fish voice, explicit provider/capture/playback grants, and funded provider
accounts. See [voice setup](docs/engineering/runbooks/operator.md#4-credentials-and-voice).
Systemd credential loading applies only to service operation; a foreground
runtime needs credentials supplied through its documented environment mechanism.
Never put keys in source, command arguments or shell history.

Nav2 needs a real scan source, transforms, measured limits, surveyed map and
localization. Once qualified, its foreground entry point is
`ros2 launch orbo_navigation navigation.launch.py` with explicit `calibration`
and `map` arguments. It replaces the teleop gate and leaves lifecycle startup
explicit. Flippers remain disabled until their pins, pulse limits and mechanics
are independently qualified.

Further reference: [runtime package](ros_ws/src/orbo_runtime/README.md),
[ROS workspace](ros_ws/README.md), [firmware](firmware/orbo/README.md), and
[Pi peripherals](docs/engineering/runbooks/pi-peripherals.md).
