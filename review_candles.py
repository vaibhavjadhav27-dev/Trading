import json, gzip, os
from datetime import datetime, timedelta
os.chdir(os.path.expanduser("~/trading-bot"))

def L(p):
    try: return json.load(gzip.open(p)) if p.endswith(".gz") else json.load(open(p))
    except Exception: return None

# PDH from daily history (day BEFORE the target date)
H = L("stock_history_30d.json") or {}
HIST = H.get("stocks", {}) if isinstance(H, dict) else {}
def pdh(sym, date):
    rows = HIST.get(sym)
    if not isinstance(rows, list): return None
    prev = None
    for r in rows:
        if r.get("date") == date: break
        prev = r
    return prev.get("high") if prev else None

# bar index -> IST clock (bar0 = 09:15, 5-min spacing)
def clk(i):
    t = datetime(2026,1,1,9,15) + timedelta(minutes=5*i)
    return t.strftime("%H:%M")

FLAT = {
  "2026-07-15": ["APLLTD","CESC"],
  "2026-07-16": ["CIEINDIA","AETHER","BAJAJELEC"],
}
# Path B spec
RVOL_MIN = 4.5; HOLD_CHECKS = 2  # cross must hold 2 consecutive bars (proxy for 2x30s)

for date, syms in FLAT.items():
    a = L(f"candle_archive/{date}.json.gz")
    g_by = {g.get("symbol"): g for g in (a.get("gainers") or [])} if isinstance(a,dict) else {}
    print("\n" + "="*86)
    print(f"{date}  —  candle-wise timing of FLAT-OPEN gainers (local 5-min archive)")
    print("="*86)
    for sym in syms:
        g = g_by.get(sym)
        if not g: print(f"\n{sym}: not in archive"); continue
        c = g.get("candles") or {}
        o,h,l,cl,v = c.get("o",[]),c.get("h",[]),c.get("l",[]),c.get("c",[]),c.get("v",[])
        pc = g.get("prev_close"); ph = pdh(sym, date)
        n = len(h)
        if not n: print(f"\n{sym}: no candles"); continue
        # find when day-high run happened: bar of session peak
        peak_i = max(range(n), key=lambda i: h[i])
        peak_gain = (h[peak_i]-pc)/pc*100 if pc else 0
        open_gap = (o[0]-pc)/pc*100 if pc and o else 0
        # PDH cross: first bar whose high >= PDH, that holds next HOLD_CHECKS bars above PDH (close)
        cross_i = None
        if ph:
            for i in range(n-HOLD_CHECKS):
                if h[i] >= ph and all(cl[i+k] >= ph for k in range(1,HOLD_CHECKS+1)):
                    cross_i = i; break
        print(f"\n{sym}  prev_close={pc}  PDH={ph}  open={o[0] if o else '?'} ({open_gap:+.2f}% gap)")
        print(f"  session peak {h[peak_i]:.1f} (+{peak_gain:.2f}%) at bar{peak_i} = {clk(peak_i)} IST")
        if cross_i is not None:
            cg = (cl[cross_i]-pc)/pc*100
            orb = "WITHIN ORB (by 09:30)" if cross_i<=2 else "AFTER ORB window"
            print(f"  PDH cross+hold: bar{cross_i} = {clk(cross_i)} IST @ {cl[cross_i]:.1f} ({cg:+.2f}%) — {orb}")
            print(f"  => Path B WOULD flag (PDH held {HOLD_CHECKS} bars). Gap-up ORB entry at 09:15 could NOT.")
        elif ph:
            print(f"  PDH cross: never held {HOLD_CHECKS} consecutive bars above {ph} — Path B would NOT fire either")
        else:
            print(f"  PDH unavailable (no prior daily bar) — cannot test cross")
