"""
Runs at 3:35 PM IST daily (after market close)
Saves today's closing prices for all watchlist stocks
Tomorrow's bot reads from this cache
"""
import sys, time, json, logging, pandas as pd, requests
sys.path.insert(0, '/home/ubuntu/trading-bot')
from secrets_manager import get_parameter, get_dhan_token, get_dhan_client_id
from datetime import date

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

def main():
    token = get_dhan_token()
    client_id = get_dhan_client_id()
    headers = {'Content-Type': 'application/json', 'access-token': token, 'client-id': client_id}

    wl = pd.read_csv('watchlist.csv')
    log.info(f"Saving prev_close for {len(wl)} stocks...")

    prev_closes = {}
    failed = 0

    for idx, row in wl.iterrows():
        ticker = str(row.iloc) if hasattr(row, 'iloc') else str(list(row))
        sid = str(int(row.iloc)) if hasattr(row, 'iloc') else str(int(list(row)))

        try:
            time.sleep(0.5)
            payload = {'securityId': sid, 'exchangeSegment': 'NSE_EQ', 'instrument': 'EQUITY'}
            resp = requests.post('https://api.dhan.co/v2/charts/intraday', headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                closes = data.get('close', [])
                if closes:
                    prev_closes[sid] = float(closes[-1])
            else:
                failed += 1
        except Exception as e:
            failed += 1

        if (idx + 1) % 50 == 0:
            log.info(f"  {idx+1}/{len(wl)} done ({len(prev_closes)} saved)")

    # Save to file
    with open('prev_close_cache.json', 'w') as f:
        json.dump({'date': str(date.today()), 'data': prev_closes}, f)

    log.info(f"DONE: {len(prev_closes)} prev_closes saved, {failed} failed")
    log.info(f"File: prev_close_cache.json ({len(prev_closes)} stocks)")

if __name__ == '__main__':
    main()
