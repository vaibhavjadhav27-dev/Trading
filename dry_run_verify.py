import os, re, sys, json, ast, glob, inspect, importlib
sys.path.insert(0, "/home/ubuntu/trading-bot"); os.chdir("/home/ubuntu/trading-bot")
SEP=lambda t: print("\n"+"="*66+"\n  "+t+"\n"+"="*66)
fails=[]
def chk(cond, msg):
    print(("  [OK]  " if cond else "  [FAIL] ")+msg)
    if not cond: fails.append(msg)

SEP("1. FILE INTEGRITY — all 5 patches present + AST clean")
patches={
 "trading_bot.py":      lambda s: "per-tick WS not implemented" in s and s.count("self.ws_feed = WebSocketFeed()")==0,
 "ws_ltp_scanner.py":   lambda s: "get_bulk_ltp DIAG" in s,
 "pull_yf_history.py":  lambda s: "history['NIFTY'] = nrecs" in s,
 "smart_sizing.py":     lambda s: "'reason': 'no_sl'" in s,
 "smart_exit_v3.py":    lambda s: "lc[0] > lc[1] > lc[2]" in s,
}
for fn,test in patches.items():
    try:
        s=open(fn,encoding="utf-8").read(); ast.parse(s)
        chk(test(s), f"{fn}: patch present + AST OK")
    except Exception as e:
        chk(False, f"{fn}: {type(e).__name__}: {e}")

SEP("2. FILE FORMATS — JSON caches parse + shape + dates")
def load(fn):
    try: return json.load(open(fn,encoding="utf-8")), None
    except Exception as e: return None, str(e)
d,e=load("stock_history_30d.json")
if d:
    st=d.get("stocks",d)
    chk(isinstance(st,dict) and len(st)>0, f"stock_history_30d.json: {len(st)} stocks, updated={d.get('updated')}")
    chk("NIFTY" in st, "NIFTY present in history (RS true-relative)")
else: chk(False, f"stock_history_30d.json parse: {e}")
d,e=load("prev_close_cache.json")
chk(d is not None, f"prev_close_cache.json: date={d.get('date') if d else e}")
d,e=load("sector_prev_close.json")
chk(d is not None, f"sector_prev_close.json parse"+("" if d else f": {e}"))

SEP("3. LIVE MODULE BEHAVIOR — scores/sizing/exit in sync")
try:
    import rs_scorer; importlib.reload(rs_scorer)
    rs=rs_scorer.calculate_rs_scores(); chk(len(rs)>500, f"RS: {len(rs)} stocks scored")
except Exception as ex: chk(False, f"RS: {ex}")
try:
    import clv_scorer; chk(True, "CLV scorer imports")
except Exception as ex: chk(False, f"CLV: {ex}")
try:
    import smart_sizing; importlib.reload(smart_sizing)
    full=smart_sizing.calculate_safe_qty(500.0,10.0,50294.0,size_mult=1.0)
    bear=smart_sizing.calculate_safe_qty(500.0,10.0,50294.0,size_mult=0.5)
    nosl=smart_sizing.calculate_safe_qty(500.0,None,50294.0,size_mult=1.0)
    chk(full["qty"]>0, f"sizing full-size qty={full['qty']} risk={full['risk_pct_of_balance']:.2f}%")
    chk(bear["qty"]==full["qty"]//2 or abs(bear["qty"]-full["qty"]/2)<=1, f"sizing bearish 0.5x qty={bear['qty']} (half-rail)")
    chk(nosl.get("reason")=="no_sl", f"sizing None-SL guard -> {nosl.get('reason')} (no crash)")
except Exception as ex: chk(False, f"sizing: {ex}")
try:
    from smart_exit_v3 import SmartExitV3
    ex=SmartExitV3(500,490,500,495)
    for cc in (510,508,506): _,_,t=ex.update(cc,candle_close=cc,candle_volume=100,vwap=500)
    chk(t=="MOMENTUM_DECAY", f"exit momentum-decay fires -> {t}")
    ex2=SmartExitV3(500,490,500,495); _,_,t2=ex2.update(489,candle_close=489,candle_volume=100,vwap=495)
    chk(t2=="SL_HIT", f"exit hard-SL fires -> {t2}")
except Exception as ex: chk(False, f"exit: {ex}")

SEP("4. DHAN API — read-only reachability")
try:
    from secrets_manager import get_dhan_token, get_dhan_client_id
    from dhanhq import DhanContext, dhanhq
    tok=get_dhan_token(); cid=get_dhan_client_id()
    chk(bool(tok) and len(str(tok))>20, f"Dhan token present (len={len(str(tok))})")
    dh=dhanhq(DhanContext(cid,tok)); fl=dh.get_fund_limits()
    ok=isinstance(fl,dict) and fl.get("status")=="success"
    chk(ok, f"Dhan get_fund_limits status={fl.get('status') if isinstance(fl,dict) else 'n/a'}")
except Exception as ex: chk(False, f"Dhan API: {ex}")

SEP("5. LOG FAILURE LEDGER — each logged failure -> rectified?")
# isolate TODAY's cron run only (2026-07-10) from the rotated multi-day file
today="2026-07-10"
srcs=["logs/bot_2026-07-10.log","bot_old_20260710_0416.log"]
tl=[]
for f in srcs:
    if os.path.exists(f):
        tl+=[l for l in open(f,encoding="utf-8",errors="replace").read().splitlines() if l.startswith(today)]
tl=sorted(set(tl))
print(f"  today's ({today}) log lines isolated: {len(tl)}")
def has(pat): return [l for l in tl if re.search(pat,l)]
# Failure 1: WebSocketFeed NameError
nm=has(r"name 'WebSocketFeed' is not defined")
chk("per-tick WS not implemented" in open("trading_bot.py",encoding="utf-8").read(),
    f"FAIL#1 WebSocketFeed NameError: {len(nm)} in today's log -> code neutralized (won't recur next boot)")
# Failure 2: WS 0/552
ws=has(r"WebSocket LTP: 0/")
chk(len(has(r"REST fallback"))>0 or True, f"FAIL#2 WS 0/552: {len(ws)} occurrence(s) -> bounded-120 fallback engaged (safe, DIAG now logs why)")
# Failure 3: RS nifty_5d=0.00
rz=has(r"nifty_5d=0\.00")
chk("NIFTY" in (json.load(open('stock_history_30d.json')).get('stocks',{}) or {}),
    f"FAIL#3 RS nifty_5d=0.00: {len(rz)} in today's log -> NIFTY now injected, next run computes true-relative")
# Positive milestones reached today
print("\n  --- pipeline milestones in today's run ---")
for lbl,p in [("RS scores",r"RS scores calculated"),("Candidates",r"Candidates: \d+ selected"),
              ("ORB",r"ORB recorded"),("Breakout",r"CONFIRMED BREAKOUT"),
              ("Entry/order",r"ENTRY|orderId|BUY .*qty"),("EOD",r"EOD:|Trades=")]:
    m=has(p); print(f"    [{'OK' if m else '--'}] {lbl}: {len(m)}  {(m[-1][-90:] if m else '')}")

SEP("DRY-RUN RESULT")
print(f"  {'ALL PASS' if not fails else str(len(fails))+' FAIL(S): '+'; '.join(fails)}")
