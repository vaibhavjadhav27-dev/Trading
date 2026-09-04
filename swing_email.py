#!/usr/bin/env python3
"""swing_email.py — Daily Swing Paper Trade Report Email (V2)"""
import json, sys, os
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
sys.path.insert(0, "/home/ubuntu/trading-bot")

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist(): return datetime.now(IST)

def fetch_current_prices(tickers):
    """Fetch current/closing prices for swing positions from Dhan"""
    try:
        from secrets_manager import get_dhan_token, get_dhan_client_id
        import requests, csv
        # Load SIDs from watchlist
        sid_map = {}
        wl_path = "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/watchlist.csv"
        with open(wl_path) as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0] in tickers:
                    sid_map[row[0]] = int(row[1])
        if not sid_map: return {}
        # Fetch LTP from Dhan
        token = get_dhan_token(); client_id = get_dhan_client_id()
        headers = {"access-token": token, "client-id": client_id, "Content-Type": "application/json", "Accept": "application/json"}
        sids = list(sid_map.values())
        # Batch in groups of 100
        prices = {}
        for i in range(0, len(sids), 100):
            batch = sids[i:i+100]
            payload = {"NSE_EQ": batch}
            resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp", json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            sid = str(item.get("security_id", item.get("SID", "")))
                            ltp = float(item.get("LTP", item.get("ltp", 0)) or 0)
                            if ltp > 0:
                                # Map SID back to ticker
                                for t, s in sid_map.items():
                                    if str(s) == sid: prices[t] = ltp
        return prices
    except Exception as e:
        print(f"LTP fetch for swing email failed: {e}")
        return {}
def today_str(): return now_ist().strftime("%Y-%m-%d")

def send_email(subject, html_body):
    import boto3
    from secrets_manager import get_ses_sender, get_ses_recipient
    ses = boto3.client("ses", region_name="ap-south-1")
    ses.send_email(Source=get_ses_sender(), Destination={"ToAddresses": [get_ses_recipient()]},
        Message={"Subject": {"Data": subject}, "Body": {"Html": {"Data": html_body}}})
    print(f"Email sent: {subject}")

def main():
    positions_file = Path("/home/ubuntu/trading-bot/swing_positions.json")
    if not positions_file.exists():
        print("No swing_positions.json found"); return
    data = json.loads(positions_file.read_text())
    active = data.get("active", [])
    closed = data.get("closed", [])

    total_unrealized = 0; total_realized = 0; winners = 0; losers = 0
    today = date.today()
    # Fetch current prices for all active positions
    all_tickers = [p.get("ticker","") for p in active if p.get("ticker")]
    current_prices = fetch_current_prices(all_tickers)
    print(f"  Fetched prices for {len(current_prices)}/{len(all_tickers)} stocks")

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:950px;margin:auto;padding:20px;">
<h2 style="color:#1a237e;">Swing Paper Trade Report - {today_str()}</h2>
<p>Mode: PAPER | Active: {len(active)} | Closed: {len(closed)} | Engine: V8.5</p><hr>

<h3 style="color:#2e7d32;">Active Positions ({len(active)})</h3>
<table style="border-collapse:collapse;width:100%;font-size:11px;border:1px solid #ddd;">
<tr style="background:#e8f5e9;font-weight:bold;">
<th style="padding:5px;border:1px solid #ddd;">#</th>
<th>Stock</th><th>Entry Date</th><th>Days</th><th>Entry</th>
<th>Current</th><th>Target</th><th>SL</th>
<th>P&L Rs.</th><th>P&L%</th><th>Score</th></tr>"""

    for i, p in enumerate(active, 1):
        entry = float(p.get("entry_price", 0))
        current = float(current_prices.get(p.get("ticker",""), 0) or p.get("peak_price", entry))
        peak = max(float(p.get("peak_price", entry)), current) if current > 0 else float(p.get("peak_price", entry))
        target = float(p.get("target", 0))
        sl = float(p.get("trailing_sl", p.get("sl", 0)))
        qty = int(p.get("qty", 0))
        entry_date = p.get("entry_date", "")
        days = (today - date.fromisoformat(entry_date)).days if entry_date else 0
        pnl_pct = ((current - entry) / entry * 100) if entry > 0 and current > 0 else 0
        pnl_rs = (current - entry) * qty if current > 0 else 0
        total_unrealized += pnl_rs
        color = "green" if pnl_pct >= 0 else "red"
        html += f"""<tr><td style="padding:4px;border:1px solid #ddd;text-align:center;">{i}</td>
