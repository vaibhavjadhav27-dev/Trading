#!/usr/bin/env python3
"""
patch_rolling_exit.py — Integrates rolling_exit.py into trading_bot.py
Run: python3 patch_rolling_exit.py
"""
import shutil
import re

src = "trading_bot.py"
shutil.copy(src, f"{src}.bak_pre_rolling")

with open(src, "r") as f:
    lines = f.readlines()

content = "".join(lines)

# ════════════════════════════════════════════════════════════════
# PATCH 1: Add import at top (after other imports)
# ═══════════════════════════════════════════════════════════════
import_line = "from rolling_exit import RollingState, evaluate_rolling_exit\n"

# Find where imports end (look for first class or def after imports)
if "from rolling_exit" not in content:
    # Insert after the last 'import' line in the top section
    last_import_idx = 0
    for i, line in enumerate(lines):
        if i > 100:  # imports should be in first 100 lines
            break
        if line.startswith("import ") or line.startswith("from "):
            last_import_idx = i
    lines.insert(last_import_idx + 1, import_line)
    print(f"PATCH 1: Import added at line {last_import_idx + 2}")
else:
    print("PATCH 1: Import already exists, skipping")

# ════════════════════════════════════════════════════════════════
# PATCH 2: Initialize RollingState when trade is entered
# Find where self.active_trade is SET (after entry confirmation)
# ════════════════════════════════════════════════════════════════
# Look for the line that sets self.active_trade = { ... }
# and add self.rolling_state = RollingState(...) after it

init_code = """
        # Initialize rolling exit state
        if getattr(config, 'ROLLING_EXIT_ENABLED', False):
            _side = self.active_trade.get('side', 'LONG')
            self.rolling_state = RollingState(
                side=_side,
                entry_price=self.active_trade['entry_price'],
                t1_pct=getattr(config, 'ROLLING_T1_PCT', 0.60),
                step_pct=getattr(config, 'ROLLING_STEP_PCT', 0.95),
                buffer_pct=getattr(config, 'ROLLING_BUFFER_PCT', 0.10),
                grace_seconds=getattr(config, 'ROLLING_GRACE_SECONDS', 60),
            )
            self.log.info(f"ROLLING_EXIT initialized: {_side} entry={self.active_trade['entry_price']:.2f}")
"""

# Find "self.active_trade = {" or the confirmation log after entry
content_new = "".join(lines)
# Look for the pattern where active_trade is fully set
# Based on earlier grep: line ~1225 area has place_order, and active_trade is set nearby
# Let's find "ENTRY_CONFIRMED" or similar log line after trade is set

entry_patterns = [
    "ENTRY_CONFIRMED",
    "Trade entered",
    "ENTRY.*placed",
    "active_trade.*entry_price",
]

inserted_init = False
for i, line in enumerate(lines):
    if "ENTRY" in line and "log" in line.lower() and "self.active_trade" not in line:
        # Check if this is the entry confirmation log
        if any(p in line for p in ["ENTRY_CONFIRMED", "ENTRY_LIVE", "═══ ENTRY"]):
            # Insert rolling state init AFTER this line
            # But first check it's not already there
            if "rolling_state" not in "".join(lines[i:i+10]):
                lines.insert(i + 1, init_code)
                inserted_init = True
                print(f"PATCH 2: RollingState init inserted after line {i + 1}")
                break

if not inserted_init:
    # Fallback: find where self.active_trade['entry_price'] is first set
    for i, line in enumerate(lines):
        if "self.active_trade" in line and "entry_price" in line and "=" in line and "{" in line:
            # Find the closing brace of this dict
            brace_count = 0
            for j in range(i, min(i + 30, len(lines))):
                brace_count += lines[j].count("{") - lines[j].count("}")
                if brace_count == 0 and j > i:
                    lines.insert(j + 1, init_code)
                    inserted_init = True
                    print(f"PATCH 2: RollingState init inserted after line {j + 1} (fallback)")
                    break
            break

if not inserted_init:
    print("PATCH 2: WARNING - Could not find entry point. Manual insertion needed.")
    print("         Add rolling_state init after self.active_trade is set.")

# ═══════════════════════════════════════════════════════════════
# PATCH 3: Add rolling exit check inside monitor_active_trade()
# This replaces/augments the existing profit-lock ladder
# ═══════════════════════════════════════════════════════════════

