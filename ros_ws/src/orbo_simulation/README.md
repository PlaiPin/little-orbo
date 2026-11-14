# Orbo in Gazebo

For a step-by-step operator walkthrough, see the
[simulation quickstart](../../../SIMULATION_QUICKSTART.md).

Native Apple Silicon / macOS physics prototype: the v9 Orbo assembly drives in a
4 × 4 m living room with a sofa, coffee table, armchair, TV console, sideboard,
plant, rug and wood floor. Gazebo Harmonic uses DART for
gravity/contact physics, and `gz_ros2_control` runs the real ROS differential-drive
controller against simulated wheel joints. No ESP32, USB, Pi or provider is used.

## Run on the Mac

From the **little-orbo repository**, outside any other ROS or Python environment:

```bash
pixi install --locked
pixi run --locked orbo-sim
```

This builds the description and simulation packages, opens **Orbo Gazebo**, starts
physics, spawns the robot, activates the drive controllers, and starts AMCL/Nav2.
Allow about 20 seconds for startup; the goal client waits for activation.
Run only one room instance at a time.
Close Gazebo or press Ctrl-C in its terminal to stop the launch.
The sensor-enabled viewer includes a live RGB camera inset.

In a second terminal, from the same repository:

```bash
pixi run --locked orbo-sim-goal tour
pixi run --locked orbo-sim-goal named sofa
pixi run --locked orbo-sim-goal goal 1.0 -0.55 1.57
pixi run --locked orbo-sim-goal cancel
```

`tour` visits **tv → sofa → reading → entry**. Named poses are in
`config/goals.yaml`; arbitrary goals use map coordinates in metres and yaw in
radians. Goals inside furniture or too close to obstacles may fail planning.
Ctrl-C in the goal terminal requests cancellation. `cancel` can also stop a goal
from a separate terminal. The CLI reports success only when Nav2 returns success.

For keyboard control, restart with navigation disabled:

```bash
pixi run --locked orbo-sim navigation:=false
# Second terminal:
pixi run --locked orbo-sim-drive keyboard
```

Keep keyboard focus in this terminal. W/S drive forward/reverse, A/D turn, Space
stops, and Q exits with a stop. Hold/repeat a key to move. Input expires after
350 ms; the controller independently brakes after 300 ms without a fresh command.
The operating system's initial key-repeat delay may briefly interrupt movement.
Use one command publisher at a time.

For the original square demo, start the original simple room:

```bash
pixi run --locked orbo-sim world:=room navigation:=false sensors:=false
# Second terminal:
pixi run --locked orbo-sim-drive demo
```

This drives a 0.5 m square in the clear southwest corner using encoder odometry,
then stops. It is a bounded scripted driving demonstration, **not autonomous
navigation or obstacle avoidance**. Restart the room before repeating it after
manual driving. Interrupt with Ctrl-C, or send a stop after the driver has exited:

```bash
pixi run --locked orbo-sim-drive stop
```

For physics without a window:

```bash
pixi run --locked orbo-sim gui:=false
```

`gui:=false` hides the viewer; GPU lidar and camera still use Ogre2/OpenGL in the
server. This was tested in a logged-in Mac desktop session, not a displayless CI
machine. `sensors:=false navigation:=false` runs physics without sensor rendering.

## Sensors and navigation

| ROS topic | Data |
| --- | --- |
| `/orbo_sim/scan` | 360-ray planar lidar, 10 Hz, 0.06–8 m, `lidar_link` |
| `/orbo_sim/camera/image_raw` | RGB8, 320 × 240, 10 Hz, ~80° horizontal field of view |
| `/orbo_sim/camera/camera_info` | Pinhole calibration, `camera_optical_frame` |
| `/orbo_sim/map` | Generated 2 cm occupancy map |
| `/orbo_sim/amcl_pose` | Lidar localization estimate in `map` |
| `/orbo_sim/drive_controller/odom` | Wheel encoder odometry |

Lidar is mounted 0.24 m above the axle; the forward camera sits at
`(0.09, 0, 0.18)` m relative to `base_link`. These are idealized sensors with no
added noise, not measured hardware specifications. The camera provides RGB data;
navigation currently uses lidar, not visual odometry or object recognition.

AMCL starts at the known spawn pose and supplies `map → odom`; the wheel
controller supplies `odom → base_link`. NavFn/A* plans around furniture and
Regulated Pure Pursuit follows the path. Both costmaps consume live lidar.
Commands pass through the velocity smoother and collision monitor before the
drive controller. All stages use `TwistStamped` and simulation time; the monitor
stops commands when lidar becomes stale. Manual driving publishes directly to the
drive controller, so keep Nav2 disabled for manual sessions.

`scripts/build_living_room.py` generates both `worlds/living_room.sdf` and
`maps/living_room.{pgm,yaml}` from the same furniture geometry. The map conservatively
includes furniture overhangs. Regenerate both after editing the layout:

