import os, re, sys, json, glob, inspect, logging, traceback
from datetime import datetime, timedelta
logging.basicConfig(level=logging.INFO, format='%(message)s')
BASE="/home/ubuntu/trading-bot"
SEP=lambda t: print("\n"+"="*64+"\n  "+t+"\n"+"="*64)

# ============================================================
SEP("SECTION 1 — RECONSTRUCT TODAY'S RUN FROM bot.log")
# ============================================================
log_path=os.path.join(BASE,"bot.log")
try:
    lines=open(log_path,encoding="utf-8",errors="replace").read().splitlines()
    today=datetime.now().strftime("%Y-%m-%d")
    tl=[l for l in lines if today in l] or lines[-400:]
    def find(pats):
        out=[]
        for l in tl:
            if any(re.search(p,l) for p in pats): out.append(l)
        return out
    def last(pats):
        m=find(pats); return m[-1] if m else None
    print(f"log lines today: {len(tl)} (of {len(lines)} total)")
    for label,pats in [
        ("Startup",       [r"Trading Bot v", r"Bot starting", r"__init__"]),
        ("Cache loaded",  [r"Cache loaded", r"prev_close"]),
        ("RS scores",     [r"RS scores calculated"]),
        ("CLV scores",    [r"CLV scores calculated"]),
        ("WS LTP",        [r"WebSocket LTP", r"get_bulk_ltp", r"WebSocket LTP:"]),
        ("REST fallback", [r"REST fallback", r"bounded to 120"]),
        ("Candidates",    [r"Candidates: \d+ selected", r"selected from"]),
        ("ORB",           [r"Recording ORB", r"ORB recorded"]),
        ("Scan loop",     [r"scan", r"Scanning", r"CONFIRMED BREAKOUT"]),
        ("Entries",       [r"ENTRY", r"orderId", r"place_order", r"BUY .*qty"]),
        ("Exits",         [r"SL_HIT|TRAIL_SL|VWAP_LOSS|ORB_REBREAK|MOMENTUM_DECAY|DEAD_TRADE|EXIT"]),
        ("Errors",        [r"ERROR", r"Traceback", r"NameError", r"NoneType"]),
        ("EOD",           [r"EOD", r"Trades=", r"PnL="]),
    ]:
        m=find(pats)
        tag="✅" if m else "—"
        print(f"\n[{tag}] {label}: {len(m)} line(s)")
        for l in m[-3:]: print("     "+l[-160:])
    # verdict on why 0 trades
    print("\n--- WHY TODAY'S OUTCOME ---")
    cand=last([r"Candidates: \d+ selected"]); brk=find([r"CONFIRMED BREAKOUT"]); ent=find([r"ENTRY|orderId"])
    print("  candidates:", cand[-90:] if cand else "none logged")
    print("  breakouts :", len(brk), "| entries:", len(ent))
    if not ent:
        print("  VERDICT: no entry fired — either no ORB breakout confirmed, or WS 0/552 → fallback universe.")
except Exception as e:
    print("SECTION 1 error:", e); traceback.print_exc()

