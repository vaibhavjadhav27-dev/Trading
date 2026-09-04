import json, os, logging
log = logging.getLogger(__name__)

def append_closes_from_cache():
    """Append today prev_close to stock_history_30d.json for RS calculation"""
    base = os.path.dirname(os.path.abspath(__file__))
    hist_path = os.path.join(base, "stock_history_30d.json")
    cache_path = os.path.join(base, "prev_close_cache.json")
    wl_path = os.path.join(base, "watchlist.csv")

    if not os.path.exists(cache_path):
        log.warning("No prev_close_cache.json - skipping RS append")
        return

    import pandas as pd
    wl = pd.read_csv(wl_path)
    sid_to_ticker = {}
    for _, row in wl.iterrows():
        sid_to_ticker[str(int(row["security_id"]))] = row["ticker"]

    with open(cache_path, "r") as f:
        cache = json.load(f)

    # Load or create history
    if os.path.exists(hist_path):
        with open(hist_path, "r") as f:
            history = json.load(f)
    else:
        history = {}

    cache_data = cache.get("data", {})
    appended = 0
    for sid, val in cache_data.items():
        ticker = sid_to_ticker.get(sid)
        if not ticker:
            continue
        close_price = val if isinstance(val, (int, float)) else val.get("close", 0) if isinstance(val, dict) else 0
        if close_price <= 0:
            continue

        if ticker not in history:
            history[ticker] = {"closes": []}

        closes = history[ticker].get("closes", [])
        # Only append if different from last (avoid duplicates)
        if not closes or abs(closes[-1] - close_price) > 0.01:
            closes.append(close_price)
            # Keep only last 30 days
            history[ticker]["closes"] = closes[-30:]
            appended += 1

    with open(hist_path, "w") as f:
        json.dump(history, f)

    valid = sum(1 for v in history.values() if len(v.get("closes", [])) >= 6)
    log.info(f"RS history: appended {appended} closes, {valid} stocks with 6+ days")
    print(f"RS history: appended {appended} closes, {valid}/{len(history)} stocks with 6+ days")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    append_closes_from_cache()
