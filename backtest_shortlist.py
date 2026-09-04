#!/usr/bin/env python3
"""LOCAL shortlist backtest — zero API. Reads stock_history_30d.json (daily OHLC),
replays each day's gap filter + 5-factor rank (RS live), and flags whether each
top pick was a gap-up that CONTINUED (open->close) with its best-case move (open->high).

HONEST LIMIT: daily bars only -> NO intraday ORB entry/exit time or price.
This validates SELECTION quality (would it shortlist sensible gap-ups that ran),
NOT fills or live P&L."""
import json, config
from rs_scorer import calculate_rs_scores

GAP_MIN=getattr(config,"GAP_MIN",0.3); GAP_REJECT=getattr(config,"GAP_REJECT",15.0)
PRICE_FLOOR=getattr(config,"PRICE_FLOOR",60); PRICE_CEIL=getattr(config,"PRICE_CEIL_TIER1",5000)
TOP_N=10

h=json.load(open("stock_history_30d.json"))
stocks=h.get("stocks",h)
rs=calculate_rs_scores()   # {ticker: rs%}  (live 5-factor RS)

# collect all dates present
all_dates=set()
for tkr,bars in stocks.items():
    if isinstance(bars,list):
        for b in bars: all_dates.add(b.get("date"))
dates=sorted(d for d in all_dates if d)

def bars_by_date(bars): return {b["date"]:b for b in bars if isinstance(b,dict) and b.get("date")}

day_rows={}
for tkr,bars in stocks.items():
    if not isinstance(bars,list): continue
    day_rows[tkr]=bars_by_date(bars)

results=[]
for di in range(1,len(dates)):
    D=dates[di]; Dprev=dates[di-1]; shortlist=[]
    for tkr,bd in day_rows.items():
        if D not in bd or Dprev not in bd: continue
        prev_close=bd[Dprev].get("close"); o=bd[D].get("open")
        hi=bd[D].get("high"); cl=bd[D].get("close")
        if not (prev_close and o and hi and cl): continue
        if o<PRICE_FLOOR or o>PRICE_CEIL: continue
        gap=(o-prev_close)/prev_close*100
        if gap<GAP_MIN or gap>GAP_REJECT: continue
        # 5-factor-ish rank from daily: gap*30 + RS bonus (RVOL/ORB not in daily)
        rs_val=rs.get(tkr,0)
        rs_bonus=15 if rs_val>=2 else 10 if rs_val>=1 else 5 if rs_val>0 else 0
        score=min(gap/3.0,1.0)*30 + rs_bonus
        # outcomes (proxy for "would the trade have worked")
        day_move=(cl-o)/o*100          # open->close (continuation)
        mfe=(hi-o)/o*100               # open->high (best case intraday)
        shortlist.append({"tkr":tkr,"gap":round(gap,2),"rs":round(rs_val,2),
                          "score":round(score,1),"day_move":round(day_move,2),
                          "mfe":round(mfe,2)})
    if not shortlist: 
        results.append({"date":D,"n":0,"pick":None}); continue
    shortlist.sort(key=lambda x:-x["score"])
    top=shortlist[:TOP_N]; pick=top[0]
    results.append({"date":D,"n":len(shortlist),"pick":pick,"top3":top[:3]})

print("=== LOCAL SHORTLIST BACKTEST (5-factor rank, daily data) ===\n")
picks=[r for r in results if r["pick"]]
for r in results:
    if r["pick"]:
        p=r["pick"]
        flag="UP" if p["day_move"]>0 else "DOWN"
        print(f"{r['date']}: shortlist={r['n']:2d} | #1 {p['tkr']:10} gap{p['gap']:+.1f}% RS{p['rs']:+.1f} "
              f"| day {p['day_move']:+.1f}% MFE {p['mfe']:+.1f}% [{flag}]")
    else:
        print(f"{r['date']}: shortlist= 0 | NO CANDIDATES")

if picks:
    ups=[r for r in picks if r["pick"]["day_move"]>0]
    avg_move=sum(r["pick"]["day_move"] for r in picks)/len(picks)
    avg_mfe=sum(r["pick"]["mfe"] for r in picks)/len(picks)
    print(f"\n=== SUMMARY ({len(picks)} days with a #1 pick) ===")
    print(f"#1 pick closed UP: {len(ups)}/{len(picks)} ({len(ups)/len(picks)*100:.0f}%)")
    print(f"Avg #1 day move (open->close): {avg_move:+.2f}%")
    print(f"Avg #1 best-case (open->high): {avg_mfe:+.2f}%")
    print("NOTE: daily proxy — real ORB entry is intraday, so these are directional")
    print("      sanity checks on SELECTION, not fills. Positive avg = filters pick movers.")
    json.dump(results, open("logs/backtest_shortlist.json","w"), indent=2, default=str)
    print("Saved -> logs/backtest_shortlist.json")
