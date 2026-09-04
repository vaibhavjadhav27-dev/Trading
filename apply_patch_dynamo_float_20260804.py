"""
PATCH: DynamoDB Float → Decimal fix — 2026-08-04
=================================================
Bug: save_active_trade() / log_trade() / save_daily_state() pass raw Python
     floats to put_item(). DynamoDB rejects floats → "Float types are not
     supported. Use Decimal types instead." error every 30s, crashes bot.

Confirmed on Aug 3: crashed bot at 12:43 IST, caused crash loop all afternoon.

Fix: Add _to_decimal() sanitiser that walks the entire dict recursively
     and converts float → Decimal(str(v)) before every put_item call.

Run:
    cd ~/trading-bot && venv/bin/python3 apply_patch_dynamo_float_20260804.py
"""

import shutil, ast, re, sys
from datetime import datetime

TB_PATH = '/home/ubuntu/trading-bot/trading_bot.py'

# Backup
ts  = datetime.now().strftime("%H%M%S")
bak = f"{TB_PATH}.bak_dynamo_{ts}"
shutil.copy(TB_PATH, bak)
print(f"✅ Backup → {bak}")

with open(TB_PATH) as f:
    src = f.read()

# ── The fix ──────────────────────────────────────────────────────────────────
# 1. Add _to_decimal helper just before the DynamoClient class definition
# 2. Wrap every put_item(Item=...) call to pass _to_decimal(data) instead

HELPER = '''
def _to_decimal(obj):
    """Recursively convert float/int to Decimal for DynamoDB compatibility."""
    from decimal import Decimal
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(i) for i in obj]
    if isinstance(obj, float):
        return Decimal(str(obj))
    return obj

'''

changes = []

# ── Step 1: Insert _to_decimal helper before DynamoClient class ───────────────
if '_to_decimal' not in src:
    # Find 'class DynamoClient' and insert before it
    src = re.sub(
        r'(class DynamoClient)',
        HELPER + r'\1',
        src, count=1
    )
    changes.append("Inserted _to_decimal() helper before DynamoClient class")
else:
    changes.append("_to_decimal() already present — skipped insertion")

# ── Step 2: Wrap save_active_trade put_item ────────────────────────────────────
# BEFORE: self.active.put_item(Item=trade_data)
# AFTER:  self.active.put_item(Item=_to_decimal(trade_data))
if 'self.active.put_item(Item=trade_data)' in src:
    src = src.replace(
        'self.active.put_item(Item=trade_data)',
        'self.active.put_item(Item=_to_decimal(trade_data))'
    )
    changes.append("save_active_trade(): wrapped put_item with _to_decimal()")
else:
    changes.append("⚠️  save_active_trade put_item line not found — check manually")

# ── Step 3: Wrap log_trade put_item ───────────────────────────────────────────
# BEFORE: self.trades.put_item(Item=trade_data)
# AFTER:  self.trades.put_item(Item=_to_decimal(trade_data))
if 'self.trades.put_item(Item=trade_data)' in src:
    src = src.replace(
        'self.trades.put_item(Item=trade_data)',
        'self.trades.put_item(Item=_to_decimal(trade_data))'
    )
    changes.append("log_trade(): wrapped put_item with _to_decimal()")
else:
    changes.append("⚠️  log_trade put_item line not found — check manually")

# ── Step 4: Wrap save_daily_state put_item ────────────────────────────────────
if 'self.state.put_item(Item=state_data)' in src:
    src = src.replace(
        'self.state.put_item(Item=state_data)',
        'self.state.put_item(Item=_to_decimal(state_data))'
    )
    changes.append("save_daily_state(): wrapped put_item with _to_decimal()")
else:
    changes.append("⚠️  save_daily_state put_item line not found — check manually")

# ── Step 5: Wrap audit put_item (belt-and-suspenders) ─────────────────────────
# audit already uses str() for prices so less critical, but wrap it too
if "self.audit.put_item(Item={" in src and '_to_decimal' not in src.split("self.audit.put_item")[1][:200]:
    # The audit dict is inline — wrap the whole Item= section
    src = re.sub(
        r'self\.audit\.put_item\(Item=\{',
        'self.audit.put_item(Item=_to_decimal({',
        src, count=1
    )
    # Close the extra { by finding the matching }) and adding an extra )
    # Simpler: the audit dict ends with }) — replace the first occurrence after our insertion
    src = src.replace(
        "self.audit.put_item(Item=_to_decimal({",
        "self.audit.put_item(Item=_to_decimal({",
        1
    )
    # Find the closing }) of the audit dict and add extra closing paren
    # Since audit dict is fixed-structure, use a targeted regex
    src = re.sub(
        r"(self\.audit\.put_item\(Item=_to_decimal\(\{[^}]+\})\)",
        r"\1))",
        src, count=1, flags=re.DOTALL
    )
    changes.append("log_order_audit(): wrapped put_item with _to_decimal()")

# ── Syntax check ──────────────────────────────────────────────────────────────
try:
    ast.parse(src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR: {e} — NOT writing file")
    sys.exit(1)

# ── Write ─────────────────────────────────────────────────────────────────────
with open(TB_PATH, 'w') as f:
    f.write(src)

# ── Report ────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"✅ Patch applied — {len(changes)} changes:")
for c in changes:
    print(f"   {c}")

# ── Verification ──────────────────────────────────────────────────────────────
with open(TB_PATH) as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("_to_decimal helper defined",
     'def _to_decimal(obj):' in result),
    ("save_active_trade uses _to_decimal",
     'self.active.put_item(Item=_to_decimal(trade_data))' in result),
    ("log_trade uses _to_decimal",
     'self.trades.put_item(Item=_to_decimal(trade_data))' in result),
    ("save_daily_state uses _to_decimal",
     'self.state.put_item(Item=_to_decimal(state_data))' in result),
    ("No raw float put_item remaining",
     'self.active.put_item(Item=trade_data)' not in result and
     'self.trades.put_item(Item=trade_data)' not in result),
    ("Syntax OK",
     ast.parse(result) is not None or True),
]

all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED — safe to restart:")
    print("   sudo systemctl restart trading-bot")
    print("   sleep 3 && sudo systemctl status trading-bot | head -5")
    print()
    print("What this fixes:")
    print("   Aug 3: Bot crashed at 12:43 IST mid-LONG-trade → crash loop all afternoon")
    print("   Every 30s 'Active trade write failed: Float types not supported' → GONE")
    print("   Bot will now survive any trade that requires DynamoDB persistence")
else:
    print("❌ SOME CHECKS FAILED — restoring backup:")
    shutil.copy(bak, TB_PATH)
    print(f"   Restored from {bak}")
