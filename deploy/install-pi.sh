#!/usr/bin/env bash
# Run only on the intended Pi after Ubuntu 24.04 / ROS 2 Jazzy is installed.
set -euo pipefail
export LC_ALL=C
ORBO_STAGING=false
ORBO_CAMERA_STACK=existing
ORBO_INSTALL_SYSTEMD=false
while (($#)); do
  case "$1" in
    --staging) ORBO_STAGING=true; shift ;;
    --install-systemd) ORBO_INSTALL_SYSTEMD=true; shift ;;
    --camera-stack)
      if [[ ${2:-} != existing && ${2:-} != ros ]]; then
        echo '--camera-stack requires existing or ros' >&2; exit 1
      fi
      ORBO_CAMERA_STACK=$2; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--staging] [--camera-stack existing|ros] [--install-systemd]"
      echo 'Default: preserve existing camera packages; build camera_ros with deploy/ros2-camera/setup-camera.sh.'
      echo 'Use ros only when deliberately selecting the apt ROS camera/libcamera stack.'
      echo 'Systemd units are installed only with --install-systemd; services are never started or enabled.'
      exit 0 ;;
    *) echo "Unknown argument: $1 (use --help)" >&2; exit 1 ;;
  esac
done
if [[ $(id -u) != 0 ]]; then echo "Run with sudo on the intended Pi" >&2; exit 1; fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || $(uname -m) != aarch64 ]]; then
  echo "Requires Ubuntu 24.04 ARM64" >&2; exit 1
fi
if [[ ! -f /opt/ros/jazzy/setup.bash ]]; then echo "Install ROS 2 Jazzy from its official Ubuntu packages first" >&2; exit 1; fi
if [[ -d /run/systemd/system ]]; then
  for ORBO_UNIT in orbo-micro-ros-agent orbo-control orbo-runtime orbo-k1-runtime orbo-teleop orbo-navigation orbo-camera; do
    if systemctl is-active --quiet "$ORBO_UNIT"; then
      echo "Stop $ORBO_UNIT and all motion helpers before updating the installation." >&2
      exit 1
    fi
  done
fi
ORBO_SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
apt-get update
ORBO_PACKAGES=(python3.12-venv python3-pip python3-setuptools python3-colcon-common-extensions python3-rosdep
  libportaudio2 portaudio19-dev build-essential git rsync
  ros-jazzy-ros2-control ros-jazzy-ros2-controllers ros-jazzy-xacro
  ros-jazzy-robot-state-publisher ros-jazzy-navigation2 ros-jazzy-nav2-bringup
  ros-jazzy-v4l2-camera ros-jazzy-joy ros-jazzy-image-transport-plugins v4l-utils alsa-utils
  ros-jazzy-ament-lint-auto ros-jazzy-ament-cmake-gtest)
if [[ "$ORBO_CAMERA_STACK" == ros ]]; then
  ORBO_PACKAGES+=(ros-jazzy-camera-ros)
else
  ORBO_APT_PLAN=$(apt-get -s install --no-install-recommends "${ORBO_PACKAGES[@]}")
  if printf '%s\n' "$ORBO_APT_PLAN" | awk '$1 == "Inst" || $1 == "Remv" {print $2}' |
      grep -E '^(libcamera|libpisp|rpicam|ros-[^-]+-(libcamera|camera-ros))' >/dev/null; then
    printf '%s\n' "$ORBO_APT_PLAN" >&2
    echo 'APT would modify the existing camera stack; resolve the dependency conflict first.' >&2
    exit 1
  fi
fi
apt-get install -y --no-remove --no-install-recommends "${ORBO_PACKAGES[@]}"
id orbo >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/orbo --groups audio,dialout,video,input orbo
usermod -a -G audio,dialout,video,input orbo
install -d -m 0755 /opt/orbo /etc/orbo
install -d -o orbo -g orbo -m 0700 /var/lib/orbo
if [[ "$ORBO_SOURCE" != /opt/orbo ]]; then
  rsync -a --include '/.env.example' --exclude .git --exclude .sdk --exclude .venv --exclude .pixi --exclude artifacts \
    --exclude .live-validation --exclude credentials.env \
    --exclude .env --exclude '.env.*' --exclude .DS_Store --exclude secrets --exclude '*.db' --exclude '*.db-*' \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    --exclude 'build-*' --exclude managed_components --exclude sdkconfig.old \
    --exclude build --exclude install --exclude log "$ORBO_SOURCE/" /opt/orbo/
fi
cd /opt/orbo
python3.12 -m venv --system-site-packages .venv
.venv/bin/pip install uv==0.9.18
.venv/bin/uv export --frozen --all-extras --no-dev --no-emit-project -o /tmp/orbo-runtime-requirements.txt
.venv/bin/uv pip install --python .venv/bin/python --require-hashes -r /tmp/orbo-runtime-requirements.txt
.venv/bin/uv pip install --python .venv/bin/python --no-deps .
set +u
source /opt/ros/jazzy/setup.bash
set -u
.venv/bin/python -c 'import rclpy, nearai_inference_sdk, sounddevice, websockets'
python3 tools/bootstrap-agent.py
cd ros_ws
export CMAKE_BUILD_PARALLEL_LEVEL=2 MAKEFLAGS=-j2
# Colcon's Python interpreter becomes the ament_python executable shebang.
# ROS CLI/launch can use the system Python; nodes use the locked dependency env.
../.venv/bin/python -m colcon build --executor sequential --merge-install --base-paths src ../.sdk/agent-locked/micro_ros_agent/micro_ros_agent ../.sdk/agent-locked/micro_ros_msgs --cmake-args -DCMAKE_BUILD_TYPE=Release -DUAGENT_USE_SYSTEM_LOGGER=OFF -DBUILD_TESTING=OFF
cd ..
[[ -f /etc/orbo/runtime.toml ]] || install -m 0600 deploy/profiles/pi.toml /etc/orbo/runtime.toml
[[ -f /etc/orbo/booster-k1.toml ]] || install -m 0600 deploy/profiles/booster-k1.toml /etc/orbo/booster-k1.toml
[[ -f /etc/orbo/embodied.toml ]] || install -m 0600 deploy/profiles/embodied.toml /etc/orbo/embodied.toml
[[ -f /etc/orbo/camera.yaml ]] || install -m 0600 ros_ws/src/orbo_bringup/config/camera.template.yaml /etc/orbo/camera.yaml
chown orbo:orbo /etc/orbo/camera.yaml
chown orbo:orbo /etc/orbo/runtime.toml /etc/orbo/booster-k1.toml /etc/orbo/embodied.toml
if [[ "$ORBO_INSTALL_SYSTEMD" == true ]]; then
  install -m 0644 deploy/systemd/*.service /etc/systemd/system/
  if [[ "$ORBO_STAGING" == false ]]; then
    systemctl daemon-reload
  else
    echo "Staging validation: units installed; systemd reload and service startup not performed"
  fi
fi
# Existing units/configuration/data are preserved on upgrade; no service starts.
printf '%s\n' 'Installation complete; source Jazzy and ros_ws/install/local_setup.bash, then follow deploy/README.md.'
