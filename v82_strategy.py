"""V8.2 final deterministic strategy layer: direction-neutral candidates and live entry confirmation."""
from __future__ import annotations
from dataclasses import dataclass,asdict
from dual_scorer import score_candidate_dual
from trade_policy import margin_tier, setup_entry_decision

@dataclass
class V82Decision:
    symbol:str; side:str; candidate_score:float; final_score:float; edge:float
    setup_type:str; entry_price:float; target:float; expected_move_pct:float
    entry_quality:float; setup_quality:float; deployment_multiple:float
    status:str; reason:str

def _ret(c,n):
    if len(c)<=n or not c[-1-n]: return 0.0
    return (c[-1]/c[-1-n]-1)*100

def momentum(df):
    if df is None or len(df)<4:return 0,0,0,0
    c=[float(x) for x in df['close'].tolist()]
    r5=_ret(c,1); r15=_ret(c,min(3,len(c)-1)); r30=_ret(c,min(6,len(c)-1)); acc=r5*2+r15+r30*.5
    return r5,r15,r30,acc

def vwap(df):
    try:
        tp=(df['high']+df['low']+df['close'])/3; vol=df['volume'].replace(0,1)
        return float((tp*vol).cumsum().iloc[-1]/vol.cumsum().iloc[-1])
    except:return 0.0

def rvol(df,avg_daily_volume=0):
    if df is None or 'volume' not in df.columns:return 0.0
    try:
        total=float(df['volume'].fillna(0).sum())
        if avg_daily_volume and avg_daily_volume>0:
            mins=max(1,min(375,(__import__('datetime').datetime.now().hour*60+__import__('datetime').datetime.now().minute)-555))
            expected=max(.05,min(1.0,mins/375))*float(avg_daily_volume)
            return total/expected if expected else 0
        vals=[float(x) for x in df['volume'].fillna(0).tolist() if float(x)>0]
        if len(vals)<4:return 1.0
        return vals[-1]/(sum(vals[:-1])/max(1,len(vals)-1))
    except:return 0.0

def _orb(df):
    if df is None or len(df)<4:return None
    return {'high':float(df['high'].iloc[:3].max()),'low':float(df['low'].iloc[:3].min())}

def setup_and_confirmation(df,entry,side,regime,allow_sudden=True):
    if df is None or len(df)<5:return 'MOMENTUM_CONTINUATION',False,7,0.0
    c=[float(x) for x in df['close']]; h=[float(x) for x in df['high']]; l=[float(x) for x in df['low']]
    vw=vwap(df); orb=_orb(df); recent_hi=max(h[:-1]); recent_lo=min(l[:-1])
    r5,r15,r30,acc=momentum(df)
    long_orb=bool(orb and entry>orb['high'] and c[-1]>orb['high'])
    short_orb=bool(orb and entry<orb['low'] and c[-1]<orb['low'])
    if side=='LONG' and long_orb:
        # Confirmation: current close above ORB + positive last candle + participation/momentum.
        confirm=(c[-1]>c[-2] and (r5>0 or r15>0))
        return 'ORB_BREAKOUT',confirm,15,acc
    if side=='SHORT' and short_orb:
        confirm=(c[-1]<c[-2] and (r5<0 or r15<0))
        return 'ORB_BREAKDOWN',confirm,15,acc
    if side=='LONG':
        reclaim=(c[-2]<=vw and c[-1]>vw) if vw else False
        failed_down=(l[-2]<recent_lo and c[-1]>recent_lo)
        if failed_down and c[-1]>c[-2]: return 'FAILED_BREAKDOWN_REVERSAL',True,14,acc
        if reclaim and c[-1]>c[-2]: return 'PULLBACK_RECLAIM',True,13,acc
        sudden=abs((entry/c[0]-1)*100)>=1 if c[0] else False
        confirm=bool(allow_sudden and sudden and acc>=0.35 and c[-1]>=c[-2])
        return 'MOMENTUM_CONTINUATION',confirm,12,acc
    reject=(c[-2]>=vw and c[-1]<vw) if vw else False
    failed_up=(h[-2]>recent_hi and c[-1]<recent_hi)
    if failed_up and c[-1]<c[-2]: return 'FAILED_BREAKOUT_REVERSAL',True,14,acc
    if reject and c[-1]<c[-2]: return 'PULLBACK_REJECTION',True,13,acc
    sudden=abs((entry/c[0]-1)*100)>=1 if c[0] else False
    confirm=bool(allow_sudden and sudden and acc<=-0.35 and c[-1]<=c[-2])
    return 'MOMENTUM_CONTINUATION',confirm,12,acc

