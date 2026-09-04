#!/usr/bin/env python3
"""Faithful historical replay of ORB gap-up logic. Simulated fills; validates LOGIC not profitability."""
import json, time, os, datetime as dt
import pandas as pd
from trading_bot import DhanClient
import config
try:
    from dhan_charges import dhan_charges_mis
except Exception:
    def dhan_charges_mis(q, b, s=None): return 25.0

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DC = DhanClient()
TOP_N=10
GAP_MIN=getattr(config,"GAP_MIN",0.3); GAP_REJECT=getattr(config,"GAP_REJECT",15.0)
PRICE_FLOOR=getattr(config,"PRICE_FLOOR",60); PRICE_CEIL=getattr(config,"PRICE_CEIL_TIER1",5000)
ORB_MIN=getattr(config,"ORB_MIN_RANGE_PCT",0.8); ORB_MAX=getattr(config,"ORB_MAX_RANGE_PCT",3.0)
RISK_RUPEES=51000*getattr(config,"RISK_PER_TRADE_PCT",2.0)/100.0
MIN_R=1.5; BASE,STEP,CAP=0.25,0.05,0.60

def hist(sid,seg,inst,interval,frm,to):
    ep="/charts/historical" if interval=="D" else "/charts/intraday"
    p={"securityId":str(sid),"exchangeSegment":seg,"instrument":inst,"fromDate":frm,"toDate":to,"expiryCode":0}
    if interval!="D": p["interval"]=str(interval)
    try:
        r=DC._request("POST",ep,p); return r if isinstance(r,dict) else {}
    except Exception: return {}

def probe():
    today=dt.date.today()
    d=hist("1333","NSE_EQ","EQUITY","5",(today-dt.timedelta(days=30)).isoformat(),today.isoformat())
    ts=d.get("timestamp") or d.get("start_Time")
    if not ts: return 0,None
    days=sorted({dt.datetime.fromtimestamp(t,IST).date() for t in ts})
    return len(days),(days[0],days[-1]) if days else None

def replay_day(watch,day):
    d1=(day-dt.timedelta(days=6)).isoformat(); d0=day.isoformat(); cands=[]
    for _,row in watch.iterrows():
        sid=str(row["security_id"]); tkr=row["ticker"]
        dd=hist(sid,"NSE_EQ","EQUITY","D",d1,d0)
        closes=dd.get("close"); opens=dd.get("open"); ts=dd.get("timestamp") or dd.get("start_Time")
        if not (closes and opens and ts and len(closes)>=2): continue
        days=[dt.datetime.fromtimestamp(t,IST).date() for t in ts]
        if day not in days: continue
        i=days.index(day)
        if i==0: continue
        prev_close=float(closes[i-1]); day_open=float(opens[i])
        if not (prev_close and day_open): continue
        if day_open<PRICE_FLOOR or day_open>PRICE_CEIL: continue
        gap=(day_open-prev_close)/prev_close*100
        if gap<GAP_MIN or gap>GAP_REJECT: continue
        cands.append({"ticker":tkr,"sid":sid,"gap":gap,"rank_score":gap})
        time.sleep(0.25)
    if not cands: return {"date":d0,"shortlisted":0,"traded":None}
    cands.sort(key=lambda c:-c["rank_score"]); top=cands[:TOP_N]; survivors=[]
    for ci,c in enumerate(top):
        idata=hist(c["sid"],"NSE_EQ","EQUITY","5",d0,d0)
        o,h,l,cl=idata.get("open"),idata.get("high"),idata.get("low"),idata.get("close")
        ts=idata.get("timestamp") or idata.get("start_Time")
        if not (h and l and ts and len(h)>=4): continue
        rows=[(dt.datetime.fromtimestamp(ts[k],IST),float(o[k]),float(h[k]),float(l[k]),float(cl[k])) for k in range(len(ts))]
        orb=[r for r in rows if r[0].time()<dt.time(9,30)]
        if len(orb)<1: continue
        orb_high=max(r[2] for r in orb); orb_low=min(r[3] for r in orb); rng=orb_high-orb_low
        if rng<=0 or (rng/orb_high*100)<ORB_MIN or (rng/orb_high*100)>ORB_MAX: continue
        frac=min(BASE+ci*STEP,CAP); buffered=orb_high+frac*rng; sl=orb_high-0.3*rng; rps=buffered-sl
        if rps<=0: continue
        target=orb_high+1.25*rng; exp_r=(target-buffered)/rps
        if exp_r<MIN_R: continue
        post=[r for r in rows if r[0].time()>=dt.time(9,30)]
        et=ep=None
        for r in post:
            if r[2]>=buffered: et,ep=r[0],buffered; break
        if not ep: continue
        qty=int(RISK_RUPEES/rps)
        if qty<1: continue
        xt=xp=None; rsn=""
        for r in [x for x in post if x[0]>=et]:
            if r[3]<=sl: xt,xp,rsn=r[0],sl,"STOP"; break
            if r[2]>=target: xt,xp,rsn=r[0],target,"TARGET"; break
        if not xp: xt,xp,rsn=post[-1][0],post[-1][4],"EOD"
        gross=qty*(xp-ep); net=gross-dhan_charges_mis(qty,ep,xp)
        survivors.append({"ticker":c["ticker"],"gap":round(c["gap"],2),"rank":ci+1,
            "entry_time":et.strftime("%H:%M"),"entry":round(ep,2),"exit_time":xt.strftime("%H:%M"),
            "exit":round(xp,2),"reason":rsn,"qty":qty,"gross":round(gross,2),"net":round(net,2),"exp_r":round(exp_r,2)})
        time.sleep(0.3)
    if not survivors: return {"date":d0,"shortlisted":len(top),"traded":None}
    survivors.sort(key=lambda s:(-s["exp_r"],s["rank"]))
    return {"date":d0,"shortlisted":len(top),"candidates_with_orb":len(survivors),"traded":survivors[0]}

def main():
    print("=== ORB REPLAY (gap-proxy rank) ===")
    depth,span=probe(); print(f"Dhan 5-min intraday depth: ~{depth} days span={span}")
    if depth<2:
        print("Intraday history NOT deep enough -> cannot do faithful fills."); return
    watch=pd.read_csv("watchlist.csv"); today=dt.date.today(); days=[]
    if span:
        d=span[0]
        while d<=span[1]:
            if d.weekday()<5 and d!=today: days.append(d)
            d+=dt.timedelta(days=1)
    results=[]
    for day in days:
        r=replay_day(watch,day); results.append(r); t=r.get("traded")
        if t: print(f"{r['date']}: shortlist={r['shortlisted']} | TRADE {t['ticker']} gap{t['gap']}% "
                    f"entry {t['entry']}@{t['entry_time']} exit {t['exit']}@{t['exit_time']} [{t['reason']}] net=Rs{t['net']}")
        else: print(f"{r['date']}: shortlist={r['shortlisted']} | NO TRADE")
    trades=[r["traded"] for r in results if r.get("traded")]
    if trades:
        nets=[t["net"] for t in trades]; wins=[n for n in nets if n>0]
        print(f"\n=== SUMMARY ===\nDays: {len(results)} | Trades: {len(trades)} | "
              f"Win%: {len(wins)/len(trades)*100:.0f}% | Net: Rs{sum(nets):.0f} | Avg: Rs{sum(nets)/len(trades):.0f}")
        os.makedirs("logs",exist_ok=True)
        json.dump(results,open("logs/replay_results.json","w"),indent=2,default=str)
        print("Saved -> logs/replay_results.json")
    else: print("\nNo trades triggered across replayed days.")

if __name__=="__main__": main()
