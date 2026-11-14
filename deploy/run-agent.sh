#!/usr/bin/env bash
set -euo pipefail
set +u
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
set -u
: "${ORBO_SERIAL_DEVICE:?Set the commissioned /dev/serial/by-id device in /etc/orbo/agent.env}"
exec ros2 run micro_ros_agent micro_ros_agent serial --dev "$ORBO_SERIAL_DEVICE" -b 115200
