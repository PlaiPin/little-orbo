#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")/.."
source ros_ws/install/macos/local_setup.bash
set -u
export ROS_DOMAIN_ID=95 GZ_PARTITION=little-orbo-sim
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_STATIC_PEERS="" SKIP_DEFAULT_XML=1
unset ROS_DISCOVERY_SERVER ROS_SUPER_CLIENT ROS_LOCALHOST_ONLY
unset FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE
# Gazebo must find the ROS control system dylib in the Pixi prefix.
export GZ_SIM_SYSTEM_PLUGIN_PATH="${CONDA_PREFIX}/lib:${CONDA_PREFIX}/lib/gz-sim-8/plugins"
if [[ "${1:-}" == test ]]; then
  shift
  export ROS_DOMAIN_ID=99 GZ_PARTITION=little-orbo-sim-test ORBO_GAZEBO_TESTS=1
  exec python -m pytest -q tests/simulation "$@"
fi
if [[ "${1:-}" == drive ]]; then
  shift
  exec ros2 run orbo_simulation drive "$@"
fi
export ORBO_GZ_RUBY="$(python tools/prepare-gazebo-macos.py)"
if [[ "${1:-}" == view ]]; then
  exec "$ORBO_GZ_RUBY" "$(command -v gz)" sim -g -v 3 --render-engine-gui-api-backend opengl --gui-config ros_ws/src/orbo_simulation/config/gui.config
fi
exec ros2 launch orbo_simulation room.launch.py "$@"
