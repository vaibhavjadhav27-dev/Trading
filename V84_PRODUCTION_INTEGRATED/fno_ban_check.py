"""F&O Ban and Series Safety Check Module"""
import requests, logging
import config
from datetime import date

log = logging.getLogger(__name__)
_fno_ban_cache = None
_cache_date = None

def get_fno_ban_list():
    global _fno_ban_cache, _cache_date
    today = str(date.today())
    if _fno_ban_cache is not None and _cache_date == today:
        return _fno_ban_cache
    try:
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        session.get('https://www.nseindia.com', timeout=5)
        resp = session.get('https://www.nseindia.com/api/live-analysis-ban', timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            banned = set()
            for item in data.get('data', []):
                symbol = item.get('symbol', '')
                if symbol:
                    banned.add(symbol.upper())
            _fno_ban_cache = banned
            _cache_date = today
            return banned
    except Exception as e:
        log.warning(f'Could not fetch F&O ban list: {e}')
    _fno_ban_cache = set()
    _cache_date = today
    return _fno_ban_cache

def is_stock_safe(ticker, series=None):
    if config.FNO_BAN_CHECK:
        ban_list = get_fno_ban_list()
        if ticker.upper() in ban_list:
            return False, f'F&O BAN: {ticker}'
    if series and series.upper() in config.SERIES_BLACKLIST:
        return False, f'SERIES: {ticker} is {series}'
    return True, 'SAFE'
