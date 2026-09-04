#!/usr/bin/env python3
"""
daily_trade_logs_email.py - Email full trade logs with all parameters
=====================================================================
Sends a daily email with ALL trade data:
  - NSE ORB: candidates with full score breakdown, entry/exit timestamps,
    prices, and all conditions that triggered entry/exit
  - Swing: active positions, closed trades, P&L
  - MCX shadow: ORB levels, positions, entry/exit reasoning

Schedule: 15:30 IST (10:00 UTC) for NSE, 23:30 IST (18:00 UTC) for full day
"""

import json, os, sys, glob, logging, boto3, csv
from datetime import datetime, date, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Paths ---
BASE = Path("/home/ubuntu/trading-bot")
V84 = BASE / "V84_PRODUCTION_INTEGRATED"
LOGS = BASE / "logs"
STATE = V84 / "mcx_state"
CANDLE_ARCHIVE = BASE / "candle_archive"

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS / "trade_logs_email.log")
    ]
)
log = logging.getLogger("trade_logs_email")

# --- Secrets ---
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(V84))
from secrets_manager import get_ses_sender, get_ses_recipient


# ============================================================
# DATA COLLECTORS
# ============================================================

def collect_nse_candidates(today: date) -> list:
    """Get full candidate scores from candle_archive CSV."""
    csv_file = CANDLE_ARCHIVE / f"candidate_scores_{today.isoformat()}.csv"
    if not csv_file.exists():
        # Try alternate naming
        alt = CANDLE_ARCHIVE / f"candidate_scores_{today.strftime('%Y%m%d')}.csv"
        if alt.exists():
            csv_file = alt
        else:
            return []
    
    candidates = []
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candidates.append(dict(row))
    return candidates


def collect_nse_entries_exits(today: date) -> dict:
    """Parse bot.log for today's entries and exits with full detail."""
    data = {"entries": [], "exits": [], "sl_updates": [], "candidates_log": []}
    bot_log = LOGS / "bot.log"
    if not bot_log.exists():
        return data
    
    today_str = today.strftime("%Y-%m-%d")
    with open(bot_log) as f:
        for line in f:
            if today_str not in line:
                continue
            line = line.strip()
            if any(kw in line for kw in ["ENTRY", "FILLED", "ORDER_PLACED", "LONG_ENTRY", "SHORT_ENTRY"]):
                data["entries"].append(line)
            elif any(kw in line for kw in ["EXIT", "CLOSED", "STOP_HIT", "TARGET", "MANDATORY_EXIT"]):
                data["exits"].append(line)
            elif "SL_UPDATE" in line or "TRAIL" in line:
                data["sl_updates"].append(line)
            elif "CANDIDATE" in line or "SCORE" in line or "QUALIFIED" in line:
                data["candidates_log"].append(line)
    
    return data


def collect_session_report(today: date) -> dict:
    """Get session report JSON if available."""
    patterns = [
        LOGS / f"session_report_{today.isoformat()}.json",
        LOGS / f"session_report_{today.strftime('%Y%m%d')}.json",
    ]
    # Also glob
    for p in sorted(glob.glob(str(LOGS / f"session_report*{today.isoformat()}*"))):
        patterns.append(Path(p))
    
    for p in patterns:
        if isinstance(p, Path) and p.exists():
            with open(p) as f:
                return json.load(f)
    return {}


def collect_swing_data() -> dict:
    """Active + recent closed swing positions."""
    data = {"active": [], "closed_recent": []}
    
    active_file = V84 / "swing_positions.json"
    if active_file.exists():
        with open(active_file) as f:
            data["active"] = json.load(f)
    
    closed_file = V84 / "swing_closed.json"
    if closed_file.exists():
        with open(closed_file) as f:
            all_closed = json.load(f)
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            data["closed_recent"] = [
                t for t in all_closed if t.get("exit_date", "") >= cutoff
            ]
    
    return data


def collect_mcx_data(today: date) -> dict:
    """MCX shadow positions and ORB data."""
    data = {"positions": [], "orb": {}, "session_log": []}
    
    pos_files = sorted(glob.glob(str(STATE / f"positions_{today.isoformat()}*.json")))
    if pos_files:
        with open(pos_files[-1]) as f:
            data["positions"] = json.load(f)
    
    orb_file = STATE / f"orb_{today.isoformat()}.json"
    if orb_file.exists():
        with open(orb_file) as f:
            data["orb"] = json.load(f)
    
    mcx_log = LOGS / "mcx_v854.log"
    if mcx_log.exists():
        with open(mcx_log) as f:
            lines = f.readlines()
            data["session_log"] = [l.strip() for l in lines if today.isoformat() in l][-30:]
    
    return data


