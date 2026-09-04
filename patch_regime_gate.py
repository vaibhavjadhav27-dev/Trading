import ast, shutil, datetime, sys

def patch(F, edits):
    src = open(F, encoding="utf-8").read()
    bak = f"{F}.bak_regime_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy(F, bak); print(f"[backup] {bak}")
    for name, old, new in edits:
        n = src.count(old)
        if n != 1:
            print(f"[ABORT {F}] anchor '{name}' matched {n}x (need 1) - no changes"); sys.exit(1)
        src = src.replace(old, new); print(f"  [ok] {F}: {name}")
    try:
        ast.parse(src)
    except SyntaxError as e:
        print(f"[ABORT {F}] does NOT parse: {e} - restore {bak}"); sys.exit(1)
    open(F, "w", encoding="utf-8").write(src)
    print(f"[DONE] {F} patched + AST-verified")

# ---------- swing_daily.py : entry gate ----------
daily = [
 ("BEARISH_SCORE_THRESHOLD const",
  '''RISK_PER_TRADE = 0.02
SCORE_THRESHOLD = 60''',
  '''RISK_PER_TRADE = 0.02
SCORE_THRESHOLD = 60
BEARISH_SCORE_THRESHOLD = 80   # raised score bar when NIFTY < SMA20 (regime gate)'''),

 ("entry regime gate",
  '''    if available_slots <= 0:
        log.info("Max positions reached. No new entries.")
        return []
    active_tickers = [p["ticker"] for p in positions["active"]]
    new_entries = []''',
  '''    if available_slots <= 0:
        log.info("Max positions reached. No new entries.")
        return []
    # === NIFTY REGIME GATE: raise score bar in bearish market (fail-open to normal) ===
    try:
        from swing_regime import nifty_trend
        _nt = nifty_trend()
        if _nt["regime"] == "BEARISH":
            _eff_threshold = BEARISH_SCORE_THRESHOLD
            log.info("SWING REGIME: NIFTY BEARISH (close %.1f < SMA20 %.1f, slope10 %+.2f%%) -> score bar %d->%d"
                     % (_nt["close"], _nt["sma20"], _nt["slope10"], SCORE_THRESHOLD, BEARISH_SCORE_THRESHOLD))
        else:
            _eff_threshold = SCORE_THRESHOLD
            log.info("SWING REGIME: NIFTY %s (close %.1f vs SMA20 %.1f, slope10 %+.2f%%) -> normal bar %d"
                     % (_nt["regime"], _nt["close"], _nt["sma20"], _nt["slope10"], SCORE_THRESHOLD))
    except Exception as _re:
        _eff_threshold = SCORE_THRESHOLD
        log.warning("SWING REGIME: trend check failed (%s) -> normal bar %d" % (_re, SCORE_THRESHOLD))
    active_tickers = [p["ticker"] for p in positions["active"]]
    new_entries = []'''),

 ("threshold check uses effective bar",
  '''        if c["score"] < SCORE_THRESHOLD:
            continue''',
  '''        if c["score"] < _eff_threshold:
            continue'''),
]

# ---------- swing_monitor.py : exit overlay ----------
monitor = [
 ("monitor regime flag",
  '''    log.info("Monitoring " + str(len(positions["active"])) + " active positions")''',
  '''    log.info("Monitoring " + str(len(positions["active"])) + " active positions")
    # === NIFTY REGIME EXIT OVERLAY: tighter give-back when market bearish (fail-open) ===
    try:
        from swing_regime import nifty_trend
        _nt = nifty_trend()
        _bearish = (_nt["regime"] == "BEARISH")
        log.info("SWING REGIME (monitor): NIFTY %s close=%.1f sma20=%.1f slope10=%+.2f%%"
                 % (_nt["regime"], _nt["close"], _nt["sma20"], _nt["slope10"]))
    except Exception as _re:
        _bearish = False
        log.warning("SWING REGIME (monitor): trend check failed (%s) -> no overlay" % _re)'''),

 ("bearish-tightened lock trail",
  '''        if gain_pct >= TARGET_GAIN_PCT:
            # +15% reached: lock at least +15%, trail tightly below peak, NO upper cap
            lock_trail = max(peak * LOCK_TRAIL_PCT, entry * (1 + TARGET_GAIN_PCT / 100))''',
  '''        if gain_pct >= TARGET_GAIN_PCT:
            # +15% reached: lock at least +15%, trail tightly below peak, NO upper cap
            # REGIME OVERLAY: bearish -> tighter give-back (0.97 vs 0.93); only tightens, hard SL untouched
            _ltf = 0.97 if _bearish else LOCK_TRAIL_PCT
            lock_trail = max(peak * _ltf, entry * (1 + TARGET_GAIN_PCT / 100))'''),
]

patch("swing_daily.py", daily)
patch("swing_monitor.py", monitor)
print("\\n[ALL DONE] regime gate applied to both scripts.")
