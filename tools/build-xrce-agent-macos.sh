#!/usr/bin/env bash
# Native serial test agent for the ESP32; does not start a controller or flash.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  printf 'This helper targets native Apple silicon macOS.\n' >&2
  exit 1
fi
if [[ "${CONDA_PREFIX:-}" != "$ROOT/.pixi/envs/default" ]]; then
  exec pixi run --locked bash "$ROOT/tools/build-xrce-agent-macos.sh"
fi
python tools/bootstrap-agent.py
ORBO_AGENT_SOURCE="$ROOT/.sdk/agent-locked/xrce_agent"
ORBO_AGENT_PREFIX="$ROOT/.sdk/macos-agent/install"
ORBO_LOGGER_PREFIX="$ROOT/.sdk/macos-agent/logger"
ORBO_AGENT_PATCH="$ROOT/deploy/patches/xrce-agent-system-clock.patch"
if ! git -C "$ORBO_AGENT_SOURCE" apply --reverse --check "$ORBO_AGENT_PATCH" 2>/dev/null; then
  git -C "$ORBO_AGENT_SOURCE" apply --check "$ORBO_AGENT_PATCH"
  git -C "$ORBO_AGENT_SOURCE" apply "$ORBO_AGENT_PATCH"
fi
cmake -S .sdk/agent-locked/spdlog -B .sdk/macos-agent/spdlog-build \
  -DCMAKE_INSTALL_PREFIX="$ORBO_LOGGER_PREFIX" -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DSPDLOG_BUILD_SHARED=ON -DSPDLOG_BUILD_EXAMPLE=OFF -DSPDLOG_BUILD_TESTS=OFF
cmake --build .sdk/macos-agent/spdlog-build -j 6
cmake --install .sdk/macos-agent/spdlog-build
cmake -S "$ORBO_AGENT_SOURCE" -B .sdk/macos-agent/xrce-build \
  -DCMAKE_INSTALL_PREFIX="$ORBO_AGENT_PREFIX" \
  -DCMAKE_PREFIX_PATH="$ORBO_LOGGER_PREFIX;$CONDA_PREFIX" \
  -Dspdlog_DIR="$ORBO_LOGGER_PREFIX/lib/cmake/spdlog" \
  -DCMAKE_NO_SYSTEM_FROM_IMPORTED=ON \
  -DUAGENT_SUPERBUILD=OFF -DUAGENT_USE_SYSTEM_FASTDDS=ON \
  -DUAGENT_USE_SYSTEM_FASTCDR=ON -DUAGENT_USE_SYSTEM_LOGGER=ON \
  -DUAGENT_P2P_PROFILE=OFF -DUAGENT_SOCKETCAN_PROFILE=OFF -DUAGENT_CED_PROFILE=OFF
cmake --build .sdk/macos-agent/xrce-build -j 6
cmake --install .sdk/macos-agent/xrce-build
printf '\nBuilt native XRCE-DDS serial agent. Run tools/run-xrce-agent-macos.sh DEVICE.\n'
