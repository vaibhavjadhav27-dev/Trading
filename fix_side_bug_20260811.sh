#!/usr/bin/env bash
# ============================================================================
# fix_side_bug_20260811.sh  � Fix A (crash) + validation gate
# Run on the server:  bash fix_side_bug_20260811.sh
# ============================================================================
set -uo pipefail
cd ~/trading-bot || { echo "FATAL: no ~/trading-bot"; exit 1; }

echo "###### STEP 0 � backup ######"
cp trading_bot.py "trading_bot.py.bak_sidefix_$(date +%Y%m%d_%H%M%S)"
echo "backup written"

echo ""
echo "###### STEP 1 � show the current broken region (line ~2579) ######"
grep -n "SIDE SELECT" trading_bot.py
echo "--- 15 lines of context above the log line ---"
LINE=$(grep -n "SIDE SELECT" trading_bot.py | head -1 | cut -d: -f1)
if [ -z "$LINE" ]; then echo ">>> SIDE SELECT line not found � STOP, do not patch blindly"; exit 1; fi
sed -n "$((LINE-15)),$((LINE))p" trading_bot.py

echo ""
echo "###### STEP 2 � MANUAL EDIT REQUIRED (do NOT auto-sed blind) ######"
cat <<'PATCH'
Open trading_bot.py at the SIDE SELECT line (~2579) and ensure the block reads:

    # --- Fix A: initialize BEFORE the conditional so _side can never be unbound ---
    _lscore, _sscore, _regime = lscore, sscore, regime
    _side, _why = "NO_TRADE", "scores unresolved (SID=None)"
    if lscore is not None and sscore is not None:
        _side, _why = pick_side(regime, lscore, sscore)
    log.info(f"SIDE SELECT: {_side} | L={_lscore} S={_sscore} regime={_regime} | {_why}")

Key point: the 4 underscore vars MUST be assigned on EVERY path before the log line.
PATCH

echo ""
echo "###### STEP 3 � HARD VALIDATION GATE (run AFTER editing) ######"
echo "--- 3a: file compiles? ---"
python3 -m py_compile trading_bot.py && echo "COMPILE OK" || { echo ">>> COMPILE FAILED � REVERT"; exit 1; }

echo "--- 3b: vars initialized before the log line? (must print all 4) ---"
python3 - <<'PYEOF'
import re, sys
src = open("trading_bot.py").read()
m = re.search(r'log\.info\(f".*SIDE SELECT.*\{_side\}', src)
if not m:
    print(">>> FAIL: SIDE SELECT log line not found"); sys.exit(1)
head = src[:m.start()]
# find the nearest enclosing function body preceding the log call
init_ok = ('_side, _why = "NO_TRADE"' in head) or ("_side, _why = 'NO_TRADE'" in head)
print("PASS: _side initialized before log line" if init_ok
      else ">>> FAIL: _side NOT initialized before the log line")
sys.exit(0 if init_ok else 1)
PYEOF
