
#!/usr/bin/env python3
"""V8.1 directional scorer.

Two-stage philosophy:
- Candidate score: used only to keep a stock on the watchlist; threshold is 50.
- Final entry score: includes live setup/entry/opportunity evidence; threshold is 60.
Each direction is independently scored on exactly 100 points.

Weights intentionally emphasize actionable intraday information:
market 7, sector 8, relative strength 15, momentum/acceleration 18,
RVOL 12, VWAP/trend 10, setup 15, entry quality 10, opportunity 5.
"""
from __future__ import annotations

MAX_SCORE = 100.0
CANDIDATE_MIN = 50.0
ENTRY_MIN = 60.0

def clamp(x, lo=0.0, hi=1.0):
    try: return max(lo, min(hi, float(x)))
    except Exception: return lo

def _dir(v, scale):
    v=float(v or 0)
    return scale*clamp(v/scale if scale else 0)

def _momentum_from_df(df):
    """Return directional momentum/acceleration points on 0..18 each."""
    if df is None or len(df) < 6:
        return 0.0,0.0
    try:
        c=[float(x) for x in df["close"].tolist()]
        def ret(n):
            if len(c)<=n or c[-1-n]==0: return 0.0
            return (c[-1]/c[-1-n]-1)*100
        r5,r15,r30=ret(1),ret(min(3,len(c)-1)),ret(min(6,len(c)-1))
        accel=(r5*2 + r15 + r30/2)
        L=18*clamp(max(0, accel)/2.0)
        S=18*clamp(max(0, -accel)/2.0)
        return L,S
    except Exception:
        return 0.0,0.0

def _vwap_points(df, ltp):
    if df is None or not ltp: return 0.0,0.0
    try:
        if "vwap" in df.columns:
            vw=float(df["vwap"].iloc[-1])
        else:
            tp=(df["high"]+df["low"]+df["close"])/3
            vol=df["volume"].replace(0,1)
            vw=float((tp*vol).cumsum().iloc[-1]/vol.cumsum().iloc[-1])
        if vw<=0: return 0.0,0.0
        d=(float(ltp)-vw)/vw*100
        return (10.0 if d>0.15 else 5.0 if d>=-0.15 else 0.0,
                10.0 if d<-0.15 else 5.0 if d<=0.15 else 0.0)
    except Exception:
        return 0.0,0.0

def _trend_points(df):
    if df is None or len(df)<6: return 0.0,0.0
    try:
        c=[float(x) for x in df["close"].tolist()]
        short=sum(c[-3:])/3
        long=sum(c[-6:])/6
        d=(short/long-1)*100 if long else 0
        return 10.0*clamp(d/0.8), 10.0*clamp(-d/0.8)
    except Exception:
        return 0.0,0.0

def score_candidate_dual(*, gap_pct=0.0, rs=0.0, rvol=0.0, df=None, ltp=0.0,
                         nifty_gap=0.0, indicators_mod=None, sector_leading=False,
                         sector_against=False, market_bias_L=0.0, market_bias_S=0.0,
                         sector_rs=0.0, entry_quality_L=0.0, entry_quality_S=0.0,
                         setup_quality_L=0.0, setup_quality_S=0.0,
                         expected_move_pct=0.0, breakout_confirmed=False,
                         breakdown_confirmed=False, sudden_move=False,
                         momentum_5m=0.0, momentum_15m=0.0, momentum_30m=0.0,
                         return_breakdown=False, final_stage=False):
    L=S=0.0; b={}

    # 1) Market context: 7 points. Context only, never a gate.
    if market_bias_L or market_bias_S:
        ml,ms=float(market_bias_L),float(market_bias_S)
        ml=max(0,min(7,ml)); ms=max(0,min(7,ms))
    else:
        g=float(nifty_gap or 0)
        ml=7*clamp(g/1.0); ms=7*clamp(-g/1.0)
        if abs(g)<0.15: ml=ms=3.5
    L+=ml; S+=ms; b.update(market_L=ml,market_S=ms)

    # 2) Sector context: 8 points.
    if sector_leading: sl,ss=8.0,1.5
    elif sector_against: sl,ss=1.5,8.0
    else:
        sr=float(sector_rs or 0)
        sl=4+4*clamp(sr/2); ss=4+4*clamp(-sr/2)
    L+=sl; S+=ss; b.update(sector_L=sl,sector_S=ss)

    # 3) Relative strength/weakness: 15.
    rv=float(rs or 0)
    rl=15*clamp(rv/3); rs_=15*clamp(-rv/3)
    L+=rl; S+=rs_; b.update(rs_L=rl,rs_S=rs_)

    # 4) Momentum + acceleration: 18.
    if momentum_5m or momentum_15m or momentum_30m:
        accel=(float(momentum_5m or 0)*2 + float(momentum_15m or 0) + float(momentum_30m or 0)*0.5)
        ml=18*clamp(max(0,accel)/2); ms=18*clamp(max(0,-accel)/2)
    else:
        ml,ms=_momentum_from_df(df)
    L+=ml; S+=ms; b.update(momentum_L=ml,momentum_S=ms)

    # 5) RVOL participation: 12 shared participation, but directionally
    # allocated only when there is directional evidence. This avoids giving
    # both sides full conviction merely because volume is high.
    rvv=float(rvol or 0)
    participation=12*clamp(rvv/3)
    if ml>ms: rlvol,rsvol=participation,participation*0.35
    elif ms>ml: rlvol,rsvol=participation*0.35,participation
    else: rlvol=rsvol=participation*0.5
    L+=rlvol; S+=rsvol; b.update(rvol_L=rlvol,rvol_S=rsvol)

    # 6) VWAP/trend: 10.
    vwL,vwS=_vwap_points(df,ltp)
    trL,trS=_trend_points(df)
    # combine VWAP 7 + trend 3, preserving total 10
    vl=vwL*0.7+trL*0.3; vs=vwS*0.7+trS*0.3
    L+=vl; S+=vs; b.update(vwap_trend_L=vl,vwap_trend_S=vs)

    # 7) Setup 15. Setup can be supplied by setup engine.
    sl=15*clamp(float(setup_quality_L or 0)/15); ss=15*clamp(float(setup_quality_S or 0)/15)
    if breakout_confirmed: sl=15
    if breakdown_confirmed: ss=15
    L+=sl; S+=ss; b.update(setup_L=sl,setup_S=ss)

    # 8) Entry quality 10.
    el=10*clamp(float(entry_quality_L or 0)/10); es=10*clamp(float(entry_quality_S or 0)/10)
    L+=el; S+=es; b.update(entry_L=el,entry_S=es)

    # 9) Remaining opportunity 5. Shared because it measures movement capacity;
    # direction is already determined by setup/RS/momentum.
    em=max(0,float(expected_move_pct or 0))
    op=5*clamp(em/1.0)
    L+=op; S+=op; b["opportunity"]=op

    b["sudden_move"]=bool(sudden_move)
    L=min(100,max(0,L)); S=min(100,max(0,S))
    if return_breakdown: return round(L,2),round(S,2),b
    return round(L,2),round(S,2)