monitor_code = """
            # ═══ ROLLING EXIT CHECK ═══
            if getattr(config, 'ROLLING_EXIT_ENABLED', False) and hasattr(self, 'rolling_state') and self.rolling_state:
                try:
                    # Get current indicators (use what's available)
                    _rsi = getattr(self, '_last_rsi', 50.0)
                    _vol_chg = getattr(self, '_last_vol_change', 0.0)
                    _red = getattr(self, '_last_red_candles', 0)
                    _vwap_slope = getattr(self, '_last_vwap_slope', 0.0)
                    _rs_entry = getattr(self, '_rs_vs_entry', 0.0)
                    _hl = getattr(self, '_higher_lows', False)

                    action, reason, exit_px = evaluate_rolling_exit(
                        self.rolling_state, ltp, _rsi, _vol_chg,
                        _red, _vwap_slope, _rs_entry, _hl, config
                    )

                    if action == 'ADVANCE':
                        self.log.info(f"ROLLING: {reason} | floor={self.rolling_state.floor_price:.2f}")
                    elif action == 'EXIT':
                        self.log.info(f"ROLLING_EXIT: {reason} | exit_px={exit_px:.2f}")
                        self.execute_exit(ltp, f"ROLLING_EXIT: {reason}")
                        return
                except Exception as e:
                    self.log.warning(f"Rolling exit error: {e}")
"""

# Find the monitor_active_trade function and insert after LTP is fetched
# Based on earlier grep, monitor_active_trade is at line 1318
# Inside it, there should be a line that gets ltp (current price)
inserted_monitor = False
in_monitor = False
for i, line in enumerate(lines):
    if "def monitor_active_trade" in line:
        in_monitor = True
        continue
    if in_monitor:
        # Look for where ltp is assigned/used for profit calculation
        if ("ltp" in line or "current_price" in line or "last_price" in line) and "profit" in "".join(lines[i:i+5]).lower():
            # Insert rolling exit check BEFORE the old exit logic
            if "ROLLING EXIT CHECK" not in "".join(lines[i-5:i+20]):
                lines.insert(i + 1, monitor_code)
                inserted_monitor = True
                print(f"PATCH 3: Rolling exit check inserted at line {i + 2}")
                break
        # Alternative: find the profit calculation line
        if "profit_pct" in line or "unrealized" in line or "current_pnl" in line:
            if "ROLLING EXIT CHECK" not in "".join(lines[max(0,i-5):i+20]):
                lines.insert(i + 1, monitor_code)
                inserted_monitor = True
                print(f"PATCH 3: Rolling exit check inserted at line {i + 2} (alt)")
                break
        # Stop if we hit the next def
        if line.strip().startswith("def ") and "monitor" not in line:
            break

if not inserted_monitor:
    # Last resort: insert at the start of monitor_active_trade, after the first few lines
    for i, line in enumerate(lines):
        if "def monitor_active_trade" in line:
            # Skip docstring and initial setup (next 10 lines)
            insert_at = i + 10
            lines.insert(insert_at, monitor_code)
            inserted_monitor = True
            print(f"PATCH 3: Rolling exit check inserted at line {insert_at} (last resort)")
            break

if not inserted_monitor:
    print("PATCH 3: WARNING - Could not find monitor insertion point.")

# ═══════════════════════════════════════════════════════════════
# PATCH 4: Initialize self.rolling_state = None in __init__
# ════════════════════════════════════════════════════════════════
for i, line in enumerate(lines):
    if "self.active_trade" in line and "= None" in line and "def " not in line:
        if "self.rolling_state" not in "".join(lines[i:i+3]):
            lines.insert(i + 1, "        self.rolling_state = None\n")
            print(f"PATCH 4: self.rolling_state = None added at line {i + 2}")
        else:
            print("PATCH 4: Already exists")
        break

# ═══════════════════════════════════════════════════════════════
# PATCH 5: Clear rolling_state on exit
# ════════════════════════════════════════════════════════════════
for i, line in enumerate(lines):
    if "self.active_trade = None" in line and "def " not in line:
        if "self.rolling_state = None" not in "".join(lines[max(0,i-2):i+3]):
            lines.insert(i + 1, "        self.rolling_state = None\n")
            print(f"PATCH 5: rolling_state cleared on exit at line {i + 2}")
        else:
            print("PATCH 5: Already exists")
        break

# ═══════════════════════════════════════════════════════════════
# WRITE
# ═══════════════════════════════════════════════════════════════
with open(src, "w") as f:
    f.writelines(lines)

print("\n═══ DONE ═══")
print(f"Backup: {src}.bak_pre_rolling")
print("Run: python3 -c \"import trading_bot; print('Syntax OK')\"")
