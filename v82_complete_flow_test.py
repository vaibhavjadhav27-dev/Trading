import sys,os,pandas as pd,types
# Stub optional Dhan SDK imports used by legacy infrastructure; V8.2 gateway itself uses REST.
m=types.ModuleType('dhanhq'); m.MarketFeed=object; m.DhanContext=object; sys.modules['dhanhq']=m
sys.path.insert(0,os.path.dirname(__file__))
from trading_bot_v82 import TradingBotV82

class FakeDhan:
    def __init__(self): self.n=0; self.cancelled=[]; self.positions={}
    def get_balance(self): return 50000
    def calculate_margin(self,sid,qty,txn,price): return {'totalMargin':50000,'availableBalance':50000,'leverage':'4.5','insufficientBalance':0}
    def place_order(self,sid,qty,price,txn,order_type,trigger_price=0,correlation_id=None):
        self.n+=1; oid=f'O{self.n}'
        if order_type=='STOP_LOSS_MARKET': self.sl=(oid,sid,qty,txn,trigger_price)
        else:
            self.positions[str(sid)] = qty if txn=='BUY' else -qty
        return {'orderId':oid,'orderStatus':'PENDING'}
    def place_hard_sl(self,sid,qty,side,trigger): return self.place_order(sid,qty,0,'SELL' if side=='LONG' else 'BUY','STOP_LOSS_MARKET',trigger_price=trigger)
    def verify_fill(self,oid,timeout=15):
        return {'status':'FILLED','qty':10,'price':100.0,'raw':{}}
    def verify_position(self,sid,side=None):
        return self.positions.get(str(sid),0)
    def get_order_status(self,oid): return {'orderId':oid,'orderStatus':'PENDING'}
    def cancel_order(self,oid): self.cancelled.append(oid); return {'orderId':oid,'orderStatus':'CANCELLED'}

def test():
    b=TradingBotV82.__new__(TradingBotV82); b.dhan=FakeDhan(); b.active_positions={}; b.dry_run=True
    df=pd.DataFrame({'open':[100+i*.2 for i in range(12)],'high':[100+i*.2+.2 for i in range(12)],'low':[100+i*.2-.2 for i in range(12)],'close':[100+i*.2 for i in range(12)],'volume':[20000]*12})
    c={'symbol':'TEST','security_id':'123','ltp':102.2,'df':df,'gap_pct':2,'nifty_gap':.4,'rs':2.2,'rvol':2.5,'sector_leading':True,'sector_against':False,'sector_rs':1,'momentum_5m':.4,'momentum_15m':.8,'momentum_30m':1.2,'accel':1.4}
    d={'side':'LONG','final_score':86,'entry_price':102.2,'target':103,'status':'ENTER'}
    ok,reason=b.execute_entry(c,d)
    assert ok,reason and 'sl_order_id' in b.active_positions['123']
    print('COMPLETE MOCK LONG FLOW PASSED: score -> margin -> BUY -> fill -> position -> hard SL -> active position')
if __name__=='__main__':test()