def collect_regime() -> str:
    """Current market regime."""
    regime_file = BASE / "regime_state.json"
    if regime_file.exists():
        with open(regime_file) as f:
            return json.load(f).get("regime", "UNKNOWN")
    return "UNKNOWN"


# ============================================================
# HTML FORMATTING
# ============================================================

def format_candidates_table(candidates: list) -> str:
    """Full score breakdown table for all candidates."""
    if not candidates:
        return "<p><em>No candidate scores CSV found for today.</em></p>"
    
    # Get all column headers from the CSV
    headers = list(candidates[0].keys()) if candidates else []
    
    header_html = "".join(f"<th>{h}</th>" for h in headers)
    rows_html = ""
    for i, c in enumerate(candidates, 1):
        cells = "".join(f"<td>{c.get(h, '')}</td>" for h in headers)
        bg = "#f0fff0" if i <= 5 else "#fff"  # highlight top 5
        rows_html += f'<tr style="background:{bg}">{cells}</tr>'
    
    return f"""
    <table>
    <tr>{header_html}</tr>
    {rows_html}
    </table>"""


def format_log_section(title: str, lines: list) -> str:
    """Format log lines as a preformatted block."""
    if not lines:
        return f"<h3>{title}</h3><p><em>None</em></p>"
    
    content = "\n".join(lines[-50:])  # last 50 lines max
    return f"""<h3>{title} ({len(lines)} events)</h3>
    <pre style="background:#1a1a2e; color:#e0e0e0; padding:12px; border-radius:6px; 
                font-size:11px; overflow-x:auto; white-space:pre-wrap;">{content}</pre>"""


def format_swing_table(positions: list, title: str) -> str:
    """Format swing positions as HTML table."""
    if not positions:
        return f"<h3>{title}</h3><p><em>None</em></p>"
    
    # Auto-detect keys
    keys = list(positions[0].keys()) if positions else []
    header_html = "".join(f"<th>{k}</th>" for k in keys)
    rows_html = ""
    for p in positions:
        cells = "".join(f"<td>{p.get(k, '')}</td>" for k in keys)
        rows_html += f"<tr>{cells}</tr>"
    
    return f"""<h3>{title} ({len(positions)})</h3>
    <table>
    <tr>{header_html}</tr>
    {rows_html}
    </table>"""


def format_mcx_section(mcx: dict) -> str:
    """Format MCX data."""
    html = "<h2>MCX Shadow (V8.5.4)</h2>"
    
    if mcx["orb"]:
        html += f"<h3>ORB Levels</h3><pre>{json.dumps(mcx['orb'], indent=2)}</pre>"
    
    if mcx["positions"]:
        html += f"<h3>Positions</h3><pre>{json.dumps(mcx['positions'], indent=2, default=str)}</pre>"
    
    if mcx["session_log"]:
        html += format_log_section("MCX Session Log", mcx["session_log"])
    
    if not mcx["orb"] and not mcx["positions"] and not mcx["session_log"]:
        html += "<p><em>No MCX data for today (market not yet open or no activity).</em></p>"
    
    return html