```bash
pixi run --locked python ros_ws/src/orbo_simulation/scripts/build_living_room.py
```

This is navigation in a known map, not SLAM. Nav2 receives no Gazebo ground-truth
pose; only the acceptance test uses ground truth to verify physical arrival.

## Mac compatibility

The locked Pixi environment targets macOS ARM64 with Gazebo Sim **8.10.0**,
`gz_ros2_control` **1.2.20**, ROS 2 Jazzy and DART physics. No Docker, Homebrew
Gazebo installation or Rosetta is needed. The lockfile includes the simulator,
ROS bridge, Python transport and control plugin.

The wrapper creates an ignored `.pixi/Orbo Gazebo.app` around Pixi's Ruby runtime
so the Qt window has a macOS application identity. It uses **Ogre2/OpenGL**:
the bundled Metal path produced a blank Ogre window in this environment. The
OpenGL path rendered the room and complete v9 robot successfully. The bundled
renderer can still log an initial Ogre plugin-path error before loading the
working fallback path; this was observed alongside successful rendering.

Gazebo's Mac build requires separate `gz sim -s` and `gz sim -g` processes.
The launch manages both and opens the GUI only after controller activation;
opening it during initialization was observed to pause physics and time out
activation. Both processes stop with the launch. A bundled ROS context destructor
may print a mutex diagnostic during shutdown; process cleanup was verified.

The shell wrapper sources colcon before enabling `set -u`, because colcon's
generated setup scripts reference unset variables. It does not alter global
shell, ROS, or system configuration.

## What the physics represents

- Wheel radius **33 mm**, separation **101.8 mm**, width **18.2 mm**, from v9 CAD.
- The front ball caster uses its CAD location and **9.525 mm** radius. A fixed
  low-friction sphere approximates its free rolling contact.
- Detailed shared v9 meshes supply appearance. Boxes and cylinders supply
  collision geometry; hidden electronics do not each become a dynamic body.
- Body mass, center of mass, inertia, friction and drive response are provisional
  simulation assumptions in `config/physics.yaml`, not measured hardware values.
- A velocity-controlled wheel interface supplies motion; this is not an XL430
  motor electrical/torque model. Flippers remain fixed.
- Simulation speed limits are 0.15 m/s and 1 rad/s. They do not change the slower
  hardware bench calibration or qualify the real robot for those speeds.

The native controller publishes encoder-derived `/orbo_sim/drive_controller/odom`
and `odom → base_link`; `robot_state_publisher` supplies the remaining transforms.
The `/clock` bridge supplies simulation time to the ROS stack. Ground-truth body pose
is available through Gazebo and used independently by the acceptance test.
Wheel slip during contact may make encoder odometry differ from body motion.
The bridge uses Gazebo Transport subscription throttling to publish the actual
simulation timestamp at up to 100 Hz. This avoids broadcasting the 1 kHz physics
clock to every Nav2 node on the Mac; physics still advances in 1 ms steps.

## Isolation and verification

The interactive launcher fixes **ROS domain 95**, Gazebo partition
`little-orbo-sim`, and ROS namespace `/orbo_sim`. It never loads `orbo_hardware`,
the robot broker, physical calibration, or the micro-ROS agent. Direct launch
without the matching simulation domain/partition fails explicitly.

```bash
pixi run --locked ros-test-simulation
```

The opt-in integration tests start fresh Gazebo processes without the viewer in **domain
99**, partition `little-orbo-sim-test`, then clean up their process groups. Ordinary
Python test runs skip this lane. They verify:

- Stable resting height and upright orientation.
- Forward and reverse physical displacement, with independent encoder comparison.
- Turning in place.
- Actual body stop after command publication ceases.
- Solid wall contact: the robot cannot drive through the east wall.
- Lidar scan geometry and rate, nonblank RGB frames, camera calibration and frames.
- All four named Nav2 goals, with independent physical position/yaw checks.
- Nav2 goal cancellation followed by a stationary physical body.

Generated physics reports are saved locally in ignored
`artifacts/test-evidence/gazebo-macos/physics-report.json`. Simulation checks
do not establish real-robot calibration or stopping performance.

Navigation evidence, the camera capture and trajectory are saved under the same
directory as `navigation-report.json`, `camera.png`, `trajectory.json` and
`navigation.log`. The navigation report is reset to `passed: false` at the start
of each run, and retains completed stages if a later check fails.

Navigation acceptance bounds are 15 cm and 0.3 rad against Gazebo ground truth,
including localization error.

## Limitations

The simulation includes room physics, lidar/RGB sensors and AMCL/Nav2 goal
execution. It does not model an IMU, SLAM, measured sensor noise, moving people,
the broker and velocity-gate session protocol, or an ESP32. The hardware
description and control launch are separate.

