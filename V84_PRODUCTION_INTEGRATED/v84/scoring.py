from __future__ import annotations
from dataclasses import dataclass
from .config import INTRADAY
from .indicators import atr, momentum, pct, rvol, vwap, zscore, opening_range

MODES=('ORB_BREAKOUT','ORB_BREAKDOWN','MOMENTUM_CONTINUATION','VWAP_REVERSAL','LATE_MOMENTUM')

@dataclass(frozen=True)
class SetupSignal:
    side:str; mode:str; score:float; opposite_score:float; edge:float
    entry:float; stop:float; target:float; expected_move_pct:float
    risk_pct:float; reason:str

def _dir(v,side): return v if side=='LONG' else -v

def _market_points(market_bias,side):
    # 7 points: 4 regime/structure + 3 direction. Neutral market can never veto.
    x=_dir(market_bias,side)
    return INTRADAY.market_points*zscore(x,-1,1)

def _sector_points(sector_bias,side): return INTRADAY.sector_points*zscore(_dir(sector_bias,side),-1,1)

def _rs_points(rs,side): return INTRADAY.rs_points*zscore(_dir(rs,side),-3,3)

def _momentum_points(acc,side): return INTRADAY.momentum_points*zscore(_dir(acc,side),-1.5,1.5)

def _rvol_points(rv,acc,side):
    participation=zscore(rv,0.8,2.8)
    directional=1 if _dir(acc,side)>0 else 0
    return INTRADAY.rvol_points*(0.55*participation+0.45*directional*participation)

def _vwap_points(px,vw,a,side,mode):
    edge=pct(px,vw)
    directed=_dir(edge,side)
    if mode=='VWAP_REVERSAL':
        # Reward reclaim/rejection; initial side-of-VWAP is not a mandatory gate.
        return INTRADAY.vwap_points*zscore(abs(edge),0,1.5)
    return INTRADAY.vwap_points*zscore(directed,-0.10,1.0)

def _setup_points(df,orb,px,a,rv,side,mode):
    c=df.close; h=df.high; l=df.low
    hi,lo=orb['high'],orb['low']
    br=px>hi*(1+INTRADAY.orb_buffer_pct/100) if side=='LONG' else px<lo*(1-INTRADAY.orb_buffer_pct/100)
    body=abs(float(c.iloc[-1]-df.open.iloc[-1]))/max(a,1e-9)
    bodyq=zscore(body,0.15,1.2)
    if mode.startswith('ORB_'):
        dist=(px-hi if side=='LONG' else lo-px)/max(a,1e-9)
        return INTRADAY.setup_points*(0.45*float(br)+0.30*zscore(dist,0,1)+0.25*bodyq)
    if mode=='MOMENTUM_CONTINUATION':
        prior_range=(float(h.tail(6).max())-float(l.tail(6).min()))/max(a,1e-9)
        pullback=zscore(1.8-prior_range,0,1.8)
        return INTRADAY.setup_points*(0.5*bodyq+0.5*pullback)
    if mode=='VWAP_REVERSAL':
        last2=list(c.tail(2)); reclaim=(last2[-1]>vwap(df) if side=='LONG' else last2[-1]<vwap(df))
        return INTRADAY.setup_points*(0.65*float(reclaim)+0.35*zscore(abs(pct(px,vwap(df))),0.6,1.8))
    # late momentum: fresh acceleration + volume + clean recent break
    recent_hi=float(h.iloc[-7:-1].max()) if len(h)>7 else float(h.iloc[:-1].max())
    recent_lo=float(l.iloc[-7:-1].min()) if len(l)>7 else float(l.iloc[:-1].min())
    brk=(px>recent_hi if side=='LONG' else px<recent_lo)
    return INTRADAY.setup_points*(0.5*float(brk)+0.5*zscore(rv,1,2.5))

def _entry_points(df,px,vw,a,side,mode):
    spread_proxy=abs(float(df.close.iloc[-1]-df.open.iloc[-1]))/max(px,1e-9)*100
    location=_dir(pct(px,vw),side)
    # Entry quality rewards confirmation and avoids excessive extension.
    confirm=zscore(abs(float(df.close.iloc[-1]-df.close.iloc[-2]))/max(a,1e-9),0.05,0.8)
    ext=abs(px-vw)/max(a,1e-9)
    extension_penalty=max(0,min(1,(ext-1.0)/1.5))
    return INTRADAY.entry_points*(0.45*zscore(location,-0.25,0.9)+0.45*confirm+0.10*(1-extension_penalty))

