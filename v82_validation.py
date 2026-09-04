import os,sys,compileall
from types import SimpleNamespace
sys.path.insert(0,os.path.dirname(__file__))
from v82_strategy import final_decision,candidate_score
from trade_policy import margin_tier
import pandas as pd

def df_for(side='LONG',n=12):
    if side=='LONG': c=[100+i*0.25+(0.15 if i>8 else 0) for i in range(n)]
    else: c=[100-i*0.25-(0.15 if i>8 else 0) for i in range(n)]
    return pd.DataFrame({'open':c,'high':[x+0.15 for x in c],'low':[x-0.15 for x in c],'close':c,'volume':[10000]*n})

def test():
    assert margin_tier(59.99)[0]==0
    assert margin_tier(60)[0]==1
    assert margin_tier(70)[0]==1
    assert margin_tier(70.01)[0]==2
    assert margin_tier(80)[0]==2
    assert margin_tier(80.01)[0]==4.5
    assert margin_tier(100)[0]==4.5
    base={'symbol':'TEST','ltp':102.0,'gap_pct':2.0,'nifty_gap':0.4,'rs':2.0,'rvol':2.5,'df':df_for('LONG'),'momentum_5m':.3,'momentum_15m':.7,'momentum_30m':1.2,'accel':1.0,'sector_leading':True,'sector_against':False,'sector_rs':1.2,'regime':'NORMAL'}
    L,S,_=candidate_score(base); assert 0<=L<=100 and 0<=S<=100
    f=dict(base); f.update(side='LONG',confirmed=True,entry_quality=9)
    d=final_decision(f); assert d['final_score']<=100 and d['side']=='LONG'
    # sudden mover continuation can qualify without ORB flag
    assert d['setup_type'] in ('MOMENTUM_CONTINUATION','ORB_BREAKOUT','PULLBACK_RECLAIM','FAILED_BREAKDOWN_REVERSAL')
    # short direction must use directional score and target below entry
    f2={'symbol':'SHORT','ltp':98,'gap_pct':-2,'nifty_gap':-.4,'rs':-2,'rvol':2.5,'df':df_for('SHORT'),'momentum_5m':-.3,'momentum_15m':-.7,'momentum_30m':-1.2,'accel':-1,'sector_against':True,'sector_rs':-1.2,'regime':'TRENDING_DOWN','side':'SHORT','confirmed':True,'entry_quality':9}
    d2=final_decision(f2); assert d2['side']=='SHORT' and d2['target']<d2['entry_price']
    assert compileall.compile_dir(os.path.dirname(__file__),quiet=1)
    print('ALL V8.2 OFFLINE STRATEGY/COMPILE TESTS PASSED')
if __name__=='__main__':test()
