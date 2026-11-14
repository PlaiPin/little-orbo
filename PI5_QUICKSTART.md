# Raspberry Pi 5: install and test Orbo

These commands target **Ubuntu 24.04 ARM64 with ROS 2 Jazzy already installed**
and an OV5647 camera already working with `rpicam-hello`. They use native ROS,
colcon, and a Python virtual environment. **Do not use Pixi on the Pi.**

Run commands in Bash on the Pi as your normal user, using `sudo` where shown.
Copy one block at a time and check its result before continuing. Replace values
marked `YOUR_...` or `ACTUAL_...` with your device/configuration values.
Initial motor tests require the current wheel-enabled ESP32 firmware, motor power,
and the wheels lifted clear of the surface. Keep the physical power cutoff within
reach. Only one motion-control path may run at a time.

## Contents

1. [Install or update](#1-install-or-update)
2. [Prepare each ROS terminal](#2-prepare-each-ros-terminal)
3. [Check the runtime without hardware](#3-check-the-runtime-without-hardware)
4. [Test the ESP32 and wheels independently](#4-test-the-esp32-and-wheels-independently)
5. [Test the complete lifted-wheel base stack](#5-test-the-complete-lifted-wheel-base-stack)
6. [Build and test the camera](#6-build-and-test-the-camera)
7. [Test microphone and speaker](#7-test-microphone-and-speaker)
8. [Run the full application with calibrated teleop](#8-run-the-full-application-with-calibrated-teleop)
9. [Logs and shutdown](#9-logs-and-shutdown)
10. [Run software tests on the Pi](#10-run-software-tests-on-the-pi)
11. [Voice and remaining commissioning](#11-voice-and-remaining-commissioning)

## 1. Install or update

For a **first checkout**:

```bash
git clone https://github.com/PlaiPin/little-orbo.git ~/little-orbo
cd ~/little-orbo
```

For an **existing checkout**, use this instead:

```bash
cd ~/little-orbo
git pull --ff-only
```

If Orbo is already installed and running, stop any motion helper first and wait
for stopped/disarmed feedback. Then stop services before reinstalling:

```bash
sudo systemctl stop orbo-teleop orbo-navigation orbo-control \
  orbo-runtime orbo-k1-runtime orbo-camera orbo-micro-ros-agent
```

Install from the checkout:

```bash
cd ~/little-orbo
sudo ./deploy/install-pi.sh --camera-stack existing
```

The installer copies source to `/opt/orbo`, builds the Orbo ROS packages and
micro-ROS agent, installs the runtime in `/opt/orbo/.venv`, and installs disabled
systemd services. It does not flash the ESP32 or start services. Existing runtime
and camera configurations are preserved; review them when upgrading.

`--camera-stack existing` preserves the working camera packages and refuses an APT
plan that would change them. Build the compatible ROS camera overlay in section 6.
If APT reports a camera dependency conflict, resolve it before continuing rather
than switching camera stacks to bypass the check.

## 2. Prepare each ROS terminal

Paste this into **every new Pi terminal that runs ROS commands**, including both
terminals used for the base-control test:

```bash
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY
```

Hardware services use domain 42. Software tests below use separate domains and
do not need the battery or USB device.

## 3. Check the runtime without hardware

In a terminal prepared with section 2:

```bash
/opt/orbo/.venv/bin/orbo diagnostics

orbo_demo_dir=$(mktemp -d /tmp/orbo-demo-XXXXXX)
/opt/orbo/.venv/bin/orbo demo --database "$orbo_demo_dir/demo.db"

sudo -u orbo bash -c '
  source /opt/ros/jazzy/setup.bash
  source /opt/orbo/ros_ws/install/local_setup.bash
  exec /opt/orbo/.venv/bin/python /opt/orbo/tools/smoke-installed.py
'
```

`diagnostics` inventories the environment; it does not verify hardware health.
The demo is synthetic. The smoke helper checks the real runtime/ROS adapter,
private socket, episode journal, and graceful shutdown in isolated domain 94.
Neither test arms motors or calls cloud providers. Expect the smoke helper to
report a successful shutdown with exit status 0.

## 4. Test the ESP32 and wheels independently

### Select the application USB transport

```bash
ls -l /dev/serial/by-id/
sudoedit /etc/orbo/agent.env
```

Put the actual **application CDC0 transport** path in that file:

```text
ORBO_SERIAL_DEVICE=/dev/serial/by-id/YOUR_ACTUAL_CDC0_DEVICE
```

CDC1 is the logging interface; the ROM bootloader device is for flashing. Do not
select either for the agent. Only one process may open the transport device.

```bash
sudo chown root:root /etc/orbo/agent.env
sudo chmod 0600 /etc/orbo/agent.env
sudo systemctl stop orbo-runtime orbo-control orbo-teleop orbo-navigation
sudo systemctl restart orbo-micro-ros-agent
sudo systemctl status orbo-micro-ros-agent --no-pager
```

### Verify health and feedback

With section 2 sourced:

```bash
ros2 topic echo /orbo/embedded/health \
  --once --qos-reliability best_effort

ros2 topic echo /orbo/embedded/wheel_state \
  --once --qos-reliability best_effort
```

Check fresh synchronized health, `armed: false`, and valid wheel feedback before
requesting motion. If no messages arrive, inspect the agent logs in section 9.

### Short direct-wheel test

With the wheels lifted and motor power on:

```bash
/opt/orbo/.venv/bin/python \
  /opt/orbo/firmware/orbo/bench_tests/control_wheels.py \
  drive --left 0.6 --right 0.6 --seconds 3
```

Expect stopped/disarmed encoder feedback. After the short test passes, optionally
run interactive control:

```bash
/opt/orbo/.venv/bin/python \
  /opt/orbo/firmware/orbo/bench_tests/control_wheels.py \
  keyboard --speed 0.6
```

W/S drive forward/reverse, A/D turn, Space/X command zero, and Q exits. Wait for
stopped/disarmed encoder feedback before switching to the next control path.
Do not run the legacy `Software/Teleoperate` program alongside this firmware.

## 5. Test the complete lifted-wheel base stack

This tests `velocity input → gate → ros2_control → micro-ROS → ESP32 → encoder
odometry`. Exit the direct-wheel helper first. Keep the agent running and the
application runtime, teleop, navigation, and control services stopped.

**Terminal A:** source section 2, then leave this launch running:

```bash
/opt/orbo/deploy/run-ros.sh launch orbo_bringup control.launch.py \
  calibration:=/opt/orbo/ros_ws/src/orbo_description/config/bench-cad.yaml
```

**Terminal B:** source section 2, then run the short test:

```bash
/opt/orbo/.venv/bin/python /opt/orbo/tools/control-base.py \
  drive --linear 0.02 --seconds 3
```

Only after it passes, run the longer test:

```bash
/opt/orbo/.venv/bin/python /opt/orbo/tools/control-base.py \
  drive --linear 0.02 --seconds 30
```

Optional interactive test, after the drive finishes:

```bash
/opt/orbo/.venv/bin/python /opt/orbo/tools/control-base.py \
  keyboard --speed 0.6
```

The helper activates controllers, requests a fresh arm, and reports measured
encoder odometry and read errors. `bench-cad.yaml` contains nominal CAD dimensions
for lifted testing; it does not qualify floor driving.

Stop the helper and verify disarming before Ctrl-C in terminal A. Do not start
the application runtime or another velocity gate while this bench path runs.

An independent software stop request from a sourced domain-42 terminal is:

```bash
ros2 topic pub --once --max-wait-time-secs 10 \
  /orbo/embedded/control_session \
  orbo_embedded_interfaces/msg/ControlSession \
  '{interface_version: 1, operation: 0}'
```

Verify disarmed feedback; a publisher message alone is not an acknowledgment.
Use the physical power cutoff for unexpected motion.

## 6. Build and test the camera

### Build against the working libcamera installation

Stop `rpicam-hello` and other capture programs. If the Orbo camera service is
running, stop it too:

```bash
sudo systemctl stop orbo-camera
cd ~/little-orbo

sudo install -d -m 0755 \
  -o "$USER" -g "$(id -gn)" /opt/orbo-camera

(
  umask 022
  bash deploy/ros2-camera/setup-camera.sh \
    --build-only --workspace /opt/orbo-camera
)
```

The build script checks OV5647 enumeration and library linkage. If matching
headers/pkg-config metadata are missing, obtain them from the same source as the
working libcamera installation. Do not replace it with an arbitrary libcamera
package. See the [camera setup guide](deploy/ros2-camera/README.md) for the
`--libcamera-pc-dir` option and compatibility checks.

### Test the standalone publisher

**Camera terminal A**, from `~/little-orbo`:

```bash
ROS_DOMAIN_ID=42 bash deploy/ros2-camera/setup-camera.sh \
  --start-only --workspace /opt/orbo-camera \
  --width 640 --height 480
```

**Camera terminal B**, after sourcing section 2:

```bash
ros2 topic echo /camera/camera_info --once
ros2 topic hz /camera/image_raw
```

Ctrl-C the rate observer, then Ctrl-C the publisher in terminal A. Record the
exact camera identifier printed by the driver for the service configuration.

### Configure the Orbo camera service

```bash
printf '%s\n' \
  'ORBO_CAMERA_OVERLAY=/opt/orbo-camera/install' \
  | sudo tee /etc/orbo/camera.env >/dev/null

sudo chmod 0644 /etc/orbo/camera.env

sudo -u orbo env \
  ROS_DOMAIN_ID=42 \
  ORBO_CAMERA_OVERLAY=/opt/orbo-camera/install \
  /opt/orbo/deploy/run-ros.sh pkg prefix camera_ros

sudoedit /etc/orbo/camera.yaml
```

The prefix command must print `/opt/orbo-camera/install/camera_ros`. In the YAML,
set these existing fields using the exact enumerated identifier:

```yaml
enabled: true
driver: libcamera
camera: "ACTUAL_ENUMERATED_OV5647_IDENTIFIER"
width: 640
height: 480
frame_id: camera_optical_frame
calibration_file: ""
```

Do not erase measured calibration or mount values on an existing installation.
On first setup, leave intrinsics and mount unset until measured.

```bash
sudo systemctl start orbo-camera
sudo journalctl -u orbo-camera -n 60 --no-pager
```

With section 2 sourced:

```bash
ros2 topic hz /orbo/camera/image_raw
```

Ctrl-C the observer, then:

```bash
ros2 topic echo /orbo/camera/diagnostics --once
```

Frames with an intrinsics warning are expected before optical calibration.
Standalone topics are `/camera/*`; service topics are `/orbo/camera/*`.

## 7. Test microphone and speaker

Enumerate devices as the service user:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo-audio-check list
```

Replace the following with actual device names or indices:

```bash
ORBO_AUDIO_IN='ACTUAL_INPUT_NAME_OR_INDEX'
ORBO_AUDIO_OUT='ACTUAL_OUTPUT_NAME_OR_INDEX'

sudo -u orbo /opt/orbo/.venv/bin/orbo-audio-check check \
  --input "$ORBO_AUDIO_IN" --output "$ORBO_AUDIO_OUT"
```

Start with low speaker volume, then play the short tone:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo-audio-check tone \
  --output "$ORBO_AUDIO_OUT" --seconds 2
```

Speak during the microphone test:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo-audio-check record \
  --input "$ORBO_AUDIO_IN" --seconds 3
```

`check` verifies sample-format support. `record` reports levels and clipping,
then discards the audio. These local checks need no cloud credentials.

## 8. Run the full application with calibrated teleop

The application path adds the owner runtime, permission grants, and bounded
teleop sessions. It requires measured wheel calibration and a qualified input
mapping. Complete the lifted checks first; do not relabel the CAD bench profile
as measured calibration. Camera and audio can be tested before this step.

### Prepare calibration and runtime configuration

Exit all manual wheel/base helpers and the bench launch from section 5. Stop any
existing application motion session, then stop services before changing config:

```bash
sudo systemctl stop orbo-teleop orbo-navigation orbo-control orbo-runtime

sudo test -e /etc/orbo/calibration.yaml || \
  sudo install -o orbo -g orbo -m 0600 \
    /opt/orbo/ros_ws/src/orbo_description/config/calibration.template.yaml \
    /etc/orbo/calibration.yaml

sudoedit /etc/orbo/calibration.yaml
sudoedit /etc/orbo/runtime.toml
```

Fill the calibration template with measured geometry, limits, and evidence.
Keep limits within the configured firmware bounds. Set qualification flags only
after performing and recording the corresponding physical checks.

In `runtime.toml`, edit the existing sections rather than appending duplicates:

```toml
[robot]
# Retain the existing id, calibration, and flipper fields.
teleop_enabled = true
navigation_enabled = false

[robot.teleop]
qualified = true # Only after the physical mapping/stop checks are recorded.
linear_axis = 0
angular_axis = 1
deadman_button = 0
linear_scale = 0.25
angular_scale = 0.25
evidence_record = "YOUR_MEASURED_KEYBOARD_TELEOP_QUALIFICATION_RECORD"
```

This mapping is for the application keyboard publisher. Keep `[near].enabled`
and `[voice].enabled` false for the first application test. For Xbox input,
use the [separate joystick walkthrough](ros_ws/README.md#remote-xbox-joystick-control)
and its mapping; do not run both keyboard and joystick publishers.

### Start services and activate controllers

```bash
sudo systemctl start \
  orbo-micro-ros-agent orbo-control orbo-runtime orbo-teleop

# Once section 6 camera setup has passed:
sudo systemctl start orbo-camera

sudo systemctl status orbo-micro-ros-agent orbo-control \
  orbo-runtime orbo-teleop orbo-camera --no-pager
```

With section 2 sourced, wait for the hardware/controllers to appear:

```bash
ros2 control list_hardware_components -c /orbo/controller_manager
ros2 control list_controllers -c /orbo/controller_manager
```

Then activate:

```bash
ros2 control set_hardware_component_state OrboWheels active \
  -c /orbo/controller_manager

ros2 control set_controller_state drive_controller active \
  -c /orbo/controller_manager

ros2 control list_controllers -c /orbo/controller_manager
```

Confirm `joint_state_broadcaster` and `drive_controller` are active. Controller
activation alone does not arm the MCU.

### Start keyboard input and authorize a bounded session

**Keyboard terminal:** source section 2, then leave this running in an interactive
terminal (use SSH with a TTY):

```bash
/opt/orbo/.venv/bin/orbo-teleop-keyboard
```

**Owner terminal:** grant five minutes of permission and create an episode:

```bash
orbo_grant_expires=$(date -u -d '+5 minutes' +%s)

sudo -u orbo /opt/orbo/.venv/bin/orbo request permission.set \
  --args "{\"scope\":\"robot.command\",\"destination\":\"orbo\",\"enabled\":true,\"expires\":$orbo_grant_expires}"

sudo -u orbo /opt/orbo/.venv/bin/orbo request episode.start \
  --args '{"profile":"physical_task.v1"}'
```

Copy the returned `episode_id` into this command. With keyboard input neutral,
request a session, then use W/S/A/D in the keyboard terminal:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request robot.teleop \
  --args '{"episode_id":"YOUR_EPISODE_ID","key":"pi-test-001","duration":20}'
```

Use a new request key for each session. Space commands neutral; Q exits the
keyboard publisher. Sessions last at most 30 seconds. The owner request remains
active while the session runs, so use another owner terminal for an early stop:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request stop
```

After the session ends and disarming is confirmed, close the episode with its
actual outcome (`completed`, `cancelled`, `failed`, or `unknown`):

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request episode.end \
  --args '{"episode_id":"YOUR_EPISODE_ID","outcome":"completed"}'
```

Restarting the runtime revokes motion grants; explicitly reauthorize afterward.

## 9. Logs and shutdown

Follow service logs; Ctrl-C stops only the log viewer:

```bash
sudo journalctl -f \
  -u orbo-micro-ros-agent -u orbo-control \
  -u orbo-runtime -u orbo-teleop -u orbo-camera
```

For a bounded log snapshot:

```bash
sudo journalctl -n 100 --no-pager \
  -u orbo-micro-ros-agent -u orbo-control \
  -u orbo-runtime -u orbo-teleop -u orbo-camera
```

If the application runtime is running, request a stop first:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request stop
```

Exit manual helpers/publishers and verify stopped/disarmed feedback before
stopping the stack. Manual launches in other terminals must be stopped there;
systemctl only stops its managed processes.

```bash
sudo systemctl stop orbo-teleop orbo-navigation orbo-control \
  orbo-runtime orbo-k1-runtime orbo-camera orbo-micro-ros-agent
```

Do not enable boot startup until the selected configuration has been qualified.

## 10. Run software tests on the Pi

Use a separate test virtual environment so development dependencies do not
replace the production environment. From a normal Pi terminal:

```bash
cd ~/little-orbo
python3.12 -m venv --system-site-packages ~/orbo-test-venv

/opt/orbo/.venv/bin/uv export --frozen --all-extras --group dev \
  --no-emit-project -o /tmp/orbo-test-requirements.txt

/opt/orbo/.venv/bin/uv pip install --python ~/orbo-test-venv/bin/python \
  --require-hashes -r /tmp/orbo-test-requirements.txt

/opt/orbo/.venv/bin/uv pip install \
  --python ~/orbo-test-venv/bin/python --no-deps .

source ~/orbo-test-venv/bin/activate
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY

python -m pytest -q tests --ignore=tests/ros --ignore=tests/k1
```

After the camera overlay has been built, run the synthetic ROS suite:

```bash
source /opt/orbo-camera/install/local_setup.bash
ROS_DOMAIN_ID=96 ORBO_ROS_TESTS=1 python -m pytest -q tests/ros
```

The synthetic ROS suite uses domain 96 and may take several minutes. Stop
physical testing while it runs to avoid competing CPU load. It does not open USB
or command the real robot. Firmware host checks use stubbed hardware:

```bash
bash firmware/orbo/host_tests/run.sh
deactivate
```

Open a fresh terminal and source section 2 before returning to hardware tests.
Passing software tests does not establish physical motor, camera, audio, or
navigation acceptance.

## 11. Voice and remaining commissioning

For full cloud voice, first complete local audio checks, then follow the
[credential and voice setup](docs/engineering/runbooks/operator.md#4-credentials-and-voice)
and [permission scopes](docs/engineering/runbooks/operator.md#5-start-and-explicitly-authorize).
Configure the actual audio devices and an owner-selected Fish voice, enable
reasoning and voice, and supply the separate provider credentials. Provider
calls may consume account credit. Keys belong in the documented credential
store, not this file or shell commands.

The [voice measurement procedure](docs/engineering/runbooks/operator.md#7-measure-actual-pi-voice-performance)
checks latency under actual Pi ROS/audio load after those prerequisites are met.

Nav2 additionally needs a real scan source, measured transforms, map,
localization, and qualified navigation limits. Flippers remain disabled until
their pins, pulse limits, and mechanics are independently commissioned. Neither
is enabled by completing the lifted-base, camera, or audio tests above.

Further reference: [operator runbook](docs/engineering/runbooks/operator.md),
[Pi peripherals](docs/engineering/runbooks/pi-peripherals.md),
[firmware instructions](firmware/orbo/README.md), and
[ROS workspace guide](ros_ws/README.md).
