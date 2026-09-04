#!/usr/bin/env python3
"""
chatgpt_trade_analyst.py - Daily GPT-4o Trade Analysis & Recommendations
=========================================================================
Runs after all markets close. Collects:
  1. NSE ORB intraday trades (candidates, entries, exits, P&L)
  2. Swing positions (active + closed)
  3. MCX shadow trades
  4. Market context (regime, NIFTY, sector data)

Sends consolidated data to GPT-4o, receives analysis, emails results.

Schedule: 23:30 IST (18:00 UTC) daily Mon-Fri
  After MCX close (23:00) and all session reports generated.

Requirements:
  pip install openai  (if not installed)
  SSM Parameter: openai-api-key (SecureString)
"""

import json, os, sys, glob, logging, boto3
from datetime import datetime, date, timedelta
from pathlib import Path

# --- Paths ---
BASE = Path("/home/ubuntu/trading-bot")
V84 = BASE / "V84_PRODUCTION_INTEGRATED"
LOGS = BASE / "logs"
STATE = V84 / "mcx_state"
SWING_DIR = V84

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS / "chatgpt_analyst.log")
    ]
)
log = logging.getLogger("chatgpt_analyst")

# --- Secrets ---
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(V84))
from secrets_manager import get_ses_sender, get_ses_recipient


def get_openai_key():
    """Fetch OpenAI API key from AWS SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name="ap-south-1")
    resp = ssm.get_parameter(Name="openai-api-key", WithDecryption=True)
    return resp["Parameter"]["Value"].strip()


# ============================================================
# DATA COLLECTORS
# ============================================================

def collect_nse_orb_data(today: date) -> dict:
    """Collect NSE ORB candidates, entries, exits from today's session."""
    data = {"candidates": [], "entries": [], "exits": [], "summary": ""}
    
    # 1. Candidates from pending_{date}.json
    pending = V84 / f"pending_{today.isoformat()}.json"
    if pending.exists():
        with open(pending) as f:
            data["candidates"] = json.load(f)
    
    # 2. Session report
    report_pattern = str(LOGS / f"session_report_{today.isoformat()}*.json")
    reports = sorted(glob.glob(report_pattern))
    if reports:
        with open(reports[-1]) as f:
            data["summary"] = json.load(f)
    
    # 3. Parse bot.log for today's entries/exits
    bot_log = LOGS / "bot.log"
    if bot_log.exists():
        today_str = today.isoformat()
        with open(bot_log) as f:
            for line in f:
                if today_str not in line and today.strftime("%Y-%m-%d") not in line:
                    continue
                if "ENTRY" in line or "FILLED" in line:
                    data["entries"].append(line.strip())
                elif "EXIT" in line or "CLOSED" in line:
                    data["exits"].append(line.strip())
    
    return data


def collect_swing_data() -> dict:
    """Collect active swing positions and recent closures."""
    data = {"active": [], "closed_recent": []}
    
    # Active positions
    active_file = V84 / "swing_positions.json"
    if active_file.exists():
        with open(active_file) as f:
            data["active"] = json.load(f)
    
    # Closed trades (last 7 days)
    closed_file = V84 / "swing_closed.json"
    if closed_file.exists():
        with open(closed_file) as f:
            all_closed = json.load(f)
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            data["closed_recent"] = [
                t for t in all_closed
                if t.get("exit_date", "") >= cutoff
            ]
    
    return data


def collect_mcx_data(today: date) -> dict:
    """Collect MCX shadow session data."""
    data = {"positions": [], "orb": {}, "session_log": []}
    
    # Position files
    pos_pattern = str(STATE / f"positions_{today.isoformat()}*.json")
    pos_files = sorted(glob.glob(pos_pattern))
    if pos_files:
        with open(pos_files[-1]) as f:
            data["positions"] = json.load(f)
    
    # ORB data
    orb_file = STATE / f"orb_{today.isoformat()}.json"
    if orb_file.exists():
        with open(orb_file) as f:
            data["orb"] = json.load(f)
    
    # Session log (last 50 lines)
    mcx_log = LOGS / "mcx_v854.log"
    if mcx_log.exists():
        with open(mcx_log) as f:
            lines = f.readlines()
            today_lines = [l.strip() for l in lines if today.isoformat() in l or today.strftime("%Y-%m-%d") in l]
            data["session_log"] = today_lines[-50:]
    
    return data


def collect_market_context() -> dict:
    """Collect regime, NIFTY level, sector data."""
    data = {"regime": "UNKNOWN", "nifty": {}, "sectors": {}}
    
    # Regime
    regime_file = BASE / "regime_state.json"
    if regime_file.exists():
        with open(regime_file) as f:
            data["regime"] = json.load(f).get("regime", "UNKNOWN")
    
    # Nifty regime scanner output
    regime_log = Path("/tmp/regime_scanner.log")
    if regime_log.exists():
        with open(regime_log) as f:
            lines = f.readlines()
            if lines:
                data["nifty"]["latest_scan"] = lines[-1].strip()
    
    return data


# ============================================================
# GPT-4o ANALYSIS
# ============================================================

