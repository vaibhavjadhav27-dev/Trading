#!/bin/bash
# READ-ONLY diagnostics — touches nothing, changes nothing.
cd ~/trading-bot
echo "==================== #4  SYSTEMD SUPERVISOR (ORB bot) ===================="
echo "-- units mentioning trading/orb --"
systemctl list-units --all 2>/dev/null | grep -iE 'trad|orb' || echo "  (none)"
echo "-- service unit files --"
ls -1 /etc/systemd/system/ 2>/dev/null | grep -iE 'trad|orb' || echo "  (no trading*.service unit)"
echo "-- how is trading_bot.py currently launched? (cron?) --"
crontab -l 2>/dev/null | grep -iE 'trading_bot|orb' || echo "  (not in crontab)"

echo ""
echo "==================== #5  GEMINI / GROQ BACKEND ORDER ===================="
grep -nE 'gemini|groq|GEMINI|GROQ|def .*(analy|ai_|llm)|primary|fallback|429' post_market_analysis.py 2>/dev/null | head -40 || echo "  post_market_analysis.py not found"

echo ""
echo "==================== #7  HOMEFIRST MISS (2026-07-13) ===================="
echo "-- any HOMEFIRST mentions in today's logs --"
grep -rniE 'homefirst|HOMEFIRST' ~/trading-bot/logs/ 2>/dev/null | tail -30 || echo "  (no HOMEFIRST in logs/)"
echo "-- SIZING REJECTED / entry rejections today --"
grep -rniE 'SIZING REJECTED|Qty.*< min|Skipping|breakout|ENTRY' ~/trading-bot/logs/*2026-07-13* 2>/dev/null | tail -30 || echo "  (no 07-13 log matches)"

echo ""
echo "==================== #2  WEBSOCKET FIX (re-verify) ===================="
grep -nE 'WebSocketFeed|def start_websocket|ws_active|get_bulk_ltp' trading_bot.py 2>/dev/null | head -20

echo ""
echo "==================== #3  RS SCORER / NIFTY BASELINE (re-verify) ===================="
grep -nE "\^NSEI|NSEI|def calculate_rs_scores|nifty_5d|stock_history_30d" trading_bot.py pull_yf_history.py 2>/dev/null | head -25
echo "-- NIFTY present in history file? --"
python3 -c "import json;d=json.load(open('stock_history_30d.json'));print('  keys sample:',list(d.get('stocks',{}).keys())[:3]);print('  ^NSEI present:', '^NSEI' in d.get('stocks',{}) or 'NIFTY' in d.get('stocks',{}))" 2>/dev/null || echo "  (could not read stock_history_30d.json)"
echo ""
echo "==================== END DIAGNOSTICS ===================="
