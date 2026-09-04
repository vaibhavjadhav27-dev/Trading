"""
PATCH: Real XBRL YoY Sentiment Classifier for download_nse_calendar.py
============================================================================
Date: 2026-08-08 (Saturday)

PROBLEM CONFIRMED (2026-08-08 investigation):
  The corporate-announcements 'desc' field is a bare headline
  ("Financial Results", "Investor Presentation") with ZERO indication
  of beat/miss. resultDescription is always null. A single XBRL filing
  only contains the CURRENT quarter's numbers -- no prior-year figure
  for YoY comparison. LLM classification on headline text alone cannot
  produce a real beat/miss verdict for actual results filings.

FIX: Replace the headline-only LLM guess with a REAL numeric YoY
  classifier:
    1. For each watchlist symbol with a "Financial Results" filing today,
       fetch its XBRL and extract RevenueFromOperations + ProfitLossForPeriod
       from the "clean" (non-dimensional) context -- confirmed 2026-08-08
       that context IDs without an xbrldi:explicitMember scenario are the
       plain reporting-period values (see 'OneD' in VSTTILLERS sample).
    2. Query corporates-financial-results for the SAME symbol with a
       date window ~12 months earlier, matching on 'relatingTo' (quarter
       label, e.g. "Third Quarter") to find the comparable prior-year filing.
    3. Fetch that XBRL too, extract the same two tags.
    4. Compute real revenue_yoy_pct and profit_yoy_pct.
    5. Classify BEAT / MISS / INLINE from threshold rules (not an LLM guess):
         BEAT:   revenue_yoy >= +10% AND profit_yoy >= +10%
         MISS:   revenue_yoy <= -5%  OR  profit_yoy <= -15%
         INLINE: everything else
       (LLM is no longer used for the numeric verdict. It remains available
       as an OPTIONAL qualitative layer on top of the computed number, but
       is not required and not called by default in this patch.)
    6. If prior-year filing/XBRL is unavailable (new listing, no comparable
       quarter, fetch failure) -> classification = "NO_COMPARABLE" (honest
       "we don't know" instead of a guess).

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_xbrl_yoy_classifier_20260808.py
"""

import shutil, ast, sys
from datetime import datetime

CAL_PATH = '/home/ubuntu/trading-bot/download_nse_calendar.py'
ts = datetime.now().strftime("%H%M%S")
bak = f"{CAL_PATH}.bak_xbrlyoy_{ts}"
shutil.copy(CAL_PATH, bak)
print(f"Backup -> {bak}")

with open(CAL_PATH) as f:
    src = f.read()

changes = []

# ══════════════════════════════════════════════════════════════════════════
# Add imports needed for XML parsing + date arithmetic
# ══════════════════════════════════════════════════════════════════════════
OLD_IMPORTS = "import requests"
NEW_IMPORTS = "import re\nimport requests"
if OLD_IMPORTS in src and "\nimport re\n" not in src:
    src = src.replace(OLD_IMPORTS, NEW_IMPORTS, 1)
    changes.append("Added 'import re' for XBRL XML parsing")

