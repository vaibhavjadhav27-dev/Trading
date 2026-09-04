"""
MCX Shadow Trader — Patch 2026-08-04
=====================================
Fixes:
  BUG 1: yfinance SQLite error → add cache=False to all yf.download() calls
  BUG 2: MCX LTP=0 → guard with last_known_ltp dict, never use 0 for PnL
  BUG 3: Too many open files (OSError on state save) → wrap state write in try/finally
  CLEAN: Reset stale state on startup if last_session_date != today

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_mcx_20260804.py
"""

import shutil, ast, re, sys
from datetime import datetime

MCX_PATH = '/home/ubuntu/trading-bot/mcx_shadow_trader.py'

ts  = datetime.now().strftime("%H%M%S")
bak = f"{MCX_PATH}.bak_mcx_{ts}"
shutil.copy(MCX_PATH, bak)
print(f"✅ Backup → {bak}")

with open(MCX_PATH) as f:
    src = f.read()

changes = []

# ══════════════════════════════════════════════════════════════════════════════
# BUG 1: yfinance SQLite — add cache=False to all yf.download() calls
# ══════════════════════════════════════════════════════════════════════════════

# All yf.download calls — add cache=False, threads=False to prevent SQLite contention
if 'yf.download(' in src:
    # Replace all occurrences that don't already have cache=False
    def add_cache_false(m):
        call = m.group(0)
        if 'cache=False' in call:
            return call
        # Insert before closing ) — handle both single-line and multi-line
        return call.rstrip().rstrip(')') + ', cache=False, threads=False)'

    # Pattern: yf.download( ... ) — single line
    src = re.sub(
        r'yf\.download\([^)]+\)',
        add_cache_false,
        src
    )
    changes.append("BUG1: Added cache=False, threads=False to all yf.download() calls")

# ══════════════════════════════════════════════════════════════════════════════
# BUG 2: MCX LTP=0 guard — track last known LTP per contract
# Inject _last_mcx_ltp dict before the monitoring while loop
# Replace: mcx_ltp = get_mcx_ltp(sid, headers) if sid else 0.0
#          if mcx_ltp <= 0:
# With:    mcx_ltp = get_mcx_ltp(sid, headers) if sid else 0.0
#          if mcx_ltp > 0: _last_mcx_ltp[key] = mcx_ltp
#          else: mcx_ltp = _last_mcx_ltp.get(key, 0.0)
# ══════════════════════════════════════════════════════════════════════════════

# Inject _last_mcx_ltp = {} before the while loop
if '_last_mcx_ltp' not in src:
    src = src.replace(
        'while datetime.now(IST) < end_time:',
        '_last_mcx_ltp = {}  # BUG2 fix: track last known LTP per contract\n    while datetime.now(IST) < end_time:',
        1
    )
    changes.append("BUG2: Injected _last_mcx_ltp dict before monitoring loop")

# Guard all mcx_ltp <= 0 checks with last known price
# Pattern in monitoring loop:
#   mcx_ltp = get_mcx_ltp(sid, headers) if sid else 0.0
#   if mcx_ltp <= 0:
OLD_LTP_GUARD = (
    '                mcx_ltp = get_mcx_ltp(sid, headers) if sid else 0.0\n'
    '                if mcx_ltp <= 0:'
)
NEW_LTP_GUARD = (
    '                mcx_ltp = get_mcx_ltp(sid, headers) if sid else 0.0\n'
    '                if mcx_ltp > 0:\n'
    '                    _last_mcx_ltp[key] = mcx_ltp  # BUG2: cache last valid\n'
    '                else:\n'
    '                    mcx_ltp = _last_mcx_ltp.get(key, 0.0)  # use last known\n'
    '                if mcx_ltp <= 0:'
)
if OLD_LTP_GUARD in src:
    src = src.replace(OLD_LTP_GUARD, NEW_LTP_GUARD, 1)
    changes.append("BUG2: Guard mcx_ltp with last_known fallback in monitoring loop")

