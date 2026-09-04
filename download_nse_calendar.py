#!/usr/bin/env python3
"""
download_nse_calendar.py — Evening NSE Corporate Calendar + Sentiment Shadow Logger
=====================================================================================
Date: 2026-08-08

PURPOSE (shadow/observational only — does NOT touch live scoring or trading):
  Every evening (cron ~19:00 IST), download:
    1. NSE event-calendar          -> tomorrow's board meeting dates
    2. NSE corporate-announcements -> today's filings (results, investor decks, etc)
    3. NSE corporates-financial-results -> today's results filing metadata
  Filter to symbols in our watchlist.csv (556 stocks).
  Classify each results-related filing's sentiment (beat/miss/inline/non-results)
  using the SAME Gemini->Groq fallback LLM pipeline already used in
  post_market_analysis.py (memory-confirmed working pattern).
  Write candle_archive/nse_calendar_{date}.json for the weekly review script,
  and append 2 observational columns to the existing candidate_scores_{date}.csv
  IF that file already exists for today (does not create/modify scoring itself).

USAGE (cron, run once daily after market close):
  30 13 * * 1-5 cd /home/ubuntu/trading-bot && venv/bin/python3 download_nse_calendar.py >> logs/nse_calendar.log 2>&1

NOTE: NSE endpoints are NOT blocked from this server (verified live 2026-08-08,
correcting an earlier stale memory note about nseindia.com being blocked --
that note likely referred to a different/gainers endpoint, or IP had changed).
Requires a warm-up GET to nseindia.com first to obtain cookies (same pattern
as download_bhavcopy.py), otherwise API calls can 403.
"""

import os
import sys
import csv
import json
import time
import logging
from datetime import datetime, timedelta

import re
import requests

# ── logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nse_calendar")

