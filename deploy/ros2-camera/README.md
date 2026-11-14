# Pi 5 OV5647 ROS 2 camera setup

`setup-camera.sh` targets Ubuntu 24.04 ARM64, ROS 2 Jazzy installed under
`/opt/ros/jazzy`, and an OV5647 already working with `rpicam-hello`.
It runs on the Pi, as your normal user. Stop any other camera capture program first.

Copy the standalone script from this directory on the Mac (replace the SSH target):

```bash
scp setup-camera.sh YOUR_PI_USER@YOUR_PI_HOST:~/setup-camera.sh
ssh YOUR_PI_USER@YOUR_PI_HOST
bash ~/setup-camera.sh
```

The default run installs build dependencies using sudo, checks the existing
libcamera development files, builds the pinned `camera_ros` 0.8.1 commit
`9e94b5b6d6cacbe77dc893d54735f06b4a2fd16f` in `~/camera_ws`, and starts the
publisher in the foreground. Ctrl-C stops it. It does not install a boot service.

Later, skip APT and the build:

```bash
bash ~/setup-camera.sh --start-only
```

Other options:

```bash
# No package installation or build; requires pkg-config and g++ already installed.
bash ~/setup-camera.sh --check-only

# Prepare the workspace without running the publisher.
bash ~/setup-camera.sh --build-only

# Lower requested resolution, or use a separate workspace.
bash ~/setup-camera.sh --start-only --width 640 --height 480
bash ~/setup-camera.sh --workspace ~/ov5647_ws

# If the working libcamera metadata is outside pkg-config's search path:
bash ~/setup-camera.sh --libcamera-pc-dir /path/to/working/lib/pkgconfig
```

Repeat `--workspace` on later runs if you selected a custom workspace. The script
inherits `ROS_DOMAIN_ID`, discovery settings, and the loader environment. It does
not modify shell startup files or choose a new ROS domain.

## Checks and library compatibility

- Confirms the OS, architecture, board, ROS installation and OV5647 enumeration.
- Records the actual shared libcamera and libcamera-base files used by rpicam.
- Checks APT's proposed dependency changes and refuses a plan that modifies
  libcamera, libpisp, rpicam or ROS camera-stack packages. It does not run a system upgrade.
- Compiles a small probe using the selected headers and pkg-config flags; checks
  its runtime library paths against rpicam, and metadata versus runtime version.
- Checks the built ROS component's library paths before starting it.

There is no automatic libcamera rebuild or `libcamera-dev` installation. If the
runtime works but headers or `libcamera.pc` are missing, obtain development files
from the same package provider or source build used to install that runtime.
`--libcamera-pc-dir` selects metadata; it does not repair a mismatched
`LD_LIBRARY_PATH`. On a mismatch, try a fresh shell without old ROS/camera
overlays and inspect the printed paths.

Matching version strings alone cannot prove headers came from the same vendor
build. Keep the matching development package/source provenance. The probe checks
linkage, not successful image acquisition; the final publisher/subscriber check
below establishes that frames actually arrive.

The existing camera_ros source checkout must be clean and at the pinned commit.
The script does not reset or update existing source; choose a new workspace if
you already have a different camera_ros checkout. The explicit dependency list
is for the pinned release; update it if changing that pin.

## Verify and view

In a second Pi terminal, using the same ROS environment/domain:

```bash
source /opt/ros/jazzy/setup.bash
ros2 topic list -t
ros2 topic info /camera/image_raw --verbose
ros2 topic echo /camera/camera_info --once
ros2 topic hz /camera/image_raw
```

The rate command runs continuously; press Ctrl-C to stop that subscriber. The
publisher in the first terminal continues running. To check compressed delivery,
run `ros2 topic hz /camera/image_raw/compressed` and stop it with Ctrl-C as well.

Expected image topics are `/camera/image_raw` (`sensor_msgs/msg/Image`),
`/camera/image_raw/compressed` (`sensor_msgs/msg/CompressedImage`) and
`/camera/camera_info` (`sensor_msgs/msg/CameraInfo`). Compression work is driven
by subscribers. H.264 is not required. Resolution is requested, not guaranteed;
inspect the negotiated configuration in the node's startup log.

In RViz on a ROS 2 Jazzy desktop, add an **Image** display and select
`/camera/image_raw`. Best Effort reliability can receive from either a reliable
or best-effort publisher. For compressed transport, install
`ros-jazzy-compressed-image-transport` on the viewing machine and select
`compressed`. Remote viewers need matching ROS domains and working network
discovery. This script does not configure the Mac or its network.

Missing calibration warnings do not prevent a basic image preview. Calibrate the
camera and configure TF before using geometry-dependent vision or RViz Camera
overlays. A camera-busy error means another process owns the camera; stop that
process and rerun `--start-only`.

## Sources and validation scope

- [camera_ros upstream instructions](https://github.com/christianrauch/camera_ros)
- [Pinned build configuration](https://github.com/christianrauch/camera_ros/blob/9e94b5b6d6cacbe77dc893d54735f06b4a2fd16f/CMakeLists.txt)
- [Pinned package dependencies](https://github.com/christianrauch/camera_ros/blob/9e94b5b6d6cacbe77dc893d54735f06b4a2fd16f/package.xml)

Local validation covers shell syntax, argument/platform checks, and mocked
dependency/linkage/build failure paths. Hardware capture, compilation on the
Pi, DDS transport and RViz rendering must still be validated on the actual Pi.
