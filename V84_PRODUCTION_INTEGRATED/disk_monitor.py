#!/usr/bin/env python3
"""Disk usage monitor — alerts via SES if volume exceeds 90%."""
import subprocess
import boto3
import sys
sys.path.insert(0, "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED")
from secrets_manager import get_ses_sender, get_ses_recipient

THRESHOLD = 90
REARM_BELOW = 85
FLAG_FILE = "/tmp/.disk_alert_fired"

def get_disk_usage_pct():
    result = subprocess.run(["df", "/"], capture_output=True, text=True)
    line = result.stdout.strip().split("\n")[-1]
    return int(line.split()[4].replace("%", ""))

def alert_fired():
    import os
    return os.path.exists(FLAG_FILE)

def set_alert_fired():
    open(FLAG_FILE, "w").write("1")

def clear_alert():
    import os
    if os.path.exists(FLAG_FILE):
        os.remove(FLAG_FILE)

def send_alert(usage_pct):
    ses = boto3.client("ses", region_name="ap-south-1")
    ses.send_email(
        Source=get_ses_sender(),
        Destination={"ToAddresses": [get_ses_recipient()]},
        Message={
            "Subject": {"Data": f"⚠️ DISK ALERT: {usage_pct}% used on trading server"},
            "Body": {"Text": {"Data": (
                f"Disk usage has reached {usage_pct}% on trading server.\n"
                f"Threshold: {THRESHOLD}%\n\n"
                f"Server: ubuntu@13.207.141.110\n"
                f"Action required: Free up space or increase volume.\n\n"
                f"Auto-cleanup runs daily at 02:00 UTC (logs > 15 days).\n"
                f"This alert will re-arm when usage drops below {REARM_BELOW}%."
            )}}
        }
    )
    print(f"ALERT SENT — disk at {usage_pct}%")

if __name__ == "__main__":
    usage = get_disk_usage_pct()
    print(f"Disk usage: {usage}%")
    
    if usage >= THRESHOLD and not alert_fired():
        send_alert(usage)
        set_alert_fired()
    elif usage < REARM_BELOW and alert_fired():
        clear_alert()
        print(f"Re-armed (usage dropped to {usage}%)")
    else:
        print("No action needed")
