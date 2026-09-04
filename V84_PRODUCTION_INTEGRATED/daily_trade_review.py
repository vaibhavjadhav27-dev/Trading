#!/usr/bin/env python3
"""
daily_trade_review.py - Post-market V8.4 daily trade review email
Compiles: scan scores, entries, exits, candidate counts, market context
Sends via SES at 15:30 IST (cron: 0 10 * * 1-5)
"""
import json, csv, os, sys, re
from pathlib import Path
from datetime import datetime, timedelta, timezone
import boto3

sys.path.insert(0, "/home/ubuntu/trading-bot")
sys.path.insert(0, "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED")

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trade_logs")
V84_LOG = Path("/home/ubuntu/trading-bot/logs/v84_live.log")

def now_ist():
    return datetime.now(IST)

def today_str():
    return now_ist().strftime("%Y-%m-%d")

def get_ses_client():
    from secrets_manager import get_ses_sender, get_ses_recipient
    return boto3.client("ses", region_name="ap-south-1"), get_ses_sender(), get_ses_recipient()

def read_jsonl(path):
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def read_csv_file(path):
    if not path.exists(): return [], []
    with open(path) as f:
        reader = csv.reader(f)
        headers = next(reader, [])
        rows = list(reader)
    return headers, rows

def get_candidate_counts(date_str):
    counts = []
    if not V84_LOG.exists(): return counts
    for line in V84_LOG.read_text().splitlines():
        if date_str in line and "candidates >=50:" in line:
            m = re.search(r"candidates >=50: (\d+)/(\d+)", line)
            if m:
                ts = line[:19]
                counts.append({"time": ts, "count": int(m.group(1)), "total": int(m.group(2))})
    return counts

def build_html(date_str, entries, exits, scan_headers, scan_rows, candidate_counts):
    total_trades = len(exits) if exits else len(entries)
    winners = sum(1 for e in exits if e.get("pnl_pct", 0) > 0)
    losers = sum(1 for e in exits if e.get("pnl_pct", 0) < 0)
    total_pnl = sum(e.get("pnl_pct", 0) for e in exits)
    max_candidates = max((c["count"] for c in candidate_counts), default=0)
    total_scans = len(candidate_counts)
    scans_with_candidates = sum(1 for c in candidate_counts if c["count"] > 0)

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;">
<h2 style="color:#1a237e;">V8.4 Daily Trade Review - {date_str}</h2>
<hr>

