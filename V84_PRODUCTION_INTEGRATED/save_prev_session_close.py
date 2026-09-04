import sys, json, time, logging
from datetime import date, datetime, timedelta
sys.path.insert(0, '/home/ubuntu/trading-bot')
from secrets_manager import get_dhan_token, get_parameter
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    filename='/home/ubuntu/trading-bot/prev_close_refresh.log', filemode='a')
log = logging.getLogger(__name__)

def get_last_trading_date():
    '''Find last trading session date (skip weekends + known holidays)'''
    today = date.today()
    check_date = today - timedelta(days=1)
    # Go back up to 7 days to find last trading day
    for _ in range(7):
        if check_date.weekday() < 5:  # Mon-Fri
            return check_date
        check_date -= timedelta(days=1)
    return today - timedelta(days=1)  # fallback

def verify_trading_day(token, client_id, test_sid, check_date):
    '''Verify the date was actually a trading day by checking if data exists'''
    url = f'https://api.dhan.co/v2/charts/historical'
    headers = {'access-token': token, 'Content-Type': 'application/json'}
    payload = {
        'securityId': test_sid,
        'exchangeSegment': 'NSE_EQ',
        'instrument': 'EQUITY',
        'fromDate': check_date.strftime('%Y-%m-%d'),
        'toDate': (check_date + timedelta(days=1)).strftime('%Y-%m-%d'),
        'expiryCode': 0
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            closes = data.get('close', [])
            if closes and len(closes) > 0:
                return True
    except:
        pass
    return False

def fetch_prev_close_rest(token, client_id, sid, from_date, to_date):
    '''Fetch previous close via REST historical API'''
    url = f'https://api.dhan.co/v2/charts/historical'
    headers = {'access-token': token, 'Content-Type': 'application/json'}
    payload = {
        'securityId': sid,
        'exchangeSegment': 'NSE_EQ',
        'instrument': 'EQUITY',
        'fromDate': from_date,
        'toDate': to_date,
        'expiryCode': 0
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            closes = data.get('close', [])
            if closes and len(closes) > 0:
                return float(closes[-1])  # Last close of the day
    except:
        pass
    return None

def main():
    token = get_dhan_token()
    client_id = get_parameter('/trading-engine/dhan/client-id')
    wl = pd.read_csv('/home/ubuntu/trading-bot/watchlist.csv')
    total = len(wl)
    log.info(f'Watchlist: {total} stocks')

    # Find last trading date
    last_trade_date = get_last_trading_date()
    log.info(f'Checking if {last_trade_date} was a trading day...')

    # Verify with a known stock (RELIANCE SID=2885)
    test_sid = '2885'
    if not verify_trading_day(token, client_id, test_sid, last_trade_date):
        # Try one more day back (could be a holiday)
        last_trade_date -= timedelta(days=1)
        if last_trade_date.weekday() >= 5:  # Skip weekend
            last_trade_date -= timedelta(days=(last_trade_date.weekday() - 4))
        log.info(f'Previous date was holiday, trying {last_trade_date}')
        if not verify_trading_day(token, client_id, test_sid, last_trade_date):
            last_trade_date -= timedelta(days=1)
            if last_trade_date.weekday() >= 5:
                last_trade_date -= timedelta(days=(last_trade_date.weekday() - 4))
            log.warning(f'Fallback to {last_trade_date}')

    log.info(f'Using trading date: {last_trade_date}')
    date_str = last_trade_date.strftime('%Y-%m-%d')
    to_date_str = (last_trade_date + timedelta(days=1)).strftime('%Y-%m-%d')

    # Fetch prev_close for all watchlist stocks
    prev_closes = {}
    failed = 0
    for idx, row in wl.iterrows():
        ticker = row["ticker"]                    # explicit column
        if pd.isna(row["security_id"]):
            continue
        sid = str(int(row["security_id"]))        # explicit column
        close = fetch_prev_close_rest(token, client_id, sid, date_str, to_date_str)
        if close:
            prev_closes[sid] = close
        else:
            failed += 1
        # Rate limiting: 0.5s per call
        time.sleep(0.5)
        if (idx + 1) % 50 == 0:
            log.info(f'  Progress: {idx+1}/{total} ({len(prev_closes)} saved, {failed} failed)')

    # Save cache
    cache = {'date': date_str, 'data': prev_closes}
    with open('/home/ubuntu/trading-bot/prev_close_cache.json', 'w') as f:
        json.dump(cache, f)
    log.info(f'DONE: {len(prev_closes)}/{total} prev_closes saved for {date_str} ({failed} failed)')

if __name__ == '__main__':
    main()
