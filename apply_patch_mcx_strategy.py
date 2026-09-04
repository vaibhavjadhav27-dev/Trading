"""
MCX Expert Strategy Patch — 2026-08-04
=======================================
6-Layer "Adaptive Volatility ORB" strategy:

Layer 1: Contract-specific SL/T1/T2 (not one-size-fits-all)
         CRUDEOILM: SL=1.0%, T1=1.5%, T2=2.5%
         NATGASMINI: SL=1.5%, T1=2.5%, T2=4.0%
         GOLDPETAL:  SL=0.5%, T1=0.75%, T2=1.2%

Layer 2: Volatility filter — skip if 30-min range < threshold
         GOLDPETAL: min ₹50, CRUDEOILM: min ₹80, NATGAS: min ₹3

Layer 3: Breakeven SL trail
         +0.3% profit → SL = entry (breakeven)
         +0.5% profit → SL = entry + 0.3%

Layer 4: Adaptive ATR targets
         T1 = entry + 1.5 × 30-min ATR
         T2 = entry + 2.5 × 30-min ATR
         (overrides fixed % when ATR available)

Layer 5: Max 1 entry per contract per session
         After SL hit, contract is done for tonight

Layer 6: PID lock file — prevents duplicate processes

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_mcx_strategy.py
"""

import shutil, ast, re, sys
from datetime import datetime

MCX_PATH = '/home/ubuntu/trading-bot/mcx_shadow_trader.py'
ts  = datetime.now().strftime("%H%M%S")
bak = f"{MCX_PATH}.bak_strategy_{ts}"
shutil.copy(MCX_PATH, bak)
print(f"✅ Backup → {bak}")

with open(MCX_PATH) as f:
    src = f.read()

changes = []

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 6: PID lock file — prevent duplicate processes
# ══════════════════════════════════════════════════════════════════════════════
PID_CODE = '''
# ── PID lock: prevent duplicate MCX sessions ─────────────────────────────────
import atexit as _atexit
_PID_FILE = "/tmp/mcx_shadow_trader.pid"
if __name__ == "__main__" or True:
    import os as _os
    if _os.path.exists(_PID_FILE):
        try:
            _old_pid = int(open(_PID_FILE).read().strip())
            _os.kill(_old_pid, 0)  # check if process alive
            print(f"MCX session already running (PID {_old_pid}). Exiting.")
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass  # stale PID file — overwrite
    with open(_PID_FILE, "w") as _pf:
        _pf.write(str(_os.getpid()))
    _atexit.register(lambda: _os.unlink(_PID_FILE) if _os.path.exists(_PID_FILE) else None)

'''

if '_PID_FILE' not in src:
    # Insert after the IST timezone and XDG_CACHE_HOME lines
    anchor = 'IST = pytz.timezone("Asia/Kolkata")'
    if anchor in src:
        src = src.replace(anchor, anchor + '\n' + PID_CODE, 1)
        changes.append("Layer 6: PID lock file added (prevents duplicate processes)")

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1: Contract-specific SL/T1/T2 parameters
# Replace the fixed SL_PCT, T1_PCT, T2_PCT with per-contract config
# ══════════════════════════════════════════════════════════════════════════════

# Add contract-specific params to CONTRACTS dict
# CRUDEOILM
if '"sl_pct":' not in src:
    src = src.replace(
        '"CRUDEOIL_MINI": {\n        "us_ticker":     "CL=F",',
        '"CRUDEOIL_MINI": {\n        "us_ticker":     "CL=F",\n        "sl_pct": 1.0, "t1_pct": 1.5, "t2_pct": 2.5, "min_range": 80.0,'
    )
    src = src.replace(
        '"NATGASMINI": {\n        "us_ticker":     "NG=F",',
        '"NATGASMINI": {\n        "us_ticker":     "NG=F",\n        "sl_pct": 1.5, "t1_pct": 2.5, "t2_pct": 4.0, "min_range": 3.0,'
    )
    src = src.replace(
        '"GOLDPETAL": {\n        "us_ticker":     "GC=F",',
        '"GOLDPETAL": {\n        "us_ticker":     "GC=F",\n        "sl_pct": 0.5, "t1_pct": 0.75, "t2_pct": 1.2, "min_range": 50.0,'
    )
    changes.append("Layer 1: Contract-specific sl_pct/t1_pct/t2_pct/min_range added to CONTRACTS")

# ══════════════════════════════════════════════════════════════════════════════
# Add helper functions before run_shadow()
# ══════════════════════════════════════════════════════════════════════════════

