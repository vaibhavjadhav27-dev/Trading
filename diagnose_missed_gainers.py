#!/usr/bin/env python3
"""Read-only: full-552 day-change scan + 5-min candles for top gainers.
No TradingBot instantiation (no email/order). Dumps JSON for offline analysis."""
import json, time, datetime
import pandas as pd
from trading_bot import DhanClient
import config

TOP_N = 15                 # how many top gainers to deep-dive (candles)
PRICE_FLOOR = getattr(config, "PRICE_FLOOR", 60)
PRICE_CEIL  = getattr(config, "PRICE_CEIL_TIER1", 5000)
GAP_MIN     = getattr(config, "GAP_MIN", 0.3)

wl = pd.read_csv("watchlist.csv").to_dict("records")
wl_ids = {str(s["security_id"]): s["ticker"] for s in wl}
row_index = {str(s["security_id"]): i for i, s in enumerate(wl)}  # position in file (head-100 test)

try:
    prev_close = json.load(open("prev_close_cache.json"))
    prev_close = {str(k): float(v) for k, v in prev_close.items()}
except Exception as e:
    print("!! prev_close_cache.json failed:", e); prev_close = {}

dc = DhanClient()

# --- batched LTP for all 552 (rate-limit safe) ---
all_ids = [str(s["security_id"]) for s in wl]
ltp_map = {}
B = 50
for i in range(0, len(all_ids), B):
    chunk = all_ids[i:i+B]
    try:
        r = dc.get_ltp_batch(chunk)
        if isinstance(r, dict):
            for k, v in r.items():
                try: ltp_map[str(k)] = float(v)
                except: pass
    except Exception as e:
        print(f"  LTP batch {i} err:", e)
    time.sleep(0.4)

rows = []
for sid in all_ids:
    ltp = ltp_map.get(sid, 0) or 0
    pc  = prev_close.get(sid, 0) or 0
    if ltp <= 0 or pc <= 0:
        continue
    chg = (ltp - pc) / pc * 100
    rows.append({
        "symbol": wl_ids[sid], "security_id": sid, "ltp": round(ltp,2),
        "prev_close": round(pc,2), "day_change_pct": round(chg,2),
        "wl_row": row_index[sid], "in_head100": row_index[sid] < 100,
        "in_price_band": PRICE_FLOOR <= ltp <= PRICE_CEIL,
    })

rows.sort(key=lambda x: -x["day_change_pct"])
top = rows[:TOP_N]

# --- 5-min candles + open for the top gainers only ---
for g in top:
    sid = g["security_id"]
    try:
        c = dc.get_ohlc_intraday(sid, "NSE_EQ", "5")
        if isinstance(c, dict) and c.get("open"):
            o = c["open"]; h = c["high"]; l = c["low"]; cl = c["close"]; v = c.get("volume",[])
            g["day_open"] = round(float(o[0]),2)
            g["open_gap_pct"] = round((float(o[0]) - g["prev_close"])/g["prev_close"]*100, 2)
            n = min(6, len(cl))
            g["last_candles_5m"] = [
                {"o":round(float(o[-n+i]),2),"h":round(float(h[-n+i]),2),
                 "l":round(float(l[-n+i]),2),"c":round(float(cl[-n+i]),2),
                 "v":int(v[-n+i]) if i < len(v) else 0}
                for i in range(n)
            ]
        else:
            g["day_open"] = None; g["open_gap_pct"] = None; g["last_candles_5m"] = []
    except Exception as e:
        g["candle_err"] = str(e)
    time.sleep(0.4)

out = {"date": str(datetime.date.today()),
       "universe": len(wl), "scanned_with_data": len(rows),
       "top_gainers": top}
json.dump(out, open("missed_gainers_diag.json","w"), indent=2, default=str)
print(json.dumps(out, indent=2, default=str))
