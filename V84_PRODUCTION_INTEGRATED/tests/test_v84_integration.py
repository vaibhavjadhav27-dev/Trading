import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
import pandas as pd
from v84.scoring import evaluate
from v84.risk import size_from_risk,RiskState,trading_allowed
from v84_strategy import final_decision

def candles(side=1,n=90):
    p=100.; rows=[]
    for i in range(n):
        p += side*(.04 if i<70 else .22)
        rows.append({'open':p-side*.03,'high':p+.15,'low':p-.15,'close':p,'volume':10000+(i%6)*2000})
    return pd.DataFrame(rows)

def test_long_short():
    for side in (1,-1):
        d=candles(side)
        s=evaluate(d,rs=3*side,market_bias=.9*side,sector_bias=.9*side,avg_daily_volume=700000)
        assert s is not None
        assert s.side==('LONG' if side==1 else 'SHORT')
        assert 0<=s.score<=100
        assert (s.stop<s.entry<s.target) if side==1 else (s.stop>s.entry>s.target)

def test_risk_and_lock():
    x=size_from_risk(100,99.4,100000)
    assert x['qty']>0 and x['risk_pct']<=0.7
    assert trading_allowed(RiskState(100000,realized_pnl=-1800))[0] is False

def test_adapter():
    d=candles(1)
    f={'symbol':'TEST','ltp':float(d.close.iloc[-1]),'df':d,'rs':2,'candidate_score':80,'sector_leading':True,'sector_against':False,'nifty_data':{'ltp':101,'vwap':100,'ema20':100.5,'ema50':100}}
    out=final_decision(f)
    assert out['status'] in ('ENTER','WATCH')

def test_no_live_guard():
    assert os.getenv('V84_ENABLE_LIVE','0') != '1'