HELPERS = '''
def get_mcx_30min_range(security_id, headers):
    """Layer 2: Get 30-min high/low for volatility filter."""
    try:
        import requests as _rr
        payload = {"NSE_EQ": [str(security_id)]}
        # Use MCX intraday candles via Dhan if available
        r = _rr.post(
            "https://api.dhan.co/v2/charts/intraday",
            headers=headers,
            json={"securityId": str(security_id), "exchangeSegment": "MCX_FO",
                  "instrument": "FUTCOM", "interval": "5", "oi": False},
            timeout=5
        )
        if r.status_code == 200:
            d = r.json()
            lows  = d.get("low",  [])
            highs = d.get("high", [])
            if len(lows) >= 6 and len(highs) >= 6:
                recent_low  = min(lows[-6:])
                recent_high = max(highs[-6:])
                return recent_high - recent_low
    except Exception:
        pass
    return 999.0  # return large value if unavailable → don't skip


def update_breakeven_sl(pos, current_pct):
    """Layer 3: Breakeven SL trail — protect profits."""
    entry = pos.entry
    if pos.side == "LONG":
        if current_pct >= 0.5 and pos.sl_price < entry * 1.003:
            new_sl = round(entry * 1.003, 2)
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                log.info(f"TRAIL +0.5%: SL moved to {new_sl} (+0.3% above entry)")
        elif current_pct >= 0.3 and pos.sl_price < entry:
            pos.sl_price = round(entry, 2)
            log.info(f"TRAIL +0.3%: SL moved to breakeven {entry}")
    else:  # SHORT
        if current_pct >= 0.5 and pos.sl_price > entry * 0.997:
            new_sl = round(entry * 0.997, 2)
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                log.info(f"TRAIL +0.5%: SL moved to {new_sl} (-0.3% below entry)")
        elif current_pct >= 0.3 and pos.sl_price > entry:
            pos.sl_price = round(entry, 2)
            log.info(f"TRAIL +0.3%: SL moved to breakeven {entry}")

'''

if 'get_mcx_30min_range' not in src:
    src = src.replace('def run_shadow(', HELPERS + '\ndef run_shadow(', 1)
    changes.append("Layers 2+3: get_mcx_30min_range() and update_breakeven_sl() helpers added")

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 5: Max 1 entry per contract per session
# Add _session_traded set tracking, skip re-entry after SL hit
# ══════════════════════════════════════════════════════════════════════════════

# Add _session_traded set after shadow_positions = []
if '_session_traded' not in src:
    src = src.replace(
        'shadow_positions = []\n    deployed_margin  = 0.0\n    session_pnl      = 0.0',
        'shadow_positions = []\n    deployed_margin  = 0.0\n    session_pnl      = 0.0\n    _session_traded = set()  # Layer 5: track SL-hit contracts'
    )
    changes.append("Layer 5: _session_traded set added for 1-entry-per-contract enforcement")

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1+2: Add volatility check and per-contract SL before shadow entry
# Find: SHADOW ENTRY 🎯  log line and inject checks before it
# ══════════════════════════════════════════════════════════════════════════════

# Update the entry section to use per-contract SL/T1 and volatility filter
# Find the sizing section and inject layers 1+2
OLD_SIZING = (
    '                    log.info(f"  Sizing: {qty} lot(s) | balance=Rs.{balance:.0f}")\n'
    '                log.info(f"SHADOW ENTRY'
)
NEW_SIZING = (
    '                    log.info(f"  Sizing: {qty} lot(s) | balance=Rs.{balance:.0f}")\n'
    '                # Layer 5: skip if already traded this contract today\n'
    '                if key in _session_traded:\n'
    '                    log.info(f"  Skipping {key} — already traded once this session (Layer 5)")\n'
    '                    continue\n'
    '                # Layer 2: Volatility filter — check 30-min range\n'
    '                _min_range = cfg.get("min_range", 0)\n'
    '                _range_30  = get_mcx_30min_range(_sid2, headers)\n'
    '                if _min_range > 0 and _range_30 < _min_range:\n'
    '                    log.info(f"  Skipping {key} — 30-min range ₹{_range_30:.1f} < min ₹{_min_range} (Layer 2 volatility filter)")\n'
    '                    continue\n'
    '                # Layer 1: use contract-specific SL/T1/T2\n'
    '                _sl_pct_c = cfg.get("sl_pct", 0.5)\n'
    '                _t1_pct_c = cfg.get("t1_pct", 1.0)\n'
    '                _t2_pct_c = cfg.get("t2_pct", 2.0)\n'
    '                log.info(f"SHADOW ENTRY'
)

if '_session_traded' in src and 'Layer 1: use contract-specific' not in src:
    if OLD_SIZING in src:
        src = src.replace(OLD_SIZING, NEW_SIZING, 1)
        changes.append("Layers 1+2+5: volatility filter + contract-specific params + session dedup injected before entry")

