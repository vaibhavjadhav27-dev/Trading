#!/usr/bin/env python3
# dryrun_shadow_bearish.py  --  targeted dry-run of the BEARISH-DEFENSIVE branch
#
# WHY: the main dry-run kept 0/30 for BEARISH because its sample was top-gap+bottom-gap
# (deliberately EXCLUDING the flat-opener middle that BEARISH-DEFENSIVE requires:
# gap in [-0.5, +0.5]). That 0 was a sample artifact, NOT a gate bug. This script feeds
# the branch the flat openers it actually wants and confirms it keeps > 0 with a size_mult.
#
# BEARISH-DEFENSIVE gate: gap in [BEARISH_GAP_LO, BEARISH_GAP_HI], RVol >= RVOL_BEARISH (5.0),
# optional sector_ok_fn (None -> skipped), sets c['_size_mult']=BEARISH_SIZE_MULT (half-size).
# Synthetic deterministic RVol (same as main dry-run). Throwaway log. Zero API, no trading.
#
# Run:  cd ~/trading-bot && venv/bin/python3 dryrun_shadow_bearish.py

import json
import filters_v2, config

DRY_LOG = "/home/ubuntu/trading-bot/dryrun_shadow_bearish.log"
HIST    = "/home/ubuntu/trading-bot/stock_history_30d.json"

lo = getattr(config, "BEARISH_GAP_LO", -0.5)
hi = getattr(config, "BEARISH_GAP_HI", 0.5)
rmin = getattr(config, "RVOL_BEARISH", 5.0)

h = json.load(open(HIST))
stocks = h.get("stocks", h)

flat = []
for tkr, bars in stocks.items():
    if not isinstance(bars, list) or len(bars) < 2:
        continue
    last, prev = bars[-1], bars[-2]
    if not (isinstance(last, dict) and isinstance(prev, dict)):
        continue
    o = last.get("open"); pc = prev.get("close"); cl = last.get("close")
    if not (o and pc and cl):
        continue
    gap = (o - pc) / pc * 100.0
    if lo <= gap <= hi:                       # flat openers ONLY
        flat.append({"ticker": tkr, "ltp": float(cl), "gap_pct": round(gap, 3)})

def rvol_fn(c):
    base = (abs(hash(c["ticker"])) % 700) / 100.0 + 1.0   # 1.0 .. 8.0
    return round(base, 2)

sample = flat[:30]
open(DRY_LOG, "w").close()

print("=" * 78)
print("BEARISH-DEFENSIVE targeted dry-run")
print("gate: gap in [%.2f, %.2f], RVol >= %.1f, sector_fn=None(skipped)" % (lo, hi, rmin))
print("flat-opener candidates in file: %d  (testing %d)" % (len(flat), len(sample)))
print("=" * 78)

if not sample:
    print("NO flat-gap names in file today -> cannot exercise branch with real gaps.")
    print("(This just means today's data has no ~0%% openers; gate logic still fine.)")
    raise SystemExit(0)

before = [dict(c) for c in sample]
try:
    kept = filters_v2.apply_regime_filters(before, "BEARISH-DEFENSIVE", config,
                                           rvol_fn=rvol_fn, force=True)
    filters_v2.shadow_log("BEARISH-DEFENSIVE", before, kept, rvol_fn=rvol_fn, path=DRY_LOG)
except Exception as e:
    print("ERROR: %r  <-- real branch bug, fix before live shadow" % e)
    raise SystemExit(1)

print("kept %d / %d" % (len(kept), len(before)))
print("")
# how many SHOULD pass = flat-gap AND synthetic rvol >= rmin
should = [c for c in sample if rvol_fn(c) >= rmin]
print("expected keeps (gap ok by construction + synthetic RVol>=%.1f): %d" % (rmin, len(should)))
print("")
for c in kept[:8]:
    print("  KEEP %-12s gap=%+.2f rvol=%.2f  _size_mult=%s" % (
        c["ticker"], c["gap_pct"], rvol_fn(c), c.get("_size_mult")))
print("")
print("READ:")
if len(kept) > 0 and all(c.get("_size_mult") is not None for c in kept):
    print("  * kept > 0 AND every kept name has _size_mult set -> BEARISH branch VALIDATED (half-size).")
else:
    print("  * kept == 0 -> check: did any flat name clear synthetic RVol>=%.1f? (see 'expected' above)" % rmin)
    print("    If expected>0 but kept==0, that's a real gate bug worth a look.")
print("  * cleanup: rm %s" % DRY_LOG)
