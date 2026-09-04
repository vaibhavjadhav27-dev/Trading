#!/usr/bin/env python3
"""Cache prior-session close for each NSE sector index -> sector_prev_close.json
Uses /v2/charts/historical (serves PRE-MARKET) so the 8:43 AM cron is reliable."""
import json, time, logging, datetime as dt
import requests
from secrets_manager import get_dhan_token
from sector_rotation import SECTOR_INDICES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sector_prev_close")

def fetch_index_prev_close(token, sid, from_date, to_date):
    url = "https://api.dhan.co/v2/charts/historical"
    headers = {"access-token": token, "Content-Type": "application/json"}
    payload = {
        "securityId": str(sid),
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "fromDate": from_date,
        "toDate": to_date,
        "expiryCode": 0,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    data = resp.json()
    closes = data.get("close") if isinstance(data, dict) else None
    if closes and len(closes) >= 1:
        return float(closes[-1])
    raise RuntimeError("no close data")

def main():
    token = get_dhan_token()
    today = dt.date.today()
    from_date = (today - dt.timedelta(days=10)).isoformat()
    to_date = today.isoformat()
    out = {}
    for name, sid in SECTOR_INDICES.items():
        if str(sid).startswith("REPLACE"):
            continue
        try:
            out[name] = fetch_index_prev_close(token, sid, from_date, to_date)
            time.sleep(0.4)
        except Exception as e:
            log.warning(f"{name} ({sid}): {e}")
    with open("sector_prev_close.json", "w") as f:
        json.dump(out, f)
    log.info(f"Saved {len(out)} sector prev-closes: {list(out.keys())}")

if __name__ == "__main__":
    main()