def build_email(today: date, candidates, nse_logs, session_report, swing, mcx, regime) -> str:
    """Build the complete HTML email."""
    
    # Summary stats
    n_candidates = len(candidates)
    n_entries = len(nse_logs["entries"])
    n_exits = len(nse_logs["exits"])
    n_swing_active = len(swing["active"])
    n_mcx_positions = len(mcx["positions"])
    
    return f"""<html>
<head>
<style>
body {{ font-family: -apple-system, 'Segoe UI', Arial, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; font-size: 13px; }}
h1 {{ color: #1a1a2e; border-bottom: 3px solid #e94560; padding-bottom: 8px; }}
h2 {{ color: #16213e; margin-top: 30px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
h3 {{ color: #333; margin-top: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 11px; }}
th {{ background: #1a1a2e; color: white; padding: 6px 8px; text-align: left; white-space: nowrap; }}
td {{ border: 1px solid #ddd; padding: 4px 8px; white-space: nowrap; }}
tr:nth-child(even) {{ background: #f8f9fa; }}
.summary {{ background: #f0f4ff; padding: 12px 16px; border-radius: 6px; margin: 12px 0; }}
.green {{ color: #28a745; font-weight: bold; }}
.red {{ color: #dc3545; font-weight: bold; }}
pre {{ font-size: 11px; }}
</style>
</head>
<body>

<h1>Daily Trade Logs - {today.strftime("%A %d %b %Y")}</h1>

<div class="summary">
<strong>Summary:</strong> Regime: <b>{regime}</b> | 
Candidates: <b>{n_candidates}</b> | 
Entries: <b>{n_entries}</b> | 
Exits: <b>{n_exits}</b> | 
Swing Active: <b>{n_swing_active}</b> | 
MCX Positions: <b>{n_mcx_positions}</b>
</div>

<h2>NSE ORB - Full Candidate Scores</h2>
<p><em>All parameters scored: gap%, RVOL, ADT, momentum, VWAP distance, sector strength, composite score</em></p>
{format_candidates_table(candidates)}

{format_log_section("NSE ORB - Entry Conditions & Timestamps", nse_logs["candidates_log"])}
{format_log_section("NSE ORB - Entries (with price & timestamp)", nse_logs["entries"])}
{format_log_section("NSE ORB - Exits (reason, price, timestamp)", nse_logs["exits"])}
{format_log_section("NSE ORB - Stop Loss Updates", nse_logs["sl_updates"])}

{"<h3>Session Report</h3><pre>" + json.dumps(session_report, indent=2, default=str) + "</pre>" if session_report else ""}

<h2>Swing Positions</h2>
{format_swing_table(swing["active"], "Active Positions")}
{format_swing_table(swing["closed_recent"], "Recently Closed (7 days)")}

{format_mcx_section(mcx)}

<hr>
<p style="color:#888; font-size:10px;">
Generated by daily_trade_logs_email.py | {datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")} | 
Data: candle_archive + bot.log + swing_positions + mcx_state
</p>
</body>
</html>"""


# ============================================================
# SEND EMAIL
# ============================================================

def send_email(subject: str, html_body: str):
    """Send via SES."""
    ses = boto3.client("ses", region_name="ap-south-1")
    sender = get_ses_sender()
    recipient = get_ses_recipient()
    
    ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Html": {"Data": html_body}
            }
        }
    )
    log.info(f"Email sent to {recipient}")


# ============================================================
# MAIN
# ============================================================

def main():
    today = date.today()
    log.info(f"=== Daily Trade Logs Email - {today.isoformat()} ===")
    
    # Collect everything
    log.info("Collecting NSE candidate scores...")
    candidates = collect_nse_candidates(today)
    log.info(f"  Found {len(candidates)} candidates")
    
    log.info("Collecting NSE entries/exits from bot.log...")
    nse_logs = collect_nse_entries_exits(today)
    log.info(f"  Entries: {len(nse_logs['entries'])}, Exits: {len(nse_logs['exits'])}")
    
    log.info("Collecting session report...")
    session_report = collect_session_report(today)
    
    log.info("Collecting swing data...")
    swing = collect_swing_data()
    log.info(f"  Active: {len(swing['active'])}, Closed (7d): {len(swing['closed_recent'])}")
    
    log.info("Collecting MCX data...")
    mcx = collect_mcx_data(today)
    
    regime = collect_regime()
    log.info(f"  Regime: {regime}")
    
    # Build and send
    subject = (
        f"Trade Logs | {today.strftime('%a %d %b')} | "
        f"Regime: {regime} | "
        f"{len(candidates)} candidates | "
        f"{len(nse_logs['entries'])} entries"
    )
    
    html = build_email(today, candidates, nse_logs, session_report, swing, mcx, regime)
    log.info(f"Email HTML: {len(html)} chars")
    
    send_email(subject, html)
    
    # Save copy
    output_file = LOGS / f"trade_logs_{today.isoformat()}.html"
    with open(output_file, "w") as f:
        f.write(html)
    log.info(f"Saved to {output_file}")
    
    log.info("=== DONE ===")


if __name__ == "__main__":
    main()
