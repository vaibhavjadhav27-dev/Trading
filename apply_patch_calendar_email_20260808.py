"""
PATCH: Same-day results-conclusion email for download_nse_calendar.py
============================================================================
Date: 2026-08-08 (Saturday). Cron target: weekdays 18:00 IST from Mon Aug 10.

REQUIREMENT (user, 2026-08-08):
  After the 18:00 IST run, send an email EVERY run (same day) — even if
  nothing was filed. Each announcement/result must be CONCLUDED as
  Positive / Negative / Neutral with a likely-impact call (stock likely
  to gain / lose / no material move).

DESIGN:
  - Real results filings (XBRL YoY available) -> conclusion driven by the
    REAL computed numbers:
        BEAT           -> Positive  | "Likely to gain"
        MISS           -> Negative  | "Likely to lose"
        INLINE         -> Neutral   | "No material move expected"
        NO_COMPARABLE  -> Neutral   | "Insufficient data (no prior-year comp)"
  - Non-results announcements (investor decks, AGM, etc.) -> headline read
    from the existing classify_sentiment() output, mapped to Pos/Neg/Neutral,
    clearly tagged "headline-only" so the softer signal is obvious.
  - Email ALWAYS sends (even empty) -> an explicit "All quiet — no watchlist
    filings today" body so silence is never ambiguous.
  - SES via get_ses_sender()/get_ses_recipient() from secrets_manager.py
    (per user's standing rule — never raw boto3 for SES addresses).
  - HTML table (Body.Html), styled like swing_daily.py.

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_calendar_email_20260808.py
"""

import shutil, ast, sys
from datetime import datetime

CAL_PATH = '/home/ubuntu/trading-bot/download_nse_calendar.py'
ts = datetime.now().strftime("%H%M%S")
bak = f"{CAL_PATH}.bak_email_{ts}"
shutil.copy(CAL_PATH, bak)
print(f"Backup -> {bak}")

with open(CAL_PATH) as f:
    src = f.read()

changes = []

# ══════════════════════════════════════════════════════════════════════════
# Ensure SES helpers + boto3 client are importable. We import lazily inside
# the send function so a missing secrets_manager never breaks the data run.
# ══════════════════════════════════════════════════════════════════════════

EMAIL_FUNCTIONS = '''
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


'''

if "send_conclusion_email" not in src:
    anchor = "def run("
    idx = src.find(anchor)
    if idx == -1:
        # fall back: insert before the first classify_ function
        anchor = "def classify_sentiment("
        idx = src.find(anchor)
    if idx == -1:
        print("ERROR: no anchor (run/classify_sentiment) found -- aborting")
        shutil.copy(bak, CAL_PATH)
        sys.exit(1)
    src = src[:idx] + EMAIL_FUNCTIONS + src[idx:]
    changes.append("Inserted conclusion email builder + send_conclusion_email() (SES, always-send)")

# ══════════════════════════════════════════════════════════════════════════
# Call send_conclusion_email() at the end of run(), right after the final log.
# ══════════════════════════════════════════════════════════════════════════
OLD_TAIL = '''    log.info(f"Wrote {out_path} "
             f"({len(classified)} classified filings, {len(yoy_classified)} real YoY results, "
             f"{len(upcoming)} upcoming board meetings)")'''

NEW_TAIL = '''    log.info(f"Wrote {out_path} "
             f"({len(classified)} classified filings, {len(yoy_classified)} real YoY results, "
             f"{len(upcoming)} upcoming board meetings)")

    # Same-day conclusion email (2026-08-08) -- always sends, even if empty.
    send_conclusion_email(today, yoy_classified, classified)'''

if OLD_TAIL in src:
    src = src.replace(OLD_TAIL, NEW_TAIL, 1)
    changes.append("run(): calls send_conclusion_email() after writing the JSON")
else:
    print("WARNING: completion-log tail not found in expected form -- check manually")
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
    ("_conclude_from_yoy() defined",        "def _conclude_from_yoy" in final),
    ("_conclude_from_headline() defined",   "def _conclude_from_headline" in final),
    ("build_conclusion_email_html() defined","def build_conclusion_email_html" in final),
    ("send_conclusion_email() defined",     "def send_conclusion_email" in final),
    ("Uses get_ses_sender/get_ses_recipient","get_ses_sender" in final and "get_ses_recipient" in final),
    ("HTML body (Body.Html)",               '"Html"' in final),
    ("run() calls send_conclusion_email",   "send_conclusion_email(today, yoy_classified, classified)" in final),
    ("Always-send (all-quiet note)",        "All quiet" in final),
    ("Syntax OK",                           ast.parse(final) is not None or True),
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
    print("CONCLUSION LOGIC:")
    print("  Real results (XBRL YoY):  BEAT->Positive/gain, MISS->Negative/lose,")
    print("                            INLINE/NO_COMPARABLE->Neutral")
    print("  Other announcements:      headline-only Pos/Neg/Neutral (tagged)")
    print("  Email sends EVERY run -- 'All quiet' note when nothing filed.")
    print()
    print("SEND A TEST NOW (Saturday -> will be an 'All quiet' email, which")
    print("confirms SES wiring end-to-end before Monday's real flow):")
    print("  venv/bin/python3 download_nse_calendar.py")
    print("  # check inbox for: 'NSE Watchlist Conclusions — 08-Aug-2026  [All quiet]'")
else:
    print("SOME CHECKS FAILED -- restoring backup")
    shutil.copy(bak, CAL_PATH)