<td style="font-weight:bold;">{p.get("ticker","?")}</td>
<td>{entry_date}</td><td>{days}d</td>
<td>Rs.{entry:.2f}</td><td>Rs.{current:.2f}</td>
<td>Rs.{target:.2f}</td><td>Rs.{sl:.2f}</td>
<td style="color:{color};font-weight:bold;">Rs.{pnl_rs:+,.0f}</td>
<td style="color:{color};">{pnl_pct:+.2f}%</td>
<td>{p.get("score",0):.0f}</td></tr>"""

    html += f"""</table>
<p><b>Total Unrealized P&L: <span style="color:{'green' if total_unrealized>=0 else 'red'}">Rs.{total_unrealized:+,.0f}</span></b></p><hr>"""

    # Closed Trades Section
    html += f"""<h3 style="color:#c62828;">Closed Trades ({len(closed)})</h3>"""
    if closed:
        html += """<table style="border-collapse:collapse;width:100%;font-size:11px;border:1px solid #ddd;">
<tr style="background:#fce4ec;font-weight:bold;">
<th style="padding:5px;border:1px solid #ddd;">#</th>
<th>Stock</th><th>Entry</th><th>Exit</th><th>Days</th>
<th>Entry Price</th><th>Exit Price</th><th>P&L Rs.</th><th>P&L%</th><th>Reason</th></tr>"""
        for i, c in enumerate(closed[-15:], 1):
            entry = float(c.get("entry_price", 0))
            exit_px = float(c.get("exit_price", 0))
            qty = int(c.get("qty", 0))
            pnl_rs = float(c.get("pnl", (exit_px - entry) * qty))
            pnl_pct = float(c.get("pnl_pct", ((exit_px - entry) / entry * 100) if entry > 0 else 0))
            total_realized += pnl_rs
            if pnl_rs > 0: winners += 1
            else: losers += 1
            color = "green" if pnl_rs >= 0 else "red"
            html += f"""<tr><td style="padding:4px;border:1px solid #ddd;text-align:center;">{i}</td>
<td style="font-weight:bold;">{c.get("ticker","?")}</td>
<td>{c.get("entry_date","?")}</td><td>{c.get("exit_date","?")}</td>
<td>{c.get("days_held",0)}d</td>
<td>Rs.{entry:.2f}</td><td>Rs.{exit_px:.2f}</td>
<td style="color:{color};font-weight:bold;">Rs.{pnl_rs:+,.0f}</td>
<td style="color:{color};">{pnl_pct:+.2f}%</td>
<td>{c.get("exit_type","?")}</td></tr>"""
        html += "</table>"
        html += f"<p><b>Realized P&L: <span style=\"color:{'green' if total_realized>=0 else 'red'}\">Rs.{total_realized:+,.0f}</span> | Winners: {winners} | Losers: {losers} | Win Rate: {winners/(winners+losers)*100:.0f}%</b></p>"
    else:
        html += "<p style='color:#999;'>No closed trades yet.</p>"

    html += """<hr><p style='font-size:10px;color:#999;'>Swing Paper Trader | V8.5 | LIVE=False | Positions updated by swing_monitor.py</p></body></html>"""

    subject = f"Swing: {len(active)} active | Unrealized Rs.{total_unrealized:+,.0f} | Realized Rs.{total_realized:+,.0f} | {today_str()}"
    send_email(subject, html)

if __name__ == "__main__":
    main()
