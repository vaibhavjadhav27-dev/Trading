#!/usr/bin/env python3
"""
daily_trade_logs_email.py - Email full trade logs as .tar.gz attachment
=======================================================================
Collects ALL trade-related files for the day and sends as a compressed
tar.gz attachment via SES. You can then paste/upload contents to ChatGPT.

Includes:
  - candle_archive/candidate_scores_{date}.csv (full score breakdown)
  - bot.log filtered for today (entries, exits, scores, SL updates)
  - pending_{date}.json (shortlisted candidates)
  - session_report*.json
  - swing_positions.json (active)
  - swing_closed.json (recent closures)
  - mcx_state/ (ORB, positions)
  - mcx_v854.log (today's lines)
  - regime_state.json

Schedule: 15:30 IST (10:00 UTC) Mon-Fri
"""

import json, os, sys, glob, logging, boto3, tarfile, tempfile, io
from datetime import datetime, date, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import base64

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
# COLLECT FILES INTO TAR.GZ
# ============================================================

def filter_bot_log_today(today: date) -> str:
    """Extract today's lines from bot.log."""
    bot_log = LOGS / "bot.log"
    if not bot_log.exists():
        return ""
    
    today_str = today.strftime("%Y-%m-%d")
    lines = []
    with open(bot_log) as f:
        for line in f:
            if today_str in line:
                lines.append(line)
    return "".join(lines)


def filter_mcx_log_today(today: date) -> str:
    """Extract today's lines from mcx_v854.log."""
    mcx_log = LOGS / "mcx_v854.log"
    if not mcx_log.exists():
        return ""
    
    today_str = today.strftime("%Y-%m-%d")
    lines = []
    with open(mcx_log) as f:
        for line in f:
            if today_str in line:
                lines.append(line)
    return "".join(lines)


def build_tarball(today: date) -> tuple:
    """
    Build a tar.gz with all trade files for the day.
    Returns (tar_bytes, file_list) tuple.
    """
    buf = io.BytesIO()
    file_list = []
    prefix = f"trade_logs_{today.isoformat()}"
    
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        
        # 1. Candidate scores CSV
        for pattern in [
            CANDLE_ARCHIVE / f"candidate_scores_{today.isoformat()}.csv",
            CANDLE_ARCHIVE / f"candidate_scores_{today.strftime('%Y%m%d')}.csv",
        ]:
            if pattern.exists():
                tar.add(str(pattern), arcname=f"{prefix}/candidate_scores.csv")
                file_list.append(f"candidate_scores.csv ({pattern.stat().st_size} bytes)")
                break
        
        # 2. Pending candidates JSON
        for pattern in [
            V84 / f"pending_{today.isoformat()}.json",
            V84 / f"pending_{today.strftime('%Y%m%d')}.json",
        ]:
            if pattern.exists():
                tar.add(str(pattern), arcname=f"{prefix}/pending_candidates.json")
                file_list.append(f"pending_candidates.json ({pattern.stat().st_size} bytes)")
                break
        
        # 3. Session report
        reports = sorted(glob.glob(str(LOGS / f"session_report*{today.isoformat()}*")))
        if not reports:
            reports = sorted(glob.glob(str(LOGS / f"session_report*{today.strftime('%Y%m%d')}*")))
        for r in reports:
            name = os.path.basename(r)
            tar.add(r, arcname=f"{prefix}/{name}")
            file_list.append(f"{name} ({os.path.getsize(r)} bytes)")
        
        # 4. Bot log (today only)
        bot_today = filter_bot_log_today(today)
        if bot_today:
            info = tarfile.TarInfo(name=f"{prefix}/bot_log_today.txt")
            data = bot_today.encode("utf-8")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            file_list.append(f"bot_log_today.txt ({len(data)} bytes)")
        
        # 5. MCX log (today only)
        mcx_today = filter_mcx_log_today(today)
        if mcx_today:
            info = tarfile.TarInfo(name=f"{prefix}/mcx_v854_log_today.txt")
            data = mcx_today.encode("utf-8")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            file_list.append(f"mcx_v854_log_today.txt ({len(data)} bytes)")
        
        # 6. Swing positions (active)
        swing_file = V84 / "swing_positions.json"
        if swing_file.exists():
            tar.add(str(swing_file), arcname=f"{prefix}/swing_positions_active.json")
            file_list.append(f"swing_positions_active.json ({swing_file.stat().st_size} bytes)")
        
        # 7. Swing closed
        closed_file = V84 / "swing_closed.json"
        if closed_file.exists():
            tar.add(str(closed_file), arcname=f"{prefix}/swing_closed.json")
            file_list.append(f"swing_closed.json ({closed_file.stat().st_size} bytes)")
        
        # 8. MCX state files (today)
        for pattern in [
            str(STATE / f"positions_{today.isoformat()}*.json"),
            str(STATE / f"orb_{today.isoformat()}*.json"),
        ]:
            for f_path in sorted(glob.glob(pattern)):
                name = os.path.basename(f_path)
                tar.add(f_path, arcname=f"{prefix}/mcx_state/{name}")
                file_list.append(f"mcx_state/{name} ({os.path.getsize(f_path)} bytes)")
        
        # 9. Regime state
        regime_file = BASE / "regime_state.json"
        if regime_file.exists():
            tar.add(str(regime_file), arcname=f"{prefix}/regime_state.json")
            file_list.append(f"regime_state.json ({regime_file.stat().st_size} bytes)")
        
        # 10. Daily trade review (if generated)
        review_file = LOGS / f"trade_review_{today.isoformat()}.json"
        if review_file.exists():
            tar.add(str(review_file), arcname=f"{prefix}/trade_review.json")
            file_list.append(f"trade_review.json ({review_file.stat().st_size} bytes)")
        
        # 11. Watchlist for reference
        watchlist = V84 / "watchlist.csv"
        if watchlist.exists():
            tar.add(str(watchlist), arcname=f"{prefix}/watchlist.csv")
            file_list.append(f"watchlist.csv ({watchlist.stat().st_size} bytes)")
        
        # 12. Add a README with file descriptions
        readme = f"""# Trade Logs - {today.isoformat()}
# ==============================
# 
# Files included:
# - candidate_scores.csv: All ORB candidates with full score breakdown
#   (gap%, RVOL, ADT, momentum, VWAP, sector, composite score)
# - pending_candidates.json: Shortlisted candidates that passed gate
# - session_report*.json: End-of-session summary with P&L
# - bot_log_today.txt: Full bot log filtered for today
#   (CANDIDATE, ENTRY, EXIT, SL_UPDATE, SCORE events)
# - mcx_v854_log_today.txt: MCX shadow session log
# - swing_positions_active.json: Current swing holdings
# - swing_closed.json: All closed swing trades
# - mcx_state/: MCX ORB levels and position state
# - regime_state.json: Current market regime
# - trade_review.json: Daily trade review analysis
# - watchlist.csv: Full 552-stock watchlist with security IDs
#
# Usage: Paste contents into ChatGPT for analysis and recommendations.
"""
        info = tarfile.TarInfo(name=f"{prefix}/README.txt")
        data = readme.encode("utf-8")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    
    return buf.getvalue(), file_list


