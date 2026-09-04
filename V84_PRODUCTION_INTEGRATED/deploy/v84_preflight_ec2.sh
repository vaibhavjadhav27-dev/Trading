#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/trading-bot"
cd "$ROOT"
PY="${PYTHON_BIN:-$ROOT/venv/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"
echo "Using Python: $PY"
"$PY" -m py_compile trading_bot_v84.py v84_strategy.py v84/*.py
"$PY" v84_preflight.py
"$PY" v84_dry_run.py
"$PY" -m pytest -q tests/test_v84_integration.py
printf '\nV84_EC2_PREFLIGHT_PASS\n'
printf 'NO LIVE ORDERS WERE PLACED BY THIS SCRIPT.\n'
