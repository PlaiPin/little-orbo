# Running Orbo's Gazebo living-room simulation

This guide runs Orbo entirely on your Mac: Gazebo physics, a furnished living
room, simulated lidar and RGB camera, and Nav2 navigation using a supplied map.
No physical robot, Pi, ESP32, Docker, or external service is needed.

## 1. Prepare the Mac once

The locked environment targets Apple Silicon, macOS 14 or newer, and Pixi
0.84.2 or newer. The simulation was verified on macOS 15.8. Intel Macs and
Rosetta terminals are not covered by this setup.

Use a fresh terminal with no active virtualenv, Conda, ESP-IDF, or other ROS
environment. Run commands from the repository root. On this Mac:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
```

If your checkout is elsewhere, substitute its path in each terminal.

If this Mac has not been set up for the repository yet, install Apple's command
line tools if missing (`xcode-select --install`), then run:

```bash
./tools/install-ros-macos.sh
```

The installer uses an existing Pixi installation, or installs it through
Homebrew if available, installs the locked environment, and checks local ROS
communication. See the [Mac setup runbook](docs/engineering/runbooks/ros-macos.md)
if neither Homebrew nor Pixi is installed or setup fails.

If Pixi is already installed, install or refresh this checkout's environment:

```bash
pixi install --locked
```

The first installation downloads the dependencies. Subsequent launches reuse
them. Do not install a separate system Gazebo or source another ROS distribution.

## 2. Start the living room — terminal 1

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi run --locked orbo-sim
```

Leave this terminal running. The task builds the description and simulation
packages automatically, starts Gazebo, spawns Orbo, activates wheel control, and
starts localization and Nav2. Allow about 20 seconds, or longer on a busy Mac.

You should see:

- An **Orbo Gazebo** window showing a sofa, coffee table, armchair, TV, rug,
  sideboard and plant in a 4 × 4 m room.
- Orbo in the open entry area and a live **Orbo · RGB camera** inset.
- Advancing simulation time; the pause control can pause/resume physics.
- `Managed nodes are active` messages in the terminal once Nav2 is ready.

Run one interactive simulation instance at a time. If the window from an earlier
session is already open, use that instance or close it before starting another.

## 3. Send navigation goals — terminal 2

