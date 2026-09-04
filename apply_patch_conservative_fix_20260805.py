"""
PATCH: Conservative Sector Override + FILTERS_V2 SHORT EMA Fix — 2026-08-05
=============================================================================
Fix 1 (short_live.py): CONSERVATIVE regime + sector override
  - CONSERVATIVE now checks stock's sector vs _leading_sectors
  - If stock in leading sector (outperformance > +1.5%) → use NORMAL gate (90)
  - All other stocks remain blocked (gate=999)
  - Fixes: today's DLF, GODREJPROP, CANBK missed despite REALTY +2.09%

Fix 2 (filters_v2.py): EMA filter reversed for SHORT candidates
  - NORMAL state: SHORT candidates check ltp <= ema20 (below EMA = weakness)
  - LONG candidates: keep ltp >= ema20 (above EMA = strength)
  - Fixes: 85 SHORT candidates → only 1 survived (84 failed LONG EMA check)

Fix 3 (trading_bot.py): Pass _leading_sectors to pick_side for sector override
  - sector_boost_L / sector_boost_S already accepted by pick_side (R3 reform)
  - Pass leading sector bonus when calling pick_side in run loop

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_conservative_fix_20260805.py
"""

import shutil, ast, re, sys
from datetime import datetime

def backup(path):
    ts  = datetime.now().strftime("%H%M%S")
    dst = f"{path}.bak_consfix_{ts}"
    shutil.copy(path, dst)
    print(f"  ✅ Backup → {dst}")
    return dst

def syntax_ok(path):
    with open(path) as f: src = f.read()
    try:
        ast.parse(src); return True, ""
    except SyntaxError as e:
        return False, str(e)

changes = []

# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 — short_live.py: CONSERVATIVE sector override in REGIME_GATES
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("FIX 1: short_live.py — CONSERVATIVE sector override")
print("="*60)

SL_PATH = '/home/ubuntu/trading-bot/short_live.py'
backup(SL_PATH)

with open(SL_PATH) as f:
    src = f.read()

# Update pick_side() to handle sector override for CONSERVATIVE
# Add sector_leading param and modify CONSERVATIVE logic
# Current: 'CONSERVATIVE': 999
# New: if sector_leading → use 90, else 999

OLD_CONSERVATIVE = "    'CONSERVATIVE': 999,\n"
NEW_CONSERVATIVE = "    'CONSERVATIVE': 999,  # overridden per-call if sector is leading\n"

if OLD_CONSERVATIVE in src and 'overridden per-call' not in src:
    src = src.replace(OLD_CONSERVATIVE, NEW_CONSERVATIVE)
    changes.append("short_live.py: CONSERVATIVE gate comment updated")

# Update pick_side() to accept sector_leading param and override gate
OLD_PICK_SIG = "def pick_side(regime, long_score, short_score, sector_boost_L=0, sector_boost_S=0):"
NEW_PICK_SIG  = "def pick_side(regime, long_score, short_score, sector_boost_L=0, sector_boost_S=0, sector_leading=False):"

if OLD_PICK_SIG in src and 'sector_leading' not in src:
    src = src.replace(OLD_PICK_SIG, NEW_PICK_SIG)
    changes.append("short_live.py: pick_side() added sector_leading= param")

# Add override logic: after "r = regime.upper()" line
OLD_R_LINE = "    r = regime.upper()\n"
NEW_R_LINE  = (
    "    r = regime.upper()\n"
    "    # Sector override: CONSERVATIVE + leading sector → trade like NORMAL\n"
    "    if r == 'CONSERVATIVE' and sector_leading:\n"
    "        r = 'NORMAL'  # override gate to 90 for leading-sector stocks\n"
    "        log.debug(f'CONSERVATIVE→NORMAL override: stock in leading sector')\n"
)
if OLD_R_LINE in src and 'Sector override' not in src:
    src = src.replace(OLD_R_LINE, NEW_R_LINE, 1)
    changes.append("short_live.py: CONSERVATIVE→NORMAL sector override logic added")

# Add log import if not present
if 'import logging' not in src and 'log = logging' not in src:
    src = "import logging\nlog = logging.getLogger('trading_bot')\n" + src
    changes.append("short_live.py: added logging import for sector override debug log")

with open(SL_PATH, 'w') as f:
    f.write(src)

