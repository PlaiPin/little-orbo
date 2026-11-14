# Orbo host runtime

The implementation now lives in the [ROS workspace package](../../ros_ws/src/orbo_runtime/README.md).
The root Python wheel and ROS executables share those modules. For Pi operation,
follow the [native ROS quickstart](../../PI5_QUICKSTART.md); systemd is optional.

This Python package runs on the host computer (Mac for development, Pi for the
robot). It coordinates application requests and ROS interfaces. Motor-bus I/O
and the final command watchdog run in the ESP32 firmware; measured wheel state
and odometry are handled by the ROS hardware plugin and drive controller.

| Modules | Responsibility |
|---|---|
| `contracts`, `commissioning`, `velocity_gate` | Shared types, geometry/limit validation, expiring velocity authority |
| `ros_adapter`, `teleop`, `navigation`, `camera` | Firmware sessions, joystick input, named Nav2 goals, camera metadata health |
| `storage`, `reminders`, `broker`, `coordinator`, `cli` | SQLite state, reminders, exclusive command ownership, conversation/tools, local owner API |
| `audio`, `audio_check`, `providers`, `live_voice` | Local audio diagnostics, verified NEAR reasoning and OpenAI Live voice |
| `traces`, `commons`, `consumer`, `economics` | Export preparation, external contributor protocol, retrieval and usage accounting |
| `k1_adapter` | Client for a separately deployed Booster K1 worker |
| `live_e2e` | Explicit OpenAI Live/NEAR/Commons checks using the project `.env` |

The Mac bench path uses calibration validation and the velocity gate. It does
not start the broker, conversation loop, cloud providers, audio, or export clients.
The K1 worker, Commons servers, service deployment and credentials are separate
integrations; installing this package does not install or start those services.

## Installation

From the repository root, for Python application development:

```bash
uv sync --locked --all-extras --group dev
uv run --locked --all-extras orbo diagnostics
uv run --locked --all-extras orbo demo --database /tmp/orbo-synthetic.db
uv run --locked --all-extras pytest -q tests
```

The demo uses synthetic inputs and makes no hardware/provider calls. ROS tests
are skipped unless explicitly enabled. The `near`, `live` and diagnostic `audio` extras install
optional dependencies; they do not enable services. For a minimal application
install, omit `--all-extras`. ROS Python modules come from the ROS environment,
not PyPI dependencies in this package.

On the Mac, Pixi installs this package editable alongside native ROS:

```bash
pixi install --locked
pixi run --locked orbo diagnostics
pixi run --locked ros-build-workspace
pixi run --locked ros-test-integrations
```

There is no manual `PYTHONPATH` step. Keep the uv environment and Pixi shell
separate. Use Pixi Python for ROS commands and generated ROS message imports.
Both locks are committed: `uv.lock` for application development, `pixi.lock` for
the Mac ROS environment. PyYAML allows compatible 6.x releases because RoboStack
supplies it; each environment still locks an exact resolved version.

For a distributable package, run `uv build`; generated wheels and source archives
in `dist/` are ignored. The package exposes `orbo`, `orbo-audio-check`,
`orbo-camera-monitor`, `orbo-teleop-keyboard`, and `orbo-velocity-gate`.

## Keyboard control paths

- `pixi run --locked ros-base-keyboard`: current Mac lifted-bench controller.
  Activates controllers, requests one firmware arm, and checks measured odometry.
- `ros2 run orbo_runtime teleop_keyboard`: application input source. Publishes `Joy` only and
  requires a separately authorized runtime teleop session; it cannot arm the robot.
- `ros2 run orbo_runtime wheel_bench keyboard`: direct firmware bench
  helper, bypassing the ROS drive controller.

Use one path at a time. See the [Mac control runbook](../../docs/engineering/runbooks/ros-control-macos.md)
for the tested terminal sequence and [ROS workspace guide](../../ros_ws/README.md)
for launch integration. The two paths outside the firmware helper have different
ownership mechanisms; do not run both velocity gates together.

## Xbox joystick control

For Xbox control, use the [joystick launch walkthrough](../../ros_ws/README.md#remote-xbox-joystick-control).
It covers Mac input, Pi services/controllers, the runtime mapping, owner grants,
bounded sessions and stopping. The Xbox source uses the same application teleop
adapter as `orbo-teleop-keyboard`; run only one of these input sources at a time.

## Validation boundary

Runtime tests use synthetic devices and mocked providers. ROS integration tests
use isolated domain 96, synthetic messages, and installed launch configuration;
they do not open USB, start camera capture, or command physical motors. Build/test
success does not qualify Pi peripherals, live services, floor navigation, or the
pending battery-powered 30-second drive. Generated logs, databases, credentials,
and test evidence stay outside source commits.