def directional_target(entry,side,df,min_move=.40):
    if entry<=0:return entry,0
    if df is None or len(df)<4:return entry*(1+(min_move/100 if side=='LONG' else -min_move/100)),min_move
    c=[float(x) for x in df['close']]; h=[float(x) for x in df['high']]; l=[float(x) for x in df['low']]
    atr=sum(abs(c[i]-c[i-1]) for i in range(1,len(c)))/max(1,len(c)-1)
    atr_pct=atr/entry*100
    impulse=abs(_ret(c,min(3,len(c)-1)))
    move=max(min_move,min(2.5,max(atr_pct*2.0,impulse*.6)))
    if side=='LONG':
        resistance=max(h[-8:]); room=(resistance-entry)/entry*100
        if room>=min_move: move=min(move,room)
        return entry*(1+move/100),move
    support=min(l[-8:]); room=(entry-support)/entry*100
    if room>=min_move: move=min(move,room)
    return entry*(1-move/100),move

def candidate_score(features):
    return score_candidate_dual(gap_pct=features.get('gap_pct',0),rs=features.get('rs',0),rvol=features.get('rvol',0),df=features.get('df'),ltp=features.get('ltp',0),nifty_gap=features.get('nifty_gap',0),sector_leading=features.get('sector_leading',False),sector_against=features.get('sector_against',False),sector_rs=features.get('sector_rs',0),momentum_5m=features.get('momentum_5m',0),momentum_15m=features.get('momentum_15m',0),momentum_30m=features.get('momentum_30m',0),return_breakdown=True)

def final_decision(features):
    f=dict(features); side=str(f.get('side','LONG')).upper(); entry=float(f.get('ltp') or 0); df=f.get('df')
    setup,confirmed,sq,acc=setup_and_confirmation(df,entry,side,f.get('regime','NORMAL'),bool(f.get('allow_sudden_move',True)))
    eq=0.0
    try:
        vw=vwap(df); c=[float(x) for x in df['close']]
        directional=((entry-vw)/vw*100 if side=='LONG' else (vw-entry)/vw*100) if vw else 0
        eq=5 + min(2.0,max(0.0,directional*3))
        if confirmed:eq+=2
        if f.get('rvol',0)>=1.5:eq+=1
        eq=min(10,eq)
    except: eq=6 if confirmed else 5
    target,move=directional_target(entry,side,df,.40)
    supplied=f.get('target');
    if supplied:
        supplied=float(supplied); sm=((supplied-entry)/entry*100 if side=='LONG' else (entry-supplied)/entry*100)
        if sm>=.40:target,move=supplied,sm
    L,S,_=score_candidate_dual(gap_pct=f.get('gap_pct',0),rs=f.get('rs',0),rvol=f.get('rvol',0),df=df,ltp=entry,nifty_gap=f.get('nifty_gap',0),sector_leading=f.get('sector_leading',False),sector_against=f.get('sector_against',False),sector_rs=f.get('sector_rs',0),momentum_5m=f.get('momentum_5m',0),momentum_15m=f.get('momentum_15m',0),momentum_30m=f.get('momentum_30m',0),setup_quality_L=sq if side=='LONG' else 0,setup_quality_S=sq if side=='SHORT' else 0,entry_quality_L=eq if side=='LONG' else 0,entry_quality_S=eq if side=='SHORT' else 0,expected_move_pct=move,breakout_confirmed=(setup=='ORB_BREAKOUT'),breakdown_confirmed=(setup=='ORB_BREAKDOWN'),sudden_move=abs(float(f.get('gap_pct',0) or 0))>=1,return_breakdown=True)
    score=L if side=='LONG' else S; other=S if side=='LONG' else L; edge=score-other
    ok,reason=setup_entry_decision(side=side,score=score,edge=edge,expected_move_pct=move,entry_quality=eq,setup_quality=sq,regime=f.get('regime','NORMAL'),confirmed=confirmed,sudden_move=(setup=='MOMENTUM_CONTINUATION'),momentum_accel=acc)
    multiple,_=margin_tier(score)
    return asdict(V82Decision(str(f.get('symbol','?')),side,max(L,S),score,edge,setup,entry,target,move,eq,sq,multiple,'ENTER' if ok else 'WATCH',reason))