# ============================================================
# SEND EMAIL WITH ATTACHMENT
# ============================================================

def send_email_with_attachment(today: date, tar_bytes: bytes, file_list: list):
    """Send email with tar.gz saved locally + plain text summary via SendEmail."""
    ses = boto3.client("ses", region_name="ap-south-1")
    sender = get_ses_sender()
    recipient = get_ses_recipient()

    body_text = f"""Daily Trade Logs - {today.strftime('%A %d %b %Y')}

Archive saved on server: /home/ubuntu/trading-bot/logs/trade_logs_{today.isoformat()}.tar.gz ({len(tar_bytes)} bytes)

Files included ({len(file_list)}):
"""
    for f in file_list:
        body_text += f"  - {f}\n"

    body_text += """
Download with:
  scp ubuntu@13.207.141.110:/home/ubuntu/trading-bot/logs/trade_logs_""" + today.isoformat() + """.tar.gz .

"""
    # Also inline all text file contents for direct paste into ChatGPT
    import tarfile, io
    body_text += "\n" + "="*60 + "\nFULL FILE CONTENTS (paste into ChatGPT):\n" + "="*60 + "\n"
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                f_obj = tar.extractfile(member)
                if f_obj:
                    try:
                        content = f_obj.read().decode("utf-8")
                        body_text += f"\n--- {member.name} ---\n"
                        body_text += content[:50000]  # 50KB max per file
                        body_text += "\n"
                    except Exception:
                        body_text += f"\n--- {member.name} (binary, skipped) ---\n"

    ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": f"Trade Logs {today.strftime('%a %d %b')} | {len(file_list)} files"},
            "Body": {"Text": {"Data": body_text}}
        }
    )
    log.info(f"Email sent to {recipient}")


def main():
    today = date.today()
    log.info(f"=== Daily Trade Logs Email - {today.isoformat()} ===")
    
    # Build tarball
    log.info("Building tar.gz archive...")
    tar_bytes, file_list = build_tarball(today)
    log.info(f"  Archive: {len(tar_bytes)} bytes, {len(file_list)} files")
    
    for f in file_list:
        log.info(f"    {f}")
    
    if not file_list:
        log.info("  No trade files found for today. Sending notification anyway.")
    
    # Send
    send_email_with_attachment(today, tar_bytes, file_list)
    
    # Save copy locally
    output = LOGS / f"trade_logs_{today.isoformat()}.tar.gz"
    with open(output, "wb") as f:
        f.write(tar_bytes)
    log.info(f"Saved to {output}")
    
    log.info("=== DONE ===")


if __name__ == "__main__":
    main()
