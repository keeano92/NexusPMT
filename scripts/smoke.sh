#!/usr/bin/env sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python -m pytest tests/smoke tests/unit -q
echo "SMOKE PASS"
