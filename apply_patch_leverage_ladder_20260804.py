"""
PATCH: Leverage Ladder — 1% floor on deployed capital — 2026-08-04
===================================================================
Target: ₹2,700 MINIMUM per trade (1% on ₹2,70,000 deployed = 5X × ₹54K)
Profit is UNCAPPED — trailing SL runs winners as far as they go.
₹2,700 is the FLOOR, achieved at ~1% move. Beyond that, trail and hold.

OLD LADDER (wrong — 2X at 108-143 needed 2.5% move for ₹2,700):
  ≥80% (144/180) → 4.5X
  60-80% (108-143) → 2X
  30-60% (54-107) → 1X
  <30% → NO TRADE

NEW LADDER (₹2,700 achievable at ~1% move on all tiers):
  ≥80% (144/180) → 5X  → need 1.05% → ₹2,700 minimum
  60-80% (108-143) → 4X → need 1.31% → ₹2,700 minimum
  50-60% (90-107) → 3X  → need 1.73% → ₹2,700 minimum
  <50% (<90) → NO TRADE  (R3 gate already blocks these)

Regime multiplier R4 still applies (already deployed):
  TRENDING × 1.25 → cap 5X
  BEARISH   × 0.75
  NORMAL    × 1.0

Profit behaviour (UNCHANGED):
  Trailing SL ladder (profit_lock_floor) runs winners freely.
  ₹2,700 is reached at ~1% move — after that bot trails and holds.
  No hard profit target — stock can run 5%, 10%, unlimited.

Run:
    cd ~/trading-bot && venv/bin/python3 apply_patch_leverage_ladder_20260804.py
"""

import shutil, ast, re, sys
from datetime import datetime

SL_PATH = '/home/ubuntu/trading-bot/short_live.py'

ts  = datetime.now().strftime("%H%M%S")
bak = f"{SL_PATH}.bak_lev_{ts}"
shutil.copy(SL_PATH, bak)
print(f"✅ Backup → {bak}")

with open(SL_PATH) as f:
    src = f.read()

changes = []

# ── Replace the 3 tier lines in size_position() ──────────────────────────────
# Old pattern:
#   if pct >= 80:        # >= 80% → 4.5× MIS
#       deploy = balance * 4.5
#       leverage = 4.5
#   elif pct >= 60:     # 60-80% → 2× MIS
#       deploy = balance * 2.0
#       leverage = 2
#   elif pct >= 30:     # < 60% → 1× cash
#       deploy = balance * 1.0
#       leverage = 1
#   else:
#       return 0, 0, 0

NEW_TIERS = '''    if pct >= 80:        # ≥80% (144/180) → 5X → need ~1.05% for ₹2,700 min
        deploy = balance * 5.0
        leverage = 5.0
    elif pct >= 60:      # 60-80% (108-143) → 4X → need ~1.31%
        deploy = balance * 4.0
        leverage = 4.0
    elif pct >= 50:      # 50-60% (90-107) → 3X → need ~1.73%
        deploy = balance * 3.0
        leverage = 3.0
    else:                # <50% = below gate (R3 blocks these anyway)
        return 0, 0, 0'''

# Use regex to replace the whole tier block
old_pattern = re.compile(
    r'if pct >= 80:.*?(?=\n    # ── Tier 3|\n    # ── Tier|\n\ndef |\nfloat|qty = int)',
    re.DOTALL
)

# Find the tier block
match = old_pattern.search(src)
if match:
    src = src[:match.start()] + NEW_TIERS + src[match.end():]
    changes.append("size_position(): leverage tiers updated 4.5X/2X/1X → 5X/4X/3X")
else:
    # Fallback: direct string replacements
    replacements = [
        # Tier 1: 4.5X → 5X
        ('deploy = balance * 4.5', 'deploy = balance * 5.0'),
        ('leverage = 4.5',         'leverage = 5.0'),
        # Tier 2: 2.0X → 4X
        ('deploy = balance * 2.0', 'deploy = balance * 4.0'),
        ('leverage = 2',           'leverage = 4.0'),
        # Tier 3: 1.0X → 3X, AND change >=30 to >=50
        ('elif pct >= 30:',        'elif pct >= 50:'),
        ('deploy = balance * 1.0', 'deploy = balance * 3.0'),
        ('leverage = 1',           'leverage = 3.0'),
    ]
    for old, new in replacements:
        if old in src:
            src = src.replace(old, new, 1)
            changes.append(f"  Replaced: {old!r} → {new!r}")

# ── Update the docstring to reflect new tiers ─────────────────────────────────
old_doc = "score > 80% (144/180) → 4.5× MIS  (highest conviction)\n       score 60-80% (108-143) → 2× MIS   (good conviction)\n       score < 60% (0-107)   → 1× cash   (full available balance, no margin)"
new_doc = "score ≥80% (144/180) → 5× MIS  (₹2,700 min at 1.05% move — uncapped upside)\n       score 60-80% (108-143) → 4× MIS   (₹2,700 min at 1.31% move)\n       score 50-60% (90-107)  → 3× MIS   (₹2,700 min at 1.73% move)\n       score <50% (<90/180)   → NO TRADE (R3 gate)"
if old_doc in src:
    src = src.replace(old_doc, new_doc)
    changes.append("Updated docstring with new tiers")

# ── Syntax check ──────────────────────────────────────────────────────────────
try:
    ast.parse(src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR: {e} — NOT writing")
    sys.exit(1)

with open(SL_PATH, 'w') as f:
    f.write(src)

print(f"\n{'='*60}")
print(f"✅ Patch applied — {len(changes)} changes:")
for c in changes:
    print(f"   {c}")

# ── Verification ──────────────────────────────────────────────────────────────
with open(SL_PATH) as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")

checks = [
    ("5X tier present (≥80%)",    "balance * 5.0" in result),
    ("4X tier present (60-80%)",  "balance * 4.0" in result),
    ("3X tier present (50-60%)",  "balance * 3.0" in result),
    ("Old 4.5X removed",          "balance * 4.5" not in result),
    ("Old 2X removed",            "balance * 2.0" not in result),
    ("Old 1X tier removed",       "balance * 1.0" not in result and "leverage = 1\n" not in result),
    ("Gate is ≥50 (not ≥30)",     "pct >= 50" in result and "pct >= 30" not in result),
    ("Regime mult R4 intact",     "_rmult" in result),
    ("Syntax OK",                  ast.parse(result) is not None or True),
]

all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED")
    print()
    print("NEW LEVERAGE BEHAVIOUR:")
    print("  Score 150/180 (83%) → 5X → deploy ₹2,70,000")
    print("    1.05% move → ₹2,700 net (MINIMUM)")
    print("    3.0%  move → ₹7,500+ net (trailing SL runs it)")
    print("    5.0%  move → ₹13,000+    (uncapped)")
    print()
    print("  Score 130/180 (72%) → 4X → deploy ₹2,16,000")
    print("    1.31% move → ₹2,700 net (MINIMUM)")
    print("    3.0%  move → ₹6,000+ net (uncapped)")
    print()
    print("  Score 100/180 (55%) → 3X → deploy ₹1,62,000")
    print("    1.73% move → ₹2,700 net (MINIMUM)")
    print("    3.0%  move → ₹4,500+ net (uncapped)")
    print()
    print("Restart:")
    print("   sudo systemctl restart trading-bot")
else:
    print("❌ CHECKS FAILED — restoring:")
    shutil.copy(bak, SL_PATH)
    print(f"   Restored from {bak}")
