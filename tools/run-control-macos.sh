#!/usr/bin/env bash
set -euo pipefail
ORBO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ORBO_ROOT"
exec pixi run --locked bash -c '
  source ros_ws/install/macos/local_setup.bash
  export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
  exec ros2 launch orbo_bringup control.launch.py calibration:="$PWD/ros_ws/src/orbo_description/config/bench-cad.yaml"
'
