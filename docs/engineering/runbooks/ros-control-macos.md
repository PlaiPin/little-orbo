# Mac ros2_control bench

This runs native ROS 2 Jazzy through Pixi on Apple silicon. The Pi is not needed
for the lifted-wheel integration test. The ESP32 runs the existing flashed
115200-baud firmware. No firmware reflash is required for these host changes.

The command path is:

```text
local bench owner / keyboard
  -> /orbo/teleop/cmd_vel (TwistStamped) + bounded session lease
  -> velocity gate
  -> /orbo/drive_controller/cmd_vel (TwistStamped)
  -> diff_drive_controller -> Orbo hardware plugin
  -> /orbo/embedded/wheel_command -> agent -> ESP32

ESP32 encoder positions -> hardware state interfaces
  -> joint_state_broadcaster + diff_drive_controller
  -> /orbo/joint_states, /orbo/drive_controller/odom, /tf
```

`open_loop=false`, `position_feedback=true`: odometry uses measured encoder
increments, not requested speed. The plugin subtracts the initial encoder
positions so counts accumulated before launching ROS do not become a startup
pose jump. Controller updates and odometry run at 50 Hz; the hardware command
stream is capped at 20 Hz to avoid outpacing the firmware's reliable input loop.

The checked-in `bench-cad.yaml` uses nominal CAD geometry: 33 mm wheel radius,
101.8 mm wheel-center separation, and the firmware's 1.2 rad/s wheel limit.
The owner selected these values for lifted-wheel testing. `bench_only: true`
and `wheels_qualified: false` are intentional. Encoder-derived travel while
lifted is not actual chassis displacement or calibrated floor odometry.
Production teleop/Nav2 still requires a measured, qualified calibration profile.

## Build once