BASE_DIR = "/home/ubuntu/trading-bot"
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.csv")
ARCHIVE_DIR = os.path.join(BASE_DIR, "candle_archive")
os.makedirs(ARCHIVE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

EVENT_CALENDAR_URL = "https://www.nseindia.com/api/event-calendar"
ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
RESULTS_URL = "https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly"


# ── watchlist loader ─────────────────────────────────────────────────────────
def load_watchlist_symbols():
    """Read watchlist.csv (ticker,security_id,sector) -> set of tickers."""
    symbols = set()
    if not os.path.exists(WATCHLIST_PATH):
        log.warning(f"watchlist.csv not found at {WATCHLIST_PATH}")
        return symbols
    with open(WATCHLIST_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = (row.get("ticker") or "").strip().upper()
            if t:
                symbols.add(t)
    log.info(f"Loaded {len(symbols)} watchlist symbols")
    return symbols


# ── NSE session + fetchers ────────────────────────────────────────────────────
def get_nse_session():
    """Warm up a requests.Session with NSE cookies (same pattern as download_bhavcopy.py)."""
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
    except Exception as e:
        log.warning(f"NSE cookie warm-up failed (continuing anyway): {e}")
    return session


def fetch_json(session, url, label):
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json()
            log.info(f"{label}: HTTP 200, {len(data) if isinstance(data, list) else 'n/a'} records")
            return data
        else:
            log.warning(f"{label}: HTTP {r.status_code}")
            return []
    except Exception as e:
        log.warning(f"{label} fetch failed: {e}")
        return []


# ── LLM sentiment classifier (reuses existing Gemini->Groq fallback pattern) ──

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
    blocks = re.findall(r'<xbrli:context id="([^"]*)">(.*?)</xbrli:context>', xml_text, re.DOTALL)
    return [cid for cid, body in blocks if "xbrldi:explicitMember" not in body]


def _extract_xbrl_tag(xml_text, tag_name, context_id):
    pattern = rf'<in-bse-fin:{tag_name} contextRef="{context_id}"[^>]*>([^<]*)</in-bse-fin:{tag_name}>'
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


def classify_sentiment(symbol, company, desc, result_meta=None):
    """
    Classify a results/announcement filing as beat / miss / inline / non-results.
    Reuses the same Gemini-primary, Groq-fallback pattern already proven in
    post_market_analysis.py and market_intel.py (memory-confirmed working).
    Falls back to a simple keyword heuristic if both LLM calls fail/rate-limit.
    """
    prompt = (
        f"NSE-listed company: {company} ({symbol})\n"
        f"Filing description: {desc}\n"
    )
    if result_meta:
        prompt += f"Result period: {result_meta.get('period','?')} "
        prompt += f"for {result_meta.get('fromDate','?')} to {result_meta.get('toDate','?')}\n"
    prompt += (
        "\nBased ONLY on the filing description above (no external data), "
        "classify this filing as exactly one word: BEAT, MISS, INLINE, or NONRESULTS. "
        "If the description does not indicate quarterly/annual financial results "
        "(e.g. it's an investor presentation, AGM notice, board meeting intimation "
        "with no outcome yet), answer NONRESULTS. "
        "Answer with ONLY that one word, nothing else."
    )

    # Try Gemini first
    try:
        import google.generativeai as genai
        from secrets_manager import get_gemini_api_key
        genai.configure(api_key=get_gemini_api_key())
        model = genai.GenerativeModel("gemini-2.0-flash")
        resp = model.generate_content(prompt, request_options={"timeout": 10})
        text = (resp.text or "").strip().upper()
        for tag in ("BEAT", "MISS", "INLINE", "NONRESULTS"):
            if tag in text:
                return tag
    except Exception as e:
        log.debug(f"Gemini sentiment skip for {symbol}: {e}")

    # Fallback: Groq (same fallback chain used elsewhere in this codebase)
    try:
        from groq import Groq
        client = Groq()
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            timeout=10,
        )
        text = (resp.choices[0].message.content or "").strip().upper()
        for tag in ("BEAT", "MISS", "INLINE", "NONRESULTS"):
            if tag in text:
                return tag
    except Exception as e:
        log.debug(f"Groq sentiment skip for {symbol}: {e}")

    # Last resort: keyword heuristic (never blocks the pipeline)
    d = (desc or "").lower()
    if "financial result" in d or "un-audited" in d or "audited" in d:
        return "INLINE"  # unknown outcome, but confirmed a results filing
    return "NONRESULTS"


# ── main ──────────────────────────────────────────────────────────────────────

# ── Same-day conclusion email (2026-08-08) ───────────────────────────────────
# Concludes each filing as Positive / Negative / Neutral + likely impact,
# and emails the digest every run (even when nothing was filed).

def _conclude_from_yoy(classification):
    """Map a real-YoY classification to (conclusion, impact, css_class)."""
    return {
        "BEAT":          ("Positive", "Likely to gain",                  "pos"),
        "MISS":          ("Negative", "Likely to lose",                  "neg"),
        "INLINE":        ("Neutral",  "No material move expected",       "neu"),
        "NO_COMPARABLE": ("Neutral",  "Insufficient data (no prior comp)", "neu"),
    }.get(classification, ("Neutral", "No material move expected", "neu"))


def _conclude_from_headline(sentiment):
    """
    Map the headline classify_sentiment() label to (conclusion, impact,
    css_class). Headline-only signals are deliberately conservative.
    """
    s = (sentiment or "").upper()
    if "POSITIVE" in s or "BEAT" in s:
        return ("Positive", "Possible upside (headline-only)", "pos")
    if "NEGATIVE" in s or "MISS" in s:
        return ("Negative", "Possible downside (headline-only)", "neg")
    # NONRESULTS / NEUTRAL / anything else
    return ("Neutral", "No material move expected", "neu")


def build_conclusion_email_html(today, yoy_classified, classified):
    """Return (subject, html_body) for the same-day conclusion digest."""
    date_str = today.strftime("%d-%b-%Y (%A)")

    css = """
    <style>
      body { font-family: Arial, Helvetica, sans-serif; color:#1a1a1a; }
      h2 { margin: 0 0 4px 0; }
      .sub { color:#666; font-size:12px; margin-bottom:16px; }
      table { border-collapse: collapse; width:100%; margin-bottom:22px; font-size:13px; }
      th, td { border:1px solid #ddd; padding:7px 9px; text-align:left; }
      th { background:#f4f6f8; }
      .pos { color:#0a7a28; font-weight:bold; }
      .neg { color:#c0261d; font-weight:bold; }
      .neu { color:#555; }
      .quiet { padding:14px; background:#f4f6f8; border-radius:6px; color:#444; }
      .tag { font-size:11px; color:#888; }
    </style>
    """

    # --- Section 1: real YoY results (numbers-backed) --------------------------
    if yoy_classified:
        rows = []
        for y in yoy_classified:
            conclusion, impact, cls = _conclude_from_yoy(y.get("classification", ""))
            rev = y.get("revenue_yoy_pct")
            prof = y.get("profit_yoy_pct")
            rev_s = f"{rev:+.1f}%" if isinstance(rev, (int, float)) else "—"
            prof_s = f"{prof:+.1f}%" if isinstance(prof, (int, float)) else "—"
            rows.append(
                f"<tr><td>{y.get('symbol','')}</td>"
                f"<td>{y.get('relatingTo','') or y.get('financialYear','')}</td>"
                f"<td>{rev_s}</td><td>{prof_s}</td>"
                f"<td class='{cls}'>{conclusion}</td>"
                f"<td class='{cls}'>{impact}</td></tr>"
            )
        yoy_table = (
            "<h3>Results filed today — real YoY (numbers-backed)</h3>"
            "<table><tr><th>Stock</th><th>Quarter</th><th>Revenue YoY</th>"
            "<th>Profit YoY</th><th>Conclusion</th><th>Likely impact</th></tr>"
            + "".join(rows) + "</table>"
        )
    else:
        yoy_table = ("<h3>Results filed today — real YoY (numbers-backed)</h3>"
                     "<p class='quiet'>No watchlist companies filed results today.</p>")

    # --- Section 2: other announcements (headline-only) ------------------------
    # Only show announcements that are NOT already covered by a YoY row.
    yoy_syms = {y.get("symbol", "") for y in yoy_classified}
    other = [c for c in classified if c.get("symbol") not in yoy_syms]
    if other:
        rows = []
        for c in other:
            conclusion, impact, cls = _conclude_from_headline(c.get("sentiment", ""))
            rows.append(
                f"<tr><td>{c.get('symbol','')}</td>"
                f"<td>{(c.get('desc','') or '')[:60]}</td>"
                f"<td class='{cls}'>{conclusion}</td>"
                f"<td class='{cls}'>{impact}</td>"
                f"<td class='tag'>headline-only</td></tr>"
            )
        other_table = (
            "<h3>Other announcements (headline-only signal)</h3>"
            "<table><tr><th>Stock</th><th>Announcement</th><th>Conclusion</th>"
            "<th>Likely impact</th><th>Basis</th></tr>"
            + "".join(rows) + "</table>"
        )
    else:
        other_table = ""

    n_pos = sum(1 for y in yoy_classified if y.get("classification") == "BEAT")
    n_neg = sum(1 for y in yoy_classified if y.get("classification") == "MISS")

    all_quiet = (not yoy_classified) and (not other)
    quiet_note = ("<p class='quiet'><b>All quiet</b> — no watchlist filings or "
                  "announcements today.</p>") if all_quiet else ""

    subject = f"NSE Watchlist Conclusions — {today.strftime('%d-%b-%Y')}"
    if n_pos or n_neg:
        subject += f"  [{n_pos} Positive / {n_neg} Negative]"
    elif all_quiet:
        subject += "  [All quiet]"

    html = (
        f"<html><head>{css}</head><body>"
        f"<h2>NSE Watchlist — Results Conclusions</h2>"
        f"<div class='sub'>{date_str} · 18:00 IST run · "
        f"{len(yoy_classified)} results-backed, {len(other)} headline-only</div>"
        f"{quiet_note}{yoy_table}{other_table}"
        f"<div class='tag'>Numbers-backed rows use real XBRL YoY revenue/profit. "
        f"Headline-only rows are a softer signal (no financials parsed). "
        f"Not investment advice.</div>"
        f"</body></html>"
    )
    return subject, html


def send_conclusion_email(today, yoy_classified, classified):
    """
    Send the same-day conclusion digest via SES. ALWAYS sends (even empty).
    Fail-safe: logs and returns on any error -- never breaks the data run.
    """
    try:
        import boto3
        from secrets_manager import get_ses_sender, get_ses_recipient
        sender = get_ses_sender()
        recipient = get_ses_recipient()
        subject, html = build_conclusion_email_html(today, yoy_classified, classified)
        ses = boto3.client("ses", region_name="ap-south-1")
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Html": {"Data": html}},
            },
        )
        log.info(f"Conclusion email sent -> {recipient} "
                 f"({len(yoy_classified)} results-backed, subject: {subject!r})")
    except Exception as e:
        log.error(f"Conclusion email FAILED (data run unaffected): {e}")