References: [Gazebo Harmonic](https://gazebosim.org/docs/harmonic/),
[Jazzy gz_ros2_control](https://control.ros.org/jazzy/doc/gz_ros2_control/doc/index.html),
[Gazebo sensors](https://gazebosim.org/docs/harmonic/sensors/),
[Nav2 Jazzy](https://docs.nav2.org/jazzy/).

## Live voice, navigation and NEAR vision

The live simulation flow has completed using the production `LocalService`,
`GazeboAdapter`, task coordinator, ROS camera, `VerifiedVision`, NEAR SDK and
GPT-Live client delegation. The [saved report](../../../docs/engineering/evidence/gazebo-near-live/report.json)
records completed navigate, capture and infer operations, a verified live finding,
accepted result context, returned speech and finalized Live usage. The simulated
body finished 0.030485 m from the TV goal.

The request was synthesized locally: “Please go to the TV area and describe the
visible objects and their colors.” GPT-Live transcribed and delegated it; NEAR
interpreted the request, Nav2 navigated from the entrance, and NEAR described the
actual 320 × 240 camera snapshot. The response was returned to Live and spoken.
No physical robot, microphone, Commons upload or privacy-filter credential was used.

**Integration passed; recognition was mixed.** Of three labeled props, the red
bottle and blue book matched their expected colors and counts. The brown box was
partly cropped and omitted. The model also misidentified the background sofa as a
shelf. The [assessment](../../../docs/engineering/evidence/gazebo-near-live/assessment.json) preserves these
errors; signature verification is not an accuracy guarantee. Request interpretation
took 31.4 seconds, navigation about 29.3 seconds, and verified vision 18.1 seconds.
The first spoken-result transcript arrived about 86.1 seconds after voice startup.
This is a working integration, not an acceptable latency or accuracy benchmark.

- [Exact camera image](../../../docs/engineering/evidence/gazebo-near-live/capture-02.png),
  [ground truth](../../../docs/engineering/evidence/gazebo-near-live/ground-truth.json),
  [vision prompt](../../../docs/engineering/evidence/gazebo-near-live/prompt-02.json), and
  [complete model response with verification receipt](../../../docs/engineering/evidence/gazebo-near-live/response-02.json).
- [Video with the actual returned audio](../../../docs/engineering/evidence/gazebo-near-live/interaction.mp4),
  [spoken response](../../../docs/engineering/evidence/gazebo-near-live/spoken-response.wav),
  [transcript](../../../docs/engineering/evidence/gazebo-near-live/transcript.json), and
  [interaction screenshot](../../../docs/engineering/evidence/gazebo-near-live/interaction-screenshot.jpg).
- [Earlier rejected replies](../../../docs/engineering/evidence/gazebo-near-live/earlier-rejected-responses.json)
  document the initial failures rather than presenting only the successful run.

The video is an evidence replay of recorded ROS frames and Gazebo body positions,
with elapsed time and stage captions. It is not a recording of the Gazebo GUI.
The screenshot captures the local evidence viewer. Input speech is synthetic;
output speech comes from GPT-Live. Audio alignment uses local receipt times and a
software playback queue because Live does not provide output playback timestamps.
Physical audibility and echo cancellation remain untested.

### Repeat the live flow

Install the existing Pixi ROS workspace and set `NEARAI_API_KEY` and
`OPENAI_API_KEY` in the ignored root `.env`. Do not run another simulation on test
domain 100 / partition `little-orbo-embodied-test` concurrently.

```bash
pixi run --frozen bash tools/run-gazebo-near-live.sh \
  --allow-cloud --allow-motion --voice --voice-only \
  --output artifacts/gazebo-near-live/new-run
```

The output directory must not already exist. This creates a fresh simulation,
spawns the checked-in bottle/book/box props, grants bounded simulation-only task
permissions, streams a synthetic spoken request and records local evidence.
The runner stops its simulator and runtime after the run. Omitting `--voice-only`
first runs a structured navigation/description task; omitting both voice flags
runs only that structured task. Each invocation is a new billed run, not an
automatic retry. The archived run used `--frozen` because the checkout's Pixi
lockfile is out of sync with its manifest; it used the existing installed ROS
environment. `uv` checks use their frozen lockfile independently.

To render a voice run into the annotated video, with no additional provider calls:

```bash
uv run --frozen --with pillow python tools/render-gazebo-live-video.py \
  artifacts/gazebo-near-live/new-run
```

`near.timeout_seconds` now configures the provider and request interpretation
budgets, bounded to 1–120 seconds; the Gazebo profile uses 120 seconds. Existing
profiles without this key keep their previous defaults. Task and operation
deadlines still apply. Verified vision accepts one whole-message JSON Markdown
fence but still rejects surrounding prose and schema violations. Enum fields must
remain scalar strings; the schema is not relaxed to accept arrays. Sofa, cabinet
and picture are now supported bounded description categories. The live smoke test
also requires nonzero PCM samples after result-context acceptance, since Live can
stream all-zero audio while waiting. That check alone does not prove intelligible
speech; the saved transcript and output audio support reviewing this run.
