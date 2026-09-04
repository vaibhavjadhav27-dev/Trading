"""
Patch script — 2026-08-04 (Pool Size + GAP_FADE fix)
Fixes:
  1. Pool cap 15 → 25 (lines 878, 879)
  2. Candle fallback pool cap 15 → 25 (lines 901, 903, 910, 912)
  3. Total candidates cap 35 → 55 (line 937)
  4. _short_candidates now includes _fade_pool (line 940)

Run on server:
    cd /home/ubuntu/trading-bot
    python3 apply_patch_pool_size_20260804.py
"""

import shutil, sys

filepath = '/home/ubuntu/trading-bot/trading_bot.py'
backup   = filepath + '.bak_pool_20260804'

shutil.copy(filepath, backup)
print(f"✅ Backup saved → {backup}")

with open(filepath, 'r') as f:
    lines = f.readlines()

changes = []

FIXES = [
    # (exact stripped line content,  old_str,              new_str,              label)

    # Fix 1a — long pool initial slice cap
    (
        "_long_pool  = [c for c in candidates if c.get('direction','LONG') == 'LONG'][:15]",
        "][:15]",
        "][:25]",
        "long pool initial cap: 15 → 25"
    ),
    # Fix 1b — short pool initial slice cap
    (
        "_short_pool = [c for c in candidates if c.get('direction','LONG') == 'SHORT'][:15]",
        "][:15]",
        "][:25]",
        "short pool initial cap: 15 → 25"
    ),
    # Fix 2a — fallback long source slice cap
    (
        "_fallback_long = [c for c in candidates if c.get('direction','LONG') == 'LONG'",
        "][:15]",
        "][:25]",
        "fallback_long source slice: 15 → 25"
    ),
    # Fix 2b — candle fallback long break threshold
    (
        "if len(_long_pool) >= 15: break",
        ">= 15",
        ">= 25",
        "candle fallback LONG break threshold: 15 → 25"
    ),
    # Fix 2c — fallback short source slice cap
    (
        "_fallback_short = [c for c in candidates if c.get('direction','LONG') == 'SHORT'",
        "][:15]",
        "][:25]",
        "fallback_short source slice: 15 → 25"
    ),
    # Fix 2d — candle fallback short break threshold
    (
        "if len(_short_pool) >= 15: break",
        ">= 15",
        ">= 25",
        "candle fallback SHORT break threshold: 15 → 25"
    ),
    # Fix 3 — total candidates merge cap
    (
        "self.candidates = (_long_pool + _short_pool + _fade_pool)[:35]",
        "][:35]",
        "][:55]",
        "total candidates merge cap: 35 → 55"
    ),
    # Fix 4 — include _fade_pool in _short_candidates
    (
        "self._short_candidates = _short_pool",
        "= _short_pool",
        "= _short_pool + _fade_pool",
        "_short_candidates now includes _fade_pool (GAP_FADE stocks scannable for breakdown)"
    ),
]

for (stub, old_str, new_str, label) in FIXES:
    found = False
    for i, line in enumerate(lines):
        if stub in line.strip():
            if old_str in line:
                lines[i] = line.replace(old_str, new_str, 1)
                changes.append(f"[Line {i+1}] {label}")
                found = True
                break
    if not found:
        print(f"⚠️  NOT FOUND: {label} | stub='{stub[:60]}'")

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
    ("long pool initial cap is 25",
     "_long_pool  = [c for c in candidates if c.get('direction','LONG') == 'LONG'][:25]" in result),
    ("short pool initial cap is 25",
     "_short_pool = [c for c in candidates if c.get('direction','LONG') == 'SHORT'][:25]" in result),
    ("candle fallback LONG break is 25",
     "if len(_long_pool) >= 25: break" in result),
    ("candle fallback SHORT break is 25",
     "if len(_short_pool) >= 25: break" in result),
    ("total candidates cap is 55",
     "][:55]" in result),
    ("_short_candidates includes _fade_pool",
     "self._short_candidates = _short_pool + _fade_pool" in result),
    ("old cap 15 gone from pool lines",
     "LONG'][:15]" not in result and "SHORT'][:15]" not in result),
    ("old cap 35 gone",
     "][:35]" not in result),
]

all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

# Syntax check
import ast
try:
    ast.parse(result)
    print(f"   ✅ Syntax OK (ast.parse passed)")
except SyntaxError as e:
    print(f"   ❌ SYNTAX ERROR: {e}")
    print(f"      Restoring backup...")
    shutil.copy(backup, filepath)
    print(f"      Restored from {backup}")
    all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED — safe to restart the bot")
else:
    print("❌ SOME CHECKS FAILED — restore with:")
    print(f"   cp {backup} {filepath}")
