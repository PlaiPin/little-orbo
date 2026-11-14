#!/usr/bin/env bash
set -euo pipefail
set +u
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
set -u
exec ros2 run orbo_runtime runtime serve --config "${1:-/etc/orbo/runtime.toml}"
