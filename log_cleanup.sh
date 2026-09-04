#!/bin/bash
LOG_DIR="/home/ubuntu/trading-bot/logs"
DAYS=15

echo "$(date) — Log cleanup starting"
find "$LOG_DIR" -type f -name "*.log" -mtime +$DAYS -print -delete
find "$LOG_DIR" -type f -name "*.log.*" -mtime +$DAYS -print -delete
find "/tmp" -type f -name "trading_review_*.tgz" -mtime +$DAYS -print -delete
echo "$(date) — Log cleanup done"
