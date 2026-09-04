"""
Patch script — 2026-08-04 (GAP_FADE loop fix)
Fixes:
  1. GAP_FADE loop: iterate _long_pool directly (not candidates)
     - Remove 'for _fc in candidates:' → 'for _fc in _long_pool:'
     - Remove 'if _fc.get("direction","LONG") != "LONG": continue'
     - Remove 'if _fc in _long_pool: continue'
  2. Fix accidental _fade_pool[:55] slice on line 935
     → self.candidates = _long_pool + _short_pool + _fade_pool (no slice)

Root cause: after removing pool caps, ALL direction=='LONG' stocks land in
_long_pool, so 'if _fc in _long_pool: continue' skips everything and
_fade_pool is always empty. Fix: iterate _long_pool directly to find
LONG stocks showing 3 red candles for the GAP_FADE fade strategy.

Run on server:
    cd /home/ubuntu/trading-bot
    python3 apply_patch_gapfade_20260804.py
"""

import shutil, sys, ast

filepath = '/home/ubuntu/trading-bot/trading_bot.py'
backup   = filepath + '.bak_gapfade_20260804'

shutil.copy(filepath, backup)
print(f"✅ Backup saved → {backup}")

with open(filepath, 'r') as f:
    lines = f.readlines()

changes = []

i = 0
while i < len(lines):
    line = lines[i]

    # Fix 1a — change loop to iterate _long_pool
    if 'for _fc in candidates:' in line:
        # Make sure we're inside the GAP_FADE block (check nearby context)
        context = ''.join(lines[max(0,i-5):i])
        if 'GAP-FADE' in context or '_fade_pool' in context or 'GAP_FADE' in context:
            lines[i] = line.replace('for _fc in candidates:', 'for _fc in _long_pool:')
            changes.append(f"[Line {i+1}] GAP_FADE loop: 'for _fc in candidates' → 'for _fc in _long_pool'")

    # Fix 1b — remove direction guard (unneeded — _long_pool is all LONG already)
    elif 'if _fc.get("direction","LONG") != "LONG": continue' in line:
        context = ''.join(lines[max(0,i-10):i])
        if 'GAP-FADE' in context or '_fade_pool' in context or 'for _fc in _long_pool' in ''.join(lines[max(0,i-3):i]):
            lines[i] = ''   # remove line
            changes.append(f"[Line {i+1}] Removed direction guard (unneeded — _long_pool is all LONG)")

    # Fix 1c — remove 'if _fc in _long_pool: continue' guard
    elif 'if _fc in _long_pool: continue' in line:
        context = ''.join(lines[max(0,i-10):i])
        if 'GAP-FADE' in context or '_fade_pool' in context:
            lines[i] = ''   # remove line
            changes.append(f"[Line {i+1}] Removed 'if _fc in _long_pool: continue' guard")

    # Fix 2 — fix accidental _fade_pool[:55] slice
    elif '_long_pool + _short_pool + _fade_pool[:55]' in line:
        lines[i] = line.replace('_fade_pool[:55]', '_fade_pool')
        changes.append(f"[Line {i+1}] Fixed accidental _fade_pool[:55] → _fade_pool (no slice)")

    i += 1

# Write back
with open(filepath, 'w') as f:
    f.writelines(lines)

print(f"\n{'='*60}")
print(f"✅ Patch applied — {len(changes)} changes:")
for c in changes:
    print(f"   {c}")

# Verification
with open(filepath, 'r') as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")

checks = [
    ("GAP_FADE loop iterates _long_pool",
     'for _fc in _long_pool:' in result),
    ("Direction guard removed",
     'if _fc.get("direction","LONG") != "LONG": continue' not in result),
    ("_long_pool membership guard removed from GAP_FADE",
     'if _fc in _long_pool: continue' not in result),
    ("No accidental _fade_pool[:55] slice",
     '_fade_pool[:55]' not in result),
    ("candidates merge still intact",
     '_long_pool + _short_pool + _fade_pool' in result),
    ("_short_candidates includes _fade_pool",
     'self._short_candidates = _short_pool + _fade_pool' in result),
    ("check_and_kill_dead_trade fully absent",
     'check_and_kill_dead_trade' not in result),
]

all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

# Syntax check
try:
    ast.parse(result)
    print(f"   ✅ Syntax OK (ast.parse passed)")
except SyntaxError as e:
    print(f"   ❌ SYNTAX ERROR: {e}")
    shutil.copy(backup, filepath)
    print(f"   Restored from {backup}")
    all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED — safe to restart the bot")
    print("\nTomorrow morning you should see:")
    print("   GAP_FADE SHORT: STOCKXYZ gap=+X.XX% (positive gap, 3 red candles → fade)")
    print("   GAP_FADE pool: ['STOCKXYZ', ...]")
else:
    print("❌ SOME CHECKS FAILED — restore with:")
    print(f"   cp {backup} {filepath}")
