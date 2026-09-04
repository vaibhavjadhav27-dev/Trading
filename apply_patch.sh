#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# apply_patch.sh — Safely patches trading_bot.py with 4 lines
# Creates backup first, then applies minimal changes
# ═══════════════════════════════════════════════════════════════════════

cd ~/trading-bot

# Backup
cp trading_bot.py trading_bot_BACKUP_$(date +%Y%m%d_%H%M%S).py
echo "✅ Backup created"

# 1. Add import at line 1 (after existing imports)
# Find the last 'import' line and add after it
LAST_IMPORT=$(grep -n "^import\|^from" trading_bot.py | tail -1 | cut -d: -f1)
sed -i "${LAST_IMPORT}a\\
from patch_integrate import side_aware_entry, side_aware_monitor, side_aware_exit, check_and_kill_dead_trade" trading_bot.py
echo "✅ Import added after line $LAST_IMPORT"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "IMPORT ADDED. Now you need to manually wire 3 calls."
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Run these to find WHERE to add the remaining 3 lines:"
echo ""
echo "  grep -n 'ENTRY\|place_order.*BUY\|breakout.*entry' trading_bot.py | head -20"
echo "  grep -n 'active_trade.*monitor\|manage_trade\|check_exit' trading_bot.py | head -20"
echo "  grep -n 'MANDATORY_EXIT\|force.*exit\|eod.*exit' trading_bot.py | head -20"
echo ""
