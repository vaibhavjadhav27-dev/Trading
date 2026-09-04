import json, os, logging, requests, time
try:
    from catalyst_engine import run_catalyst_analysis, get_cached_catalysts
except ImportError:
    run_catalyst_analysis = None
    get_cached_catalysts = lambda: []
from datetime import datetime, date, timedelta
from secrets_manager import get_parameter
try:
    from swing_logger import log_event
except ImportError:
    def log_event(a, p): return False

log = logging.getLogger("swing_scanner")
logging.basicConfig(level=logging.INFO)

# ═══ CONFIG ═══
MIN_RS_10D = 3.0
MIN_RVOL = 1.0
PULLBACK_MIN = 0.5
PULLBACK_MAX = 12.0
MIN_PRICE = 60
MAX_PRICE = 5000
MIN_DAYS = 15
SHEETS_URL = get_parameter("/trading-engine/google/apps-script-url")

def load_history():
    with open("stock_history_30d.json", "r") as f:
        data = json.load(f)
    return data.get("stocks", {})

def calculate_indicators(ticker, candles):
    if len(candles) < MIN_DAYS:
        return None
    current = candles[-1]["close"]
    if current < MIN_PRICE or current > MAX_PRICE:
        return None
    closes_20 = [c["close"] for c in candles[-20:]]
    sma_20 = sum(closes_20) / len(closes_20)
    above_sma = current > sma_20
    vol_10 = sum(c["volume"] for c in candles[-10:]) / 10
    last_vol = candles[-1]["volume"]
    rvol = last_vol / vol_10 if vol_10 > 0 else 0
    high_5d = max(c["high"] for c in candles[-5:])
    pullback_pct = (high_5d - current) / high_5d * 100
    rs_10d = (current - candles[-10]["close"]) / candles[-10]["close"] * 100
    rs_20d = (current - candles[-20]["close"]) / candles[-20]["close"] * 100
    high_20d = max(c["high"] for c in candles[-20:])
    low_20d = min(c["low"] for c in candles[-20:])
    range_20d = high_20d - low_20d
    clv = ((current - low_20d) - (high_20d - current)) / range_20d if range_20d > 0 else 0
    atr_vals = []
    for j in range(1, min(15, len(candles))):
        tr = max(candles[j]["high"] - candles[j]["low"], abs(candles[j]["high"] - candles[j-1]["close"]), abs(candles[j]["low"] - candles[j-1]["close"]))
        atr_vals.append(tr)
    atr = sum(atr_vals) / len(atr_vals) if atr_vals else 1
    return {
        "ticker": ticker,
        "close": round(current, 2),
        "sma20": round(sma_20, 2),
        "above_sma": above_sma,
        "rvol": round(rvol, 2),
        "pullback": round(pullback_pct, 2),
        "rs_10d": round(rs_10d, 2),
        "rs_20d": round(rs_20d, 2),
        "clv": round(clv, 2),
        "atr": round(atr, 2),
        "high_20d": round(high_20d, 2),
        "low_20d": round(low_20d, 2),
        "volume": last_vol,
        "signal": "BUY" if (above_sma and rvol >= MIN_RVOL and rs_10d >= MIN_RS_10D and PULLBACK_MIN <= pullback_pct <= PULLBACK_MAX) else "WATCH"
    }

def run_swing_scan():
    # AUTHORITATIVE SCAN: delegate to swing_daily.scan_candidates() so both entry
    # paths (swing_daily auto-entry and paper_trader watchlist) use ONE ruleset.
    # We adapt swing_daily's scored candidates into this module's BUY/WATCH shape.
    from swing_daily import scan_candidates, SCORE_THRESHOLD
    cands = scan_candidates()
    results = []
    for c in cands:
        results.append({
            "ticker": c["ticker"],
            "close": c["cmp"],
            "sma20": c["sma20"],
            "above_sma": True,               # scan_candidates already requires cmp >= sma20
            "rvol": c["rvol"],
            "pullback": c["pullback_pct"],
            "rs_10d": c["rs_10d"],
            "rs_20d": c.get("rs_10d", 0),    # 20d not computed upstream; RS-10 is the ranked field
            "clv": 0,
            "atr": 0,
            "high_20d": c["high_20d"],
            "low_20d": c["sl"],
            "volume": 0,
            "score": c["score"],
            "sl": c["sl"],
            "target": c["target"],
            "pullback_pct": c["pullback_pct"],
            "signal": "BUY" if c["score"] >= SCORE_THRESHOLD else "WATCH",
        })
    results.sort(key=lambda x: -x["rs_10d"])
    buy_signals = [r for r in results if r["signal"] == "BUY"]
    watch_signals = [r for r in results if r["signal"] == "WATCH" and r["rs_10d"] > 5][:10]
    log.info(f"Scanned {len(results)} stocks (authoritative), BUY: {len(buy_signals)}, WATCH: {len(watch_signals)}")
    return buy_signals[:20], watch_signals

def push_to_sheets(buy_signals, watch_signals):
    today = date.today().isoformat()
    scan_rows = []
    for r in buy_signals:
        scan_rows.append([today, r["ticker"], r["close"], r["sma20"], r["rvol"], r["pullback"], r["rs_10d"], r["rs_20d"], r["clv"], r["atr"], r["signal"]])
    watch_rows = []
    for r in watch_signals:
        watch_rows.append([today, r["ticker"], r["close"], r["sma20"], r["rvol"], r["pullback"], r["rs_10d"], r["rs_20d"], r["clv"], r["atr"], r["signal"]])
    payload = {"action": "swing_scan", "scan_data": scan_rows, "watchlist_data": watch_rows, "date": today}
    log_event("swing_scan", payload)
    try:
        resp = requests.post(SHEETS_URL, json=payload, timeout=30)
        log.info(f"Sheets push: {resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Sheets push failed: {e}")
        return False

def main():
    log.info("=== SWING SCANNER START ===")
    buy_signals, watch_signals = run_swing_scan()
    if buy_signals or watch_signals:
        push_to_sheets(buy_signals, watch_signals)
    log.info(f"=== SWING SCANNER DONE: {len(buy_signals)} BUY signals ===")
    return buy_signals

if __name__ == "__main__":
    results = main()
    for r in results[:10]:
        print(f"  {r['ticker']:<12} Rs.{r['close']:>8.2f} RS:{r['rs_10d']:>+6.2f}% RVOL:{r['rvol']:>5.2f} PB:{r['pullback']:>5.2f}% [{r['signal']}]")