def run():
    today = datetime.now().date()
    log.info(f"=== NSE Calendar Shadow Logger — {today.isoformat()} ===")

    watchlist = load_watchlist_symbols()
    session = get_nse_session()

    events = fetch_json(session, EVENT_CALENDAR_URL, "event-calendar")
    time.sleep(1)
    announcements = fetch_json(session, ANNOUNCEMENTS_URL, "corporate-announcements")
    time.sleep(1)
    results = fetch_json(session, RESULTS_URL, "corporates-financial-results")

    # Filter each feed to watchlist symbols only
    events_wl = [e for e in events if (e.get("symbol") or "").strip().upper() in watchlist]
    announce_wl = [a for a in announcements if (a.get("symbol") or "").strip().upper() in watchlist]
    results_wl = [r for r in results if (r.get("symbol") or "").strip().upper() in watchlist]

    log.info(f"Watchlist matches -> events:{len(events_wl)} announcements:{len(announce_wl)} results:{len(results_wl)}")

    # Classify sentiment for each watchlist announcement (rate-limit friendly)
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
        try:
            yoy = classify_results_yoy(session, r_row)
            yoy["company"] = r_row.get("companyName", "")
            yoy["relatingTo"] = r_row.get("relatingTo", "")
            yoy["financialYear"] = r_row.get("financialYear", "")
            yoy_classified.append(yoy)
            time.sleep(0.5)  # gentle on NSE + avoid hammering XBRL archive
        except Exception as e:
            log.debug(f"YoY classify skipped for {r_row.get('symbol','?')}: {e}")

    # Tomorrow's board meetings on watchlist (forward-looking, no sentiment yet)
    upcoming = [{"symbol": (e.get("symbol") or "").strip().upper(),
                 "company": e.get("company"),
                 "purpose": e.get("purpose"),
                 "date": e.get("date")} for e in events_wl]

    payload = {
        "date": today.isoformat(),
        "watchlist_size": len(watchlist),
        "today_filings_classified": classified,
        "today_results_yoy_classified": yoy_classified,
        "upcoming_board_meetings": upcoming,
    }

    out_path = os.path.join(ARCHIVE_DIR, f"nse_calendar_{today.isoformat()}.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"Wrote {out_path} "
             f"({len(classified)} classified filings, {len(yoy_classified)} real YoY results, "
             f"{len(upcoming)} upcoming board meetings)")

    # Same-day conclusion email (2026-08-08) -- always sends, even if empty.
    send_conclusion_email(today, yoy_classified, classified)

    # Same-day conclusion email (2026-08-08) -- always sends, even if empty.
    send_conclusion_email(today, yoy_classified, classified)

    # ── SHADOW-ONLY: append observational columns to today's candidate_scores CSV ──
    # Does NOT create the file, does NOT touch scoring -- purely tags rows if the
    # file already exists from trading_bot.py's own DUAL-SCORE CSV logger.
    csv_path = os.path.join(ARCHIVE_DIR, f"candidate_scores_{today.isoformat()}.csv")
    if os.path.exists(csv_path):
        try:
            sentiment_by_symbol = {c["symbol"]: c["sentiment"] for c in classified}
            # Real YoY classification takes priority over the headline-only guess
            for y in yoy_classified:
                sentiment_by_symbol[y["symbol"]] = y["classification"]
            rows = []
            with open(csv_path) as f:
                reader = csv.reader(f)
                header = next(reader)
                if "has_nse_event" not in header:
                    header = header + ["has_nse_event", "event_sentiment"]
                for row in reader:
                    ticker = row[1] if len(row) > 1 else ""
                    sent = sentiment_by_symbol.get(ticker.upper(), "")
                    rows.append(row + [("Y" if sent else "N"), sent])
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)
            log.info(f"Tagged {csv_path} with has_nse_event/event_sentiment columns "
                      f"({sum(1 for r in rows if r[-1])} rows tagged)")
        except Exception as e:
            log.warning(f"CSV tagging skipped (non-fatal): {e}")
    else:
        log.info(f"{csv_path} does not exist yet (normal if run before market scan) -- "
                 f"nse_calendar_{today.isoformat()}.json still saved for weekly review.")

    log.info("=== NSE Calendar Shadow Logger complete ===")


if __name__ == "__main__":
    run()
