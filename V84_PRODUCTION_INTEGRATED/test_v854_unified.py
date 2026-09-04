import importlib.util, sys
from pathlib import Path
p=Path(__file__).with_name("V854_UNIFIED_PATCH.py")
spec=importlib.util.spec_from_file_location("v854",p); m=importlib.util.module_from_spec(spec); sys.modules["v854"]=m; spec.loader.exec_module(m)

# Structural stop signs: LONG below entry, SHORT above entry.
assert m.structural_stop("LONG",100,2,support=99,trigger=100) < 100
assert m.structural_stop("SHORT",100,2,resistance=101,trigger=100) > 100

# Crossed broker stop is repaired to the valid side, not market-exited.
assert m.broker_valid_trigger("LONG",102,100,2) < 100
assert m.broker_valid_trigger("SHORT",98,100,2) > 100

# Original-risk based trailing: changing current SL must not change R.
pos={"side":"LONG","entry":100,"initial_sl":97,"sl":99,"peak":100,"best_r":0}
assert m.live_r(pos,103)==1.0
m.update_peak(pos,106)
assert abs(pos["best_r"]-2.0)<1e-9

# Temporary pullback does not exit without multi-factor reversal.
pos={"side":"LONG","entry":100,"initial_sl":97,"sl":102,"peak":106,"best_r":2}
r=m.evaluate_profit_exit(pos,105,{"momentum_5m":0.2,"momentum_15m":0.4,"vwap_reversal":False,"rs":1,"structure_break":False,"volume_climax":False,"price_progress_stalling":False,"setup_invalidated":False})
assert r["action"] in ("HOLD","PROTECT") and r["action"]!="EXIT"

# Confirmed reversal can exit.
r=m.evaluate_profit_exit(pos,103,{"momentum_5m":-0.5,"momentum_15m":-0.7,"vwap_reversal":True,"rs":-2,"structure_break":True,"volume_climax":False,"price_progress_stalling":True,"setup_invalidated":False})
assert r["action"]=="EXIT"

# Audit corruption from today's log is rejected.
ok,reason=m.validate_entry_audit({"symbol":"BLS","side":"SHORT","entry_price":251.08,"fill_price":250.88,"qty":100,"raw_score":81.68,"final_score":81.68,"initial_sl":253.35,"risk_per_share":2.47,"expected_r":81.68})
assert not ok and reason=="EXPECTED_R_CONTAINS_SCORE"
print("V8.5.4 unified tests: PASS")
