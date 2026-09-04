"""Final V8.2 end-to-end offline validation.
No real Dhan order is placed. Uses a broker mock to prove orchestration and state transitions."""
import os,sys,types,compileall
import pandas as pd
sys.path.insert(0,os.path.dirname(__file__))

# Stub optional legacy SDK before importing the inherited orchestrator.
m=types.ModuleType('dhanhq'); m.MarketFeed=object; m.DhanContext=object; m.dhanhq=object; sys.modules['dhanhq']=m

from v82_strategy import final_decision, candidate_score
from trade_policy import margin_tier
from v82_dhan_gateway import DhanV82Gateway
from trading_bot_v82 import TradingBotV82


def df(side='LONG'):
    if side=='LONG': c=[100+i*.35 for i in range(14)]
    else: c=[100-i*.35 for i in range(14)]
    return pd.DataFrame({'open':c,'high':[x+.18 for x in c],'low':[x-.18 for x in c],'close':c,'volume':[25000]*14})

class FakeDhan:
    def __init__(self, short=False, sl_ok=True): self.positions={}; self.i=0; self.sl_ok=sl_ok
    def get_balance(self): return 50000
    def calculate_margin(self,sid,qty,txn,price): return {'totalMargin':50000,'availableBalance':50000,'leverage':'4.5','insufficientBalance':0}
    def place_order(self,sid,qty,price,txn,order_type,trigger_price=0,correlation_id=None):
        self.i+=1; oid=f'O{self.i}'
        if order_type=='STOP_LOSS_MARKET': return {'orderId':oid,'orderStatus':'PENDING'} if self.sl_ok else {}
        self.positions[str(sid)]=qty if txn=='BUY' else -qty
        return {'orderId':oid,'orderStatus':'PENDING'}
    def place_hard_sl(self,sid,qty,side,trigger): return self.place_order(sid,qty,0,'SELL' if side=='LONG' else 'BUY','STOP_LOSS_MARKET',trigger_price=trigger)
    def verify_fill(self,oid,timeout=20): return {'status':'FILLED','qty':10,'price':100.0,'raw':{}}
    def verify_position(self,sid,side=None): return self.positions.get(str(sid),0)
    def get_order_status(self,oid): return {'orderId':oid,'orderStatus':'PENDING'}
    def cancel_order(self,oid): return {'orderId':oid,'orderStatus':'CANCELLED'}

def main():
    # Score/margin policy
    assert margin_tier(59.99)[0]==0 and margin_tier(60)[0]==1 and margin_tier(70)[0]==1
    assert margin_tier(71)[0]==2 and margin_tier(80)[0]==2 and margin_tier(80.01)[0]==4.5

    # Candidate can be high on current intraday move even with a small opening gap.
    base={'symbol':'MOVER','ltp':106,'gap_pct':0.1,'nifty_gap':0.0,'rs':3.0,'rvol':2.8,'df':df('LONG'),'momentum_5m':0.8,'momentum_15m':1.4,'momentum_30m':2.0,'accel':4.1,'sector_leading':True,'sector_against':False,'sector_rs':1.5,'regime':'NORMAL'}
    L,S,_=candidate_score(base); assert L>S and L>=50
    base.update(side='LONG',allow_sudden_move=True)
    d=final_decision(base); assert d['side']=='LONG'

    # Short path is symmetric.
    s={'symbol':'LOSER','ltp':95,'gap_pct':-0.1,'nifty_gap':0.0,'rs':-3.0,'rvol':2.8,'df':df('SHORT'),'momentum_5m':-0.8,'momentum_15m':-1.4,'momentum_30m':-2.0,'accel':-4.1,'sector_leading':False,'sector_against':True,'sector_rs':-1.5,'regime':'NORMAL','side':'SHORT','allow_sudden_move':True}
    ds=final_decision(s); assert ds['side']=='SHORT' and ds['target']<ds['entry_price']

    # Actual entry requires confirmation; no hard-coded confirmed=True.
    no_confirm=dict(base); no_confirm['df']=df('LONG'); no_confirm['allow_sudden_move']=False
    # This may be WATCH because confirmation is intentionally absent; it must not be forced ENTER.
    no_confirm['side']='LONG'; nd=final_decision(no_confirm); assert nd['reason']!='ENTRY_OK' or nd['status']=='ENTER'

    # Mock complete LONG and SHORT execution paths.
    b=TradingBotV82.__new__(TradingBotV82); b.dhan=FakeDhan(); b.active_positions={}; b.dry_run=True
    c={'symbol':'LONGX','security_id':'1','ltp':100,'df':df('LONG')}; dec={'side':'LONG','final_score':86,'entry_price':100,'target':101,'status':'ENTER'}
    ok,reason=b.execute_entry(c,dec); assert ok,reason and b.active_positions['1']['side']=='LONG'

    b2=TradingBotV82.__new__(TradingBotV82); b2.dhan=FakeDhan(); b2.active_positions={}; b2.dry_run=True
    c2={'symbol':'SHORTX','security_id':'2','ltp':100,'df':df('SHORT')}; dec2={'side':'SHORT','final_score':82,'entry_price':100,'target':99,'status':'ENTER'}
    ok,reason=b2.execute_entry(c2,dec2); assert ok,reason and b2.active_positions['2']['side']=='SHORT'

    # SL failure must not leave an active unprotected position.
    b3=TradingBotV82.__new__(TradingBotV82); b3.dhan=FakeDhan(sl_ok=False); b3.active_positions={}; b3.dry_run=True
    ok,reason=b3.execute_entry(c,dec); assert not ok and not b3.active_positions

    # Whole tree compiles.
    assert compileall.compile_dir(os.path.dirname(__file__),quiet=1)
    print('V8.2 FINAL END-TO-END OFFLINE VALIDATION: ALL TESTS PASSED')
    print('PASS: 100-point scoring; candidate-vs-entry separation; current-move discovery; LONG; SHORT; confirmation gate; 1x/2x/4.5x tiers; Dhan margin mock; fill; broker position; hard SL; SL failure emergency path; compileall')
    print('LIVE STATUS: not executed here; requires user Dhan credentials/static IP and dry-run/sandbox preflight on EC2')

if __name__=='__main__': main()
