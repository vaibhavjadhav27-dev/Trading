import json, gzip, os
from datetime import datetime, timedelta
os.chdir(os.path.expanduser("~/trading-bot"))
def L(p):
    try: return json.load(gzip.open(p)) if p.endswith(".gz") else json.load(open(p))
    except Exception: return None
H = L("stock_history_30d.json") or {}
HIST = H.get("stocks", {}) if isinstance(H, dict) else {}
def prior_rows(sym, date):
    rows = HIST.get(sym)
    if not isinstance(rows, list): return []
    out=[]
    for r in rows:
        if r.get("date")==date: break
        out.append(r)
    return out
def pdh(sym,date):
    pr=prior_rows(sym,date); return pr[-1].get("high") if pr else None
def adv(sym,date,n=20):
    pr=prior_rows(sym,date)[-n:]
    vols=[r.get("volume") for r in pr if r.get("volume")]
    return sum(vols)/len(vols) if vols else None
def clk(i):
    return (datetime(2026,1,1,9,15)+timedelta(minutes=5*i)).strftime("%H:%M")

RVOL_MIN=4.5; HOLD=2; SESSION_MIN=375
FLAT={"2026-07-15":["APLLTD","CESC"],"2026-07-16":["CIEINDIA","AETHER","BAJAJELEC"]}
for date,syms in FLAT.items():
    a=L(f"candle_archive/{date}.json.gz")
    gby={g.get("symbol"):g for g in (a.get("gainers") or [])} if isinstance(a,dict) else {}
    print("\n"+"="*88); print(f"{date}  —  Path B FULL gate (PDH-cross + hold + RVol>={RVOL_MIN}x)"); print("="*88)
    for sym in syms:
        g=gby.get(sym)
        if not g: print(f"\n{sym}: not in archive"); continue
        c=g.get("candles") or {}
        o,h,cl,v=c.get("o",[]),c.get("h",[]),c.get("c",[]),c.get("v",[])
        pc=g.get("prev_close"); ph=pdh(sym,date); av=adv(sym,date)
        n=len(h)
        if not n: print(f"\n{sym}: no candles"); continue
        ogap=(o[0]-pc)/pc*100 if pc and o else 0
        # PDH cross+hold
        cross=None
        if ph:
            for i in range(n-HOLD):
                if h[i]>=ph and all(cl[i+k]>=ph for k in range(1,HOLD+1)): cross=i; break
        print(f"\n{sym}  open_gap={ogap:+.2f}%  PDH={ph}  ADV={av:,.0f}" if av else f"\n{sym}  open_gap={ogap:+.2f}%  PDH={ph}  ADV=n/a")
        # gate 1: flat-open window
        g1 = (-0.5<=ogap<=0.5)
        print(f"  [1] flat-open gap in [-0.5,+0.5]: {ogap:+.2f}% -> {'PASS' if g1 else 'FAIL'}")
        # gate 2: PDH cross+hold
        if cross is not None:
            print(f"  [2] PDH cross+hold {HOLD}: bar{cross} {clk(cross)} -> PASS")
        else:
            print(f"  [2] PDH cross+hold {HOLD}: never -> FAIL"); continue
        # gate 3: time-adjusted RVol AT cross bar
        if av:
            cum=sum(v[:cross+1]); mins=(cross+1)*5; frac=mins/SESSION_MIN
            exp=av*frac; rv=cum/exp if exp>0 else 0
            g3 = rv>=RVOL_MIN
            print(f"  [3] RVol@cross: cum_vol={cum:,.0f} / (ADV*{frac:.2f}={exp:,.0f}) = {rv:.2f}x -> {'PASS' if g3 else 'FAIL'} (need>={RVOL_MIN})")
        else:
            g3=None; print(f"  [3] RVol@cross: ADV unavailable -> UNKNOWN")
        verdict = "WOULD FIRE" if (g1 and cross is not None and g3) else ("BLOCKED" if g3 is False else "PDH-ok, RVol-unknown")
        print(f"  => Path B FULL gate: {verdict}")
