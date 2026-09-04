#!/usr/bin/env python3
# pdh_cache.py  --  Previous-Day-High resolver + one-shot cache for Path B.
#
# DESIGN: PDH is a pre-open constant. Resolve it ONCE at candidate-selection time
# into a flat {security_id: pdh_float} dict. The live tick loop then does an O(1)
# dict lookup — never parses bar arrays per tick. This is why NO writer change to
# pull_yf_history.py is needed: caching at selection is cheaper and simpler than a
# dedicated JSON field, and works TODAY (probe confirmed 553/553 derivable).
#
# SAFETY MODEL: pure data helper. No orders, no network, no file writes.
# resolve_pdh() is the same logic verified by pdh_probe_and_resolve.py (100% cover).
#
# INTEGRATION (Path B, shadow-first): after select_candidates(), call
#     self.pdh_map = build_pdh_map(self.candidates)   # {sid: pdh}
# then in the tick loop, a PDH cross is:  price > self.pdh_map.get(sid, inf)
# Ship this behind FILTERS_V2 in SHADOW (log would-be entries, place NO orders)
# until a clean shadow log is observed. Do NOT arm live on first run (executor n=1).

import json
from datetime import datetime

DEFAULT_HISTORY = "/home/ubuntu/trading-bot/stock_history_30d.json"


def _load(path):
    with open(path) as f:
        return json.load(f)


def resolve_pdh(ticker, stocks, today=None):
    """(pdh_float | None, source_str). Uses last COMPLETED session high.
    Skips today's (partial/just-closed) bar so PDH = yesterday's high for live use."""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    rec = stocks.get(ticker)
    if rec is None:
        return None, "no-record"
    if isinstance(rec, dict):
        if rec.get("prev_day_high"):
            return float(rec["prev_day_high"]), "field"
        bars = rec.get("bars") or rec.get("history") or []
    else:
        bars = rec
    if not bars:
        return None, "no-bars"
    completed = [b for b in bars if b.get("date", "") < today]
    src_bar = completed[-1] if completed else bars[-1]
    tag = "derived:last-completed" if completed else "derived:last-bar(today?)"
    try:
        return float(src_bar["high"]), tag
    except Exception:
        return None, "bad-bar"


def build_pdh_map(candidates, history_path=DEFAULT_HISTORY, today=None):
    """Resolve PDH ONCE for the selected candidates.

    candidates: list of dicts each with 'security_id' and a resolvable ticker key
                ('symbol' / 'ticker' / 'trading_symbol').
    Returns {str(security_id): pdh_float}. Tickers with no usable PDH are OMITTED
    (so a missing PDH can never fire a false cross — lookup returns None -> no entry).
    """
    stocks = _load(history_path).get("stocks", {})
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    out, missing = {}, []
    for c in candidates:
        sid = str(c.get("security_id", ""))
        tkr = c.get("symbol") or c.get("ticker") or c.get("trading_symbol")
        if not sid or not tkr:
            missing.append((sid, tkr, "no-sid-or-ticker")); continue
        pdh, src = resolve_pdh(tkr, stocks, today)
        if pdh is None or pdh <= 0:
            missing.append((sid, tkr, src)); continue
        out[sid] = pdh
    return out, missing


if __name__ == "__main__":
    # self-test against the live file, mimicking a small candidate set
    stocks = _load(DEFAULT_HISTORY).get("stocks", {})
    sample = list(stocks)[:5]
    print("PDH self-test (first 5 tickers):")
    for t in sample:
        print("  %-12s %s" % (t, resolve_pdh(t, stocks)))