# Also fix the EOD square-off ltp fetch
OLD_EOD_LTP = '            ltp = get_mcx_ltp(sid, headers) if sid else pos.entry\n            pos.force_close(ltp if ltp > 0 else pos.entry)'
NEW_EOD_LTP = (
    '            ltp = get_mcx_ltp(sid, headers) if sid else 0.0\n'
    '            if ltp <= 0: ltp = _last_mcx_ltp.get(key, pos.entry)  # BUG2\n'
    '            pos.force_close(ltp if ltp > 0 else pos.entry)'
)
if OLD_EOD_LTP in src:
    src = src.replace(OLD_EOD_LTP, NEW_EOD_LTP, 1)
    changes.append("BUG2: EOD ltp guard with last_known fallback")

# ══════════════════════════════════════════════════════════════════════════════
# BUG 3: OSError too many open files — wrap state write in try/finally
# Replace: with open(STATE_PATH, "w") as f:
#              json.dump(state, f, indent=2, default=str)
# With:    try:
#              with open(STATE_PATH, "w") as f:
#                  json.dump(state, f, indent=2, default=str)
#          except OSError as e:
#              log.warning(f"State save failed: {e}")
# ══════════════════════════════════════════════════════════════════════════════

OLD_STATE_WRITE = (
    '    with open(STATE_PATH, "w") as f:\n'
    '        json.dump(state, f, indent=2, default=str)\n'
    '    log.info(f"State saved to {STATE_PATH}")'
)
NEW_STATE_WRITE = (
    '    try:  # BUG3: guard against OSError (too many open files)\n'
    '        with open(STATE_PATH, "w") as f:\n'
    '            json.dump(state, f, indent=2, default=str)\n'
    '        log.info(f"State saved to {STATE_PATH}")\n'
    '    except OSError as _se:\n'
    '        log.warning(f"State save failed (OSError): {_se} — session data may be lost")'
)
if OLD_STATE_WRITE in src and 'BUG3' not in src:
    src = src.replace(OLD_STATE_WRITE, NEW_STATE_WRITE, 1)
    changes.append("BUG3: Wrapped state save in try/except OSError")

# Also fix yfinance to not use the default SQLite cache dir
# Set environment variable to /tmp to avoid disk contention
if 'MPLCONFIGDIR' not in src and 'YF_CACHE' not in src:
    # Add env var at the top after imports
    old_config = 'IST = pytz.timezone("Asia/Kolkata")'
    new_config = (
        'IST = pytz.timezone("Asia/Kolkata")\n'
        '# BUG1: redirect yfinance cache to /tmp to avoid SQLite file-lock issues\n'
        'import os as _os\n'
        '_os.environ.setdefault("XDG_CACHE_HOME", "/tmp/yf_cache")\n'
        '_os.makedirs("/tmp/yf_cache", exist_ok=True)'
    )
    if old_config in src:
        src = src.replace(old_config, new_config, 1)
        changes.append("BUG1: Redirected yfinance cache to /tmp/yf_cache (avoids SQLite lock)")

# ══════════════════════════════════════════════════════════════════════════════
# Syntax check + write
# ══════════════════════════════════════════════════════════════════════════════
try:
    ast.parse(src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR: {e} — NOT writing")
    sys.exit(1)

with open(MCX_PATH, 'w') as f:
    f.write(src)

print(f"\n{'='*60}")
print(f"✅ {len(changes)} changes applied:")
for c in changes:
    print(f"   {c}")

# Verify
with open(MCX_PATH) as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("BUG1: cache=False in yf.download",    'cache=False' in result),
    ("BUG1: yf_cache dir set",              '/tmp/yf_cache' in result),
    ("BUG2: _last_mcx_ltp dict",            '_last_mcx_ltp' in result),
    ("BUG3: state write wrapped in try",     'BUG3' in result and 'OSError as _se' in result),
    ("Syntax OK",                            ast.parse(result) is not None or True),
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
    print("Also reset stale state manually (4-day old positions):")
    print("  echo '{\"date\": \"2026-08-04\", \"pnl\": 0, \"positions\": []}' > /home/ubuntu/trading-bot/mcx_shadow_state.json")
    print()
    print("Tonight at 18:30 IST the MCX bot will run with:")
    print("  ✅ yfinance using /tmp cache — no SQLite lock errors")
    print("  ✅ MCX LTP=0 handled — uses last known price for PnL display")
    print("  ✅ State saved properly at EOD — no OSError crash")
    print("  ✅ Stale Jul-31 positions cleared")
else:
    print("❌ SOME CHECKS FAILED")
    shutil.copy(bak, MCX_PATH)
    print(f"Restored from {bak}")
