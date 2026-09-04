#!/usr/bin/env python3
"""End-to-end readiness check for the ORB trading bot. Read-only, places NO orders."""
import os, ast, json, glob, subprocess, datetime as dt

PASS, FAIL, WARN = [], [], []
def ok(m):   PASS.append(m); print(f"  [OK]   {m}")
def bad(m):  FAIL.append(m); print(f"  [FAIL] {m}")
def warn(m): WARN.append(m); print(f"  [WARN] {m}")

print("="*70); print("  ORB BOT - END-TO-END PREFLIGHT"); print("="*70)

print("\n1) SYNTAX (all .py files)")
_synfail=False
for f in sorted(glob.glob("*.py")):
    try: ast.parse(open(f, encoding="utf-8").read())
    except SyntaxError as e: bad(f"{f}: line {e.lineno} {e.msg}"); _synfail=True
if not _synfail: ok("all .py files compile")

print("\n2) IMPORTS (core modules load)")
for m in ["config","trading_bot","smart_sizing","sector_rotation","dhan_charges",
          "gainer_enrichment","post_market_analysis","save_sector_prev_close",
          "candidate_logger","secrets_manager"]:
    try: __import__(m); ok(f"import {m}")
    except Exception as e: bad(f"import {m}: {type(e).__name__}: {str(e)[:80]}")

print("\n3) CONFIG (filter values practical?)")
try:
    import config as C
    checks = [
        ("PRICE_FLOOR", getattr(C,"PRICE_FLOOR",None), lambda v: 20<=v<=200),
        ("GAP_MIN", getattr(C,"GAP_MIN",None), lambda v: 0.1<=v<=1.0),
        ("GAP_REJECT", getattr(C,"GAP_REJECT",None), lambda v: 5<=v<=25),
        ("ORB_MIN_RANGE_PCT", getattr(C,"ORB_MIN_RANGE_PCT",None), lambda v: 0.3<=v<=1.5),
        ("ORB_MAX_RANGE_PCT", getattr(C,"ORB_MAX_RANGE_PCT",None), lambda v: 2<=v<=6),
        ("RISK_PER_TRADE_PCT", getattr(C,"RISK_PER_TRADE_PCT",None), lambda v: 0.5<=v<=3),
        ("MAX_POSITION_PCT", getattr(C,"MAX_POSITION_PCT",None), lambda v: 20<=v<=60),
        ("BEARISH_LIVE_ENABLED", getattr(C,"BEARISH_LIVE_ENABLED",None), lambda v: isinstance(v,bool)),
    ]
    for name, val, test in checks:
        if val is None: warn(f"{name} missing")
        elif test(val): ok(f"{name} = {val}")
        else: warn(f"{name} = {val} (outside typical range - confirm intentional)")
except Exception as e: bad(f"config load: {e}")

print("\n4) DATA FILES")
def check_json(path, min_keys=1):
    if not os.path.exists(path): return bad(f"{path} MISSING")
    try:
        d = json.load(open(path)); n = len(d.get("data", d)) if isinstance(d,dict) else len(d)
        age_h = (dt.datetime.now()-dt.datetime.fromtimestamp(os.path.getmtime(path))).total_seconds()/3600
        (ok if n>=min_keys else warn)(f"{path}: {n} entries, {age_h:.0f}h old")
    except Exception as e: bad(f"{path}: {e}")
check_json("prev_close_cache.json", 400)
check_json("sector_prev_close.json", 10)
if os.path.exists("watchlist.csv"):
    hdr = open("watchlist.csv").readline().strip()
    ok(f"watchlist.csv header: {hdr}") if "sector" in hdr else warn(f"watchlist.csv no 'sector' col: {hdr}")
else: bad("watchlist.csv MISSING")

print("\n5) SECRETS & SECURITY")
try:
    from secrets_manager import get_dhan_token
    tok = get_dhan_token()
    ok("Dhan token from SSM") if tok and len(str(tok))>20 else bad("token empty/short")
except Exception as e: bad(f"token fetch: {e}")
import re
_leaks=0
for f in glob.glob("*.py"):
    txt=open(f,encoding="utf-8",errors="ignore").read()
    if re.search(r'(access[_-]?token|api[_-]?key|password)\s*=\s*["\'][A-Za-z0-9]{20,}', txt, re.I):
        warn(f"possible hardcoded secret in {f}"); _leaks+=1
if not _leaks: ok("no hardcoded secrets in .py files")

print("\n6) DHAN API (connectivity + timeout)")
try:
    from trading_bot import DhanClient
    from secrets_manager import get_dhan_token
    import time
    dc = DhanClient()
    t0=time.time()
    d = dc.get_ohlc_intraday("13","IDX_I","5")
    dt_ms=(time.time()-t0)*1000
    c = d.get("close") if isinstance(d,dict) else None
    if c: ok(f"NIFTY quote OK ({c[-1]}), {dt_ms:.0f}ms")
    else: bad("API returned no data for NIFTY")
except Exception as e: bad(f"API call: {type(e).__name__}: {str(e)[:80]}")

print("\n7) CRON SCHEDULE (UTC -> IST ordering)")
try:
    cron = subprocess.getoutput("crontab -l")
    def ist(h,m):
        t=(h*60+m+330)%1440; return f"{t//60:02d}:{t%60:02d}"
    order=[]
    for ln in cron.splitlines():
        if ln.strip().startswith("#") or not ln.strip(): continue
        mm=re.match(r"(\d+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+.*?(\w+\.py)", ln)
        if mm:
            order.append((ist(int(mm.group(2)),int(mm.group(1))), mm.group(3)))
    for t,s in sorted(order): print(f"     {t} IST  {s}")
    seq={s:t for t,s in order}
    def before(a,b): return a in seq and b in seq and seq[a]<seq[b]
    (ok if before("token_refresher.py","trading_bot.py") else bad)("token refresh before bot")
    (ok if before("save_prev_session_close.py","trading_bot.py") else bad)("stock prev_close before bot")
    (ok if before("save_sector_prev_close.py","trading_bot.py") else bad)("sector cache before bot")
except Exception as e: warn(f"cron parse: {e}")

print("\n"+"="*70)
print(f"  RESULT: {len(PASS)} pass | {len(WARN)} warn | {len(FAIL)} FAIL")
print("="*70)
if FAIL:
    print("  BLOCKERS:"); [print(f"   - {m}") for m in FAIL]
    print("  -> Do NOT enable live trading until FAILs are resolved.")
elif WARN:
    print("  Review WARNs, but no hard blockers.")
else:
    print("  All green.")
