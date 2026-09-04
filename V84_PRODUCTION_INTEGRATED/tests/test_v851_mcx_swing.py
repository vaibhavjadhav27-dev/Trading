import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from mcx_v851_strategy import *
from swing_v851_strategy import *

def test_mcx_native_orb_signal():
    s = evaluate_mcx({
        "symbol":"MCX_TEST","price":100,"vwap":99.5,"atr":1.0,
        "orb_high":99,"orb_low":97,"mom5":1.0,"mom15":1.0,
        "rvol":2.5,"spread_pct":0.02,"lot_size":1,
        "global_bias":1.0,"fx_bias":1.0,"compression":False
    })
    assert s and s.side=="LONG" and s.setup=="ORB_CONTINUATION"

def test_mcx_no_forced_lot():
    sig=MCXSignal("X","LONG",80,100,99,102,2,1000,"ORB",())
    x=size_mcx(sig,10000,0,9000)
    assert x["qty"]==0

def test_swing_early_accumulation():
    s=evaluate_swing({
        "ticker":"TEST","price":108,"sma20":106,"sma50":102,"sma200":98,
        "atr":2,"rs10":5,"rs20":8,"rvol":1.5,"sector_rs":1,
        "market_rs":0.5,"high20":112,"low20":100,"base_tightness":0.8,
        "accumulation":0.8,"breakout":False,"retest":False,
        "close_strength":0.8,"catalyst":0.2
    })
    assert s is not None and s.expected_move_pct>=6

def test_swing_6pct_is_not_exit():
    action, sl=swing_exit_v851(5,100,106,106,94,True,True)
    assert action=="HOLD_RUNNER"

def test_swing_risk_budget():
    s=SwingSignal("X","LONG",80,100,95,8,6,"BREAKOUT",())
    x=size_swing(s,100000,2200)
    assert x["qty"]>0
    assert x["risk_rupees"]<=200

if __name__=="__main__":
    for fn in [test_mcx_native_orb_signal,test_mcx_no_forced_lot,
               test_swing_early_accumulation,test_swing_6pct_is_not_exit,
               test_swing_risk_budget]:
        fn()
    print("MCX/Swing V8.5.1 pure tests: PASS")
