import requests
import json
from datetime import date

FNO_BAN_URL = "https://www.nseindia.com/api/quote-equity?section=trade_info"

def get_fno_ban_list():
    """Return list of stocks currently in F&O ban period"""
    # NSE does not provide a simple API for this
    # We maintain a manual list updated weekly
    # Empty list means no stocks are banned
    try:
        with open("fno_ban_cache.json", "r") as f:
            data = json.load(f)
            if data.get("date") == str(date.today()):
                return data.get("banned", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []

def is_stock_safe(ticker):
    """Check if stock is safe to trade (not in F&O ban)"""
    banned = get_fno_ban_list()
    if ticker in banned:
        return False, f"{ticker} in F&O ban"
    return True, "OK"
