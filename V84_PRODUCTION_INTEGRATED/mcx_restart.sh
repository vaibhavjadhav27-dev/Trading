#!/bin/bash
# Force-restart MCX shadow with a fresh token (fixes token-401-death).
# Kills any existing --mcx process, then launches a new one that reads
# the freshly-refreshed SSM token at startup.
cd /home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED
pkill -f "shadow_orchestrator.py --mcx" 2>/dev/null
sleep 3
nohup /home/ubuntu/trading-bot/venv/bin/python3 shadow_orchestrator.py --mcx >> /home/ubuntu/trading-bot/logs/shadow_orchestrator.log 2>&1 &
echo "$(date -u +'%Y-%m-%d %H:%M:%S UTC') MCX force-restarted (fresh token)" >> /home/ubuntu/trading-bot/logs/mcx_restart.log
