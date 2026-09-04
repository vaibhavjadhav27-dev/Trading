#!/usr/bin/env python3
"""Disk-space monitor for the NSE trading server.
Emails via SES (same SSM param-store creds the bot uses) when root disk >= 90%.
Anti-spam: one alert per crossing (state file); re-arms only after dropping below 85%.
ALERT-ONLY: reports usage + top offenders. It NEVER deletes anything."""
import shutil, os, subprocess, datetime, sys
import boto3
from secrets_manager import get_ses_sender, get_ses_recipient

REGION          = 'ap-south-1'
ALERT_PCT       = 90
REARM_PCT       = 85
STATE_FILE      = '/home/ubuntu/trading-bot/.disk_alert_state'
SENDER_PARAM    = '/trading-engine/ses/sender-email'
RECIPIENT_PARAM = '/trading-engine/ses/recipient-email'

def get_parameter(name):
    ssm = boto3.client('ssm', region_name=REGION)
    return ssm.get_parameter(Name=name)['Parameter']['Value']

def disk_pct():
    u = shutil.disk_usage('/')
    return u.used / u.total * 100.0, u

def top_offenders():
    try:
        r = subprocess.run("du -xh / --max-depth=1 2>/dev/null | sort -rh | head -8",
                           shell=True, capture_output=True, text=True, timeout=90)
        return r.stdout.strip() or "(du unavailable)"
    except Exception as e:
        return "(du failed: %s)" % e

def send_alert(pct, u):
    ses = boto3.client('ses', region_name=REGION)
    sender    = get_ses_sender()
    recipient = get_ses_recipient()
    body = ("Disk usage on the trading-bot server has crossed %d%%.\n\n"
            "  Used: %.1f%%    Free: %.2f GB    Total: %.2f GB\n\n"
            "Top space consumers:\n%s\n\n"
            "ALERT-ONLY - nothing was deleted. If low, check retained snap\n"
            "revisions (snap list --all | grep disabled) and bot.log size.\n\n"
            "Time: %s IST-offset applies (server clock: %s)\n"
            % (ALERT_PCT, pct, u.free/1e9, u.total/1e9, top_offenders(),
               datetime.datetime.now(), datetime.datetime.now(datetime.UTC)))
    ses.send_email(Source=sender,
                   Destination={'ToAddresses': [recipient]},
                   Message={'Subject': {'Data': 'Disk %.0f%% on trading-bot server' % pct},
                            'Body': {'Text': {'Data': body}}})

def main():
    pct, u = disk_pct()
    alerted = os.path.exists(STATE_FILE)
    if pct >= ALERT_PCT and not alerted:
        send_alert(pct, u)
        with open(STATE_FILE, 'w') as f:
            f.write('%s pct=%.1f' % (datetime.datetime.now(), pct))
        print("ALERT sent (%.1f%%)" % pct)
    elif pct < REARM_PCT and alerted:
        os.remove(STATE_FILE)
        print("Re-armed (dropped to %.1f%%)" % pct)
    else:
        print("OK (%.1f%%, alerted=%s)" % (pct, alerted))

if __name__ == '__main__':
    if '--test' in sys.argv:
        p, u = disk_pct(); send_alert(p, u)
        print("TEST alert sent at %.1f%% (bypassed threshold)" % p)
    else:
        main()