Open a second terminal in the repository. Start the four-stop tour:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi run --locked orbo-sim-goal tour
```

The client waits for Nav2 activation, then visits **TV → sofa → reading → entry**.
It prints `Goal reached` after each successful Nav2 result. Allow several minutes
for the complete tour. Keep the simulator running and unpaused throughout.

Alternatively, send one named goal:

```bash
pixi run --locked orbo-sim-goal named sofa
```

Available names and map poses:

| Name | X (m) | Y (m) | Yaw (rad) |
| --- | ---: | ---: | ---: |
| `entry` | -0.8 | -0.8 | 0.0 |
| `tv` | 1.0 | -0.55 | 1.57 |
| `sofa` | 0.9 | 0.85 | 3.14 |
| `reading` | -0.8 | 0.7 | -1.57 |

For a custom goal, provide X, Y and optionally yaw:

```bash
pixi run --locked orbo-sim-goal goal 1.0 -0.55 1.57
```

Coordinates are in the `map` frame, with the room centered at `(0, 0)`.
Yaw is in radians: `0` faces +X, approximately `1.57` faces +Y. Omitted yaw
defaults to zero. The CLI accepts finite X/Y coordinates within ±1.85 m;
Nav2 still rejects unreachable goals, including positions inside furniture or
without enough clearance for Orbo's footprint. Use one goal client at a time.

## 4. Cancel, stop, and restart

To cancel the active goal, press **Ctrl-C in terminal 2**, or run this from
another terminal in the repository:

```bash
pixi run --locked orbo-sim-goal cancel
```

Cancellation stops navigation and leaves the simulation running. To shut down
the entire simulation, close the Gazebo window or press **Ctrl-C in terminal 1**.
Wait for the launch to exit before starting it again.

For a fresh starting pose, stop and restart the launch. This resets the world,
wheel odometry and AMCL together; use this instead of the GUI's world-reset
button when running Nav2.

## 5. Other run modes

Stop the existing launch before selecting another mode.

### Navigation without the viewer

```bash
pixi run --locked orbo-sim gui:=false
```

Use the same goal commands in terminal 2. Lidar and camera still render through
Ogre2/OpenGL in the server. This mode is verified in a logged-in Mac desktop
session; it is not a guarantee of operation on a displayless CI host.

### Manual keyboard driving

Disable Nav2 in terminal 1:

```bash
pixi run --locked orbo-sim navigation:=false
```

Then run in terminal 2:

```bash
pixi run --locked orbo-sim-drive keyboard
```

Keep keyboard focus in terminal 2. Hold/repeat **W/S** for forward/reverse,
**A/D** to turn, **Space** to stop, and **Q** to stop and exit. Input expires
after 350 ms; the wheel controller independently brakes after 300 ms without a
fresh command. The initial operating-system key-repeat delay may interrupt
motion briefly. Manual driving bypasses Nav2, so do not run it alongside an
active navigation session.

After exiting the manual driver, an explicit stop is available with:

```bash
pixi run --locked orbo-sim-drive stop
```

For an active Nav2 goal, use `orbo-sim-goal cancel` instead: Nav2 otherwise keeps
publishing new motion commands.

### Original simple-room square demo

Terminal 1:

```bash
pixi run --locked orbo-sim world:=room navigation:=false sensors:=false
```

Terminal 2, on a fresh room:

```bash
pixi run --locked orbo-sim-drive demo
```

This is an encoder-controlled 0.5 m square, not obstacle-avoiding navigation.
Restart the room before repeating it after manual driving.

To run only living-room physics without sensors or navigation:

```bash
pixi run --locked orbo-sim sensors:=false navigation:=false
```

## 6. Inspect sensors and Nav2

The live RGB image is visible in the Gazebo inset. For ROS diagnostics, open a
third terminal and enter the same isolated environment as the simulator:

```bash
cd /Users/kpatch/startup/opendive/projects/little-orbo
pixi run --locked bash
source ros_ws/install/macos/local_setup.bash
export ROS_DOMAIN_ID=95 GZ_PARTITION=little-orbo-sim
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST ROS_STATIC_PEERS=""
export SKIP_DEFAULT_XML=1
unset ROS_DISCOVERY_SERVER ROS_SUPER_CLIENT ROS_LOCALHOST_ONLY
unset FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE
```

Then inspect the running system:

```bash
ros2 lifecycle get /orbo_sim/amcl
ros2 lifecycle get /orbo_sim/bt_navigator
ros2 lifecycle get /orbo_sim/collision_monitor
ros2 topic info /orbo_sim/scan
ros2 topic info /orbo_sim/camera/image_raw
ros2 action list
```

The three lifecycle nodes should report `active [3]`. Exit this diagnostic shell
with `exit` when finished.

| Topic | Expected data |
| --- | --- |
| `/orbo_sim/scan` | 360-ray lidar, 10 Hz, 0.06–8 m range |
| `/orbo_sim/camera/image_raw` | 320 × 240 RGB8 image, 10 Hz |
| `/orbo_sim/camera/camera_info` | Camera calibration and optical frame |
| `/orbo_sim/map` | Supplied occupancy map, 2 cm resolution |
| `/orbo_sim/amcl_pose` | Lidar localization estimate |
| `/orbo_sim/drive_controller/odom` | Wheel encoder odometry |

Navigation uses lidar, AMCL, NavFn/A*, Regulated Pure Pursuit, a velocity smoother
and a collision monitor. The camera supplies images but is not used for visual
navigation. The map is supplied, not built through SLAM. Sensor and physics
parameters are prototype assumptions, not calibrated measurements of hardware.

## 7. Run acceptance tests

For the most predictable timing, stop the interactive simulator and other heavy
rendering work first, then run:

```bash
pixi run --locked ros-test-simulation
```

The tests start and clean up their own simulator instances in ROS domain **99**
and Gazebo partition `little-orbo-sim-test`; the interactive instance uses domain
**95** and partition `little-orbo-sim`. Allow several minutes.

The checks cover physical driving, wall contact, command expiry, sensor streams,
all four Nav2 goals, action cancellation and the goal client's Ctrl-C handler.
Goal arrival is checked against Gazebo's actual body pose, independently of AMCL.
Ordinary Python test runs skip these integration tests unless explicitly enabled
through this task.

To select one integration test:

```bash
pixi run --locked ros-test-simulation -k navigation
pixi run --locked ros-test-simulation -k physics
```

Local evidence is written to `artifacts/test-evidence/gazebo-macos/`:

- `physics-report.json`: physical driving checks.
- `navigation-report.json`: sensor rates, goal errors and stopping checks.
- `camera.png`: captured simulated RGB image.
- `trajectory.json`: physical body positions during navigation.
- `navigation.log`: simulator and Nav2 logs from that test.

Inspect the current command's exit status and test output. The navigation report
is reset at startup; the physics report is written on success and may otherwise
still contain an earlier result.

## 8. Change goals or room furniture

Edit named poses in
[`config/goals.yaml`](ros_ws/src/orbo_simulation/config/goals.yaml). For furniture,
edit [`scripts/build_living_room.py`](ros_ws/src/orbo_simulation/scripts/build_living_room.py),
then regenerate the world and matching map together:

```bash
pixi run --locked python ros_ws/src/orbo_simulation/scripts/build_living_room.py
```

Stop and restart `pixi run --locked orbo-sim` to rebuild and load the changes.
The generator includes furniture overhangs in the map conservatively. If you
change furniture, verify that named goals remain in free space and rerun the
navigation test. Tune navigation separately in
[`config/nav2.yaml`](ros_ws/src/orbo_simulation/config/nav2.yaml).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `pixi` is missing | Complete step 1 and the linked Mac setup runbook. |
| Package or executable not found | Run `pixi run --locked ros-build-simulation`, then retry from the repository root. |
| No goals accepted during startup | Keep terminal 1 open and wait for activation; the goal client waits up to 60 seconds. Inspect its launch log if activation never finishes. |
| Orbo does not move | Confirm physics is unpaused, Nav2 is active, the goal is in free space, and only one interactive simulator is running. |
| A goal fails or times out | Check terminal 1 for planner, transform or stale-sensor errors. Try a named goal. Restart the launch after manual world resets or moving Orbo outside navigation. |
| ROS diagnostic commands find nothing | Use the diagnostic shell in step 6. The repository's hardware ROS domain is different from simulation domain 95. |
| Blank Gazebo window | Use `orbo-sim` so it selects the tested Ogre2/OpenGL backend and launches separate server/GUI processes. Do not replace it with a bare combined `gz sim` command. |
| Slow startup, reduced frame rate or intermittent braking | Let initialization finish and reduce competing rendering/CPU work. The controller deliberately expires stale commands. |
| Initial Ogre plugin-path warning | A fallback path can still render successfully; check the actual scene and camera. A persistently blank view is not a successful launch. |
| Mutex diagnostic during shutdown | This has occurred in the bundled native libraries. Confirm the launch has exited before restarting; retain the terminal log if processes fail to stop. |

For model assumptions, implementation details and recorded acceptance results,
see the [simulation package guide](ros_ws/src/orbo_simulation/README.md).
