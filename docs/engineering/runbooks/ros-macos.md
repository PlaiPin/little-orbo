# Native ROS 2 Jazzy on macOS

Little Orbo uses RoboStack packages managed by Pixi for native ROS development on
Apple silicon. The environment lives in `.pixi/` at the repository root. The
manifest is `pixi.toml`; `pixi.lock` records the exact packages and checksums.
Ubuntu 24.04 on the Raspberry Pi remains the deployment target.

## Install

Requirements:

- Apple silicon Mac, running macOS 14 or newer, with a native ARM64 terminal.
- Apple command-line tools (`xcode-select --install` if missing).
- Homebrew, or Pixi 0.84.2 or newer on `PATH`.
- Internet access and space for the desktop ROS packages and package cache.

From a fresh terminal, without an activated Python/Conda/ROS environment:

```sh
cd /path/to/little-orbo
./tools/install-ros-macos.sh
```

The script checks the host, installs Pixi through Homebrew if necessary, restores
the environment using `pixi install --locked`, and runs the messaging check. It
can be rerun: it reuses the environment and does not update the lockfile. It does
not edit shell startup files or install ROS into the system Python.

The manifest currently targets `osx-arm64` only. An Intel Mac needs its own
platform added and resolved before use; the installer intentionally rejects it.

## Everyday commands

Run these from the repository root:

```sh
pixi run ros2 --help
pixi run ros-check
pixi run rviz
pixi shell
# Inside the activated shell:
ros2 pkg list
exit
```

For a manual demo, run `pixi run ros-talker` in one terminal and
`pixi run ros-listener` in another. Stop each with Ctrl-C. These manual tasks use
your normal ROS discovery settings; use `pixi run ros-check` for the isolated
local test.

The automated check imports `rclpy`, checks the CLI, and starts the C++ demo
publisher and Python demo subscriber. It requires a received message within
120 seconds (allowing for slow first launches), uses Fast DDS on localhost/domain
93 and a unique topic, and shuts
down its process groups on success or failure. It does not use the ROS daemon,
connect to the robot, or start any Orbo control nodes.

## Build Orbo messages

```sh
pixi run ros-build-interfaces
pixi shell
source ros_ws/install/macos/local_setup.zsh
ros2 interface show orbo_embedded_interfaces/msg/Health
python - <<'PY'
import orbo_embedded_interfaces.msg as messages
from rclpy.serialization import serialize_message, deserialize_message

for name in (
    "ControlSession", "WheelCommand", "FlipperCommand",
    "WheelState", "ActuatorStatus", "Health",
):
    message_type = getattr(messages, name)
    original = message_type()
    assert deserialize_message(serialize_message(original), message_type) == original
    print(f"PASS: {name} native typesupport and serialization")
PY
exit
```

Use `local_setup.bash` instead if your shell is Bash. This task builds only
`orbo_embedded_interfaces`; it keeps build products in `ros_ws/build/macos`,
`ros_ws/install/macos`, and `ros_ws/log/macos`. It does not build or start the Pi
hardware, camera, audio, navigation, or control stack.

## Build the complete workspace

```bash
pixi install --locked
pixi run --locked ros-build-workspace
pixi run --locked ros-test-integrations
```

This includes the navigation package and checks installed launch configuration
and synthetic peripheral/navigation traffic in domain 96. It does not start
physical drivers or motion. See the [workspace guide](../../../ros_ws/README.md)
for TF conventions, package roles and the Pi validation boundary.

## Test an ESP32-S3-Nano

For the physical ESP32-S3-Nano, use the [firmware README](../../../firmware/orbo/README.md).
It includes the native XRCE-DDS serial bridge, its macOS epoch-clock fix, and
the distinction between wheel-enabled and historical bare-board tests.
The general `ros-check` task above does not communicate with the board.

## View Orbo in RViz

From the repository root:

```sh
pixi run orbo-preview
```

This builds `orbo_description`, sources its macOS overlay, and starts
`robot_state_publisher` and RViz. Close RViz (or press Ctrl-C in the launching
terminal) to stop the preview and its transform publisher. Close an existing
preview before starting another.

