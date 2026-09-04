#!/bin/bash
# FULL BOT AUDIT — 2026-08-04
# Run: bash run_full_audit.sh
# Paste output back for analysis

cd /home/ubuntu/trading-bot
echo "================================================================"
echo "L1: CODE PATCHES — verifying all today's changes"
echo "================================================================"

echo ""
echo "--- short_live.py ---"
echo -n "REGIME_GATES dict: "
grep -c "REGIME_GATES" short_live.py && echo "✅" || echo "❌ MISSING"

echo -n "pick_side sector_boost params: "
grep -c "sector_boost_L" short_live.py && echo "✅" || echo "❌ MISSING"

echo -n "size_position 5X tier: "
grep -c "balance \* 5.0" short_live.py && echo "✅" || echo "❌ MISSING"

echo -n "size_position 4X tier: "
grep -c "balance \* 4.0" short_live.py && echo "✅" || echo "❌ MISSING"

echo -n "size_position 3X tier: "
grep -c "balance \* 3.0" short_live.py && echo "✅" || echo "❌ MISSING"

echo -n "Old 4.5X tier GONE: "
grep -c "balance \* 4.5" short_live.py | grep -q "^0$" && echo "✅" || echo "❌ STILL PRESENT"

echo -n "profit_lock_floor 1.10->1.00: "
grep -c "peak_pct >= 1.10: return 1.00" short_live.py && echo "✅" || echo "❌ MISSING"

echo -n "Old 0.60->0.55 GONE: "
grep -c "peak_pct >= 0.60" short_live.py | grep -q "^0$" && echo "✅" || echo "❌ STILL PRESENT"

echo ""
echo "--- patch_integrate.py ---"
echo -n "DH-906 SL fix (_sl_anchor): "
grep -c "_sl_anchor" patch_integrate.py && echo "✅" || echo "❌ MISSING"

echo -n "Live LTP at entry (_ltp_live): "
grep -c "_ltp_live" patch_integrate.py && echo "✅" || echo "❌ MISSING"

echo -n "Regime passed to size_position: "
grep -c "_regime_sz" patch_integrate.py && echo "✅" || echo "❌ MISSING"

echo -n "FLOOR_HARD_OVERRIDE: "
grep -c "FLOOR_HARD_OVERRIDE" patch_integrate.py && echo "✅" || echo "❌ MISSING"

echo -n "check_and_kill_dead_trade GONE: "
grep -c "check_and_kill_dead_trade" patch_integrate.py | grep -q "^0$" && echo "✅" || echo "❌ STILL PRESENT"

echo ""
echo "--- trading_bot.py ---"
echo -n "import orb_rescan: "
grep -c "import orb_rescan" trading_bot.py && echo "✅" || echo "❌ MISSING"

echo -n "orb_rescan.persist_balance: "
grep -c "orb_rescan.persist_balance" trading_bot.py && echo "✅" || echo "❌ MISSING"

echo -n "orb_rescan.trigger_post_orb: "
grep -c "orb_rescan.trigger_post_orb" trading_bot.py && echo "✅" || echo "❌ MISSING"

echo -n "is_entry_allowed guard: "
grep -c "is_entry_allowed" trading_bot.py && echo "✅" || echo "❌ MISSING"

echo -n "_to_decimal helper: "
grep -c "_to_decimal" trading_bot.py && echo "✅" || echo "❌ MISSING"

echo -n "scan_for_breakdown uses _short_candidates: "
grep -c "_short_candidates" trading_bot.py && echo "✅" || echo "❌ MISSING"

echo -n "Dead trade GONE from trading_bot: "
grep -c "check_and_kill_dead_trade" trading_bot.py | grep -q "^0$" && echo "✅" || echo "❌ STILL PRESENT"

echo ""
echo "--- orb_rescan.py ---"
echo -n "Trigger at 04:15 UTC (09:45 IST): "
grep -c "minute >= 15" orb_rescan.py && echo "✅" || echo "❌ MISSING"

echo -n "is_entry_allowed function: "
grep -c "def is_entry_allowed" orb_rescan.py && echo "✅" || echo "❌ MISSING"

echo -n "run_candle_fallback function: "
grep -c "def run_candle_fallback" orb_rescan.py && echo "✅" || echo "❌ MISSING"

echo ""
echo "================================================================"
echo "L2: DEPENDENCY CHAIN — import dry-run"
echo "================================================================"
echo ""
echo "--- Syntax check all patched files ---"
for f in short_live.py patch_integrate.py trading_bot.py orb_rescan.py; do
    venv/bin/python3 -c "import ast; ast.parse(open('$f').read()); print('  ✅ $f syntax OK')" 2>&1 || echo "  ❌ $f SYNTAX ERROR"
done

echo ""
echo "--- Import chain dry-run ---"
venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
try:
    import orb_rescan
    print('  ✅ orb_rescan imports OK')
except Exception as e:
    print(f'  ❌ orb_rescan: {e}')

try:
    import short_live
    print('  ✅ short_live imports OK')
except Exception as e:
    print(f'  ❌ short_live: {e}')

try:
    import config
    print(f'  ✅ config OK — ENABLE_SHORTS={config.ENABLE_SHORTS}, FILTERS_V2={config.FILTERS_V2}')
except Exception as e:
    print(f'  ❌ config: {e}')

try:
    import filters_v2
    print(f'  ✅ filters_v2 OK')
except Exception as e:
    print(f'  ❌ filters_v2: {e}')
" 2>&1

echo ""
echo "================================================================"
echo "L3: DATA FILES — existence and freshness"
echo "================================================================"
echo ""
for f in watchlist.csv stock_history_30d.json prev_close.json stock_metrics.json fno_members.json last_balance.json; do
    if [ -f "$f" ]; then
        age=$(( ($(date +%s) - $(stat -c %Y "$f")) / 3600 ))
        size=$(wc -l < "$f" 2>/dev/null || du -sh "$f" | cut -f1)
        echo "  ✅ $f — age=${age}h, size=$size"
    else
        echo "  ❌ $f — NOT FOUND"
    fi
done

echo ""
echo "--- last_balance.json contents ---"
cat last_balance.json 2>/dev/null || echo "  (file not yet created — will be written on first startup)"

echo ""
echo "================================================================"
echo "L4: CONFIG KEY VALUES"
echo "================================================================"
grep -E "ENABLE_SHORTS|FILTERS_V2|RVOL_NORMAL|RVOL_MIN|DEAD_TRADE|RISK_PER_TRADE|CONFIDENCE_PCT|BEARISH_LIVE|ROLLING_EXIT|MAX_POSITIONS|DEAD_ZONE" config.py | head -25

echo ""
echo "================================================================"
echo "L5: TWO ENTRY PATHS — which one fires for LONG trades?"
echo "================================================================"
echo ""
echo "--- side_aware_entry call sites ---"
grep -n "side_aware_entry\|place_entry\|_side.*LONG\|_side.*SHORT" trading_bot.py | grep -v "def \|#" | head -20

echo ""
echo "--- Which path handles LONG breakouts? ---"
grep -n -B2 -A2 "side_aware_entry\|self.place_entry" trading_bot.py | head -40

echo ""
echo "================================================================"
echo "L6: RUNTIME — bot log last 10 meaningful lines"
echo "================================================================"
grep -v "NO_TRADE\|Dead zone\|ACCELERATING" /home/ubuntu/trading-bot/bot.log | tail -15

echo ""
echo "================================================================"
echo "AUDIT COMPLETE"
echo "================================================================"
