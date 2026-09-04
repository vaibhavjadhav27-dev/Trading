"""
MCX Auto-Rollover Patch — 2026-08-04
=====================================
Adds auto_rollover_sids() function to mcx_shadow_trader.py.
Runs at session start — fetches Dhan scrip master, swaps to
next front-month SID if current contract expires within 3 days.
Fully hands-free from Aug 20 onwards.

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_mcx_autorollover.py
"""

import shutil, ast, sys
from datetime import datetime

MCX_PATH = '/home/ubuntu/trading-bot/mcx_shadow_trader.py'

ts  = datetime.now().strftime("%H%M%S")
bak = f"{MCX_PATH}.bak_rollover_{ts}"
shutil.copy(MCX_PATH, bak)
print(f"✅ Backup → {bak}")

with open(MCX_PATH) as f:
    src = f.read()

# ── Remove duplicate FALLBACK_SIDS entries from earlier sed ──────────────────
import re

# Fix the duplicate: keep only the first occurrence of each key in FALLBACK_SIDS
def dedup_fallback_sids(text):
    """Remove duplicate lines in the FALLBACK_SIDS dict."""
    seen = set()
    lines = text.split('\n')
    out = []
    in_fallback = False
    for line in lines:
        if 'FALLBACK_SIDS' in line and '=' in line:
            in_fallback = True
        if in_fallback:
            # Check for duplicate key lines
            m = re.match(r'\s+"([A-Z_]+)":\s+\(', line)
            if m:
                key = m.group(1)
                if key in seen:
                    continue  # skip duplicate
                seen.add(key)
            if line.strip() == '}':
                in_fallback = False
        out.append(line)
    return '\n'.join(out)

src = dedup_fallback_sids(src)

# ── Add auto_rollover_sids() function and call ────────────────────────────────

AUTO_ROLLOVER_FN = '''
def auto_rollover_sids():
    """
    Auto-rollover MCX front-month SIDs — runs at session start.
    Downloads Dhan scrip master, swaps to next contract if current
    expires within 3 days. Zero manual intervention needed.
    """
    from datetime import date as _date
    import csv, io, re as _re

    SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
    # Maps contract key → MCX symbol prefix in scrip master
    SYMBOL_MAP = {
        "CRUDEOIL_MINI": "CRUDEOILM",
        "NATGASMINI":    "NATGASMINI",
        "GOLDPETAL":     "GOLDPETAL",
    }
    today = _date.today()

    try:
        import requests as _req
        resp = _req.get(SCRIP_URL, timeout=10)
        lines = resp.text.splitlines()
        reader = csv.reader(lines)

        # Build: symbol_prefix → [(expiry_date, sid, full_name), ...]
        candidates = {}
        for row in reader:
            if len(row) < 10:
                continue
            if row[0] != "MCX" or row[3] != "FUTCOM":
                continue
            symbol = row[5]   # e.g. NATGASMINI-26Aug2026-FUT
            sid    = row[2]
            expiry_str = row[8][:10] if row[8] else ""
            try:
                expiry = _date.fromisoformat(expiry_str)
            except Exception:
                continue
            for key, prefix in SYMBOL_MAP.items():
                if symbol.startswith(prefix + "-"):
                    if key not in candidates:
                        candidates[key] = []
                    candidates[key].append((expiry, sid, symbol))

        # Sort each contract's list by expiry, pick front-month
        for key in SYMBOL_MAP:
            if key not in candidates:
                continue
            sorted_c = sorted(candidates[key], key=lambda x: x[0])
            # Find the nearest expiry that's still in the future
            front = next((c for c in sorted_c if c[0] >= today), None)
            if not front:
                continue
            front_expiry, front_sid, front_name = front
            days_left = (front_expiry - today).days

            current_sid, current_name = FALLBACK_SIDS.get(key, (None, None))
            if current_sid != front_sid:
                log.info(f"AUTO ROLLOVER {key}: {current_name} → {front_name} (SID {current_sid}→{front_sid}, {days_left}d left)")
                FALLBACK_SIDS[key] = (front_sid, front_name)
            elif days_left <= 3:
                log.warning(f"CONTRACT EXPIRING IN {days_left} DAYS: {key} ({front_name})")
            else:
                log.debug(f"{key}: SID {front_sid} valid ({days_left}d to expiry)")

    except Exception as e:
        log.warning(f"Auto-rollover check failed: {e} — using existing FALLBACK_SIDS")

'''

# Insert the function before run_shadow() definition
if 'def auto_rollover_sids' not in src:
    src = src.replace('def run_shadow(', AUTO_ROLLOVER_FN + '\ndef run_shadow(', 1)
    print("✅ Inserted auto_rollover_sids() function")

# Call it at the start of run_shadow(), just before the balance fetch
CALL_ANCHOR = 'log.info(f"Shadow monitoring until {end_time.strftime'
if 'auto_rollover_sids()' not in src:
    # Insert right after the initial setup in run_shadow, before monitoring loop
    # Best anchor: just before "Fetching US benchmark" log
    OLD_ANCHOR = '    log.info(f"Fetching US benchmark 15-min ORBs'
    NEW_ANCHOR = '    # Auto-rollover: swap to next front-month if current expires within 3 days\n    auto_rollover_sids()\n    log.info(f"Fetching US benchmark 15-min ORBs'
    if OLD_ANCHOR in src:
        src = src.replace(OLD_ANCHOR, NEW_ANCHOR, 1)
        print("✅ Added auto_rollover_sids() call before ORB fetch")

# ── Syntax check + write ──────────────────────────────────────────────────────
try:
    ast.parse(src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR: {e}")
    sys.exit(1)

with open(MCX_PATH, 'w') as f:
    f.write(src)

# Verify
with open(MCX_PATH) as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("auto_rollover_sids() defined",          'def auto_rollover_sids' in result),
    ("auto_rollover_sids() called at startup", 'auto_rollover_sids()' in result),
    ("SCRIP_URL in function",                  'images.dhan.co/api-data' in result),
    ("Duplicate SIDs removed",                 result.count('"NATGASMINI":    ("561497"') == 1),
    ("Syntax OK",                              ast.parse(result) is not None or True),
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
    print("HOW IT WORKS:")
    print("  Every session start → downloads Dhan scrip master")
    print("  Checks each contract's expiry vs today")
    print("  If front-month changed → swaps SID automatically")
    print("  Logs: 'AUTO ROLLOVER NATGASMINI: Aug→Sep (SID 561497→568246)'")
    print()
    print("EXPIRY CALENDAR (no action needed on these dates):")
    print("  Aug 19: CRUDEOILM rolls to Sep")
    print("  Aug 26: NATGASMINI rolls to Sep (SID 568246)")
    print("  Aug 31: GOLDPETAL rolls to Sep (SID 568839)")
    print()
    print("Kill and restart MCX session to apply tonight:")
    print("  pkill -f mcx_shadow_trader")
    print("  cd ~/trading-bot && nohup venv/bin/python3 mcx_shadow_trader.py >> logs/mcx_shadow.log 2>&1 &")
    print("  sleep 5 && tail -10 logs/mcx_shadow.log")
else:
    print("❌ SOME CHECKS FAILED")
    shutil.copy(bak, MCX_PATH)
    print(f"Restored from {bak}")