def _mode_candidates(df,side,market_bias,sector_bias,rs,adv):
    px=float(df.close.iloc[-1]); a=atr(df,14); vw=vwap(df); rv=rvol(df,adv)
    if a<=0:return []
    r1,r3,r6,acc=momentum(df); orb=opening_range(df,3)
    hi,lo=orb['high'],orb['low']
    extension=abs(px-vw)/a
    long_orb=side=='LONG' and px>hi*(1+INTRADAY.orb_buffer_pct/100)
    short_orb=side=='SHORT' and px<lo*(1-INTRADAY.orb_buffer_pct/100)
    continuation=(r1>0 and r3>0 and acc>0.30 and px>vw) if side=='LONG' else (r1<0 and r3<0 and acc<-0.30 and px<vw)
    # Reversal: prior bar on wrong side, current bar reclaims/rejects VWAP with momentum flip.
    c=df.close; prev=float(c.iloc[-2]);
    prev2=float(c.iloc[-3]) if len(c)>=3 else prev
    reversal=((prev2<=vw and prev<=vw and px>vw and r1>0 and float(c.iloc[-1])>float(c.iloc[-2])) if side=='LONG' else (prev2>=vw and prev>=vw and px<vw and r1<0 and float(c.iloc[-1])<float(c.iloc[-2])))
    recent_hi=float(df.high.iloc[-7:-1].max()) if len(df)>7 else float(df.high.iloc[:-1].max())
    recent_lo=float(df.low.iloc[-7:-1].min()) if len(df)>7 else float(df.low.iloc[:-1].min())
    late=(px>recent_hi and acc>0.35 and rv>=1.40) if side=='LONG' else (px<recent_lo and acc<-0.35 and rv>=1.40)
    modes=[]
    if long_orb or short_orb:modes.append('ORB_BREAKOUT' if side=='LONG' else 'ORB_BREAKDOWN')
    if continuation:modes.append('MOMENTUM_CONTINUATION')
    if reversal:modes.append('VWAP_REVERSAL')
    if late and not (long_orb or short_orb):modes.append('LATE_MOMENTUM')
    out=[]
    for mode in modes:
        if side=='LONG': stop=px-max(a*.85,px*INTRADAY.hard_stop_min_pct/100); stop=max(stop,px*(1-INTRADAY.hard_stop_max_pct/100)); target=px+max(a*1.8,px*INTRADAY.min_expected_move_pct/100)
        else: stop=px+max(a*.85,px*INTRADAY.hard_stop_min_pct/100); stop=min(stop,px*(1+INTRADAY.hard_stop_max_pct/100)); target=px-max(a*1.8,px*INTRADAY.min_expected_move_pct/100)
        expected=pct(target,px) if side=='LONG' else pct(px,target)
        risk=pct(stop,px) if side=='SHORT' else pct(px,stop)
        setup=_setup_points(df,orb,px,a,rv,side,mode)
        entry=_entry_points(df,px,vw,a,side,mode)
        mkt=_market_points(market_bias,side); sec=_sector_points(sector_bias,side); rspts=_rs_points(rs,side); mom=_momentum_points(acc,side); rvpts=_rvol_points(rv,acc,side); vwpts=_vwap_points(px,vw,a,side,mode)
        # 5-point opportunity is directional remaining reward net of risk/cost.
        rr=max(0,(expected-(INTRADAY.hard_stop_min_pct*0.35+0.12))/max(risk,0.1))
        opp=INTRADAY.opportunity_points*zscore(rr,0.8,3.5)
        score=sum([mkt,sec,rspts,mom,rvpts,vwpts,setup,entry,opp])
        # Universal exhaustion penalty, but mode-specific reversal is exempt.
        if extension>INTRADAY.max_extension_atr and mode!='VWAP_REVERSAL': score-=10
        minscore=dict(zip(MODES,INTRADAY.min_score_by_mode))[mode]
        reason=f'{mode}|MKT={mkt:.1f}|SEC={sec:.1f}|RS={rspts:.1f}|MOM={mom:.1f}|RVOL={rvpts:.1f}|VWAP={vwpts:.1f}|SETUP={setup:.1f}|ENTRY={entry:.1f}|OPP={opp:.1f}'
        out.append(SetupSignal(side,mode,max(0,min(100,score)),0,0,px,stop,target,expected,risk,reason))
    return out

def evaluate(df,*,rs=0,market_bias=0,sector_bias=0,avg_daily_volume=0):
    if df is None or len(df)<20:return None
    candidates=_mode_candidates(df,'LONG',market_bias,sector_bias,rs,avg_daily_volume)+_mode_candidates(df,'SHORT',market_bias,sector_bias,rs,avg_daily_volume)
    if not candidates:return None
    candidates.sort(key=lambda s:(s.score,s.expected_move_pct/max(s.risk_pct,0.1)),reverse=True)
    best=candidates[0]; opp=max((x.score for x in candidates if x.side!=best.side),default=0)
    best=SetupSignal(best.side,best.mode,best.score,opp,best.score-opp,best.entry,best.stop,best.target,best.expected_move_pct,best.risk_pct,best.reason)
    threshold=dict(zip(MODES,INTRADAY.min_score_by_mode))[best.mode]
    if best.score<threshold:return None
    if best.edge<INTRADAY.min_edge:return None
    if best.expected_move_pct<INTRADAY.min_expected_move_pct:return None
    if best.mode!='VWAP_REVERSAL' and rvol(df,avg_daily_volume)<INTRADAY.min_rvol:return None
    return best
