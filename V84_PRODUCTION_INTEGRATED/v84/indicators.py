from __future__ import annotations
import pandas as pd
import numpy as np

def clean_ohlcv(df):
    req=['open','high','low','close','volume']
    miss=[c for c in req if c not in df.columns]
    if miss: raise ValueError(f'Missing OHLCV columns: {miss}')
    d=df.copy()
    for c in req: d[c]=pd.to_numeric(d[c],errors='coerce')
    d=d.dropna(subset=req).reset_index(drop=True)
    if d.empty: raise ValueError('No valid OHLCV rows')
    return d

def pct(a,b): return (float(a)/float(b)-1)*100 if b else 0.0

def atr(df,n=14):
    d=clean_ohlcv(df); prev=d.close.shift(1)
    tr=pd.concat([d.high-d.low,(d.high-prev).abs(),(d.low-prev).abs()],axis=1).max(axis=1)
    return float(tr.tail(n).mean()) if len(tr) else 0.0

def ema(s,n): return float(pd.Series(s).ewm(span=n,adjust=False).mean().iloc[-1])

def vwap(df):
    d=clean_ohlcv(df); v=d.volume.clip(lower=0); den=float(v.sum())
    return float(((d.high+d.low+d.close)/3*v).sum()/den) if den else float(d.close.iloc[-1])

def rvol(df,adv=0,bars_per_day=75):
    d=clean_ohlcv(df); bars=len(d); cum=float(d.volume.sum())
    if adv and adv>0:
        expected=float(adv)*min(1,bars/bars_per_day)
        return cum/expected if expected else 0
    if bars<6:return 1.0
    base=float(d.volume.iloc[:-1].tail(min(20,bars-1)).mean()); cur=float(d.volume.iloc[-1])
    return cur/base if base else 1.0

def momentum(df):
    d=clean_ohlcv(df); c=d.close
    def ret(n): return pct(c.iloc[-1],c.iloc[-1-n]) if len(c)>n else 0
    r1,r3,r6=ret(1),ret(3),ret(6); accel=r1*2+r3+r6*.5
    return r1,r3,r6,accel

def directional_price_volume(df,side):
    d=clean_ohlcv(df)
    if len(d)<8:return 0.0
    r1,r3,r6,acc=momentum(d); vw=vwap(d); px=float(d.close.iloc[-1]); ve=pct(px,vw)
    raw=acc+1.5*ve
    return max(0,raw) if side=='LONG' else max(0,-raw)

def opening_range(df,bars=3):
    d=clean_ohlcv(df)
    if len(d)<bars:return None
    x=d.iloc[:bars]
    return {'high':float(x.high.max()),'low':float(x.low.min()),'bars':bars}

def zscore(x,lo,hi): return max(0,min(1,(x-lo)/(hi-lo))) if hi!=lo else .5
