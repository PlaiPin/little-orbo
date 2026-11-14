#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")/.."
source ros_ws/install/macos/local_setup.bash
export PYTHONPATH="$PWD/ros_ws/src/orbo_runtime:${PYTHONPATH:-}"
export ROS_DOMAIN_ID=100 GZ_PARTITION=little-orbo-embodied-test ORBO_GAZEBO_TESTS=1
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_STATIC_PEERS="" SKIP_DEFAULT_XML=1
export GZ_SIM_SYSTEM_PLUGIN_PATH="${CONDA_PREFIX}/lib:${CONDA_PREFIX}/lib/gz-sim-8/plugins"
export GZ_SIM_RESOURCE_PATH="$PWD/ros_ws/src/orbo_simulation/models:${GZ_SIM_RESOURCE_PATH:-}"
unset ROS_DISCOVERY_SERVER ROS_SUPER_CLIENT ROS_LOCALHOST_ONLY
unset FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE
exec python tools/gazebo-near-live.py "$@"
