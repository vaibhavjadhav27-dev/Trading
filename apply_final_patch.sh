#!/bin/bash
cd ~/trading-bot

echo "═══ Applying final integration (3 sed commands) ═══"

# ─────────────────────────────────────────────────────────────────
# PATCH A: Add dead trade check as FIRST line inside monitor_active_trade()
# Line 1318 is: def monitor_active_trade(self):
# We add the dead trade check right after the function starts
# (after the position recovery block, at line 1353 where it says
#  "if not self.active_trade: return")
# ─────────────────────────────────────────────────────────────────

# Find "if not self.active_trade:" after monitor_active_trade and add dead check before it
sed -i '1353a\        # ── PATCH: Dead trade check (kills trades held >20min at <0.5R) ──
        if self.active_trade:
            _ltp_dead = self.fetch_ltp_concurrent([self.active_trade["security_id"]])
            _dead_ltp = _ltp_dead.get(str(self.active_trade["security_id"]), 0)
            if _dead_ltp > 0 and check_and_kill_dead_trade(self, _dead_ltp):
                return' trading_bot.py

echo "✅ Patch A: Dead trade check added at line 1353"

# ─────────────────────────────────────────────────────────────────
# PATCH B: Make EOD exit side-aware
# Line 2123: self.exit_trade(exit_ltp, "MANDATORY_EXIT_EOD")
# Replace with side_aware_exit
# ─────────────────────────────────────────────────────────────────

sed -i 's/self.exit_trade(exit_ltp, "MANDATORY_EXIT_EOD")/side_aware_exit(self, exit_ltp, "MANDATORY_EXIT_EOD")/' trading_bot.py

echo "✅ Patch B: EOD exit now side-aware"

# ─────────────────────────────────────────────────────────────────
# PATCH C: Make the 3:15 PM exit side-aware too
# Line 1579: exit_reason = "MANDATORY_EXIT_3:15PM"
# Find the exit_trade call near it
# ─────────────────────────────────────────────────────────────────

sed -i 's/self.exit_trade(.*"MANDATORY_EXIT_3:15PM")/side_aware_exit(self, exit_ltp, "MANDATORY_EXIT_3:15PM")/' trading_bot.py

echo "✅ Patch C: 3:15PM exit now side-aware"

echo ""
echo "═══ VERIFICATION ═══"
echo ""
grep -n "check_and_kill_dead_trade\|side_aware_exit\|side_aware_entry\|patch_integrate" trading_bot.py
echo ""
echo "═══ DONE ═══"
echo ""
echo "To test (syntax check only, no trading):"
echo "  python3 -c 'import trading_bot; print(\"✅ Syntax OK\")'"
echo ""
echo "To run:"
echo "  python3 trading_bot.py"

