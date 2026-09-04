#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/trading-bot"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$ROOT/v84_release_$STAMP"
mkdir -p "$DEST"
cp -a "$SRC/v84" "$DEST/"
cp "$SRC/trading_bot_v84.py" "$SRC/v84_strategy.py" "$SRC/v84_preflight.py" "$SRC/v84_dry_run.py" "$SRC/README_V84_PRODUCTION.md" "$DEST/"
cp -a "$SRC/tests" "$DEST/"
cp -a "$SRC/deploy" "$DEST/"
printf 'STAGED_AT=%s\n' "$DEST"
printf 'Current V8.2 service was NOT modified.\n'
