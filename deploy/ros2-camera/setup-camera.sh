#!/usr/bin/env bash
# Ubuntu 24.04 ARM64 / Raspberry Pi 5 / ROS 2 Jazzy / working OV5647.
set -Eeuo pipefail
export LC_ALL=C

readonly CAMERA_ROS_COMMIT=9e94b5b6d6cacbe77dc893d54735f06b4a2fd16f
readonly CAMERA_ROS_URL=https://github.com/christianrauch/camera_ros.git
workspace="$HOME/camera_ws"
mode=setup
pc_dir=
width=1280
height=720
camera=0
jobs=2
scratch=

usage() {
  cat <<'EOF'
Usage: bash setup-camera.sh [options]

Default: check camera, install build dependencies, build camera_ros 0.8.1,
and publish in the foreground. Ctrl-C stops the camera.

  --check-only             Check camera and development libraries; no APT/build
  --build-only             Set up and build, but do not start the ROS publisher
  --start-only             Check runtime libraries and start an existing build
  --workspace PATH         Workspace (default: ~/camera_ws)
  --libcamera-pc-dir PATH  Directory containing the working libcamera.pc
  --width N                Output width (default: 1280)
  --height N               Output height (default: 720)
  --camera N               libcamera camera index (default: 0)
  --jobs N                 Build jobs (default: 2)
  -h, --help               Show help

Run as your normal user, NOT with sudo. Setup uses sudo only for APT.
ROS_DOMAIN_ID and ROS discovery settings are inherited, never changed.
Stop other camera applications before running this script.
EOF
}
log() { printf '\n[camera-setup] %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
cleanup() { if [[ -n "$scratch" ]]; then rm -rf -- "$scratch"; fi; }
trap cleanup EXIT
trap 'printf "\nFailed at line %s. See the command output above.\n" "$LINENO" >&2' ERR

while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --check-only|--build-only|--start-only)
      [[ "$mode" == setup ]] || die 'Choose only one mode.'
      mode="${1#--}"; shift ;;
    --workspace|--libcamera-pc-dir|--width|--height|--camera|--jobs)
      (($# >= 2)) || die "Missing value for $1"
      [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
      case "$1" in
        --workspace) workspace="$2" ;;
        --libcamera-pc-dir) pc_dir="$2" ;;
        --width) width="$2" ;;
        --height) height="$2" ;;
        --camera) camera="$2" ;;
        --jobs) jobs="$2" ;;
      esac
      shift 2 ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
done
for value in "$width" "$height" "$jobs"; do
  [[ "$value" =~ ^[1-9][0-9]{0,5}$ ]] || die 'Width, height and jobs must be positive integers (up to six digits).'
done
[[ "$camera" =~ ^(0|[1-9][0-9]{0,3})$ ]] || die 'Camera must be a nonnegative integer index.'

[[ "$(uname -s)" == Linux ]] || die 'Run this script on the Raspberry Pi, not on the Mac.'
[[ "$(uname -m)" == aarch64 ]] || die 'This script targets Ubuntu ARM64 (aarch64).'
[[ "$EUID" != 0 ]] || die 'Run as your normal user, without sudo.'
# shellcheck disable=SC1091
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || die 'Ubuntu 24.04 is required.'
[[ -r /proc/device-tree/model ]] || die 'Cannot identify the Raspberry Pi board.'
model="$(tr -d '\0' </proc/device-tree/model)"
[[ "$model" == *'Raspberry Pi 5'* ]] || die "Expected Raspberry Pi 5, found: $model"
[[ -r /opt/ros/jazzy/setup.bash ]] || die 'ROS 2 Jazzy is not installed at /opt/ros/jazzy.'
[[ -z "${ROS_DISTRO:-}" || "$ROS_DISTRO" == jazzy ]] || die 'Open a fresh shell without another ROS distribution sourced.'
command -v rpicam-hello >/dev/null || die 'rpicam-hello is missing. Restore the working camera stack first.'
command -v python3 >/dev/null || die 'Python 3 is required.'
workspace="$(realpath -m -- "$workspace")"
[[ "$workspace" != / && "$workspace" != "$HOME" ]] || die 'Choose a dedicated workspace directory.'
scratch="$(mktemp -d)"

