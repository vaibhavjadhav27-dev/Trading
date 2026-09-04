"""Offline V8 validation. No network, broker or AWS calls."""
import os,sys,types,tempfile,importlib,py_compile
ROOT=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,ROOT)

def check(name, cond):
    if not cond: raise AssertionError(name)
    print('[PASS]',name)

from trade_policy import confidence_pct, margin_tier, pick_side, expected_move_ok
from dual_scorer import score_candidate_dual
from short_live import size_position, profit_lock_floor, current_profit_pct

# 100 point scale + exact margin tiers
check('score scale is 100', confidence_pct(100)==100)
for c,lev in [(59,0),(60,1),(70,1),(71,2),(80,2),(81,4.5),(95,4.5)]:
    check(f'margin {c}', margin_tier(c)[0]==lev)
check('1x sizing', size_position(50000,500,60)[0]==100)
check('2x sizing', size_position(50000,500,75)[0]==200)
check('4.5x sizing', size_position(50000,500,90)[0]==450)

# all regimes remain tradable; low edge waits
for reg,L,S,want in [('TRENDING_UP',80,60,'LONG'),('TRENDING_DOWN',60,80,'SHORT'),('NORMAL',82,65,'LONG'),('CHOPPY',82,70,'LONG'),('CHOPPY',78,75,'NO_TRADE')]:
    side,_=pick_side(reg,L,S); check(f'{reg} {L}/{S}',side==want)
check('expected move gate', expected_move_ok(100,100.39,'LONG') is False)
check('expected move gate pass', expected_move_ok(100,100.40,'LONG') is True)
check('short expected move', expected_move_ok(100,99.60,'SHORT') is True)

# Score components cannot exceed 100.
class I:
    @staticmethod
    def compute_trend_quality(df): return 1.0
    @staticmethod
    def compute_atr(df,period=14):
        import pandas as pd
        return pd.Series([10.0]*len(df))
    @staticmethod
    def compute_vwap(df):
        import pandas as pd
        return pd.Series([90.0]*len(df))
import pandas as pd
df=pd.DataFrame({'open':[90]*30,'high':[100]*30,'low':[80]*30,'close':[100]*30,'volume':[100000]*30})
L,S,b=score_candidate_dual(gap_pct=3,rs=3,rvol=3,df=df,ltp=100,nifty_gap=1,indicators_mod=I,sector_leading=True,setup_quality_L=15,entry_quality_L=10,expected_move_pct=1,return_breakdown=True)
check('long score <=100',L<=100)
check('short score <=100',S<=100)
check('strong synthetic long',L>80)

# Sudden mover remains eligible when remaining move is supported.
check('sudden mover is not auto rejected', score_candidate_dual(gap_pct=4,rs=2,rvol=2.5,df=df,ltp=110,nifty_gap=0.5,indicators_mod=I,sector_leading=True,setup_quality_L=15,entry_quality_L=10,expected_move_pct=0.6,breakout_confirmed=True)[0] >= 60)

# Import trading_bot without dhanhq installed.
mod=types.ModuleType('dhanhq'); mod.MarketFeed=object; mod.DhanContext=object; sys.modules['dhanhq']=mod
import trading_bot
check('trading_bot import without broker SDK', True)

# Test 30-minute snapshot without AWS/network by constructing object and stubbing methods.
b=trading_bot.TradingBot.__new__(trading_bot.TradingBot)
b.regime='CHOPPY'; b.nifty_data={'ltp':100.0,'prev_close':99.5,'vwap':99.8,'ema20':100.1,'ema50':99.0}
b._last_market_snapshot_slot=None
b.check_market_quality=lambda:'FULL'
b._sector_snapshot=lambda:([{'sector':'IT','return_pct':1.0,'rs_vs_nifty':0.5}],[('IT',1.0,0.5)],[('REALTY',-1.0,-1.5)])
with tempfile.TemporaryDirectory() as td:
    os.chdir(td)
    ok=b.maybe_record_market_snapshot(force=True)
    check('30m snapshot writes',ok is True)
    check('snapshot file exists',os.path.exists('candle_archive/market_snapshot_2026-08-11.csv') or len(os.listdir('candle_archive'))==1)

# Test side-aware fill/position/SL path with mocked Dhan.
import patch_integrate
class Dhan:
    def __init__(self): self.client_id='X'; self.orders=[]
    def get_balance(self): return 50000
    def calculate_margin(self,*a): return {'totalMargin':45000,'availableBalance':50000,'leverage':'1'}
    def place_order(self,*a,**kw):
        # entry or exit; stop order uses _request
        txn=a[3] if len(a)>3 else kw.get('transaction_type')
        oid='E1' if txn in ('BUY','SELL') and not self.orders else 'X1'; self.orders.append((txn,oid)); return {'orderId':oid,'orderStatus':'TRADED'}
    def get_order_status(self,oid): return {'orderId':oid,'orderStatus':'TRADED','tradedQuantity':2250,'tradedPrice':100.0}
    def get_trades_for_order(self,oid): return [{'tradedQuantity':2250,'tradedPrice':100.0}]
    def get_positions(self): return [{'securityId':'123','netQty':2250}]
    def cancel_order(self,oid): return {'orderId':oid,'orderStatus':'CANCELLED'}
    def _request(self,*a,**kw): return {'orderId':'SL1','orderStatus':'PENDING'}
class Dynamo:
    def save_active_trade(self,t): self.t=t
    def clear_active_trade(self): pass
class Bot:
    def __init__(self): self.dhan=Dhan(); self.dynamo=Dynamo(); self.active_trade=None
    def fetch_ltp_concurrent(self,s): return {'123':100.0}
bot=Bot()
# Patch time sleep in fill poll to zero.
patch_integrate.time.sleep=lambda x: None
res=patch_integrate.side_aware_entry(bot,'NORMAL',85,None,{'symbol':'ABC','security_id':'123','expected_move_pct':0.6},None)
check('mock LONG entry succeeds',res is not None)
check('mock LONG trade active',bot.active_trade and bot.active_trade['side']=='LONG')
check('profit floor activates',profit_lock_floor(0.40)>=0.35)
check('short pnl directional',current_profit_pct(100,99,'SHORT')==1.0)

# Mock SHORT entry path with correct negative broker netQty.
class DhanShort(Dhan):
    def get_positions(self): return [{'securityId':'123','netQty':-2250}]
class BotShort(Bot):
    def __init__(self): self.dhan=DhanShort(); self.dynamo=Dynamo(); self.active_trade=None
bs=BotShort()
rs=patch_integrate.side_aware_entry(bs,'TRENDING_DOWN',None,86,None,{'symbol':'XYZ','security_id':'123','expected_move_pct':0.8})
check('mock SHORT entry succeeds',rs is not None)
check('mock SHORT trade active',bs.active_trade and bs.active_trade['side']=='SHORT')

# SL installation failure must not leave a position recorded as active.
class DhanNoSL(Dhan):
    def _request(self,*a,**kw): raise RuntimeError('simulated SL rejection')
bn=Bot(); bn.dhan=DhanNoSL()
rn=patch_integrate.side_aware_entry(bn,'NORMAL',85,None,{'symbol':'FAIL','security_id':'123','expected_move_pct':0.7},None)
check('SL failure does not activate trade',rn is None and bn.active_trade is None)

# Compile all Python modules.
for root,_,files in os.walk(ROOT):
    for fn in files:
        if fn.endswith('.py'):
            py_compile.compile(os.path.join(root,fn),doraise=True)
check('all python modules compile',True)
print('ALL V8 OFFLINE TESTS PASSED')
