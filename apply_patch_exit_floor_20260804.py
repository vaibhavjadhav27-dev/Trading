"""
PATCH: Profit-Lock Floor + Confirm-Exit Hard Override — 2026-08-04
===================================================================
Issue #4: profit_lock_floor first step 0.60→0.55 exits below ₹2,700 target.
  FIX: Remove 0.60→0.55 step. Change 1.00→0.75 to 1.10→1.00.
  Result: first floor exit at 1.00% gross = ₹2,700 at 5X.

Issue #2: confirm_exit() with stale last_vwap=0 refuses exits permanently.
  FIX: Hard override — if profit drops 0.15%+ below floor, exit regardless.

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_exit_floor_20260804.py
"""

import shutil, ast, sys
from datetime import datetime

def backup(path):
    ts  = datetime.now().strftime("%H%M%S")
    dst = f"{path}.bak_exitfloor_{ts}"
    shutil.copy(path, dst)
    print(f"  ✅ Backup → {dst}")

def syntax_ok(path):
    with open(path) as f: src = f.read()
    try:
        ast.parse(src); return True, ""
    except SyntaxError as e:
        return False, str(e)

changes = []

# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 — short_live.py: profit_lock_floor ladder
# Remove:  if peak_pct >= 0.60: return 0.55
# Change:  if peak_pct >= 1.00: return 0.75
#       →  if peak_pct >= 1.10: return 1.00
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("FIX 1: short_live.py — profit_lock_floor ladder")
print("="*60)

SL_PATH = '/home/ubuntu/trading-bot/short_live.py'
backup(SL_PATH)

with open(SL_PATH) as f:
    src = f.read()

# Remove the 0.60→0.55 step entirely
if 'if peak_pct >= 0.60: return 0.55' in src:
    src = src.replace('    if peak_pct >= 0.60: return 0.55\n', '')
    changes.append("Removed profit_lock_floor 0.60→0.55 step")
else:
    print("  ⚠️  0.60→0.55 line not found — may already be removed")

# Change 1.00→0.75 to 1.10→1.00
if 'if peak_pct >= 1.00: return 0.75' in src:
    src = src.replace(
        'if peak_pct >= 1.00: return 0.75',
        'if peak_pct >= 1.10: return 1.00'
    )
    changes.append("Changed profit_lock_floor 1.00→0.75 to 1.10→1.00")

with open(SL_PATH, 'w') as f:
    f.write(src)

ok, err = syntax_ok(SL_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}")
    sys.exit(1)
print(f"  ✅ short_live.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — patch_integrate.py: hard override for stale VWAP
# Find the floor breach check block and add override before confirm_exit call
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("FIX 2: patch_integrate.py — hard override for stale VWAP")
print("="*60)

PI_PATH = '/home/ubuntu/trading-bot/patch_integrate.py'
backup(PI_PATH)

with open(PI_PATH) as f:
    lines = f.readlines()

# Find the confirm_exit call block and insert hard override before it
# Anchor: "            _should_exit, _exit_reason = confirm_exit("
ANCHOR = '            _should_exit, _exit_reason = confirm_exit('
OVERRIDE = (
    '            # Hard override (Issue #2 fix 2026-08-04):\n'
    '            # If stale VWAP (=0) or profit dropped 0.15%+ below floor → exit immediately\n'
    '            _cur_pct = current_profit_pct(entry_price, ltp, side)\n'
    '            _hard_exit = (_vwap == 0 and breached) or \\\n'
    '                         (floor_pct is not None and breached and \\\n'
    '                          (_cur_pct < floor_pct - 0.15 if side == "LONG"\n'
    '                           else _cur_pct < floor_pct - 0.15))\n'
    '            if _hard_exit:\n'
    '                _should_exit, _exit_reason = True, "FLOOR_HARD_OVERRIDE (stale VWAP or 0.15% below floor)"\n'
    '            else:\n'
)

inserted = False
for i, line in enumerate(lines):
    if ANCHOR in line and 'FLOOR_HARD_OVERRIDE' not in ''.join(lines[max(0,i-5):i]):
        # Insert override before this line, and indent the confirm_exit call one extra level
        lines.insert(i, OVERRIDE)
        # The confirm_exit call itself now needs to be in the else block — add extra indent
        lines[i+1] = '    ' + lines[i+1]  # add 4 spaces to existing 12-space indent
        inserted = True
        changes.append(f"patch_integrate.py L{i+1}: hard override before confirm_exit()")
        break

if not inserted:
    print("  ⚠️  confirm_exit anchor not found or already patched")

with open(PI_PATH, 'w') as f:
    f.writelines(lines)

ok, err = syntax_ok(PI_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}")
    # restore
    shutil.copy(f"{PI_PATH}.bak_exitfloor_{datetime.now().strftime('%H%M%S')}", PI_PATH)
    sys.exit(1)
print(f"  ✅ patch_integrate.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"✅ {len(changes)} changes applied:")
for c in changes: print(f"   {c}")

with open(SL_PATH) as f: sl = f.read()
with open(PI_PATH) as f: pi = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("0.60→0.55 step removed",            'if peak_pct >= 0.60: return 0.55' not in sl),
    ("First lock now 1.10→1.00",          'if peak_pct >= 1.10: return 1.00' in sl),
    ("1.50→1.30 still present",           'if peak_pct >= 1.50: return 1.30' in sl),
    ("2.00→1.75 still present",           'if peak_pct >= 2.00: return 1.75' in sl),
    ("Hard override in patch_integrate",  'FLOOR_HARD_OVERRIDE' in pi),
    ("Stale VWAP check present",          '_vwap == 0' in pi),
    ("0.15% below floor check present",   '0.15' in pi),
    ("Syntax OK — short_live.py",         syntax_ok(SL_PATH)[0]),
    ("Syntax OK — patch_integrate.py",    syntax_ok(PI_PATH)[0]),
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
    print("NEW PROFIT-LOCK BEHAVIOUR:")
    print("  Peak <1.10%  → no floor → hold (hard SL at 0.75% protects)")
    print("  Peak ≥1.10%  → floor = 1.00% → exit at 1.00% gross minimum")
    print("  Peak ≥1.50%  → floor = 1.30%")
    print("  Peak ≥2.00%  → floor = 1.75%")
    print("  Peak ≥2.30%  → floor = peak - 0.15% (trails every 0.30%)")
    print()
    print("  At 5X (₹2,70,000): exit at 1.00% = ₹2,700 floor ✅")
    print("  At 4X (₹2,16,000): exit at 1.00% = ₹2,160 (needs 1.25% for ₹2,700)")
    print()
    print("STALE VWAP OVERRIDE:")
    print("  last_vwap=0 AND floor breached → exit immediately ✅")
    print("  profit drops 0.15% below floor → exit immediately ✅")
    print()
    print("Restart:")
    print("  sudo systemctl restart trading-bot && sleep 3 && sudo systemctl status trading-bot | head -5")
else:
    print("❌ SOME CHECKS FAILED")