# Compare resolved files, not just SONAMEs or version strings. Includes libcamera-base.
camera_libraries() {
  local binary="$1" dependencies path
  dependencies="$(ldd "$binary")" || die "Cannot inspect shared libraries: $binary"
  if [[ "$dependencies" == *'not found'* ]]; then
    printf '%s\n' "$dependencies" >&2
    die "Missing runtime dependency for $binary"
  fi
  while IFS= read -r path; do
    readlink -f -- "$path"
  done < <(printf '%s\n' "$dependencies" | awk '$1 ~ /^libcamera(-base)?\.so/ && $2 == "=>" {print $3}') | sort -u
}
assert_libraries() {
  local actual
  actual="$(camera_libraries "$1")"
  if [[ "$actual" != "$baseline_libraries" ]]; then
    printf 'Working rpicam libraries:\n%s\nSelected libraries:\n%s\n' "$baseline_libraries" "$actual" >&2
    die 'libcamera mismatch. Use a fresh shell and the matching development files. Check LD_LIBRARY_PATH and PKG_CONFIG_PATH; do not replace the working camera stack.'
  fi
}

log "Checking $model; rpicam and OV5647 detection"
rpicam_binary="$(command -v rpicam-hello)"
rpicam-hello --version
baseline_libraries="$(camera_libraries "$rpicam_binary")"
[[ -n "$baseline_libraries" ]] || die 'rpicam-hello has no inspectable shared libcamera dependency.'
printf '%s\n' "$baseline_libraries"
timeout 20s rpicam-hello --list-cameras >"$scratch/cameras.txt" 2>&1 || {
  cat "$scratch/cameras.txt"; die 'Camera enumeration failed or timed out.';
}
cat "$scratch/cameras.txt"
grep -qi ov5647 "$scratch/cameras.txt" || die 'OV5647 was not detected; restore rpicam capture before ROS setup.'

# ROS-generated setup scripts may refer to unset variables.
set +u
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
set -u
assert_libraries "$rpicam_binary"

if [[ "$mode" == setup || "$mode" == build-only ]]; then
  log 'Installing build dependencies (camera libraries are excluded)'
  # Explicit dependencies from the pinned camera_ros package.xml/CMakeLists.txt.
  # Avoid rosdep's libcamera resolution and any system-wide libcamera install.
  packages=(git build-essential cmake pkg-config python3-colcon-common-extensions
    ros-jazzy-ament-cmake ros-jazzy-rclcpp ros-jazzy-rclcpp-components
    ros-jazzy-sensor-msgs ros-jazzy-diagnostic-msgs ros-jazzy-camera-info-manager
    ros-jazzy-cv-bridge ros-jazzy-ros2launch ros-jazzy-ament-index-python
    ros-jazzy-image-view ros-jazzy-ros2run ros-jazzy-ros2topic ros-jazzy-ros2param)
  sudo apt-get update
  apt-get -s install --no-install-recommends "${packages[@]}" >"$scratch/apt-plan.txt"
  if awk '$1 == "Inst" || $1 == "Remv" {print $2}' "$scratch/apt-plan.txt" |
      grep -Eq '^(libcamera|libpisp|rpicam|ros-[^-]+-(libcamera|camera-ros))'; then
    cat "$scratch/apt-plan.txt"
    die 'APT would modify camera-stack packages. Resolve the dependency conflict before proceeding.'
  fi
  sudo apt-get install -y --no-remove --no-install-recommends "${packages[@]}"
  assert_libraries "$rpicam_binary"
fi

if [[ "$mode" != start-only ]]; then
  log 'Checking libcamera headers, pkg-config metadata and runtime linkage'
  command -v pkg-config >/dev/null || die 'pkg-config is missing. Run setup mode to install build tools.'
  command -v g++ >/dev/null || die 'g++ is missing. Run setup mode to install build tools.'
  if [[ -n "$pc_dir" ]]; then
    [[ -r "$pc_dir/libcamera.pc" ]] || die "No libcamera.pc in $pc_dir"
    export PKG_CONFIG_PATH="$pc_dir${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  fi
  pkg-config --exists 'libcamera >= 0.1' || die 'Matching libcamera development files are missing. Install headers/.pc files from the SAME source or packages used for your working rpicam installation, or pass --libcamera-pc-dir. Do not blindly install Ubuntu libcamera-dev.'
  pc_version="$(pkg-config --modversion libcamera)"
  libdir="$(pkg-config --variable=libdir libcamera)"
  [[ -d "$libdir" ]] || die "libcamera pkg-config libdir does not exist: $libdir"
  printf 'pkg-config version: %s\nLibrary directory: %s\nMetadata directory: %s\n' \
    "$pc_version" "$libdir" "$(pkg-config --variable=pcfiledir libcamera)"
  cat >"$scratch/probe.cpp" <<'CPP'
