import json, os, logging
from datetime import date

log = logging.getLogger(__name__)

def calculate_rs_scores(watchlist_sids=None):
    """RS vs NIFTY from stock_history_30d.json (shape: {updated, stocks:{tkr:[bars]}})."""
    import json, os
    cache_path = os.path.join(os.path.dirname(__file__), "stock_history_30d.json")
    if not os.path.exists(cache_path):
        log.warning("No stock_history_30d.json - RS disabled"); return {}
    with open(cache_path) as f:
        h = json.load(f)
    stocks = h.get("stocks", h)  # support both nested and flat
    def closes_of(v):
        if isinstance(v, list):   # list of {date,close,...} bars
            return [b.get("close") for b in v if isinstance(b, dict) and b.get("close")]
        if isinstance(v, dict):   # legacy {closes:[...]}
            return v.get("closes", [])
        return []
    # NIFTY series if present, else 0 (raw return fallback)
    nifty = stocks.get("NIFTY") or stocks.get("NIFTY 50") or stocks.get("13") or h.get("NIFTY")
    ncl = closes_of(nifty) if nifty else []
    nifty_5d = (ncl[-1]/ncl[-6]-1)*100 if len(ncl) >= 6 and ncl[-6] else 0.0
    scores = {}
    for ticker, v in stocks.items():
        if ticker in ("NIFTY","NIFTY 50","^NSEI","metadata","13"): continue
        cl = closes_of(v)
        if len(cl) >= 6 and cl[-6] > 0:
            scores[ticker] = round((cl[-1]/cl[-6]-1)*100 - nifty_5d, 2)
    log.info(f"RS scores calculated for {len(scores)} stocks (nifty_5d={nifty_5d:.2f})")
    return scores


def get_rs_bonus(ticker, rs_scores):
    """Get RS bonus for ranking: +20 if RS>2%, +10 if RS>0%"""
    rs = rs_scores.get(ticker, 0)
    if rs > 2.0:
        return 20
    elif rs > 0.0:
        return 10
    elif rs < -2.0:
        return -10  # Penalty for laggards
    return 0
