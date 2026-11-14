#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -ne 1 || ! -c "$1" ]]; then
  printf 'Usage: %s /dev/cu.ACTUAL_CDC0_DEVICE\n' "$0" >&2
  exit 2
fi
cd "$ROOT"
exec pixi run --locked env \
  DYLD_LIBRARY_PATH="$ROOT/.sdk/macos-agent/logger/lib:$ROOT/.sdk/macos-agent/install/lib:$ROOT/.pixi/envs/default/lib" \
  "$ROOT/.sdk/macos-agent/install/bin/MicroXRCEAgent" serial --dev "$1" -v 4
