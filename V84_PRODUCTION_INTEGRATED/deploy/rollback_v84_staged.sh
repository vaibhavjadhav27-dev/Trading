#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/trading-bot"
if [ -z "${V84_RELEASE_DIR:-}" ]; then echo "Set V84_RELEASE_DIR=/home/ubuntu/trading-bot/v84_release_YYYYMMDD-HHMMSS"; exit 2; fi
[ -d "$V84_RELEASE_DIR" ] || { echo "Release not found: $V84_RELEASE_DIR"; exit 2; }
rm -rf "$V84_RELEASE_DIR"
echo "Removed staged V8.4 release. Existing V8.2 service was not modified."