<h3>Session Summary</h3>
<table style="border-collapse:collapse;width:100%;">
<tr><td><b>Total Trades</b></td><td>{total_trades}</td></tr>
<tr><td><b>Winners / Losers</b></td><td>{winners} / {losers}</td></tr>
<tr><td><b>Total P&L</b></td><td style="color:{'green' if total_pnl>=0 else 'red'};font-weight:bold;">{total_pnl:+.3f}%</td></tr>
<tr><td><b>Scan Cycles</b></td><td>{total_scans} (candidates found in {scans_with_candidates})</td></tr>
<tr><td><b>Peak Candidates</b></td><td>{max_candidates}/180 in single cycle</td></tr>
</table>
<hr>
"""

    # Entries
    if entries:
        html += "<h3>Entries</h3><table style='border-collapse:collapse;width:100%;font-size:12px;'>"
        html += "<tr style='background:#e3f2fd;'><th>Time</th><th>Symbol</th><th>Side</th><th>Score</th><th>Price</th><th>Qty</th><th>RS</th><th>RVOL</th><th>Momentum</th></tr>"
        for e in entries:
            html += f"<tr><td>{e.get('timestamp','')[:19]}</td><td><b>{e.get('symbol','?')}</b></td>"
            html += f"<td style='color:{"red" if e.get("side")=="SHORT" else "green"}'>{e.get('side','')}</td>"
            html += f"<td>{e.get('score_total',0):.1f}</td><td>Rs.{e.get('fill_price',e.get('ltp_at_signal',0)):.2f}</td>"
            html += f"<td>{e.get('fill_qty',0)}</td><td>{e.get('rs_score',0):.2f}</td>"
            html += f"<td>{e.get('rvol',0):.2f}x</td><td>{e.get('score_momentum',0):.1f}</td></tr>"
        html += "</table><hr>"

    # Exits
    if exits:
        html += "<h3>Exits</h3><table style='border-collapse:collapse;width:100%;font-size:12px;'>"
        html += "<tr style='background:#fce4ec;'><th>Time</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L%</th><th>Reason</th><th>Duration</th></tr>"
        for e in exits:
            pnl = e.get("pnl_pct", 0)
            html += f"<tr><td>{e.get('timestamp','')[:19]}</td><td><b>{e.get('symbol','?')}</b></td>"
            html += f"<td>{e.get('side','')}</td><td>Rs.{e.get('entry_price',0):.2f}</td>"
            html += f"<td>Rs.{e.get('exit_price',0):.2f}</td>"
            html += f"<td style='color:{"green" if pnl>=0 else "red"};font-weight:bold;'>{pnl:+.3f}%</td>"
            html += f"<td>{e.get('exit_reason','')}</td><td>{e.get('duration_minutes',0)}m</td></tr>"
        html += "</table><hr>"

    # Top Scored Candidates (from scan CSV)
    if scan_rows:
        html += "<h3>Top Scored Candidates (Scan Logger)</h3><table style='border-collapse:collapse;width:100%;font-size:11px;'>"
        html += "<tr style='background:#e8f5e9;'><th>Time</th><th>Symbol</th><th>Score</th><th>LTP</th><th>RS</th><th>RVOL</th><th>Mom5</th><th>Mom15</th></tr>"
        for row in scan_rows[:20]:
            if len(row) >= 12:
                html += f"<tr><td>{row[0][:16]}</td><td><b>{row[1]}</b></td><td>{float(row[4]):.1f}</td>"
                html += f"<td>Rs.{float(row[5]):.2f}</td><td>{float(row[7]):.2f}</td>"
                html += f"<td>{float(row[8]):.2f}x</td><td>{float(row[10]):.2f}%</td><td>{float(row[11]):.2f}%</td></tr>"
        html += "</table><hr>"

    # Candidate count distribution
    if candidate_counts:
        html += "<h3>Candidate Activity Through Day</h3>"
        html += "<p style='font-size:11px;color:#666;'>"
        for c in candidate_counts[-30:]:
            html += f"{c['time'][11:16]}={c['count']} | "
        html += "</p><hr>"

    # No trades warning
    if not entries and not exits:
        html += "<div style='background:#fff3e0;padding:15px;border-radius:8px;'>"
        html += "<h3 style='color:#e65100;'>No Trades Today</h3>"
        html += f"<p>V8.4 scanned {total_scans} cycles, found candidates in {scans_with_candidates} cycles "
        html += f"(peak: {max_candidates}/180), but no trades were executed.</p>"
        html += "<p>Possible reasons: entry confirmation failed, volume gate rejected, or crash prevented execution.</p>"
        html += "</div><hr>"

    html += "<p style='font-size:10px;color:#999;'>Generated by daily_trade_review.py | V8.4 Production</p>"
    html += "</body></html>"
    return html

def send_email(subject, html_body):
    try:
        ses, sender, recipient = get_ses_client()
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Html": {"Data": html_body}}
            }
        )
        print(f"Email sent: {subject}")
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False

def main():
    date_str = today_str()
    print(f"Compiling V8.4 daily review for {date_str}...")

    entries = read_jsonl(LOG_DIR / f"entries_{date_str}.jsonl")
    exits = read_jsonl(LOG_DIR / f"exits_{date_str}.jsonl")
    scan_headers, scan_rows = read_csv_file(LOG_DIR / f"scans_{date_str}.csv")
    candidate_counts = get_candidate_counts(date_str)

    print(f"  Entries: {len(entries)}")
    print(f"  Exits: {len(exits)}")
    print(f"  Scan rows: {len(scan_rows)}")
    print(f"  Candidate cycles: {len(candidate_counts)}")

    html = build_html(date_str, entries, exits, scan_headers, scan_rows, candidate_counts)
    subject = f"V8.4 Trade Review: {date_str} | {len(entries)} trades | P&L {sum(e.get('pnl_pct',0) for e in exits):+.2f}%"

    send_email(subject, html)

if __name__ == "__main__":
    main()
