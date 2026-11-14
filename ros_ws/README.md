# Orbo ROS workspace

| Package | Purpose |
|---|---|
| `orbo_embedded_interfaces` | Host/firmware message definitions |
| `orbo_hardware` | ros2_control plugin using measured ESP32 wheel feedback |
| `orbo_description` | Complete v9 visuals, wheel joints, calibration templates, RViz views |
| `orbo_bringup` | Control, camera and application teleop launch files |
| `orbo_navigation` | Calibration-gated Nav2 configuration and velocity gate |

All these package sources are versioned. Build/install/log directories are local.
Only `meshes/v9` is installed; obsolete `meshes/v05` exports are ignored and excluded
from installation even if they remain on a developer's disk.

## Mac setup and software checks

From the repository root, outside an ESP-IDF or uv environment:

```bash
pixi install --locked
pixi run --locked ros-build-workspace
pixi run --locked ros-test-integrations
pixi run --locked ros-test-control
```

Pixi supplies Jazzy, ros2_control, Nav2, the installed editable `orbo-runtime`
package and the async test plugin. The workspace build includes navigation;
`ros-build-control` remains available for the narrower base-control build.
The installed runtime provides Python imports and command-line entry points
without a `PYTHONPATH` workaround.

`ros-test-integrations` checks installed launch configuration, camera health,
keyboard expiry, application teleop, the Nav2 action adapter and runtime packaging.
`ros-test-control` exercises measured odometry and control-session loss, including
a 30-second synthetic drive. Both use domain 96 and require no battery or agent.
Neither establishes physical navigation or peripheral acceptance. The latest
8 integration checks passed, but the repeated 30-second control test stopped
early in both the existing and clean-source installations. See the
[control runbook](../docs/engineering/runbooks/ros-control-macos.md#work-without-the-battery)
for this unresolved timing limitation; `ros-test-control` is not currently a
consistently passing sustained-drive check.

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

## Navigation and Pi deployment boundary

Navigation generates parameters from the installed Jazzy defaults and a measured
calibration file. It installs only its `launch` directory; no empty, unversioned
`config` directory is required. It rejects the nominal `bench-cad.yaml` profile.
It requires qualified geometry/limits, a real LaserScan source and sensor frame,
and a surveyed map. A successful launch/configuration test does not supply those.

On the Pi, install Jazzy and the required drivers, install `orbo-runtime` into the
Python environment used to invoke ROS launch, then build this workspace with
colcon. The package manifests declare ROS dependencies; Python packaging supplies
the runtime. Native CSI/V4L2 drivers are Pi/Linux dependencies and are not validated
by the Mac suite. Device profiles, system services, sensor calibration, ground
odometry and stopping behavior still need target-specific validation.
