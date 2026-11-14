# Orbo ROS Python package

`orbo_runtime` is an `ament_python` package. Its Python modules are the same
implementation used by the standalone application wheel built from the root
`pyproject.toml`; there is no second copy of the motion/permission logic.

| ROS executable | Purpose |
|---|---|
| `base_bench` | Lifted-wheel session owner through gate and ros2_control; checks measured odometry |
| `wheel_bench` | Direct MCU bench session owner; checks encoder feedback |
| `teleop_keyboard` | Input-only Joy publisher; application owner authorization is separate |
| `velocity_gate` | Source selection, expiring authority and stamped velocity validation |
| `camera_monitor` | Camera stream and calibration metadata health |
| `runtime` | Owner CLI and application coordinator; use `serve` to start it |
| `audio_check` | Local microphone/speaker checks |
| `installed_smoke` | Hardware-free owner IPC and shutdown check in domain 94 |

After building and sourcing the workspace:

```bash
ros2 pkg executables orbo_runtime
ros2 run orbo_runtime base_bench --help
ros2 run orbo_runtime runtime diagnostics
ros2 launch orbo_bringup bench.launch.py
```

The bench launch starts one control stack and gate without arming. Run
`base_bench` separately in an interactive terminal or with an explicitly bounded
`drive` command. Do not run direct-wheel, base-bench and application motion
owners together. Existing source-path bench scripts forward to these modules for
compatibility; the installed executables do not read those source scripts.

`base_bench` resolves its nominal CAD profile from the installed
`orbo_description` package. It retains fresh-feedback checks, competing-publisher
rejection, epoch fencing, speed/duration limits, lease expiry, and confirmed
stop/disarm behavior. No watchdog thresholds changed in the packaging refactor.

## Dependencies and interpreter

ROS dependencies are declared in `package.xml`. The root `uv.lock` supplies
application/provider/audio dependencies for the Pi installation. The Pi installer
runs colcon using the dependency virtualenv's Python so the installed node
shebangs select that interpreter. Sourcing the workspace makes the package
available to the system ROS CLI and launch parser; users run ordinary `ros2`
commands without activating the virtualenv or setting PYTHONPATH manually.

A direct developer build uses the Python interpreter running colcon; install the
required dependencies in that interpreter first. On the Pi use the native
installer or the documented native build command; on the Mac use the existing
Pixi workspace tasks. Keep native ROS and Python ABI versions compatible.

The root wheel still exposes the existing standalone `orbo`/audio command names
for application development without ROS. ROS executable names and their ament
index are supplied by this package's `setup.py`/`setup.cfg`.

See the [Pi quickstart](../../../PI5_QUICKSTART.md),
[workspace guide](../../README.md), and
[application module guide](../../../packages/orbo_runtime/README.md).