From a fresh terminal outside ESP-IDF:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi install --locked
pixi run --locked ros-build-control
```

Pixi includes `ros-jazzy-ros2-control`, `ros-jazzy-ros2-controllers`, and xacro.
The build installs interfaces, hardware plugin, robot description and bringup.
Pixi also installs the local Python runtime and its console commands; no manual
`PYTHONPATH` export is needed. To include navigation and run the separate
peripheral/launch checks, use `ros-build-workspace` and `ros-test-integrations`;
see the [workspace guide](../../../ros_ws/README.md).

## Mac transport and timing

Pixi runs ARM64 ROS 2 directly on macOS; it does not need Docker or a Linux VM.
The Mac environment selects `FASTDDS_BUILTIN_TRANSPORTS=UDPv4` and
`ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET`. Start fresh Pixi shells and restart ROS
processes/agent after updating this configuration. Existing processes retain
their old environment. Keep the robot on domain 42 and synthetic tests on their
separate test domain.

A native stack sample caught Fast DDS 2.14 shared-memory recovery blocked on an
interprocess mutex. UDP avoids that path without changing topic reliability.
Do not override discovery to `LOCALHOST` for these timing tests: Jazzy's
[Fast DDS participant setup](https://github.com/ros2/rmw_fastrtps/blob/jazzy/rmw_fastrtps_shared_cpp/src/participant.cpp)
explicitly adds shared-memory transport in that mode, bypassing the built-in
transport selection. `SUBNET` allows LAN discovery, including the future Pi.

The hardware plugin now services incoming callbacks continuously instead of
allowing a 2 ms callback budget followed by a 50 ms sleep. Fresh authorization
triggers dispatch with a 20 Hz cap; a short timer releases pending input when the
cap allows. Each original source timestamp is forwarded
once, so resending an aging command cannot consume the next send slot ahead of
fresh input. The keyboard/base helper keeps one executor
attached rather than adding and removing its node on every callback. The 150 ms
feedback guard, 100 ms controller-write guard, 200 ms source expiry, and original
command timestamps are unchanged. No automatic rearm was added.

The synthetic firmware runs in a separate process so host callback work cannot
hold up its expiry checks through Python's shared interpreter lock. Test cleanup
terminates the whole process group even if its launcher has exited; earlier
Linux qualification runs had left 17 controller processes running in Docker.
A heavily loaded or memory-starved desktop can still miss these deadlines; a
watchdog stop in that situation must not be treated as a reason to extend them.

## Run

Keep the base lifted, motor power on, and USB connected. Stop the direct
`control_wheels.py` helper before using this stack; both must not own the wheels.
Run all commands from the repository root.

**Terminal 1 — agent:** reuse it if already running.

```bash
./tools/run-xrce-agent-macos.sh /dev/cu.usbmodem1234561
```

**Terminal 2 — controllers, robot description and velocity gate:**

```bash
pixi run --locked ros-control
```

This starts the hardware, drive controller and joint-state broadcaster inactive. The next command checks
feedback, activates the controllers and requests one fresh MCU arm. It never
rearms automatically after a fault.

**Terminal 3 — choose keyboard or timed drive:**

```bash
pixi run --locked ros-base-keyboard
# Or, after keyboard mode exits: 0.02 m/s for 30 seconds.
pixi run --locked ros-base-drive
```

Keyboard: W/S both wheels positive/negative, A/D rotate, Space/X zero, Q/Ctrl-C
stop and disarm. A 350 ms key-repeat timeout requests zero. Default wheel speed
is 1.2 rad/s (0.0396 m/s straight using the nominal radius).

For custom body speeds in a configured ROS shell:

```bash
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
python tools/control-base.py drive --linear 0.02 --angular 0 --seconds 30
python tools/control-base.py keyboard --speed 0.6
```

The helper bounds the combined linear/angular command to 1.2 rad/s per wheel
and drive duration to 60 seconds. It checks fresh health, wheel feedback, joint
states, odometry and publisher exclusivity. It publishes no raw wheel commands.
This is a local bench owner, not the production task broker or navigation stack.

**Terminal 4 — live RViz, after activating the controllers:**

```bash
pixi run --locked rviz-live
```

RViz receives `/orbo/robot_description`, measured wheel joint transforms, and
`odom -> base_link` on global `/tf`. Before the drive controller is activated,
`odom` does not yet exist. Firmware flippers remain disabled and their CAD
visuals are fixed references; no measured flipper joint state is invented.

Exit terminal 3 first and verify `Stopped and disarmed (encoder feedback).`,
then stop terminal 2 and finally terminal 1 with Ctrl-C. A fault requires an
explicit new controller invocation; do not bypass its diagnostics.

## Current verification boundary

The native packages and hardware plugin build on the Mac. Live commands reached
the motors through the full path and produced encoder-derived odometry. Initial
physical runs stopped early with `EXPIRED (2)` while motor read errors stayed
zero. A separate idle capture also showed telemetry-delivery gaps. The plugin
transport cadence has been reduced from approximately 50 Hz to 20 Hz; a full
30-second physical rerun remains pending battery charging after telemetry
fell to 6.4 V. Do not interpret the earlier direct-wheel 30-second pass as a
pass of this new controller path.

## Work without the battery

The complete software-only checks run in domain 96 and do not open USB or join
robot domain 42. No agent or powered robot is required. Build first if needed,
then run:

```bash
pixi run --locked ros-build-control
pixi run --locked ros-test-control
# Five consecutive full 30-second runs, with a fresh stack for each (domain 97):
pixi run --locked ros-test-control-repeat
```

The suite runs the real controller manager, drive controller, velocity gate and
Orbo hardware plugin with synthetic firmware messages. It covers:

- A full 30-second run with nonzero initial encoder counts and encoder motion
  deliberately different from commanded motion. Odometry must follow the encoders.
- Reverse and in-place turning, checked against encoder displacement and yaw.
- Loss of the command source: wheel-command refresh stops within its validity
  window, the synthetic firmware disarms, and the owner never automatically rearms.
- Lost feedback and a changed firmware boot ID: the plugin requests a stop and
  does not resume merely because messages return.
- Real plugin deactivation on stale feedback, plus 13 helper/model checks.

After the host timing fixes and shutdown of the leftover Docker qualification
workload/VM, `ros-test-control-repeat` passed **five consecutive 30-second runs**
with a freshly launched stack each time (165.75 seconds total test time). Each run
sent 562–566 wheel commands with strictly increasing source timestamps. Across
those runs, the largest received-command age was 102 ms (200 ms validity), and
the largest armed feedback interval was 77.9 ms (150 ms freshness guard).
The subsequent `ros-test-control` run passed all 8 integration/cleanup cases
(including another full 30-second drive) and all 13 helper/model checks.
`ros-test-integrations` also passed all 16 installed-launch, peripheral, navigation
adapter, and runtime-packaging checks.

The loaded-desktop runs before that cleanup still failed correctly: one publisher
paused for 160 ms, and another run logged a 190 ms controller cycle. Four isolated
passes followed by a failure were not counted as a successful repeat batch.
These results establish software behavior on the tested Mac workload, not hard
real-time guarantees under arbitrary memory pressure. Watchdog limits have not
been relaxed. Logs and captures remain in temporary test directories.

A startup race found by these tests was fixed: the bench launch loads both
controllers inactive, and the bench owner activates them together. It waits for
fresh joint states and odometry before arming. Long keyboard sessions keep only
the latest two minutes of odometry in memory and reject missing/non-finite states.

These checks do not simulate USB electrical behavior, motor power, motor-bus
integrity, or physical stopping distance. The revised host dispatch/transport still needs
the battery-powered 30-second drive and keyboard rerun. Captures and test logs
stay local, outside committed source files.

After charging, keep the base lifted and follow the four-terminal run above.
Verify fresh feedback, then run `ros-base-drive`; compare encoder-derived travel
with odometry and confirm final zero speed/disarm. Follow with keyboard turns and
source-loss/recovery checks before qualifying ground driving.

## CAD model

The old preview referenced v05, while the installation rule copied only two
unrelated STL files. The updated description exports the complete
`cad/v9_display_mount/CAD/Orbo_V9_DisplayMount.step`: all 176 STEP assembly leaves
(including a compound display reference), grouped into 13 meshes. V9 preserves
the V8 chassis, flippers and shells and adds the display mount. Both preview and
live robot description now use these meshes; CMake installs the mesh directory.

CAD millimetres are converted to ROS metres, with forward +X, left +Y, up +Z,
and the origin at the wheel-axle midpoint. Source hash and part-to-mesh mapping
are in `cad/meshes/v9/manifest.json`.

Full visual preview, independent of the robot, in isolated domain 94:

```bash
pixi run --locked orbo-preview
```

To inspect the chassis, electronics, display mount and flippers with the outer
shells hidden:

```bash
pixi run --locked bash -c 'source ros_ws/install/macos/local_setup.bash && python tools/preview-orbo-macos.py --internal'
```

Both views were visually checked in native RViz with model status OK. Meshes are
visual geometry; the simplified collision volume is not a validated Nav2 footprint.
Re-export only after reviewing a CAD revision:

```bash
uv venv .sdk/ros-cad-tools --python 3.12
uv pip install --python .sdk/ros-cad-tools/bin/python cadquery-ocp==7.8.1.1.post1
.sdk/ros-cad-tools/bin/python tools/build-orbo-v9-description.py
pixi run --locked ros-build-description
```
