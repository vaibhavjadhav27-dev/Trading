#!/usr/bin/env python3
"""
token_refresher.py - Auto-refreshes Dhan token using DhanHQ SDK + TOTP
Runs daily at 8:30 AM IST via cron (before bot starts at 8:50)
"""
import pyotp
import boto3
import logging
from dhanhq import DhanLogin

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger('TokenRefresher')

ssm = boto3.client('ssm', region_name='ap-south-1')

def get_param(name):
    return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

def update_token_in_ssm(new_token):
    ssm.put_parameter(
        Name='/trading-engine/dhan/access-token',
        Value=new_token,
        Type='SecureString',
        Overwrite=True
    )

def send_email(subject, body_html):
    try:
        ses = boto3.client('ses', region_name='ap-south-1')
        sender = get_param('/trading-engine/ses/sender-email')
        recipient = get_param('/trading-engine/ses/recipient-email')
        ses.send_email(
            Source=sender,
            Destination={'ToAddresses': [recipient]},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Html': {'Data': body_html}}
            }
        )
    except Exception as e:
        log.error(f"Email failed: {e}")

def refresh_token():
    # Load credentials from SSM Parameter Store
    client_id = get_param('/trading-engine/dhan/client-id')
    password = get_param('/trading-engine/dhan/password')
    totp_secret = get_param('/trading-engine/dhan/totp-secret')

    log.info(f"Client ID: {client_id}")
    log.info(f"TOTP Secret: {totp_secret[:4]}****")

    try:
        # Generate current TOTP code
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()
        log.info(f"Generated TOTP: {current_otp}")

        # Use DhanHQ SDK to generate token
        dhan_login = DhanLogin(client_id)
        auth_data = dhan_login.generate_token(password, current_otp)

        log.info(f"Auth response type: {type(auth_data)}")
        log.info(f"Auth response type: {type(auth_data).__name__}, keys: {list(auth_data.keys()) if isinstance(auth_data, dict) else 'n/a'}")

        # Extract token from response
        access_token = None
        if isinstance(auth_data, dict):
            access_token = (
                auth_data.get('accessToken') or
                auth_data.get('access_token') or
                auth_data.get('token') or
                (auth_data.get('data', {}) or {}).get('accessToken') or
                (auth_data.get('data', {}) or {}).get('access_token')
            )
        elif isinstance(auth_data, str) and len(auth_data) > 50:
            access_token = auth_data.strip()

        if access_token and len(access_token) > 50:
            # Update SSM Parameter Store
            update_token_in_ssm(access_token)
            log.info(f"✅ Token refreshed! Length: {len(access_token)} chars")

            send_email(
                '🔑 Dhan Token Auto-Refreshed ✅',
                f'<p>Token refreshed successfully.</p>'
                f'<p>Length: {len(access_token)} chars</p>'
                f'<p>First 20: {access_token[:20]}...</p>'
            )
            return True
        else:
            log.error(f"❌ No valid token in response: {auth_data}")
            send_email(
                '🚨 Token Refresh FAILED — Manual Update Required',
                f'<p>Could not extract token from response.</p>'
                f'<p>Response: {str(auth_data)[:300]}</p>'
                f'<p>Please update manually before 8:50 AM.</p>'
            )
            return False

    except Exception as e:
        log.error(f"❌ Token refresh failed: {e}")
        send_email(
            '🚨 Token Refresh FAILED — Manual Update Required',
            f'<p>Error: {str(e)[:300]}</p>'
            f'<p>Please update token manually before 8:50 AM.</p>'
        )
        return False

if __name__ == "__main__":
    import time as _t, random as _rand
    log.info("=== Dhan Token Auto-Refresh (DhanHQ SDK) ===")

    # STAMPEDE AVOIDANCE: At 03:00 UTC (08:30 IST), thousands of Indian
    # trading bots hit Dhan's auth API simultaneously. Dhan's server takes
    # 10+ seconds to respond under load → TOTP expires during the round-trip
    # → "Invalid TOTP". By 03:02+ the load clears and response is 0.2s.
    # Fix: random jitter 0-45s so we don't fire at the exact same moment.
    _jitter = _rand.randint(0, 45)
    log.info(f"Stampede avoidance: sleeping {_jitter}s jitter before first attempt")
    _t.sleep(_jitter)

    MAX_ATTEMPTS = 5
    success = False
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        # Avoid TOTP 30s window boundary: if <10s remain, wait for fresh window
        _secs_into = int(_t.time()) % 30
        _remaining = 30 - _secs_into
        if _remaining < 10:
            log.info(f"Attempt {_attempt}: {_remaining}s left in TOTP window, waiting {_remaining+3}s")
            _t.sleep(_remaining + 3)
        log.info(f"Token refresh attempt {_attempt}/{MAX_ATTEMPTS}")
        try:
            success = refresh_token()
        except Exception as _e:
            log.error(f"Attempt {_attempt} raised: {_e}")
            success = False
        if success:
            log.info(f"Token refresh SUCCESS on attempt {_attempt}")
            break
        if _attempt < MAX_ATTEMPTS:
            # Progressive backoff: 35s, 45s, 55s, 65s — spread retries further
            _wait = 35 + (_attempt - 1) * 10
            log.info(f"Attempt {_attempt} failed; waiting {_wait}s before retry")
            _t.sleep(_wait)
    if not success:
        log.error("ALL token refresh attempts FAILED - MANUAL UPDATE REQUIRED before 08:50 IST")
