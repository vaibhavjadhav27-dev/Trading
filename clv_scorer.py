import json, os, csv, logging
log = logging.getLogger(__name__)

def _ticker_to_sid(watchlist_path="watchlist.csv"):
    m = {}
    try:
        with open(watchlist_path) as f:
            for row in csv.DictReader(f):
                m[row["ticker"]] = str(row["security_id"])
    except Exception as e:
        log.warning(f"CLV: watchlist map failed: {e}")
    return m

def get_clv_scores():
    """CLV = (close-low)/(high-low) on the latest daily bar (prior session close strength).
    Keyed by security_id to match get_clv_bonus lookups."""
    path = os.path.join(os.path.dirname(__file__), "stock_history_30d.json")
    if not os.path.exists(path):
        log.warning("CLV: no stock_history_30d.json"); return {}
    h = json.load(open(path))
    stocks = h.get("stocks", h)
    t2s = _ticker_to_sid()
    scores = {}
    for ticker, bars in stocks.items():
        if not isinstance(bars, list) or not bars:
            continue
        last = bars[-1]
        if not isinstance(last, dict):
            continue
        hi, lo, cl = last.get("high"), last.get("low"), last.get("close")
        if hi is None or lo is None or cl is None or hi <= lo:
            continue
        clv = (cl - lo) / (hi - lo)      # 0 = closed at low, 1 = closed at high
        sid = t2s.get(ticker)
        if sid:
            scores[sid] = round(clv, 3)
    log.info(f"CLV scores calculated for {len(scores)} stocks")
    return scores

def get_clv_bonus(sid, clv_scores):
    """+10 if closed strong (top 20% of range), -5 if closed weak (bottom 20%)."""
    clv = clv_scores.get(str(sid), 0)
    if clv > 0.80:
        return 10
    elif clv < 0.20:
        return -5
    return 0
