#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/trading-bot"
RELEASE_DIR="${1:?Usage: $0 /home/ubuntu/trading-bot/v84_release_YYYYMMDD-HHMMSS}"
[ -d "$RELEASE_DIR" ] || { echo "Release not found"; exit 2; }
cat <<EOF
V8.4 PROMOTION IS MANUAL.
Release: $RELEASE_DIR
Before promotion verify:
  1) ./v84_preflight.py passes
  2) ./v84_dry_run.py passes
  3) pytest passes
  4) Dhan static-IP preflight passes
  5) V82 live service is stopped before enabling V84 live
  6) V84_ENABLE_LIVE=1 and V82_DRY_RUN=0 are explicitly set
No service was changed by this script.
EOF
