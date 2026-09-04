"""
Patch script — 2026-08-04 (Remove all arbitrary pool caps)
Fixes:
  1. Remove [:25] from _long_pool initial build   (line 878)
  2. Remove [:25] from _short_pool initial build  (line 879)
  3. Remove [:25] from _fallback_long source      (line ~901)
  4. Remove 'if len(_long_pool) >= 25: break'     (line ~903)
  5. Remove [:25] from _fallback_short source     (line ~910)
  6. Remove 'if len(_short_pool) >= 25: break'    (line ~912)
  7. Remove [:55] from candidates merge           (line 937)

FILTERS_V2 (apply_regime_filters) is the correct quality gate — no need
for arbitrary number caps before it. All qualifying stocks pass through.

Run on server:
    cd /home/ubuntu/trading-bot
    python3 apply_patch_remove_caps_20260804.py
"""

import shutil, sys, re

filepath = '/home/ubuntu/trading-bot/trading_bot.py'
backup   = filepath + '.bak_nocaps_20260804'

shutil.copy(filepath, backup)
print(f"✅ Backup saved → {backup}")

with open(filepath, 'r') as f:
    lines = f.readlines()

changes = []

for i, line in enumerate(lines):
    stripped = line.rstrip()

    # Fix 1 — long pool initial slice cap
    if re.search(r"_long_pool\s*=\s*\[c for c in candidates.*'LONG'\s*\]\[:25\]", stripped):
        lines[i] = line.replace('][:25]', ']')
        changes.append(f"[Line {i+1}] Removed [:25] cap from _long_pool initial build")

    # Fix 2 — short pool initial slice cap
    elif re.search(r"_short_pool\s*=\s*\[c for c in candidates.*'SHORT'\s*\]\[:25\]", stripped):
        lines[i] = line.replace('][:25]', ']')
        changes.append(f"[Line {i+1}] Removed [:25] cap from _short_pool initial build")

    # Fix 3 — fallback_long source slice
    elif re.search(r"_fallback_long\s*=\s*\[c for c in candidates.*'LONG'", stripped) and '][:25]' in stripped:
        lines[i] = line.replace('][:25]', ']')
        changes.append(f"[Line {i+1}] Removed [:25] cap from _fallback_long source")

    # Fix 4 — candle fallback LONG break threshold — remove entire line
    elif re.search(r"if len\(_long_pool\)\s*>=\s*25:\s*break", stripped):
        lines[i] = ''   # blank the line (preserve line count, avoids index shift issues)
        changes.append(f"[Line {i+1}] Removed 'if len(_long_pool) >= 25: break'")

    # Fix 5 — fallback_short source slice
    elif re.search(r"_fallback_short\s*=\s*\[c for c in candidates.*'SHORT'", stripped) and '][:25]' in stripped:
        lines[i] = line.replace('][:25]', ']')
        changes.append(f"[Line {i+1}] Removed [:25] cap from _fallback_short source")

    # Fix 6 — candle fallback SHORT break threshold — remove entire line
    elif re.search(r"if len\(_short_pool\)\s*>=\s*25:\s*break", stripped):
        lines[i] = ''
        changes.append(f"[Line {i+1}] Removed 'if len(_short_pool) >= 25: break'")

    # Fix 7 — total candidates merge cap
    elif re.search(r"self\.candidates\s*=\s*\(_long_pool\s*\+\s*_short_pool\s*\+\s*_fade_pool\)\[:55\]", stripped):
        lines[i] = line.replace('][:55]', ']')  # remove the slice entirely
        lines[i] = lines[i].replace('(_long_pool + _short_pool + _fade_pool)', '_long_pool + _short_pool + _fade_pool')
        changes.append(f"[Line {i+1}] Removed [:55] cap from candidates merge")

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
    ("_long_pool initial — no [:25] cap",
     bool(re.search(r"_long_pool\s*=\s*\[c for c in candidates.*'LONG'\s*\](?!\[:)", result))),
    ("_short_pool initial — no [:25] cap",
     bool(re.search(r"_short_pool\s*=\s*\[c for c in candidates.*'SHORT'\s*\](?!\[:)", result))),
    ("_fallback_long — no [:25] cap",
     "if len(_long_pool) >= 25: break" not in result),
    ("_fallback_short — no [:25] cap",
     "if len(_short_pool) >= 25: break" not in result),
    ("no [:55] on candidates merge",
     "][:55]" not in result),
    ("no [:35] on candidates merge (old cap)",
     "][:35]" not in result),
    ("_short_candidates includes _fade_pool (from earlier patch)",
     "self._short_candidates = _short_pool + _fade_pool" in result),
    ("FILTERS_V2 hook still intact",
     "filters_v2.apply_regime_filters" in result),
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
    shutil.copy(backup, filepath)
    print(f"   Restored from {backup}")
    all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED — safe to restart the bot")
else:
    print("❌ SOME CHECKS FAILED — restore with:")
    print(f"   cp {backup} {filepath}")