#include <libcamera/camera_manager.h>
#include <iostream>
int main() { std::cout << libcamera::CameraManager::version() << '\n'; }
CPP
  # shlex parses pkg-config's escaping without eval or shell word splitting.
  python3 - "$scratch" "$libdir" <<'PY'
import pathlib, shlex, subprocess, sys
root = pathlib.Path(sys.argv[1])
flags = shlex.split(subprocess.check_output(
    ['pkg-config', '--cflags', '--libs', 'libcamera'], text=True))
subprocess.run(['g++', '-std=c++17', str(root / 'probe.cpp'), '-o',
                str(root / 'probe'), '-Wl,-rpath,' + sys.argv[2], *flags], check=True)
PY
  assert_libraries "$scratch/probe"
  runtime_version="$("$scratch/probe")"
  printf 'Loaded libcamera version: %s\n' "$runtime_version"
  python3 - "$pc_version" "$runtime_version" <<'PY'
import re, sys
versions = [re.match(r'v?(\d+\.\d+\.\d+)', value) for value in sys.argv[1:]]
if not all(versions) or versions[0][1] != versions[1][1]:
    sys.exit('ERROR: libcamera pkg-config and runtime versions differ; install matching development files.')
PY
fi

if [[ "$mode" == check-only ]]; then
  log 'Checks passed. No packages installed or workspace built.'
  exit 0
fi

if [[ "$mode" != start-only ]]; then
  log "Building camera_ros 0.8.1 ($CAMERA_ROS_COMMIT) in $workspace"
  mkdir -p "$workspace/src"
  repo="$workspace/src/camera_ros"
  if [[ ! -e "$repo" ]]; then
    git clone --depth 1 --branch 0.8.1 "$CAMERA_ROS_URL" "$repo"
  fi
  [[ -d "$repo/.git" ]] || die "$repo exists but is not a standalone Git checkout. Use a new --workspace."
  [[ "$(git -C "$repo" rev-parse HEAD)" == "$CAMERA_ROS_COMMIT" ]] || die 'Existing camera_ros checkout differs from the pinned commit. Use a new --workspace; existing source is left untouched.'
  [[ -z "$(git -C "$repo" status --porcelain)" ]] || die 'Existing camera_ros checkout contains changes. Use a new --workspace.'
  cd "$workspace"
  # Both Make and Ninja builds are bounded to avoid exhausting Pi memory.
  export MAKEFLAGS="-j$jobs"
  export CMAKE_BUILD_PARALLEL_LEVEL="$jobs"
  colcon build --base-paths "$repo" --packages-select camera_ros \
    --executor sequential --symlink-install --cmake-clean-cache \
    --event-handlers console_direct+ \
    --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
      "-DCMAKE_BUILD_RPATH=$libdir" "-DCMAKE_INSTALL_RPATH=$libdir"
  printf '%s\n' "$CAMERA_ROS_COMMIT" >"$workspace/camera_ros.commit"
fi

[[ -r "$workspace/install/local_setup.bash" ]] || die 'No workspace installation found. Run setup first.'
set +u
# shellcheck disable=SC1091
source "$workspace/install/local_setup.bash"
set -u
component="$workspace/install/camera_ros/lib/libcamera_component.so"
[[ -f "$component" ]] || die "Camera component not found: $component"
assert_libraries "$component"
[[ "$(ros2 pkg prefix camera_ros)" == "$workspace/install/camera_ros" ]] || die 'Another camera_ros installation is taking precedence in the ROS environment.'

if [[ "$mode" == build-only ]]; then
  log 'Build and runtime linkage checks passed. Run this script with --start-only to publish.'
  exit 0
fi

log "Starting camera $camera at requested ${width}x${height}; ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}"
cat <<EOF
Expected topics (confirm receipt from a second terminal):
  /camera/image_raw              sensor_msgs/msg/Image
  /camera/image_raw/compressed   sensor_msgs/msg/CompressedImage
  /camera/camera_info            sensor_msgs/msg/CameraInfo

  source /opt/ros/jazzy/setup.bash
  ros2 topic hz /camera/image_raw

Use the same ROS_DOMAIN_ID/discovery environment in the second terminal.
Check the startup log for the negotiated format/resolution. Ctrl-C stops capture.
A missing calibration-file warning is normal for an uncalibrated camera.
EOF
cleanup
scratch=
exec ros2 run camera_ros camera_node --ros-args \
  -r __node:=camera -r __ns:=/ \
  -p "camera:=$camera" -p role:=viewfinder \
  -p "width:=$width" -p "height:=$height" \
  -p format:=RGB888 -p frame_id:=camera_optical_frame
