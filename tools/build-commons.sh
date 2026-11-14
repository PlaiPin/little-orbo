#!/usr/bin/env bash
set -euo pipefail
ORBO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ORBO_COMMONS_SOURCE=${ORBO_COMMONS_SOURCE:-"$ORBO_ROOT/.sdk/commons-orbo"}
python3 "$ORBO_ROOT/tools/bootstrap-commons.py" --directory "$ORBO_COMMONS_SOURCE"
# Explicit source identity avoids accidentally reporting the enclosing Orbo repo commit.
TRACE_COMMONS_BUILD_COMMIT=$(python3 - "$ORBO_COMMONS_SOURCE" <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);m=json.loads((root/'.orbo-patches.json').read_text())
print(m['revision'][:12]+'-orbo-'+m['patch_sha256'][:16])
PY
)
export TRACE_COMMONS_BUILD_COMMIT
export CARGO_INCREMENTAL=0
export RUSTFLAGS="${RUSTFLAGS:--D warnings}"
cargo +1.96.0 build --manifest-path "$ORBO_COMMONS_SOURCE/Cargo.toml" \
  -p trace-commons-contributor --bin trace-commons-contributor --locked
