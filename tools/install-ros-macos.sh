#!/usr/bin/env bash
# Install the repository's locked native ROS environment; never source this file.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: %s\nInstalls Pixi with Homebrew if needed, restores pixi.lock, and checks local ROS messaging.\n' "$0"
  exit 0
fi
if [[ $# -ne 0 ]]; then
  printf 'Unexpected arguments. Use --help.\n' >&2
  exit 2
fi
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  printf 'This environment targets native Apple silicon macOS. Run from an ARM64 terminal, outside Rosetta.\n' >&2
  exit 1
fi
if [[ "$(sw_vers -productVersion | cut -d. -f1)" -lt 14 ]]; then
  printf 'RoboStack requires macOS 14 or newer.\n' >&2
  exit 1
fi
if [[ -n "${CONDA_PREFIX:-}" || -n "${VIRTUAL_ENV:-}" || -n "${ROS_DISTRO:-}" || -n "${AMENT_PREFIX_PATH:-}" ]]; then
  printf 'Use a fresh terminal without an activated Conda, venv, Pixi, or ROS environment.\n' >&2
  exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
  printf 'Install Apple command-line tools with xcode-select --install, then rerun this script.\n' >&2
  exit 1
fi
if [[ ! -f "$ROOT/pixi.lock" ]]; then
  printf 'Missing pixi.lock. Restore it from the repository before installing.\n' >&2
  exit 1
fi
if ! command -v pixi >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    printf 'Install Pixi (https://pixi.sh/latest/installation/) or Homebrew first, then rerun.\n' >&2
    exit 1
  fi
  HOMEBREW_NO_INSTALL_CLEANUP=1 brew install pixi
fi

cd "$ROOT"
pixi --version
pixi install --locked
pixi run --locked ros-check
printf '\nROS 2 Jazzy is ready. From %s:\n  pixi run ros2 --help\n  pixi run rviz\n  pixi shell\n' "$ROOT"
