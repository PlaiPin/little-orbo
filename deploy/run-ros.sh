#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
if [[ -n ${ORBO_CAMERA_OVERLAY:-} ]]; then
  if [[ "$ORBO_CAMERA_OVERLAY" != /* || ! -r "$ORBO_CAMERA_OVERLAY/local_setup.bash" ]]; then
    echo 'ORBO_CAMERA_OVERLAY must name a readable absolute camera install directory' >&2
    exit 1
  fi
  source "$ORBO_CAMERA_OVERLAY/local_setup.bash"
fi
set -u
exec /opt/orbo/.venv/bin/python /opt/ros/jazzy/bin/ros2 "$@"