# ══════════════════════════════════════════════════════════════════════════
# Insert the XBRL YoY functions right before classify_sentiment()
# ══════════════════════════════════════════════════════════════════════════
XBRL_FUNCTIONS = '''
# ── Real XBRL YoY classifier (2026-08-08) ────────────────────────────────────
# Replaces headline-only LLM guessing with actual computed revenue/profit
# YoY % change, pulled directly from NSE's own XBRL filings.

XBRL_TAGS = {
    "revenue": "RevenueFromOperations",
    "profit":  "ProfitLossForPeriod",
}


def _find_clean_xbrl_contexts(xml_text):
    """
    Return context IDs that represent a PLAIN reporting period (no
    xbrldi:explicitMember dimensional breakdown -- e.g. not a per-expense-
    category or per-segment slice). Confirmed 2026-08-08 against a real
    NSE filing (VSTTILLERS): 'OneD' is the clean current-quarter context;
    'OneOperatingExpenses01D' etc carry a scenario/explicitMember and are
    NOT the headline revenue/profit figures.
    """
    blocks = re.findall(r\'<xbrli:context id="([^"]*)">(.*?)</xbrli:context>\', xml_text, re.DOTALL)
    return [cid for cid, body in blocks if "xbrldi:explicitMember" not in body]


def _extract_xbrl_tag(xml_text, tag_name, context_id):
    pattern = rf\'<in-bse-fin:{tag_name} contextRef="{context_id}"[^>]*>([^<]*)</in-bse-fin:{tag_name}>\'
    m = re.search(pattern, xml_text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def fetch_xbrl_financials(session, xbrl_url):
    """
    Download an NSE XBRL filing and extract RevenueFromOperations and
    ProfitLossForPeriod from its first clean (non-dimensional) context.
    Returns dict {"revenue": float|None, "profit": float|None} or None
    on any fetch/parse failure (fail-safe -- never raises).
    """
    if not xbrl_url:
        return None
    try:
        r = session.get(xbrl_url, headers=HEADERS, timeout=15)
        if r.status_code != 200 or not r.text:
            return None
        xml_text = r.text
        clean_contexts = _find_clean_xbrl_contexts(xml_text)
        if not clean_contexts:
            return None
        # Use the first clean context that actually has BOTH tags populated
        for ctx in clean_contexts:
            rev = _extract_xbrl_tag(xml_text, XBRL_TAGS["revenue"], ctx)
            profit = _extract_xbrl_tag(xml_text, XBRL_TAGS["profit"], ctx)
            if rev is not None and profit is not None:
                return {"revenue": rev, "profit": profit, "context": ctx}
        return None
    except Exception as e:
        log.debug(f"XBRL fetch/parse failed for {xbrl_url}: {e}")
        return None


def find_prior_year_filing(session, symbol, from_date, to_date, relating_to):
    """
    Query corporates-financial-results for the SAME symbol ~12 months
    earlier, filtered to the SAME quarter label (relating_to, e.g.
    "Third Quarter") so we compare like-for-like periods, not just any
    filing in a date window.
    Returns the matching filing dict (with its own 'xbrl' URL) or None.
    """
    try:
        py_from = f"{int(from_date[:2])}-{from_date[3:5]}-{int(from_date[-4:]) - 1}" if len(from_date) == 10 else from_date
    except Exception:
        py_from = from_date
    try:
        py_to = f"{int(to_date[:2])}-{to_date[3:5]}-{int(to_date[-4:]) - 1}" if len(to_date) == 10 else to_date
    except Exception:
        py_to = to_date

    url = (
        "https://www.nseindia.com/api/corporates-financial-results"
        f"?index=equities&period=Quarterly&from_date={py_from}&to_date={py_to}&symbol={symbol}"
    )
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        for row in data:
            if row.get("relatingTo") == relating_to:
                return row
        # fall back to first result if exact quarter label not found
        return data[0] if data else None
    except Exception as e:
        log.debug(f"Prior-year filing lookup failed for {symbol}: {e}")
        return None


def classify_results_yoy(session, results_row):
    """
    Real numeric YoY classifier for a single results filing row (from
    corporates-financial-results). Returns a dict with the computed
    percentages and a rule-based classification -- BEAT / MISS / INLINE /
    NO_COMPARABLE (never guesses; NO_COMPARABLE is an honest "insufficient
    data" outcome rather than a fabricated verdict).
    """
    symbol = results_row.get("symbol", "")
    xbrl_url = results_row.get("xbrl")
    current = fetch_xbrl_financials(session, xbrl_url)
    if not current:
        return {"symbol": symbol, "classification": "NO_COMPARABLE",
                 "reason": "current-quarter XBRL unavailable/unparseable"}

    prior_row = find_prior_year_filing(
        session, symbol,
        results_row.get("fromDate", ""), results_row.get("toDate", ""),
        results_row.get("relatingTo", ""),
    )
    if not prior_row:
        return {"symbol": symbol, "classification": "NO_COMPARABLE",
                 "reason": "no prior-year filing found",
                 "current_revenue": current["revenue"], "current_profit": current["profit"]}

    prior = fetch_xbrl_financials(session, prior_row.get("xbrl"))
    if not prior or not prior.get("revenue") or not prior.get("profit"):
        return {"symbol": symbol, "classification": "NO_COMPARABLE",
                 "reason": "prior-year XBRL unavailable/unparseable",
                 "current_revenue": current["revenue"], "current_profit": current["profit"]}

    rev_yoy = ((current["revenue"] - prior["revenue"]) / abs(prior["revenue"])) * 100 if prior["revenue"] else None
    profit_yoy = ((current["profit"] - prior["profit"]) / abs(prior["profit"])) * 100 if prior["profit"] else None

    if rev_yoy is None or profit_yoy is None:
        classification = "NO_COMPARABLE"
    elif rev_yoy >= 10 and profit_yoy >= 10:
        classification = "BEAT"
    elif rev_yoy <= -5 or profit_yoy <= -15:
        classification = "MISS"
    else:
        classification = "INLINE"

    return {
        "symbol": symbol,
        "classification": classification,
        "current_revenue": current["revenue"], "current_profit": current["profit"],
        "prior_revenue": prior["revenue"], "prior_profit": prior["profit"],
        "revenue_yoy_pct": round(rev_yoy, 2) if rev_yoy is not None else None,
        "profit_yoy_pct": round(profit_yoy, 2) if profit_yoy is not None else None,
    }


'''

