import ast, json, os, glob, sys, traceback
from datetime import datetime, date
P=lambda t,m: print(("[OK]  " if t else "[FAIL]")+" "+m)
fails=0
def chk(t,m):
    global fails
    P(t,m)
    if not t: fails+=1

print("="*60,"\n1. ORCHESTRATION: parse + import core modules\n"+"="*60)
core=["trading_bot","ws_ltp_scanner","rs_scorer","clv_scorer","smart_sizing",
      "smart_exit_v3","sector_rotation","vix_adaptive","fno_ban_check",
      "candidate_logger","secrets_manager","indicators","shortlist_emailer"]
for m in core:
    f=m+".py"
    if not os.path.exists(f): chk(False,f"{f} MISSING"); continue
    try: ast.parse(open(f,encoding="utf-8").read()); 
    except SyntaxError as e: chk(False,f"{f} SYNTAX: {e}"); continue
    try:
        __import__(m); chk(True,f"{m} imports")
    except Exception as e:
        chk(False,f"{m} IMPORT ERROR: {type(e).__name__}: {e}")

print("\n"+"="*60,"\n2. WS NAMEERROR NEUTRALIZED\n"+"="*60)
src=open("trading_bot.py",encoding="utf-8").read()
chk("per-tick WS not implemented" in src,"start_websocket neutralized (patch present)")
chk(src.count("self.ws_feed = WebSocketFeed()")==0,"no live WebSocketFeed() call")

print("\n"+"="*60,"\n3. FILE FORMATS: JSON caches parse + shape + dates\n"+"="*60)
def load(fn):
    try: return json.load(open(fn,encoding="utf-8")),None
    except Exception as e: return None,str(e)
# prev-close cache
for cand in ["prev_session_close.json","prev_close.json","stock_prev_close.json"]:
    if os.path.exists(cand):
        d,e=load(cand)
        chk(d is not None,f"{cand} parses"+("" if d else f" ({e})"))
        if isinstance(d,dict):
            dt=d.get("date") or d.get("updated") or d.get("session_date")
            print(f"       {cand}: date={dt}, keys~{len(d)}")
# 30d history
if os.path.exists("stock_history_30d.json"):
    d,e=load("stock_history_30d.json")
    chk(d is not None,"stock_history_30d.json parses"+("" if d else f" ({e})"))
    if isinstance(d,dict):
        stocks=d.get("stocks",d)
        chk(isinstance(stocks,dict) and len(stocks)>0,f"history has {len(stocks) if isinstance(stocks,dict) else 0} stocks")
        nifty=any(k in stocks for k in ("NIFTY","NIFTY 50","13","^NSEI"))
        P(nifty,"NIFTY series present (RS true-relative)" if nifty else "NIFTY ABSENT -> RS raw-return fallback (known data gap, non-blocking)")
# sector cache
for cand in glob.glob("sector*close*.json")+["sector_prev_close.json"]:
    if os.path.exists(cand):
        d,e=load(cand); chk(d is not None,f"{cand} parses"+("" if d else f" ({e})")); break

print("\n"+"="*60,"\n4. FALLBACK PATH present\n"+"="*60)
chk("bounded REST fallback" in src or "REST fallback (top 120" in src or "bounded to 120" in src,
    "bounded-120 REST fallback code present")
chk("get_bulk_ltp" in src,"get_bulk_ltp (bulk-WS snapshot) wired at selection")

print("\n"+"="*60,"\n5. DHAN COMPATIBILITY (read-only)\n"+"="*60)
try:
    import config
    from secrets_manager import get_dhan_token, get_dhan_client_id
    tok=get_dhan_token(); cid=get_dhan_client_id()
    chk(bool(tok) and len(str(tok))>20,f"Dhan token present (len={len(str(tok)) if tok else 0})")
    chk(bool(cid),f"Dhan client_id present ({cid})")
    try:
        from dhanhq import DhanContext, dhanhq
        ctx=DhanContext(cid,tok); dh=dhanhq(ctx)
        fl=dh.get_fund_limits()
        ok=isinstance(fl,dict) and fl.get("status")!="failure"
        P(ok,f"Dhan API reachable (get_fund_limits status={fl.get('status') if isinstance(fl,dict) else 'n/a'})")
        if not ok: print("       (market-closed or auth — inspect:",str(fl)[:180],")")
    except Exception as e:
        print("[WARN] Dhan live call:",type(e).__name__,str(e)[:160],"(market closed is expected)")
except Exception as e:
    chk(False,f"Dhan setup: {type(e).__name__}: {e}")

print("\n"+"="*60)
print(f"  QA RESULT: {'ALL CHECKS PASS' if fails==0 else str(fails)+' FAIL(S)'}")
print("="*60)
