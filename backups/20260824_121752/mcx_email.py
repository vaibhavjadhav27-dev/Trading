#!/usr/bin/env python3
"""mcx_email.py — MCX Evening Session Report Email"""
import json, sys, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, "/home/ubuntu/trading-bot")
sys.path.insert(0, "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED")

IST = timezone(timedelta(hours=5, minutes=30))
MCX_LOG_DIR = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trade_logs/mcx")
STATE_PATH = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/mcx_native_state.json")

def now_ist(): return datetime.now(IST)
def today_str(): return now_ist().strftime("%Y-%m-%d")

def read_jsonl(path):
    if not path.exists(): return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

def send_email(subject, html_body):
    import boto3
    from secrets_manager import get_ses_sender, get_ses_recipient
    ses = boto3.client("ses", region_name="ap-south-1")
    ses.send_email(Source=get_ses_sender(), Destination={"ToAddresses": [get_ses_recipient()]},
        Message={"Subject": {"Data": subject}, "Body": {"Html": {"Data": html_body}}})
    print(f"Email sent: {subject}")

def main():
    date = today_str()
    orbs = read_jsonl(MCX_LOG_DIR / f"orb_{date}.jsonl")
    signals = read_jsonl(MCX_LOG_DIR / f"signals_{date}.jsonl")
    exits = read_jsonl(MCX_LOG_DIR / f"exits_{date}.jsonl")
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

    session_pnl = float(state.get("session_pnl_pct", 0))
    total_signals = len(signals)

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;">
<h2 style="color:#4a148c;">MCX Evening Session Report - {date}</h2>
<p>Mode: SHADOW (no real orders) | Session: 19:00-23:00 IST | Architecture: Native ORB 3-Layer</p><hr>

<h3>Session Summary</h3>
<table style="border-collapse:collapse;">
<tr><td><b>Signals Generated</b></td><td>{total_signals}</td></tr>
<tr><td><b>Trades (shadow)</b></td><td>{len(exits)}</td></tr>
<tr><td><b>Session P&L</b></td><td style="color:{'green' if session_pnl>=0 else 'red'}">{session_pnl:+.3f}%</td></tr>
</table><hr>"""

    # ORB Levels
    if orbs:
        html += "<h3>Native MCX ORB Levels (19:00-19:15 IST)</h3>"
        html += "<table style='border-collapse:collapse;width:100%;font-size:12px;border:1px solid #ddd;'>"
        html += "<tr style='background:#f3e5f5;'><th>Contract</th><th>ORB High</th><th>ORB Low</th><th>Range</th><th>Range%</th></tr>"
        for orb in orbs:
            h = float(orb.get("high", 0)); l = float(orb.get("low", 0))
            rng = h - l; rng_pct = (rng / l * 100) if l > 0 else 0
            html += f"<tr><td><b>{orb.get('contract','?')}</b></td><td>Rs.{h:.2f}</td><td>Rs.{l:.2f}</td><td>Rs.{rng:.2f}</td><td>{rng_pct:.2f}%</td></tr>"
        html += "</table><hr>"

    # Signals
    if signals:
        html += "<h3>Signals</h3>"
        html += "<table style='border-collapse:collapse;width:100%;font-size:12px;border:1px solid #ddd;'>"
        html += "<tr style='background:#e8f5e9;'><th>Time</th><th>Contract</th><th>Side</th><th>Entry</th><th>SL</th><th>T1</th><th>Score</th><th>US Confirm</th><th>Action</th></tr>"
        for s in signals:
            html += f"<tr><td>{s.get('timestamp','')[:16]}</td><td><b>{s.get('contract','?')}</b></td>"
            html += f"<td style='color:{"green" if s.get("side")=="LONG" else "red"}'>{s.get('side','')}</td>"
            html += f"<td>Rs.{s.get('entry_price',0):.2f}</td><td>Rs.{s.get('stop',0):.2f}</td>"
            html += f"<td>Rs.{s.get('t1',0):.2f}</td><td>{s.get('score',0):.1f}</td>"
            html += f"<td>{'Yes' if s.get('us_confirms') else 'No'}</td><td>{s.get('action','')}</td></tr>"
        html += "</table><hr>"

    # Exits
    if exits:
        html += "<h3>Exits</h3>"
        html += "<table style='border-collapse:collapse;width:100%;font-size:12px;border:1px solid #ddd;'>"
        html += "<tr style='background:#fce4ec;'><th>Contract</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L%</th><th>Reason</th></tr>"
        for e in exits:
            pnl = float(e.get("pnl_pct", 0))
            html += f"<tr><td><b>{e.get('contract','?')}</b></td><td>{e.get('side','')}</td>"
            html += f"<td>Rs.{e.get('entry',0):.2f}</td><td>Rs.{e.get('exit',0):.2f}</td>"
            html += f"<td style='color:{"green" if pnl>=0 else "red"}'>{pnl:+.3f}%</td>"
            html += f"<td>{e.get('reason','')}</td></tr>"
        html += "</table><hr>"

    if not orbs and not signals:
        html += "<div style='background:#fff3e0;padding:15px;border-radius:8px;'>"
        html += "<h3>No MCX Data Today</h3>"
        html += "<p>MCX session may not have run, or no ORB was formed (insufficient candle data).</p></div><hr>"

    html += "<p style='font-size:10px;color:#999;'>MCX Native ORB v2 | SHADOW | V8.5</p></body></html>"

    subject = f"MCX Session: {total_signals} signals | P&L {session_pnl:+.2f}% | {date}"
    send_email(subject, html)

if __name__ == "__main__":
    main()
