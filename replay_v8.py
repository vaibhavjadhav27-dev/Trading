#!/usr/bin/env python3
"""READ-ONLY v8 replay. Re-scores archived candles through v8's REAL dual_scorer,
then applies v8's REAL pick_side/margin_tier. Writes nothing, places no orders."""
import sys, os, csv, glob
from collections import defaultdict
import pandas as pd
from trade_policy import pick_side, margin_tier, confidence_pct, MIN_CONVICTION, EDGE_MIN
from dual_scorer import score_candidate_dual
import indicators as ind

date = sys.argv[1] if len(sys.argv) > 1 else pd.Timestamp.now().strftime("%Y-%m-%d")
arch = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candle_archive")
csv_path = os.path.join(arch, f"candidate_scores_{date}.csv")
candles_dir = os.path.join(arch, f"candles_5min_{date}")
print(f"=== v8 REPLAY (read-only, re-scored via v8 dual_scorer) — {date} ===")
if not os.path.exists(csv_path):
    print("no candidate_scores CSV for", date); sys.exit(1)

rows = list(csv.DictReader(open(csv_path)))
def f(x):
    try: return float(x)
    except: return 0.0
# take the LAST scan cycle's candidate set (most complete pool) as inputs to re-score
by_scan = defaultdict(list)
for r in rows: by_scan[r.get("scan_time","?")].append(r)
last_scan = sorted(by_scan)[-1]
cands = by_scan[last_scan]
regime = cands[0].get("regime","NORMAL")
print(f"rows={len(rows)}  scan_used={last_scan}  pool={len(cands)}  regime={regime}")
print(f"candles archived: {'YES' if os.path.isdir(candles_dir) else 'NO -> falls back to CSV inputs only'}\n")

rescored = []
for r in cands:
    tk, sid = r.get("ticker","?"), r.get("sid","")
    df = None
    cf = os.path.join(candles_dir, f"{tk}.csv")
    if os.path.exists(cf):
        try: df = pd.read_csv(cf)
        except: df = None
    try:
        out = score_candidate_dual(gap_pct=f(r.get("gap_pct")), rs=f(r.get("rs")),
              rvol=f(r.get("rvol")) or None, df=df, ltp=f(r.get("ltp")),
              nifty_gap=0.0, indicators_mod=ind, return_breakdown=False)
        Ls, Ss = out[0], out[1]
    except Exception as e:
        Ls, Ss = 0.0, 0.0
    rescored.append((tk, sid, Ls, Ss, r.get("ltp"), r.get("gap_pct"), r.get("rs")))

mx = max((max(x[2],x[3]) for x in rescored), default=0)
print(f"v8 re-scored max = {mx:.1f} (should be <=100 if dual_scorer is v8-native)\n")
best_L = max(rescored, key=lambda x: x[2])
best_S = max(rescored, key=lambda x: x[3])
side, why = pick_side(regime, best_L[2], best_S[3])
conf = confidence_pct(max(best_L[2], best_S[3]))
lev, tier = margin_tier(conf) if side!="NO_TRADE" else (0.0,"NO_TRADE")
print(f"best LONG : {best_L[0]:10} L={best_L[2]:.1f}")
print(f"best SHORT: {best_S[0]:10} S={best_S[3]:.1f}")
print(f"\n>>> v8 DECISION @ {last_scan} [{regime}]: {side}  {tier} lev={lev}x  conf={conf:.1f}%")
print(f"    reason: {why}")
if side!="NO_TRADE":
    w = best_L if side=="LONG" else best_S
    print(f"    would enter: {w[0]} (sid={w[1]}) ltp={w[4]} gap={w[5]}% rs={w[6]}")
print(f"\nGATES: MIN_CONVICTION={MIN_CONVICTION}  EDGE_MIN={EDGE_MIN}  (v8 0-100 scale)")
print("Top 8 by long_score:")
for x in sorted(rescored, key=lambda x:-x[2])[:8]:
    print(f"  {x[0]:10} L={x[2]:5.1f} S={x[3]:5.1f} ltp={x[4]}")
