import sys, os; sys.path.insert(0,"/home/ubuntu/trading-bot"); os.chdir("/home/ubuntu/trading-bot")
import smart_sizing, importlib; importlib.reload(smart_sizing)
bal=50294.0; price=500.0; sl_dist=10.0   # entry 500, SL 490 -> distance 10
print("signature:", __import__("inspect").signature(smart_sizing.calculate_safe_qty))
for mult,label in [(1.0,"FULL-SIZE (trending/normal)"),(0.5,"BEARISH 0.5x rail")]:
    r=smart_sizing.calculate_safe_qty(price, sl_dist, bal, size_mult=mult)
    print(f"\n{label}: {r}")
# stress: tiny SL distance (risk-ceiling should cap qty), and huge (should shrink)
print("\ntight SL dist=2 (qty capped by 3% risk?):", smart_sizing.calculate_safe_qty(price,2.0,bal,size_mult=1.0))
print("wide  SL dist=40:",                         smart_sizing.calculate_safe_qty(price,40.0,bal,size_mult=1.0))
# None-SL: should be rejected/guarded, not crash
try:
    print("\nNone SL_dist ->", smart_sizing.calculate_safe_qty(price,None,bal,size_mult=1.0))
except Exception as e:
    print("\nNone SL_dist -> guarded:", type(e).__name__, e)