The preview now uses the complete v9 STEP assembly: chassis, shells, flippers,
wheels, equipment and the added display mount. All 176 STEP assembly entries are
exported into 13 mesh groups by `tools/build-orbo-v9-description.py`. CMake installs
the mesh directory. The source hash and part mapping live beside the meshes.
CAD millimetres are converted to ROS metres, with the origin at the wheel axle
midpoint and forward +X, left +Y, up +Z.

To inspect the internal structure with the outer shells hidden:

```sh
pixi run --locked bash -c 'source ros_ws/install/macos/local_setup.bash && python tools/preview-orbo-macos.py --internal'
```

These CAD-only previews have fixed joints and no controllers. For measured wheel
joint motion and odometry, use the [Mac control-stack runbook](ros-control-macos.md)
and `pixi run --locked rviz-live`. Physical flippers remain disabled; their
visuals are fixed CAD references. Neither view establishes physical calibration.

The macOS helper makes an ignored `.pixi/Orbo RViz.app` bundle around the installed
RViz executable so macOS and UI inspection can identify its window. Launch it
through the Pixi task so ROS and library paths are activated. The preview uses
localhost discovery and ROS domain 94. On another ROS desktop, after building
and sourcing the description package, the generic entry point is:

```sh
ros2 launch orbo_description preview.launch.py
```

Both the full v9 exterior and shell-hidden structure were visually checked in
native RViz with model status OK. The earlier two-mesh shell-only preview has
been replaced.

## Python and package management

The ROS environment pins Python 3.12 and includes the ROS desktop metapackage and
`ros-dev-tools`. Use its Python through `pixi run python`, not the repository's
`.venv/bin/python`, when importing `rclpy` or generated ROS messages.

Pixi installs the local `orbo-runtime` package editable, including its console
commands and core Python dependencies. No manual `PYTHONPATH` is needed. The
optional cloud/audio extras remain outside the default ROS environment. The
`uv` workflow manages application development with those extras separately. Avoid
activating the two environments together. Do not source `/opt/ros/...` or another
ROS installation into the Pixi environment.

To add a ROS dependency, first check its macOS availability in RoboStack, then:

```sh
pixi add ros-jazzy-<package>
pixi run ros-check
```

`apt install` instructions from Ubuntu tutorials do not apply to this environment.
If a package or hardware driver is Linux-only, run it on the Ubuntu target.

## Updating and reproducing

Keep `pixi.toml` and `pixi.lock` together in version control. `.pixi/` is ignored
by Git and Docker; it is a local environment, not a distributable artifact. The
source packaging tool includes the manifest and lockfile on its next run. Existing
release archives were not regenerated as part of this workstation setup.

Normal setup uses the existing lock. Maintainers can deliberately resolve an
edited manifest with `pixi lock`, or update dependencies with `pixi update`.
Afterward run the messaging check and interface build and review both manifest
and lock changes. Homebrew-installed Pixi is updated with `brew upgrade pixi`.

## Troubleshooting

- **Installer rejects an active environment:** use `exit`, `deactivate`, or
  `conda deactivate` as appropriate, or open a fresh terminal. Remove any
  automatic ROS sourcing from that session.
- **Solver reports an unsupported macOS version:** keep the manifest's
  platform entry with `macos = "14.0"`; it tells the solver the minimum OS
  supported by this workspace.
- **Imports fail:** run `pixi install --locked` and use `pixi run python`.
  Check for inherited `PYTHONPATH`, `PYTHONHOME`, or external ROS setup scripts.
- **Messaging times out:** the checker prints both processes' logs. Check local
  networking permissions and firewall/VPN settings. The test uses localhost;
  passing it does not establish communication with a robot over Wi-Fi.
- **RViz fails to open:** launch from a logged-in graphical macOS session and
  inspect its terminal output. The messaging check does not verify graphics.

## Sources

- [RoboStack getting started](https://robostack.github.io/GettingStarted.html)
- [RoboStack Jazzy package availability](https://robostack.github.io/jazzy.html)
- [Pixi installation](https://pixi.sh/latest/installation/)
- [Pixi ROS 2 tutorial](https://pixi.sh/latest/tutorials/ros2/)

## Verification limits

The isolated messaging check and the control-stack tests establish software
behavior on the Mac. They do not establish Pi connectivity, motor power, USB
or motor-bus integrity, physical stopping distance, or floor-odometry accuracy.
See the control-stack runbook for the current physical-test boundary.
