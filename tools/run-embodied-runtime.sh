#!/usr/bin/env bash
# Interactive Gazebo-only runtime; start the gated room in another terminal.
set -eo pipefail
cd "$(dirname "$0")/.."
source ros_ws/install/macos/local_setup.bash
export PYTHONPATH="$PWD/ros_ws/src/orbo_runtime:${PYTHONPATH:-}"
export ROS_DOMAIN_ID=95 GZ_PARTITION=little-orbo-sim
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_STATIC_PEERS="" SKIP_DEFAULT_XML=1
unset ROS_DISCOVERY_SERVER ROS_SUPER_CLIENT ROS_LOCALHOST_ONLY
unset FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE
exec python -m orbo_runtime.cli serve --config "${1:-deploy/profiles/gazebo-embodied.toml}"
