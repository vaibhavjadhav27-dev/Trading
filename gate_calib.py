#!/usr/bin/env python3
"""READ-ONLY gate calibration: re-score recent sessions via v8 dual_scorer,
report best-of-day distribution and trade frequency at candidate gates."""
import os, csv, glob
from collections import defaultdict
import pandas as pd
from dual_scorer import score_candidate_dual
from trade_policy import pick_side, _market_bias, EDGE_MIN
import indicators as ind

arch = "candle_archive"
def f(x):
    try: return float(x)
    except: return 0.0

files = sorted(glob.glob(os.path.join(arch,"candidate_scores_*.csv")))[-8:]
print(f"{'date':12} {'poolN':>5} {'bestL':>6} {'bestS':>6} {'edge':>5}  decisions @gate 50/55/60")
print("-"*72)
dist = []
for path in files:
    date = os.path.basename(path).replace("candidate_scores_","").replace(".csv","")
    rows = list(csv.DictReader(open(path)))
    by_scan = defaultdict(list)
    for r in rows: by_scan[r.get("scan_time","?")].append(r)
    cdir = os.path.join(arch, f"candles_5min_{date}")
    # best-of-day across ALL scans (re-scored)
    day_bestL = day_bestS = 0.0
    fired = {50:0, 55:0, 60:0}
    scans = 0
    for st, cands in by_scan.items():
        scans += 1
        regime = cands[0].get("regime","NORMAL")
        bL = bS = 0.0
        for r in cands:
            tk = r.get("ticker","?")
            cf = os.path.join(cdir, f"{tk}.csv")
            df = pd.read_csv(cf) if os.path.exists(cf) else None
            try:
                o = score_candidate_dual(gap_pct=f(r.get("gap_pct")), rs=f(r.get("rs")),
                    rvol=f(r.get("rvol")) or None, df=df, ltp=f(r.get("ltp")),
                    nifty_gap=0.0, indicators_mod=ind, return_breakdown=False)
                bL = max(bL, o[0]); bS = max(bS, o[1])
            except: pass
        day_bestL = max(day_bestL, bL); day_bestS = max(day_bestS, bS)
        # would each gate fire this scan? (winner-takes-all + edge)
        win = max(bL, bS); edge = abs(bL - bS)
        for g in (50,55,60):
            if win >= g and edge >= EDGE_MIN: fired[g] += 1
    edge = abs(day_bestL - day_bestS)
    dist.append(max(day_bestL, day_bestS))
    print(f"{date:12} {len(rows):5} {day_bestL:6.1f} {day_bestS:6.1f} {edge:5.1f}  "
          f"{fired[50]:>2}/{fired[55]:>2}/{fired[60]:>2}  (of {scans} scans)")

print("-"*72)
if dist:
    dist.sort()
    n=len(dist)
    print(f"best-of-day score: min={min(dist):.1f} median={dist[n//2]:.1f} max={max(dist):.1f}")
    print(f"days best-of-day >= 60: {sum(1 for d in dist if d>=60)}/{n}")
    print(f"days best-of-day >= 55: {sum(1 for d in dist if d>=55)}/{n}")
    print(f"days best-of-day >= 50: {sum(1 for d in dist if d>=50)}/{n}")
