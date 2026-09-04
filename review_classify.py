import json, gzip, os
os.chdir(os.path.expanduser("~/trading-bot"))
try:
    import config
    GAP_MIN   = getattr(config, "GAP_MIN", 1.0)
    PRICE_MIN = getattr(config, "PRICE_MIN", getattr(config, "MIN_PRICE", 60.0))
except Exception:
    GAP_MIN, PRICE_MIN = 1.0, 60.0
GAP_SETUP = 0.3  # min opening gap to be an ORB gap-up setup at all
print(f"config.GAP_MIN={GAP_MIN} (shortlist gate)  PRICE_MIN={PRICE_MIN}  gap-up setup floor={GAP_SETUP}%")

def L(p):
    try: return json.load(gzip.open(p)) if p.endswith(".gz") else json.load(open(p))
    except Exception: return None

for d in ("2026-07-15", "2026-07-16"):
    a = L(f"candle_archive/{d}.json.gz")
    if not isinstance(a, dict): 
        print(f"\n{d}: no archive"); continue
    gainers = a.get("gainers") or []
    print("\n" + "="*84)
    print(f"{d}  —  {len(gainers)} REAL captured gainers")
    print("="*84)
    print(f"{'Symbol':<14}{'chg%':>7}{'open_gap%':>11}{'ltp':>9}  Classification")
    print("-"*84)
    setup, notsetup = [], []
    for g in gainers:
        sym = g.get("symbol"); ltp = g.get("ltp"); pc = g.get("prev_close")
        chg = g.get("change")
        cndl = g.get("candles") or {}
        op = (cndl.get("o") or [None])[0]
        if op and pc:
            ogap = (op - pc)/pc*100
        else:
            ogap = None
        # classify: was this even an ORB gap-up setup?
        if ogap is None:
            cls, why = "UNKNOWN", "no open candle"
        elif ltp and ltp < PRICE_MIN:
            cls, why = "not-our-setup", f"price<{PRICE_MIN}"
        elif ogap < GAP_SETUP:
            cls, why = "not-our-setup", f"opened flat/down ({ogap:+.2f}%) — ran intraday"
        else:
            cls, why = "OUR SETUP", f"gapped +{ogap:.2f}% at open"
        (setup if cls=="OUR SETUP" else notsetup).append(sym)
        og = f"{ogap:+.2f}" if ogap is not None else "  n/a"
        print(f"{sym:<14}{(chg or 0):>7.2f}{og:>11}{(ltp or 0):>9.1f}  {cls:<14} {why}")
    print("-"*84)
    n = len(gainers)
    print(f"OUR SETUP (gapped up, tradeable): {len(setup)}/{n}  -> {', '.join(setup) or '-'}")
    print(f"NOT our setup (flat/down open or <price floor): {len(notsetup)}/{n}  -> {', '.join(notsetup) or '-'}")
    print(f"  => Of {n} gainers, only {len(setup)} were structurally catchable by an ORB gap-up filter.")
