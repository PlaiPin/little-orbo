# Orbo ROS workspace

| Package | Purpose |
|---|---|
| `orbo_embedded_interfaces` | Host/firmware message definitions |
| `orbo_hardware` | ros2_control plugin using measured ESP32 wheel feedback |
| `orbo_description` | Complete v9 visuals, wheel joints, calibration templates, RViz views |
| `orbo_runtime` | Installed Python runtime, peripheral nodes and bounded bench helpers |
| `orbo_bringup` | Bench, control, runtime, camera and application teleop launch files |
| `orbo_navigation` | Calibration-gated Nav2 configuration and velocity gate |

All these package sources are versioned. Build/install/log directories are local.
For the combined model/camera/odometry/TF viewer, run
`ros2 launch orbo_description rviz.launch.py` from a sourced graphical ROS desktop,
or `pixi run --locked rviz-pi` on the Mac. See the
[Pi viewer guide](../docs/engineering/runbooks/ros-macos.md#combined-pi-viewer-model-camera-odometry-and-frames).
Meshes from `../cad/meshes/v9` are installed as `orbo_description/meshes/v9`; obsolete `meshes/v05` exports are ignored and excluded
from installation even if they remain on a developer's disk.

## Native Pi workflow

See [PI5_QUICKSTART.md](../PI5_QUICKSTART.md) for first installation and the
in-place upgrade from the original installer. No configuration or database needs
to be deleted. Stop/disable old services before foreground operation.

The Pi installer builds the ament Python package with the locked dependency
interpreter; afterward source Jazzy and `ros_ws/install/local_setup.bash` and use
`ros2 run orbo_runtime ...` or `ros2 launch orbo_bringup ...`. A native manual build
of the same installed tree, with services stopped and dependencies already
installed, is:

```bash
sudo bash -c '
  source /opt/ros/jazzy/setup.bash
  cd /opt/orbo/ros_ws
  export CMAKE_BUILD_PARALLEL_LEVEL=2 MAKEFLAGS=-j2
  exec /opt/orbo/.venv/bin/python -m colcon build --merge-install --executor sequential \
    --base-paths src ../.sdk/agent-locked/micro_ros_agent/micro_ros_agent ../.sdk/agent-locked/micro_ros_msgs \
    --cmake-args -DCMAKE_BUILD_TYPE=Release -DUAGENT_USE_SYSTEM_LOGGER=OFF -DBUILD_TESTING=OFF
'
```

Prefer rerunning the installer for upgrades: it also refreshes dependencies and
source. Systemd units are installed only with `--install-systemd` and supervise
the same ROS launches. They do not replace ROS lifecycle or readiness checks.

## Mac setup and software checks

From the repository root, outside an ESP-IDF or uv environment:

```bash
pixi install --locked
pixi run --locked ros-build-workspace
pixi run --locked ros-test-integrations
pixi run --locked ros-test-control
```

Pixi supplies Jazzy, ros2_control, Nav2, the `joy` driver, the installed editable `orbo-runtime`
package and the async test plugin. The workspace build includes navigation;
`ros-build-control` remains available for the narrower base-control build.
The installed runtime provides Python imports and command-line entry points
without a `PYTHONPATH` workaround.

`ros-test-integrations` checks installed launch configuration, camera health,
keyboard expiry, application teleop, the Nav2 action adapter and runtime packaging.
`ros-test-control` exercises measured odometry and control-session loss, including
a 30-second synthetic drive. Both use domain 96 and require no battery or agent.
Neither establishes physical navigation or peripheral acceptance.
`pixi run --locked ros-test-control-repeat` runs five fresh 30-second stacks in
domain 97. The final repeat batch passed all five after the callback/dispatch
fixes, UDP transport configuration, and removal of a leftover Docker qualification
workload. Watchdog limits remain unchanged; battery-powered verification is still
pending. See the [control runbook](../docs/engineering/runbooks/ros-control-macos.md#work-without-the-battery)
for results and limitations.

Pixi runs natively on the Mac and does not require Docker. Its activation selects
UDPv4 and `SUBNET` discovery; restart existing Pixi shells/ROS processes after
updating. Jazzy `LOCALHOST` discovery re-enables shared memory and bypasses that
UDP-only choice. Test cleanup now also kills orphaned controller children when a
launcher has already exited.

For manual ROS inspection:

```bash
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
ros2 launch orbo_bringup control.launch.py --show-args
ros2 launch orbo_bringup camera.launch.py --show-args
ros2 launch orbo_navigation navigation.launch.py --show-args
```

Use the [Mac control runbook](../docs/engineering/runbooks/ros-control-macos.md)
for physical lifted-wheel operation. Installing Nav2 does not enable navigation.

## Transforms and launch ownership

All current launches use global `/tf` and `/tf_static`. Frames are unprefixed:
`map -> odom -> base_link -> wheel/sensor frames`. Other robot topics remain under
`/orbo`, including `/orbo/drive_controller/odom` and velocity/authority topics.
This convention assumes one robot per ROS domain.

The optional measured camera mount publishes `base_link -> camera_optical_frame`
on `/tf_static`, so live RViz sees the same tree as the controller. The camera
template is disabled and has no fabricated mount or intrinsics. Configure the
actual Pi driver and device before launching capture; the Mac software tests use
synthetic images and do not exercise CSI hardware.

Nav2's upstream Jazzy launches remap TF into their namespace. Orbo overrides those
remaps within the Nav2 launch group, preserving global TF while retaining `/orbo`
for navigation topics. Composition is disabled to use the tested process launch
path; lifecycle autostart remains disabled. Configuration tests evaluate the
installed upstream nodes and ROS name resolution, including remap precedence.
See the [upstream launch](https://github.com/ros-navigation/navigation2/blob/jazzy/nav2_bringup/launch/navigation_launch.py).

Run exactly one velocity gate. The bench control launch starts its own teleop
gate. The application `teleop.launch.py` and `navigation.launch.py` each start a
gate for their selected source and are alternatives to that bench path.
Application keyboard input uses a separately authorized owner session; see the
[runtime guide](../packages/orbo_runtime/README.md).

## Remote Xbox joystick control

Connect the Xbox controller by USB or Bluetooth to the operator Mac. Run the
input driver there; Orbo's Pi runs the runtime, velocity gate and controllers:

```text
Xbox -> joy/game_controller_node -> /orbo/teleop/joy (Joy over DDS)
     -> authorized runtime teleop session -> /orbo/teleop/cmd_vel + lease
     -> velocity gate -> /orbo/drive_controller/cmd_vel -> ros2_control -> ESP32
```

The runtime already converts Joy into bounded, stamped velocity commands, so
there is no additional `teleop_twist_joy` converter in this path. Launching the
input driver alone cannot arm Orbo. Use one input publisher and one velocity gate.

### 1. Mac: connect and launch the Xbox controller

Build once from the repository root (outside an ESP-IDF or uv environment):

```bash
pixi install --locked
pixi run --locked ros-build-workspace
```

Connect or pair the controller, then open a ROS shell from the repository root:

```bash
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY
ros2 run joy joy_enumerate_devices
```

Replace the device-name placeholder with the exact enumerated name and leave
this launch running:

```bash
ros2 launch orbo_bringup joy.launch.py driver:=game_controller_node \
  device_name:="EXACT NAME FROM ENUMERATOR"
```

Choose the enumerated Xbox device with an SDL mapping. The explicit device name
avoids silently selecting another attached controller. For generic unmapped
joysticks, use `driver:=joy_node` and measure the axes/buttons separately.
The [upstream Joy documentation](https://github.com/ros-drivers/joystick_drivers/blob/3.3.0/joy/README.md)
defines the SDL layout used by the installed
[`teleop-gamepad.template.toml`](src/orbo_bringup/config/teleop-gamepad.template.toml):

| Xbox control | Mapping | Behavior |
|---|---|---|
| Left stick up/down | Axis 1 | Forward/reverse |
| Left stick left/right | Axis 0 | Turn left/right |
| Hold LB | Button 9 | Deadman; release commands zero velocity |

Both scales start at 25% of commissioned linear/angular limits, with combined
wheel-speed bounds applied by the runtime. Input is published at 50 Hz with a
10% stick deadzone and non-latching buttons. Leave LB released when admitting
each session, then hold LB to drive.

### 2. Pi: configure and start the robot

This subsection uses optional systemd deployment (`--install-systemd`). For
foreground ROS operation, use [the Pi quickstart](../PI5_QUICKSTART.md#9-full-application-teleop)
with the Xbox mapping and remote Joy source instead of the keyboard publisher.

These commands assume the [Pi deployment](../docs/engineering/runbooks/operator.md#3-install-the-pi-runtime-and-agent)
is installed at `/opt/orbo`, with the ESP32 agent device configured and measured
wheel calibration at `/etc/orbo/calibration.yaml`. Keep the wheels lifted for
initial checks. Stop other motion publishers before selecting this control path.

Merge the template's `[robot.teleop]` section into `/etc/orbo/runtime.toml`
(replace the keyboard mapping). Verify stick signs, LB release and disconnect
behavior with wheels lifted, record the results in `evidence_record`, and set
`qualified = true` only after that qualification. Set `[robot].teleop_enabled = true`
and `navigation_enabled = false`. Use measured wheel calibration; the application
teleop path rejects `bench-cad.yaml`. See the
[operator runbook](../docs/engineering/runbooks/operator.md#5-start-and-explicitly-authorize)
for commissioning requirements. Restart an already-running runtime after changing
its configuration, with motion stopped; a restart revokes existing motion grants.

On the Pi, start the installed services:

```bash
sudo systemctl start orbo-micro-ros-agent orbo-control orbo-runtime orbo-teleop
sudo systemctl status orbo-micro-ros-agent orbo-control orbo-runtime orbo-teleop --no-pager
```

`orbo-teleop.service` supplies the single velocity gate and accepts the remote
Joy input through the runtime. It does not start a local joystick driver. Do not
also launch `teleop.launch.py` manually while this service runs.

For the Pi commands below, define a ROS CLI helper in the current Pi terminal.
The wrapper sources Jazzy and the installed workspace:

```bash
orbo_ros() {
  sudo -u orbo env ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET ROS_LOG_DIR=/var/lib/orbo/ros-logs \
    /opt/orbo/deploy/run-ros.sh "$@"
}
orbo_ros control list_hardware_components -c /orbo/controller_manager
orbo_ros control list_controllers -c /orbo/controller_manager
orbo_ros control set_hardware_component_state OrboWheels active -c /orbo/controller_manager
orbo_ros control set_controller_state drive_controller active -c /orbo/controller_manager
```

Wait for the hardware and controllers to appear before activating them. The
qualified control launch already activates `joint_state_broadcaster`; confirm
that both it and `drive_controller` report `active`. Controller activation alone
does not arm the ESP32.

Check reception on the Pi while the Mac joystick launch runs:

```bash
orbo_ros topic info /orbo/teleop/joy --verbose
orbo_ros topic echo /orbo/teleop/joy
# Ctrl-C the echo before checking the rate:
orbo_ros topic hz /orbo/teleop/joy
```

Expect exactly one publisher, updates near 50 Hz while idle, axes 0/1 for the left
stick and button 9 for LB. Ctrl-C the observer when finished.

### 3. Pi: authorize a session and drive

In a Pi terminal, grant five minutes of owner motion permission and start an
episode. This permission does not itself command motion:

```bash
orbo_grant_expires=$(date -u -d '+5 minutes' +%s)
sudo -u orbo /opt/orbo/.venv/bin/orbo request permission.set \
  --args "{\"scope\":\"robot.command\",\"destination\":\"orbo\",\"enabled\":true,\"expires\":$orbo_grant_expires}"
sudo -u orbo /opt/orbo/.venv/bin/orbo request episode.start \
  --args '{"profile":"physical_task.v1"}'
```

Copy the returned `episode_id` into the next command. With **LB released**, request
a 20-second session, then hold LB and move the left stick:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request robot.teleop \
  --args '{"episode_id":"ACTIVE_EPISODE_ID","key":"xbox-session-001","duration":20}'
```

Use a new key for each request. Sessions last at most 30 seconds and require
fresh input throughout. Release LB to command zero. To end a session early, run
from another Pi owner terminal:

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request stop
```

After stopping, Ctrl-C the Mac joystick launch. Close the episode with the same
ID and the actual outcome (`completed`, `cancelled`, `failed`, or `unknown`):

```bash
sudo -u orbo /opt/orbo/.venv/bin/orbo request episode.end \
  --args '{"episode_id":"ACTIVE_EPISODE_ID","outcome":"completed"}'
```

### Network checks and alternate launches

Both machines need the same domain, subnet discovery and synchronized system
clocks. Input more than 200 ms old, future-dated or reordered is rejected; do not
widen that timeout to mask clock skew or poor Wi-Fi. DDS multicast must pass on
the trusted LAN (disable Wi-Fi client isolation if necessary). On the Pi, confirm
one publisher with `ros2 topic info /orbo/teleop/joy --verbose`, inspect the mapping
with `ros2 topic echo /orbo/teleop/joy`, and check updates with
`ros2 topic hz /orbo/teleop/joy` before requesting motion.

For a manual remote-input gate, stop `orbo-teleop.service` first, keep the agent,
control and runtime services running, then use the `orbo_ros` helper above:

```bash
sudo systemctl stop orbo-teleop
orbo_ros launch orbo_bringup teleop.launch.py input:=remote \
  calibration:=/etc/orbo/calibration.yaml
```

For a controller attached directly to the Pi, omit the Mac driver, stop the
teleop service as above, and enumerate/launch on the Pi instead:

```bash
orbo_ros run joy joy_enumerate_devices
orbo_ros launch orbo_bringup teleop.launch.py input:=joystick \
  driver:=game_controller_node device_name:="EXACT NAME FROM ENUMERATOR" \
  calibration:=/etc/orbo/calibration.yaml
```

The local-controller launch starts the input driver alongside one gate; retain
the same runtime mapping, controller activation and owner-session steps.
The Pi installer already installs `ros-jazzy-joy`; the Mac Pixi lock now includes it.

Software integration tests use synthetic Xbox-layout Joy messages to check
deadman release, speed bounds and input loss. They do not establish USB/Bluetooth
support, Mac-to-Pi discovery or physical stopping. Before driving on the floor,
test LB release, controller power-off/unplug, operator process exit and network
loss with the actual controller and lifted wheels. Input expiry terminates the
session; reconnecting requires a new session with LB released.

## Navigation and Pi deployment boundary

Navigation generates parameters from the installed Jazzy defaults and a measured
calibration file. It installs only its `launch` directory; no empty, unversioned
`config` directory is required. It rejects the nominal `bench-cad.yaml` profile.
It requires qualified geometry/limits, a real LaserScan source and sensor frame,
and a surveyed map. A successful launch/configuration test does not supply those.

On the Pi, use the native installer to install dependencies and build all six
Orbo workspace packages with colcon. The workspace provides both ROS launch
imports and installed Python node entry points. The package manifests declare ROS dependencies; Python packaging supplies
the runtime. Native CSI/V4L2 drivers are Pi/Linux dependencies and are not validated
by the Mac suite. Device profiles, system services, sensor calibration, ground
odometry and stopping behavior still need target-specific validation.