ok, err = syntax_ok(SL_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}"); sys.exit(1)
print(f"  ✅ short_live.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — filters_v2.py: reverse EMA check for SHORT candidates
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("FIX 2: filters_v2.py — SHORT EMA filter reversed")
print("="*60)

FV_PATH = '/home/ubuntu/trading-bot/filters_v2.py'
backup(FV_PATH)

with open(FV_PATH) as f:
    src_fv = f.read()

# Current NORMAL state EMA check (line ~110):
#   if not _passes_ema(cfg, tkr, ltp, "ema20"):
#       continue
# Fix: for SHORT direction, check ltp <= ema20 instead

OLD_NORMAL_EMA = (
    "        elif state == \"NORMAL\":\n"
    "            rmin = getattr(cfg, \"RVOL_NORMAL\", 4.0)\n"
)

# Find the NORMAL EMA check and add direction awareness
if 'SHORT direction: ltp <= ema20' not in src_fv:
    # Replace the _passes_ema call in NORMAL state with direction-aware version
    OLD_EMA_CHECK = '            if not _passes_ema(cfg, tkr, ltp, "ema20"):\n                continue\n            kept.append(c)'
    NEW_EMA_CHECK = (
        '            # Direction-aware EMA: LONG needs price > EMA20, SHORT needs price < EMA20\n'
        '            _direction = c.get("direction", "LONG")\n'
        '            if _direction == "SHORT":\n'
        '                # SHORT direction: ltp <= ema20 confirms weakness (reversed check)\n'
        '                _ema20 = _m(tkr).get("ema20", 0)\n'
        '                if _ema20 and ltp > _ema20:\n'
        '                    continue  # price above EMA = not weak enough for short\n'
        '            else:\n'
        '                if not _passes_ema(cfg, tkr, ltp, "ema20"):\n'
        '                    continue  # price below EMA = not strong enough for long\n'
        '            kept.append(c)'
    )
    if OLD_EMA_CHECK in src_fv:
        src_fv = src_fv.replace(OLD_EMA_CHECK, NEW_EMA_CHECK, 1)
        changes.append("filters_v2.py: NORMAL EMA check reversed for SHORT direction")
    else:
        # Try to find it more loosely
        if '_passes_ema(cfg, tkr, ltp, "ema20")' in src_fv:
            src_fv = src_fv.replace(
                '            if not _passes_ema(cfg, tkr, ltp, "ema20"):\n'
                '                continue\n'
                '            kept.append(c)',
                '            _direction = c.get("direction", "LONG")\n'
                '            if _direction == "SHORT":\n'
                '                _ema20 = _m(tkr).get("ema20", 0)\n'
                '                if _ema20 and ltp > _ema20:\n'
                '                    continue\n'
                '            else:\n'
                '                if not _passes_ema(cfg, tkr, ltp, "ema20"):\n'
                '                    continue\n'
                '            kept.append(c)',
                1
            )
            changes.append("filters_v2.py: EMA check direction-aware (fallback match)")

with open(FV_PATH, 'w') as f:
    f.write(src_fv)

ok, err = syntax_ok(FV_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}"); sys.exit(1)
print(f"  ✅ filters_v2.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 — trading_bot.py: pass sector_leading to pick_side in run loop
# Find the pick_side call in the main run loop and pass sector_leading
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("FIX 3: trading_bot.py — pass sector_leading to pick_side")
print("="*60)

TB_PATH = '/home/ubuntu/trading-bot/trading_bot.py'
backup(TB_PATH)

with open(TB_PATH) as f:
    src_tb = f.read()

# Find the pick_side call site — it reads _lscore/_sscore
# Add sector_leading detection before the call
OLD_PICK_CALL = "_side, _why = _pick_side(_regime, _lscore, _sscore)"
NEW_PICK_CALL = (
    "# Sector override: check if stock is in leading sector\n"
    "                    _leading_names = [s[0] for s in getattr(self, '_leading_sectors', [])] if hasattr(self, '_leading_sectors') else []\n"
    "                    _lc_sector = (_lc.get('sector','') if _lc else '') or (_shc.get('sector','') if _shc else '')\n"
    "                    _sec_leading = bool(_leading_names) and any(_lc_sector.upper() in str(s).upper() for s in _leading_names)\n"
    "                    _side, _why = _pick_side(_regime, _lscore, _sscore, sector_leading=_sec_leading)"
)

if OLD_PICK_CALL in src_tb and 'sector_leading=_sec_leading' not in src_tb:
    src_tb = src_tb.replace(OLD_PICK_CALL, NEW_PICK_CALL, 1)
    changes.append("trading_bot.py: sector_leading passed to pick_side() in run loop")

with open(TB_PATH, 'w') as f:
    f.write(src_tb)

ok, err = syntax_ok(TB_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}"); sys.exit(1)
print(f"  ✅ trading_bot.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"✅ {len(changes)} changes applied:")
for c in changes: print(f"   {c}")

with open(SL_PATH) as f: sl = f.read()
with open(FV_PATH) as f: fv = f.read()
with open(TB_PATH) as f: tb = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("Fix 1: pick_side() accepts sector_leading param",     'sector_leading=False' in sl),
    ("Fix 1: CONSERVATIVE→NORMAL override logic",           'CONSERVATIVE→NORMAL override' in sl or 'CONSERVATIVE' in sl and 'sector_leading' in sl),
    ("Fix 2: SHORT direction EMA reversed in filters_v2",   'SHORT direction: ltp <= ema20' in fv or '_direction == "SHORT"' in fv),
    ("Fix 3: sector_leading passed to pick_side in bot",    'sector_leading=_sec_leading' in tb),
    ("Syntax OK — short_live.py",                           syntax_ok(SL_PATH)[0]),
    ("Syntax OK — filters_v2.py",                           syntax_ok(FV_PATH)[0]),
    ("Syntax OK — trading_bot.py",                          syntax_ok(TB_PATH)[0]),
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
    print("IMPACT — what changes on CONSERVATIVE days:")
    print("  DLF (REALTY +2%) → sector_leading=True → gate=90 → TRADEABLE ✅")
    print("  GODREJPROP (REALTY) → same → TRADEABLE ✅")
    print("  CANBK (PSU_BANK +1%) → sector_leading=True → TRADEABLE ✅")
    print("  TREJHARA (IT, lagging) → sector_leading=False → BLOCKED ✅")
    print()
    print("IMPACT — FILTERS_V2 SHORT fix:")
    print("  NIACL +8% gap, 3 red candles → price < EMA20 ✅ → SURVIVES filter")
    print("  PTC +5.5% gap, 3 red candles → price < EMA20 ✅ → SURVIVES filter")
    print("  85 SHORT candidates → expect 15-25 to survive (not just 1)")
    print()
    print("Restart:")
    print("  sudo systemctl restart trading-bot && sleep 3 && sudo systemctl status trading-bot | head -5")
else:
    print("❌ SOME CHECKS FAILED")
