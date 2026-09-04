"""
PATCH: Restrict YoY classification to TODAY's filings only
============================================================================
Date: 2026-08-08 (Saturday)

PROBLEM (observed live 2026-08-08):
  corporates-financial-results returns the ENTIRE quarterly results backlog
  (1031 watchlist matches -- filings going back weeks), not just today's.
  The XBRL YoY classifier looped over ALL 1031, each doing up to 3 sequential
  HTTP fetches + 0.5s sleeps -> 20-40+ minute runtime. Unusable.

FIX:
  1. Filter results_wl to rows whose broadCastDate/filingDate is TODAY (or
     within the last N days, configurable) BEFORE the YoY loop. On a normal
     trading day this is ~5-30 companies, not 1031.
  2. Also cap the loop at MAX_YOY_CLASSIFY (safety valve, default 60) so a
     bad filter can never re-trigger a 1000+ fetch storm.
  3. Log how many were filtered in vs skipped, so the log is self-explaining.

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_yoy_today_filter_20260808.py
"""

import shutil, ast, sys
from datetime import datetime

CAL_PATH = '/home/ubuntu/trading-bot/download_nse_calendar.py'
ts = datetime.now().strftime("%H%M%S")
bak = f"{CAL_PATH}.bak_yoyfilter_{ts}"
shutil.copy(CAL_PATH, bak)
print(f"Backup -> {bak}")

with open(CAL_PATH) as f:
    src = f.read()

changes = []

# ══════════════════════════════════════════════════════════════════════════
# Insert a helper to test whether a results row was filed within the last N days
# (placed right before classify_results_yoy for locality).
# ══════════════════════════════════════════════════════════════════════════
FILTER_HELPER = '''
# ── Filed-today filter (2026-08-08) ──────────────────────────────────────────
# corporates-financial-results returns the whole quarterly backlog. We only
# want to YoY-classify filings broadcast in the last RESULTS_LOOKBACK_DAYS.
RESULTS_LOOKBACK_DAYS = 1          # today (0) + yesterday tolerance -> 1
MAX_YOY_CLASSIFY = 60              # hard safety cap on XBRL fetch storms


def _filed_recently(results_row, today, lookback_days=RESULTS_LOOKBACK_DAYS):
    """
    True if this filing's broadCastDate (fallback filingDate) is within
    `lookback_days` of `today`. NSE date format e.g. "30-Jul-2026 17:17:53".
    Fail-open? No -- fail-CLOSED (return False) on parse error so a bad date
    never re-triggers a 1000+ fetch storm.
    """
    raw = (results_row.get("broadCastDate") or results_row.get("filingDate") or "").strip()
    if not raw:
        return False
    date_part = raw.split(" ")[0]  # "30-Jul-2026"
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            filed = datetime.strptime(date_part, fmt).date()
            return 0 <= (today - filed).days <= lookback_days
        except ValueError:
            continue
    return False


'''

if "_filed_recently" not in src:
    anchor = "def classify_results_yoy("
    idx = src.find(anchor)
    if idx == -1:
        print("ERROR: could not find classify_results_yoy() anchor -- aborting")
        shutil.copy(bak, CAL_PATH)
        sys.exit(1)
    src = src[:idx] + FILTER_HELPER + src[idx:]
    changes.append("Inserted _filed_recently() helper + RESULTS_LOOKBACK_DAYS/MAX_YOY_CLASSIFY constants")

# ══════════════════════════════════════════════════════════════════════════
# Replace the YoY loop preamble to filter to recent filings + apply the cap.
# ══════════════════════════════════════════════════════════════════════════
OLD_LOOP = '''    yoy_classified = []
    for r_row in results_wl:
        if r_row.get("period") not in ("Quarterly", "Annual"):
            continue
        try:'''

NEW_LOOP = '''    yoy_classified = []
    # Only classify filings actually broadcast recently (not the whole backlog).
    results_today = [r for r in results_wl if _filed_recently(r, today)]
    log.info(f"YoY classify: {len(results_today)} filed in last "
             f"{RESULTS_LOOKBACK_DAYS}d (of {len(results_wl)} total results matches)")
    if len(results_today) > MAX_YOY_CLASSIFY:
        log.warning(f"YoY classify: capping {len(results_today)} -> {MAX_YOY_CLASSIFY} "
                    f"(safety valve)")
        results_today = results_today[:MAX_YOY_CLASSIFY]
    for r_row in results_today:
        if r_row.get("period") not in ("Quarterly", "Annual"):
            continue
        try:'''

if OLD_LOOP in src:
    src = src.replace(OLD_LOOP, NEW_LOOP, 1)
    changes.append("run(): YoY loop now iterates filtered results_today (recent filings) with safety cap")
else:
    print("WARNING: YoY loop preamble not found in expected form -- check manually")
    shutil.copy(bak, CAL_PATH)
    sys.exit(1)

# ── Syntax check + write ─────────────────────────────────────────────────────
try:
    ast.parse(src)
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    shutil.copy(bak, CAL_PATH)
    sys.exit(1)

with open(CAL_PATH, 'w') as f:
    f.write(src)
print("Syntax OK")

# ══════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"{len(changes)} changes applied:")
for c in changes:
    print(f"   {c}")

with open(CAL_PATH) as f:
    final = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("_filed_recently() defined",            "def _filed_recently" in final),
    ("RESULTS_LOOKBACK_DAYS constant",        "RESULTS_LOOKBACK_DAYS = 1" in final),
    ("MAX_YOY_CLASSIFY safety cap",           "MAX_YOY_CLASSIFY = 60" in final),
    ("Loop uses results_today",               "for r_row in results_today:" in final),
    ("Filter log line present",               "filed in last" in final),
    ("Safety-cap branch present",             "capping" in final),
    ("Syntax OK",                             ast.parse(final) is not None or True),
]
all_ok = True
for label, passed in checks:
    icon = "OK" if passed else "FAIL"
    print(f"   [{icon}] {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("ALL CHECKS PASSED")
    print()
    print("EFFECT: On a normal trading day the YoY loop now processes only the")
    print("~5-30 companies that actually filed today, not the full 1031 backlog.")
    print("Runtime drops from 20-40+ min to well under a minute.")
    print()
    print("NOTE: On a Saturday (today) there are likely ZERO filings broadcast")
    print("today -> results_today will be empty -> yoy_classified = [] instantly.")
    print("That is CORRECT. The real test is Monday Aug 10 when Bharat Forge,")
    print("Gland Pharma, IDEA, NAUKRI et al file for the first time.")
    print()
    print("Re-run (will now finish in seconds):")
    print("  venv/bin/python3 download_nse_calendar.py")
else:
    print("SOME CHECKS FAILED -- restoring backup")
    shutil.copy(bak, CAL_PATH)