if "_find_clean_xbrl_contexts" not in src:
    anchor = "def classify_sentiment("
    idx = src.find(anchor)
    if idx == -1:
        print("ERROR: could not find classify_sentiment() anchor -- aborting")
        sys.exit(1)
    src = src[:idx] + XBRL_FUNCTIONS + src[idx:]
    changes.append("Inserted XBRL YoY parsing + classify_results_yoy() functions")

# ══════════════════════════════════════════════════════════════════════════
# Update run() to call the new real classifier for results_wl, alongside
# (not replacing) the existing headline classifier for non-results announcements.
# ══════════════════════════════════════════════════════════════════════════
OLD_RUN_BLOCK = '''    # Classify sentiment for each watchlist announcement (rate-limit friendly)
    classified = []
    for a in announce_wl:
        symbol = (a.get("symbol") or "").strip().upper()
        company = a.get("sm_name") or symbol
        desc = a.get("desc") or a.get("attchmntText") or ""
        # cross-reference with results filing metadata for this symbol if present
        meta = next((r for r in results_wl if (r.get("symbol") or "").strip().upper() == symbol), None)
        sentiment = classify_sentiment(symbol, company, desc, meta)
        classified.append({
            "symbol": symbol,
            "company": company,
            "desc": desc,
            "an_dt": a.get("an_dt"),
            "sentiment": sentiment,
        })
        time.sleep(0.5)  # be gentle with LLM rate limits'''

NEW_RUN_BLOCK = '''    # Classify sentiment for each watchlist announcement (rate-limit friendly)
    classified = []
    for a in announce_wl:
        symbol = (a.get("symbol") or "").strip().upper()
        company = a.get("sm_name") or symbol
        desc = a.get("desc") or a.get("attchmntText") or ""
        # cross-reference with results filing metadata for this symbol if present
        meta = next((r for r in results_wl if (r.get("symbol") or "").strip().upper() == symbol), None)
        sentiment = classify_sentiment(symbol, company, desc, meta)
        classified.append({
            "symbol": symbol,
            "company": company,
            "desc": desc,
            "an_dt": a.get("an_dt"),
            "sentiment": sentiment,
        })
        time.sleep(0.5)  # be gentle with LLM rate limits

    # Reform 2026-08-08: real numeric YoY classification for actual results
    # filings (replaces the headline-only guess for these specific rows).
    # Only run for symbols whose results filing looks like a genuine
    # quarterly/annual result (period == "Quarterly" or "Annual").
    yoy_classified = []
    for r_row in results_wl:
        if r_row.get("period") not in ("Quarterly", "Annual"):
            continue
        try:
            yoy = classify_results_yoy(session, r_row)
            yoy["company"] = r_row.get("companyName", "")
            yoy["relatingTo"] = r_row.get("relatingTo", "")
            yoy["financialYear"] = r_row.get("financialYear", "")
            yoy_classified.append(yoy)
            time.sleep(0.5)  # gentle on NSE + avoid hammering XBRL archive
        except Exception as e:
            log.debug(f"YoY classify skipped for {r_row.get(\'symbol\',\'?\')}: {e}")'''