SYSTEM_PROMPT = """You are a professional quantitative trading analyst reviewing daily trade data 
from an automated NSE/MCX trading system. The system uses:
- NSE ORB (Opening Range Breakout) for intraday equity
- Swing positions (multi-day holds)  
- MCX commodity futures (shadow/paper mode)

Your job:
1. REVIEW today's trades: what worked, what didn't, score accuracy, timing quality
2. RECOMMEND for tomorrow:
   - Top 3-5 stocks to watch for ORB entry (with reasoning)
   - Any swing positions to add/exit based on trend + P&L
   - MCX contracts showing setup potential
3. TIMING guidance: optimal entry/exit windows based on observed patterns
4. RISK NOTES: position sizing issues, correlation risk, regime mismatches

Format: Use clear HTML tables and sections. Be concise but specific with numbers.
Currency: INR (Rs.). Exchange: NSE (India) and MCX (India commodities).
Trading hours: NSE 09:15-15:30 IST, MCX 09:00-23:30 IST.
"""

def call_chatgpt(trade_data: str, api_key: str) -> str:
    """Send trade data to GPT-4o and get analysis."""
    try:
        from openai import OpenAI
    except ImportError:
        os.system("pip install openai -q")
        from openai import OpenAI
    
    client = OpenAI(api_key=api_key)
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"""Here is today's complete trading data. 
Analyse everything and provide your full analysis + tomorrow's recommendations.

{trade_data}"""}
        ],
        temperature=0.3,
        max_tokens=4000
    )
    
    return response.choices[0].message.content


# ============================================================
# EMAIL
# ============================================================

def send_email(subject: str, html_body: str):
    """Send analysis email via SES."""
    ses = boto3.client("ses", region_name="ap-south-1")
    sender = get_ses_sender()
    recipient = get_ses_recipient()
    
    ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject},
            "Body": {
                "Html": {"Data": html_body},
                "Text": {"Data": html_body}  # fallback
            }
        }
    )
    log.info(f"Email sent to {recipient}")


def format_email(analysis: str, today: date) -> str:
    """Wrap GPT analysis in email HTML."""
    return f"""<html>
<head>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
h1 {{ color: #1a1a2e; border-bottom: 2px solid #e94560; padding-bottom: 8px; }}
h2 {{ color: #16213e; margin-top: 24px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th {{ background: #1a1a2e; color: white; padding: 8px 12px; text-align: left; }}
td {{ border: 1px solid #ddd; padding: 6px 12px; }}
tr:nth-child(even) {{ background: #f8f9fa; }}
.green {{ color: #28a745; font-weight: bold; }}
.red {{ color: #dc3545; font-weight: bold; }}
.section {{ background: #f0f4ff; padding: 12px; border-radius: 6px; margin: 12px 0; }}
</style>
</head>
<body>
<h1>GPT-4o Trade Analysis - {today.strftime("%a %d %b %Y")}</h1>
{analysis}
<hr>
<p style="color:#888; font-size:11px;">
Generated by chatgpt_trade_analyst.py | Model: gpt-4o | 
Data: NSE ORB + Swing + MCX Shadow + Regime
</p>
</body>
</html>"""


# ============================================================
# MAIN
# ============================================================

def main():
    today = date.today()
    log.info(f"=== ChatGPT Trade Analyst - {today.isoformat()} ===")
    
    # 1. Collect all data
    log.info("Collecting NSE ORB data...")
    nse = collect_nse_orb_data(today)
    
    log.info("Collecting swing data...")
    swing = collect_swing_data()
    
    log.info("Collecting MCX data...")
    mcx = collect_mcx_data(today)
    
    log.info("Collecting market context...")
    context = collect_market_context()
    
    # 2. Format for GPT
    trade_data = f"""
## MARKET CONTEXT
- Regime: {context['regime']}
- Nifty: {json.dumps(context.get('nifty', {}), indent=2)}

## NSE ORB INTRADAY ({today.isoformat()})
Candidates shortlisted: {len(nse['candidates'])}
Candidates: {json.dumps(nse['candidates'][:20], indent=2, default=str)}

Entries today: {len(nse['entries'])}
{chr(10).join(nse['entries'][:15])}

Exits today: {len(nse['exits'])}
{chr(10).join(nse['exits'][:15])}

Session summary: {json.dumps(nse.get('summary', 'N/A'), indent=2, default=str)}

## SWING POSITIONS
Active ({len(swing['active'])} positions):
{json.dumps(swing['active'], indent=2, default=str)}

Recently closed (last 7 days): {len(swing['closed_recent'])}
{json.dumps(swing['closed_recent'], indent=2, default=str)}

## MCX SHADOW ({today.isoformat()})
ORB levels: {json.dumps(mcx.get('orb', {}), indent=2, default=str)}
Positions: {json.dumps(mcx.get('positions', []), indent=2, default=str)}
Session log (last entries):
{chr(10).join(mcx.get('session_log', [])[-20:])}
"""
    
    # 3. Call GPT-4o
    log.info("Calling GPT-4o...")
    api_key = get_openai_key()
    analysis = call_chatgpt(trade_data, api_key)
    log.info(f"GPT response: {len(analysis)} chars")
    
    # 4. Email
    subject = f"GPT-4o Trade Analysis | {today.strftime('%a %d %b')} | Regime: {context['regime']}"
    html = format_email(analysis, today)
    send_email(subject, html)
    
    # 5. Save locally
    output_file = LOGS / f"gpt_analysis_{today.isoformat()}.html"
    with open(output_file, "w") as f:
        f.write(html)
    log.info(f"Saved to {output_file}")
    
    log.info("=== DONE ===")


if __name__ == "__main__":
    main()