# ============================================================
SEP("SECTION 2 — PIPELINE ORCHESTRATION + SIZING (NO ORDERS)")
# ============================================================
try:
    sys.path.insert(0, BASE); os.chdir(BASE)
    import importlib
    # 2a. scores compute live
    import rs_scorer; importlib.reload(rs_scorer)
    try:
        rs=rs_scorer.calculate_rs_scores()
        print(f"[✅] RS scores: {len(rs)} stocks")
    except Exception as e:
        # try with watchlist sids
        print(f"[i] RS no-arg failed ({e}); trying with sids")
        rs={}
    try:
        import clv_scorer
        print("[✅] clv_scorer imports")
    except Exception as e:
        print("[—] clv_scorer:", e)
    # 2b. sizing — introspect real signature, simulate ONE candidate, place NOTHING
    import smart_sizing; importlib.reload(smart_sizing)
    sig=inspect.signature(smart_sizing.calculate_safe_qty)
    print(f"\n[i] calculate_safe_qty{sig}")
    # build kwargs from common param names
    balance=50294.0; entry=500.0; sl=490.0
    trials=[
        dict(entry_price=entry, sl_price=sl, balance=balance),
        dict(entry_price=entry, stop_price=sl, balance=balance),
        dict(price=entry, sl=sl, capital=balance),
    ]
    done=False
    for kw in trials:
        kw2={k:v for k,v in kw.items() if k in sig.parameters}
        if len(kw2)>=2:
            # add size_mult if supported
            if "size_mult" in sig.parameters: kw2["size_mult"]=1.0
            try:
                q=smart_sizing.calculate_safe_qty(**kw2)
                print(f"[✅] sizing full-size {kw2} -> qty={q}")
                if "size_mult" in sig.parameters:
                    kw2["size_mult"]=0.5
                    qh=smart_sizing.calculate_safe_qty(**kw2)
                    print(f"[✅] sizing bearish 0.5x -> qty={qh}")
                done=True; break
            except Exception as e:
                print(f"[i] trial {kw2} -> {type(e).__name__}: {e}")
    if not done:
        print("[!] Could not auto-match sizing args — paste calculate_safe_qty signature and I'll fix the call.")
    # 2c. None-guard: ensure sizing doesn't crash on missing SL
    try:
        kwn={k:(None if k in("sl_price","stop_price","sl") else (entry if "price" in k or k=="entry_price" else balance)) for k in sig.parameters if k!="size_mult"}
        qn=smart_sizing.calculate_safe_qty(**{k:v for k,v in kwn.items()})
        print(f"[✅] None-SL guard: returned {qn} (no crash)")
    except TypeError as e:
        print(f"[i] None-SL guard: TypeError (expected if SL required): {e}")
    except Exception as e:
        print(f"[!] None-SL guard CRASHED: {type(e).__name__}: {e}")
except Exception as e:
    print("SECTION 2 error:", e); traceback.print_exc()

# ============================================================
SEP("SECTION 3 — EXIT-LOGIC REPLAY (SmartExitV3)")
# ============================================================
try:
    from smart_exit_v3 import SmartExitV3
    # scenario A: winner that trails up then gives back -> TRAIL_SL
    entry, sl, orbh, orbl = 500.0, 490.0, 500.0, 495.0   # R=10
    def replay(name, ticks):
        ex=SmartExitV3(entry, sl, orbh, orbl)
        print(f"\n--- {name} (entry={entry}, SL={sl}, R={ex.r_value}) ---")
        for (ltp, cc, cv, vw) in ticks:
            done,msg,tag=ex.update(ltp, candle_close=cc, candle_volume=cv, vwap=vw)
            print(f"  ltp={ltp:>6} close={cc} R={((ltp-entry)/ex.r_value):+.2f} best={ex.best_r:.2f} trail={ex.trail_sl:.1f} -> {tag}: {msg[:60]}")
            if done: return tag
        return "NO_EXIT"
    # winner: climbs to 3R (trail to +2.5R=525), then drops to 524 -> TRAIL_SL
    r=replay("A: winner trails then TRAIL_SL",
        [(505,505,1000,498),(515,515,1000,505),(520,520,1000,510),
         (530,530,1000,515),(524,524,1000,518)])
    print(f"  => exit tag: {r}")
    # loser: straight to SL
    r=replay("B: hard SL", [(497,497,1000,499),(492,492,1000,498),(489,489,1000,497)])
    print(f"  => exit tag: {r}")
    # dead trade: flat 31 min (simulate by forcing entry_time back)
    ex=SmartExitV3(entry,sl,orbh,orbl); ex.entry_time=datetime.now()-timedelta(minutes=31)
    d,m,t=ex.update(500.5, candle_close=500.5, candle_volume=100, vwap=501.0)
    print(f"\n--- C: dead-trade (31min, flat, below VWAP) --- -> {t}: {m}")
    # momentum-decay branch bug check
    ex=SmartExitV3(entry,sl,orbh,orbl)
    for cc in (510,508,506):  # 3 lower closes, <1.5R
        d,m,t=ex.update(cc, candle_close=cc, candle_volume=100, vwap=500)
    print(f"\n--- D: 3 lower closes (should be MOMENTUM_DECAY) --- -> got {t}")
    print("    NOTE: line 59 'if lc>lc>lc' compares list to itself = always False —")
    print("          MOMENTUM_DECAY branch is DEAD (never fires). Confirmed:", t!="MOMENTUM_DECAY")
except Exception as e:
    print("SECTION 3 error:", e); traceback.print_exc()

SEP("FULL-FLOW TEST COMPLETE")