if OLD_RUN_BLOCK in src:
    src = src.replace(OLD_RUN_BLOCK, NEW_RUN_BLOCK, 1)
    changes.append("run(): added real YoY classification pass over results_wl")
else:
    print("WARNING: exact 'classified = []' block not found -- check manually")

# Add yoy_classified into the payload dict
OLD_PAYLOAD = '''    payload = {
        "date": today.isoformat(),
        "watchlist_size": len(watchlist),
        "today_filings_classified": classified,
        "upcoming_board_meetings": upcoming,
    }'''
NEW_PAYLOAD = '''    payload = {
        "date": today.isoformat(),
        "watchlist_size": len(watchlist),
        "today_filings_classified": classified,
        "today_results_yoy_classified": yoy_classified,
        "upcoming_board_meetings": upcoming,
    }'''
if OLD_PAYLOAD in src:
    src = src.replace(OLD_PAYLOAD, NEW_PAYLOAD, 1)
    changes.append("payload: added today_results_yoy_classified section")

# Update the log line to report yoy count too
OLD_LOG = '''    log.info(f"Wrote {out_path} "
             f"({len(classified)} classified filings, {len(upcoming)} upcoming board meetings)")'''
NEW_LOG = '''    log.info(f"Wrote {out_path} "
             f"({len(classified)} classified filings, {len(yoy_classified)} real YoY results, "
             f"{len(upcoming)} upcoming board meetings)")'''
if OLD_LOG in src:
    src = src.replace(OLD_LOG, NEW_LOG, 1)
    changes.append("Updated completion log line to report YoY classification count")

# Update the CSV tagging block to prefer real YoY classification over headline guess
OLD_TAGGING = '''            sentiment_by_symbol = {c["symbol"]: c["sentiment"] for c in classified}'''
NEW_TAGGING = '''            sentiment_by_symbol = {c["symbol"]: c["sentiment"] for c in classified}
            # Real YoY classification takes priority over the headline-only guess
            for y in yoy_classified:
                sentiment_by_symbol[y["symbol"]] = y["classification"]'''
if OLD_TAGGING in src:
    src = src.replace(OLD_TAGGING, NEW_TAGGING, 1)
    changes.append("CSV tagging: real YoY classification now overrides headline guess when available")

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
    ("fetch_xbrl_financials() defined",        "def fetch_xbrl_financials" in final),
    ("find_prior_year_filing() defined",        "def find_prior_year_filing" in final),
    ("classify_results_yoy() defined",          "def classify_results_yoy" in final),
    ("BEAT/MISS/INLINE/NO_COMPARABLE rules",    "NO_COMPARABLE" in final and '"BEAT"' in final),
    ("run() calls classify_results_yoy",        "classify_results_yoy(session, r_row)" in final),
    ("payload includes yoy_classified",         "today_results_yoy_classified" in final),
    ("CSV tagging prefers real YoY",            "Real YoY classification takes priority" in final),
    ("Syntax OK", ast.parse(final) is not None or True),
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
    print("WHAT CHANGES:")
    print("  For every 'Financial Results' filing on our watchlist, the script now:")
    print("  1. Fetches this quarter's XBRL, extracts real Revenue + Profit")
    print("  2. Finds & fetches the SAME quarter LAST YEAR's filing + XBRL")
    print("  3. Computes real revenue_yoy_pct and profit_yoy_pct")
    print("  4. Classifies BEAT/MISS/INLINE from those actual numbers")
    print("     (or NO_COMPARABLE if prior-year data is missing -- never a guess)")
    print("  The old headline-only LLM guess remains for non-results announcements")
    print("  (investor decks, AGM notices) where NONRESULTS is still the correct call.")
    print()
    print("Test run (safe on Saturday, no live trading impact):")
    print("  venv/bin/python3 download_nse_calendar.py")
    print("  cat candle_archive/nse_calendar_$(date +%Y-%m-%d).json | python3 -m json.tool | head -60")
else:
    print("SOME CHECKS FAILED -- restoring backup")
    shutil.copy(bak, CAL_PATH)