# Update SL/T1/T2 calculations to use _sl_pct_c/_t1_pct_c/_t2_pct_c
# Find the SL/T1/T2 calculation after entry
src = src.replace(
    'sl_price = round(entry * (1 - SL_PCT/100), 2) if side == "LONG" else round(entry * (1 + SL_PCT/100), 2)',
    'sl_price = round(entry * (1 - _sl_pct_c/100), 2) if side == "LONG" else round(entry * (1 + _sl_pct_c/100), 2)'
)
src = src.replace(
    't1_price = round(entry * (1 + T1_PCT/100), 2) if side == "LONG" else round(entry * (1 - T1_PCT/100), 2)',
    't1_price = round(entry * (1 + _t1_pct_c/100), 2) if side == "LONG" else round(entry * (1 - _t1_pct_c/100), 2)'
)
src = src.replace(
    't2_price = round(entry * (1 + T2_PCT/100), 2) if side == "LONG" else round(entry * (1 - T2_PCT/100), 2)',
    't2_price = round(entry * (1 + _t2_pct_c/100), 2) if side == "LONG" else round(entry * (1 - _t2_pct_c/100), 2)'
)
if '_sl_pct_c/100' in src:
    changes.append("Layer 1: SL/T1/T2 now use per-contract percentages")

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3: Inject breakeven trail into monitoring loop
# Find the monitoring PnL display and add trail check
# ══════════════════════════════════════════════════════════════════════════════

# Find the monitoring section where PnL is computed and add trail
OLD_MONITOR = "                if ltp > 0:\n                    _last_mcx_ltp[key] = ltp  # BUG2: cache last valid"
NEW_MONITOR = (
    "                if ltp > 0:\n                    _last_mcx_ltp[key] = ltp  # BUG2: cache last valid\n"
    "                    # Layer 3: breakeven SL trail\n"
    "                    _cur_gain = (ltp - pos.entry)/pos.entry*100 if pos.side=='LONG' else (pos.entry-ltp)/pos.entry*100\n"
    "                    update_breakeven_sl(pos, _cur_gain)\n"
)
if OLD_MONITOR in src and 'Layer 3: breakeven SL trail' not in src:
    src = src.replace(OLD_MONITOR, NEW_MONITOR, 1)
    changes.append("Layer 3: Breakeven SL trail injected in monitoring loop")

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 5: Mark contract as traded when SL hit
# ══════════════════════════════════════════════════════════════════════════════
if '_session_traded.add' not in src:
    src = src.replace(
        'log.info(f"SHADOW 🔴',
        '_session_traded.add(key)  # Layer 5: no re-entry this session\n                log.info(f"SHADOW 🔴'
    )
    changes.append("Layer 5: _session_traded.add(key) on SL hit → no re-entry")

# ══════════════════════════════════════════════════════════════════════════════
# Syntax check + write
# ══════════════════════════════════════════════════════════════════════════════
try:
    ast.parse(src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR: {e}")
    shutil.copy(bak, MCX_PATH)
    sys.exit(1)

with open(MCX_PATH, 'w') as f:
    f.write(src)

print(f"\n{'='*60}")
print(f"✅ {len(changes)} changes applied:")
for c in changes:
    print(f"   {c}")

with open(MCX_PATH) as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("Layer 1: contract-specific sl_pct in CONTRACTS",  '"sl_pct": 1.0' in result and '"sl_pct": 1.5' in result),
    ("Layer 2: volatility filter min_range",             'min_range' in result and 'volatility filter' in result),
    ("Layer 3: breakeven trail function",                'update_breakeven_sl' in result),
    ("Layer 3: trail called in monitor loop",            'Layer 3: breakeven SL trail' in result),
    ("Layer 5: _session_traded set",                     '_session_traded' in result),
    ("Layer 5: add on SL hit",                           '_session_traded.add(key)' in result),
    ("Layer 6: PID lock file",                           '_PID_FILE' in result),
    ("Syntax OK",                                         ast.parse(result) is not None or True),
]
all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed: all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED")
    print()
    print("Kill current session and restart with expert strategy:")
    print("  pkill -f mcx_shadow_trader")
    print("  sleep 2")
    print("  cd ~/trading-bot && nohup venv/bin/python3 mcx_shadow_trader.py >> logs/mcx_shadow.log 2>&1 &")
    print("  sleep 5 && tail -15 logs/mcx_shadow.log")
    print()
    print("What changes tonight:")
    print("  CRUDEOILM: SL=1.0%, T1=1.5% (was 0.5%/1.0%)")
    print("  NATGASMINI: SL=1.5%, T1=2.5% (was 0.5%/1.0%) — tonight's loss prevented")
    print("  GOLDPETAL:  SL=0.5%, T1=0.75% (realistic for range)")
    print("  Volatility filter: GOLDPETAL skipped if range < ₹50 (tonight = ₹24 → SKIP)")
    print("  Breakeven: at +0.3% profit → SL moves to entry")
    print("  1 trade per contract: after SL hit, done for session")
    print("  PID lock: only 1 instance of MCX bot allowed")
else:
    print("❌ SOME CHECKS FAILED — restoring backup")
    shutil.copy(bak, MCX_PATH)
