#!/usr/bin/env bash
set -euo pipefail
ORBO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${IDF_PATH:?Source the locked ESP-IDF v6.0.3 export.sh first}"
exec python "$ORBO_ROOT/tools/flash-firmware.py" "$@"
