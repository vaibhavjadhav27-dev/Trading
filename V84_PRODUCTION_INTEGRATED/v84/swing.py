from dataclasses import dataclass
import pandas as pd
from .indicators import atr,pct
from .config import SWING
@dataclass(frozen=True)
class SwingSignal:
    ticker:str; score:float; entry:float; stop:float; target:float; expected_gain_pct:float; hold_days:int; reason:str

def evaluate(ticker,candles,sector_rs=0,market_rs=0,catalyst=0):
    if candles is None or len(candles)<60:return None
    d=pd.DataFrame(candles).copy()
    for c in ['open','high','low','close','volume']: d[c]=pd.to_numeric(d[c],errors='coerce')
    d=d.dropna().reset_index(drop=True)
    px=float(d.close.iloc[-1]); sma20=float(d.close.tail(20).mean()); sma50=float(d.close.tail(50).mean()); sma200=float(d.close.tail(200).mean()) if len(d)>=200 else sma50
    a=atr(d,14)
    if a<=0:return None
    r10=pct(px,float(d.close.iloc[-11])); r20=pct(px,float(d.close.iloc[-21])); avgvol=float(d.volume.tail(20).mean()); rv=float(d.volume.iloc[-1]/avgvol) if avgvol else 1
    trend=int(px>sma20)+int(sma20>sma50)+int(sma50>=sma200)
    high10=float(d.high.tail(10).max()); high20=float(d.high.tail(20).max()); pullback=pct(px,high20)
    breakout=px>=high10*0.998; retest=(pullback<=5 and px>=sma20 and d.close.iloc[-1]>=d.close.iloc[-2])
    base_range=(float(d.high.tail(15).max())-float(d.low.tail(15).min()))/px*100
    rs=max(-1,min(1,(r10/8)))
    score=0
    score+=25*trend/3
    score+=18*max(0,min(1,(r10+1)/9))
    score+=12*max(0,min(1,(r20+1)/14))
    score+=12*max(0,min(1,rv/2))
    score+=10*max(0,min(1,(sector_rs+1)/3))
    score+=8*max(0,min(1,(market_rs+1)/3))
    score+=10*max(0,min(1,(catalyst+1)/2))
    score+=5*max(0,min(1,(6-abs(pullback-3))/6))
    if breakout:score+=5
    elif retest:score+=3
    if base_range>15:score-=8
    extension=(px-sma20)/a
    if extension>2.5:score-=12
    score=max(0,min(100,score))
    structure=float(d.low.tail(5).min()); stop=max(structure,px*(1-SWING.hard_stop_max_pct/100)); risk=px-stop
    if risk<=0:return None
    # The 6% objective is a minimum opportunity screen, never an automatic exit target.
    target=px*1.06
    expected=pct(target,px)
    if score<SWING.min_score or not (breakout or retest) or expected<SWING.min_expected_gain_pct:return None
    return SwingSignal(ticker,round(score,2),px,round(stop,2),round(target,2),round(expected,2),SWING.preferred_hold_days,'trend+RS+sector+volume+breakout/retest')
