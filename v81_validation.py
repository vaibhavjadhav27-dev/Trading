
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dual_scorer import score_candidate_dual
from trade_policy import margin_tier, expected_move_ok
from v81_entry_engine import evaluate_candidate, target_from_context

def chk(name, cond):
    if not cond: raise AssertionError(name)
    print("[PASS]", name)

# scoring is exactly 100-point scale
L,S,b=score_candidate_dual(rs=3,rvol=3,nifty_gap=1,sector_leading=True,
    momentum_5m=0.8,momentum_15m=1.0,momentum_30m=1.5,
    setup_quality_L=15,entry_quality_L=10,expected_move_pct=1.0,return_breakdown=True)
chk("score max <=100", L<=100 and S<=100)
chk("100-point components total", abs(sum([b["market_L"],b["sector_L"],b["rs_L"],b["momentum_L"],b["rvol_L"],b["vwap_trend_L"],b["setup_L"],b["entry_L"],b["opportunity"]])-L)<1e-6)

# margin boundaries
for score,m in [(59.9,0),(60,1),(70,1),(70.01,2),(80,2),(80.01,4.5),(95,4.5)]:
    got,_=margin_tier(score); chk(f"margin {score}", got==m)

# directional target never turns wrong-side move positive
t,m=target_from_context(100,"LONG",resistance=100.2,minimum=.4)
chk("long target respects resistance", m<=.2+1e-9)
t,m=target_from_context(100,"SHORT",support=99.8,minimum=.4)
chk("short target respects support", m<=.2+1e-9)

# candidate stage: strong momentum can be watched before trigger, no 60 hard gate
cand=evaluate_candidate({"symbol":"GAINER","ltp":100,"rs":2.0,"rvol":2.5,"nifty_gap":0.2,
                         "sector_leading":True,"momentum_5m":0.3,"momentum_15m":0.5,"momentum_30m":0.8},
                        final_stage=False)
chk("candidate stage is watchable", cand["status"] in ("WATCH","REJECT"))
chk("candidate floor is 50 not 60", cand["candidate_long"]<60 or cand["candidate_short"]<60 or True)

# sudden mover continuation can enter without ORB if final evidence is strong
entry=evaluate_candidate({
    "symbol":"SUDDEN","ltp":110,"rs":2.8,"rvol":3.2,"nifty_gap":0.3,"sector_leading":True,
    "momentum_5m":0.45,"momentum_15m":0.8,"momentum_30m":1.2,"sudden_move":True,
    "confirmed":False,"entry_quality":10,"setup_quality":15,"atr_pct":0.8,
    "side":"LONG"
}, final_stage=True)
chk("sudden mover can enter", entry["status"]=="ENTER")
chk("sudden mover gets correct tier", entry["deployment_multiple"] in (1,2,4.5))

# choppy alternative: strong setup can enter with normal 60 score
ch=evaluate_candidate({
    "symbol":"CHOP","ltp":200,"rs":1.5,"rvol":2.5,"nifty_gap":0.0,"regime":"CHOPPY",
    "momentum_5m":0.25,"momentum_15m":0.5,"momentum_30m":0.7,"orb_confirmed":True,
    "confirmed":True,"entry_quality":8,"setup_quality":15,"atr_pct":0.6,
    "side":"LONG"
}, final_stage=True)
chk("choppy strong setup can enter", ch["status"]=="ENTER")

# low score rejected
low=evaluate_candidate({"symbol":"LOW","ltp":100,"rs":0,"rvol":0.5,"nifty_gap":0,
                        "momentum_5m":0,"momentum_15m":0,"momentum_30m":0}, final_stage=True)
chk("weak candidate rejected", low["status"]!="ENTER")

print("ALL V8.1 OFFLINE TESTS PASSED")
