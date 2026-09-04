"""Shared NIFTY regime signal for swing entry gate + exit overlay.
Reads stock_history_30d.json (key 'NIFTY'). Only ~21 bars available, so SMA20
is the honest trend line — a seeded EMA20/EMA50 is not supportable on 21 bars.
Bearish = NIFTY close < SMA20. Fail-OPEN: UNKNOWN -> callers use normal behavior."""
import json, logging
log = logging.getLogger("swing_regime")
HISTORY_FILE = "stock_history_30d.json"

def nifty_trend(history_file=HISTORY_FILE):
    try:
        d = json.load(open(history_file))
        s = d.get("stocks", d)
        key = next((k for k in ("NIFTY", "^NSEI", "NIFTY50", "NIFTY_50") if k in s), None)
        if not key:
            return {"regime": "UNKNOWN", "close": 0.0, "sma20": 0.0, "slope10": 0.0}
        closes = [float(b["close"]) for b in s[key] if "close" in b]
        if len(closes) < 20:
            return {"regime": "UNKNOWN", "close": (closes[-1] if closes else 0.0), "sma20": 0.0, "slope10": 0.0}
        sma20 = sum(closes[-20:]) / 20.0
        close = closes[-1]
        slope10 = ((close - closes[-11]) / closes[-11] * 100.0) if len(closes) >= 11 and closes[-11] else 0.0
        regime = "BEARISH" if close < sma20 else "BULLISH"
        return {"regime": regime, "close": round(close, 2), "sma20": round(sma20, 2), "slope10": round(slope10, 2)}
    except Exception as e:
        log.warning("nifty_trend failed: %s" % e)
        return {"regime": "UNKNOWN", "close": 0.0, "sma20": 0.0, "slope10": 0.0}
