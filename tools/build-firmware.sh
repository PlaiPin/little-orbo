#!/usr/bin/env bash
set -eo pipefail
ORBO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
: "${IDF_PATH:?Source the locked ESP-IDF v6.0.3 export.sh first}"
ORBO_SDK_DIR=${ORBO_SDK_DIR:-"$ORBO_ROOT/.sdk/locked-build"}
python3 "$ORBO_ROOT/tools/bootstrap-firmware.py" --sdk-dir "$ORBO_SDK_DIR"
export ORBO_MICROROS_COMPONENT="$ORBO_SDK_DIR/micro_ros_espidf_component"
python - "$IDF_PATH" <<'PY'
import pathlib, sys
s=(pathlib.Path(sys.argv[1])/'tools/cmake/version.cmake').read_text()
assert 'IDF_VERSION_MAJOR 6' in s and 'IDF_VERSION_MINOR 0' in s and 'IDF_VERSION_PATCH 3' in s, 'ESP-IDF version differs from lock'
PY
# No flash action: confirm board, flash size, USB and calibration first.
idf.py -C "$ORBO_ROOT/firmware/orbo" -B "$ORBO_ROOT/build-locked" build
