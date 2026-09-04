#!/usr/bin/env python3
# dryrun_shadow.py  --  LOCAL dry-run of the FILTERS_V2 shadow path (zero API, no trading)
#
# PURPOSE: prove the shadow PLUMBING is sound BEFORE tomorrow's live session, so we don't
# discover an empty filters_v2_shadow.log tomorrow afternoon. Builds real-schema candidates
# from stock_history_30d.json, runs apply_regime_filters(force=True) through EVERY state with
# a SYNTHETIC rvol_fn, writes to a THROWAWAY log (NOT the real one), and prints per-gate drops.
#
# HONEST LIMITS (says so out loud):
#   - RVol here is SYNTHETIC (hash-based) -> exercises the recording path, NOT real volume.
#   - Regime state is INJECTED (we loop all 4) -> proves each branch runs, NOT which fires live.
#   - _passes_ema uses your REAL precomputed EMA -> if EMA fields are missing, gates drop to 0
#     and the script SAYS SO (so 0-kept reads as "no EMA data", not a mystery bug).
# This validates plumbing only. Real selection quality still comes from live shadow + backtest.
#
# Run:  cd ~/trading-bot && venv/bin/python3 dryrun_shadow.py

import json, importlib
import filters_v2, config

REAL_LOG = "/home/ubuntu/trading-bot/filters_v2_shadow.log"
DRY_LOG  = "/home/ubuntu/trading-bot/dryrun_shadow.log"  # throwaway; real log stays clean
HIST     = "/home/ubuntu/trading-bot/stock_history_30d.json"

# ---- build real-schema candidates from the daily history file ----
h = json.load(open(HIST))
stocks = h.get("stocks", h)

cands = []
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
    cands.append({"ticker": tkr, "ltp": float(cl), "gap_pct": round(gap, 3)})

# spread across gap ranges so different states keep different names; cap at 30 for readability
cands.sort(key=lambda c: -c["gap_pct"])
sample = cands[:15] + cands[-15:] if len(cands) > 30 else cands
print("Built %d candidates from %s (showing gates over %d)" % (len(cands), HIST, len(sample)))

# ---- synthetic, DETERMINISTIC rvol so runs are repeatable; spans below & above thresholds ----
def rvol_fn(c):
    base = (abs(hash(c["ticker"])) % 700) / 100.0 + 1.0   # 1.0 .. 8.0
    return round(base, 2)

# ---- clear throwaway log ----
open(DRY_LOG, "w").close()

STATES = ["TRENDING", "NORMAL", "BEARISH-DEFENSIVE", "CHOPPY"]
print("=" * 78)
print("FILTERS_V2 SHADOW DRY-RUN  (force=True, synthetic RVol, throwaway log)")
print("log -> %s   (real %s untouched)" % (DRY_LOG, REAL_LOG))
print("=" * 78)

for st in STATES:
    before = [dict(c) for c in sample]   # fresh copies (filter may set _size_mult)
    try:
        kept = filters_v2.apply_regime_filters(before, st, config, rvol_fn=rvol_fn, force=True)
    except Exception as e:
        print("  %-18s ERROR: %r  <-- plumbing bug, fix before live shadow" % (st, e))
        continue
    # shadow write (throwaway path)
    try:
        filters_v2.shadow_log(st, before, kept, rvol_fn=rvol_fn, path=DRY_LOG)
    except TypeError:
        # older shadow_log without path kw -> skip write, still report counts
        print("  (shadow_log has no path kw; skipping write for %s)" % st)
    kept_tk = set(id(c) for c in kept)
    print("  %-18s kept %2d / %2d" % (st, len(kept), len(before)))

print("=" * 78)
print("Sample shadow log lines (first 3):")
try:
    for i, line in enumerate(open(DRY_LOG)):
        if i >= 3: break
        print("  " + line.rstrip())
except FileNotFoundError:
    print("  (no log written)")

print("=" * 78)
print("READ THIS:")
print("  * Every state prints a count and NO ERROR  -> plumbing is sound; safe for live shadow.")
print("  * TRENDING/NORMAL kept 0 with candidates present -> EMA fields likely MISSING from")
print("    stock_history_30d.json (Step-2 precompute) OR synthetic RVol below threshold.")
print("  * CHOPPY should keep 0 (hard pause) and log a would-have list -> that's CORRECT.")
print("  * This is PLUMBING validation only. Real RVol/regime come from tomorrow's live shadow.")
print("  * Throwaway log: rm %s when done." % DRY_LOG)
