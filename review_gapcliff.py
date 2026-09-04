import json, gzip, os
os.chdir(os.path.expanduser("~/trading-bot"))
def L(p):
    try: return json.load(gzip.open(p)) if p.endswith(".gz") else json.load(open(p))
    except Exception: return None
H=L("stock_history_30d.json") or {}; HIST=H.get("stocks",{}) if isinstance(H,dict) else {}
def day_gaps(date):
    out={}
    for t,rows in HIST.items():
        if not isinstance(rows,list): continue
        for i in range(1,len(rows)):
            if rows[i].get("date")==date:
                pc=rows[i-1].get("close"); op=rows[i].get("open")
                if pc and op: out[t]=(op-pc)/pc*100
                break
    return out
TRADING=[f"2026-07-{d:02d}" for d in (6,7,8,9,10,13,14,15)]  # 16 has no daily row
print("="*78)
print("GAP CLIFF BACKTEST — universe stocks admitted at GAP_MIN 0.30 vs 0.25")
print("="*78)
print(f"{'Date':<12}{'univ':<7}{'@0.30':<8}{'@0.25':<8}{'+extra':<8}{'% wider':<8}")
print("-"*78)
tot30=tot25=0
for d in TRADING:
    gp=day_gaps(d)
    if not gp: print(f"{d:<12}(no data)"); continue
    p30=sum(1 for v in gp.values() if v>=0.30)
    p25=sum(1 for v in gp.values() if v>=0.25)
    tot30+=p30; tot25+=p25
    ex=p25-p30; pw=100.0*ex/p30 if p30 else 0
    print(f"{d:<12}{len(gp):<7}{p30:<8}{p25:<8}{ex:<8}{pw:<8.1f}")
print("-"*78)
print(f"{'TOTAL':<12}{'':<7}{tot30:<8}{tot25:<8}{tot25-tot30:<8}{100.0*(tot25-tot30)/tot30:<8.1f}")
print(f"\nNOISE COST: dropping 0.30->0.25 admits {tot25-tot30} extra universe-stock-days ({100.0*(tot25-tot30)/tot30:.1f}% wider net).")

# CATCH BENEFIT on real-gainer days
print("\n"+"="*78); print("CATCH BENEFIT — REAL gainers newly caught at 0.25 (07-15/16)"); print("="*78)
for d in ("2026-07-15","2026-07-16"):
    a=L(f"candle_archive/{d}.json.gz")
    gns=(a.get("gainers") or []) if isinstance(a,dict) else []
    newly=[]
    for g in gns:
        pc=g.get("prev_close"); o=(g.get("candles") or {}).get("o") or []
        if pc and o:
            og=(o[0]-pc)/pc*100
            if 0.25<=og<0.30: newly.append(f"{g.get('symbol')}({og:+.2f}%)")
    print(f"{d}: {len(newly)} real gainer(s) newly caught -> {', '.join(newly) or 'none'}")
print("\nTRADE-OFF: weigh extra real gainers caught vs the % wider net (noise). Single-variable, reversible.")
