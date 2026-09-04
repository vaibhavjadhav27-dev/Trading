#!/usr/bin/env python3
# backtest_filters_v2.py  --  STEP 4a  (daily gap+EMA selection backtest, ZERO API)
#
# HONEST SCOPE: daily bars only. This validates the SELECTION-quality delta of the
# gap-window + EMA-alignment gates ONLY. It CANNOT test regime state (intraday VWAP)
# or RVol (intraday volume) -> those are validated by shadow-mode (Step 4b), NOT here.
#
# What it does: for every replay date D, compares three selection gates and reports,
# across all days, how each gate's picks behaved (continuation + best-case move):
#   LEGACY      : gap in [GAP_MIN, GAP_REJECT]                      (today's behavior)
#   V2_NORMAL   : gap >= GAP_MIN  AND open > ema20(as-of D-1)
#   V2_TRENDING : gap in [GAP_FLOOR_TRENDING, GAP_CEIL_TRENDING] AND open > ema5(as-of D-1)
#
# EMA is recomputed AS-OF each date from trailing closes through D-1 (mirrors how the
# 19:00 precompute would have looked the evening before) -> no lookahead.
#
# Metrics per gate: #picks, continuation% (close>open), avg open->close %, avg open->high %.
# Higher continuation% / avg open->close at similar pick-count = the EMA gate is helping.
#
# Run:  venv/bin/python3 backtest_filters_v2.py

import json, config
import pandas as pd

GAP_MIN   = getattr(config, "GAP_MIN", 0.3)
GAP_REJECT= getattr(config, "GAP_REJECT", 15.0)
PFLOOR    = getattr(config, "PRICE_FLOOR", 60)
PCEIL     = getattr(config, "PRICE_CEIL_TIER1", 5000)
GF_T      = getattr(config, "GAP_FLOOR_TRENDING", 2.5)
GC_T      = getattr(config, "GAP_CEIL_TRENDING", 7.5)
TOP_N     = 10

h = json.load(open("/home/ubuntu/trading-bot/stock_history_30d.json"))
stocks = h.get("stocks", h)

# ordered dates
all_dates = set()
for tkr, bars in stocks.items():
    if isinstance(bars, list):
        for b in bars:
            if isinstance(b, dict) and b.get("date"):
                all_dates.add(b["date"])
dates = sorted(all_dates)

# per-ticker date->bar and ordered close series
by_date = {}
for tkr, bars in stocks.items():
    if isinstance(bars, list):
        by_date[tkr] = {b["date"]: b for b in bars if isinstance(b, dict) and b.get("date")}

def ema_asof(tkr, upto_idx, span):
    """EMA of closes through dates[upto_idx] inclusive (as-of prev evening)."""
    bd = by_date.get(tkr, {})
    cs = []
    for k in range(0, upto_idx + 1):
        b = bd.get(dates[k])
        if b and b.get("close", 0) > 0:
            cs.append(b["close"])
    if len(cs) < 2:
        return None
    return float(pd.Series(cs).ewm(span=span, adjust=False).mean().iloc[-1])

def blank():
    return {"picks": 0, "cont": 0, "occ_sum": 0.0, "ooh_sum": 0.0}

def record(acc, o, hi, cl):
    acc["picks"] += 1
    if cl > o:
        acc["cont"] += 1
    acc["occ_sum"] += (cl - o) / o * 100
    acc["ooh_sum"] += (hi - o) / o * 100

LEG, VN, VT = blank(), blank(), blank()

for di in range(1, len(dates)):
    D, Dprev = dates[di], dates[di - 1]
    rows = []
    for tkr, bd in by_date.items():
        if D not in bd or Dprev not in bd:
            continue
        pc = bd[Dprev].get("close"); o = bd[D].get("open")
        hi = bd[D].get("high"); cl = bd[D].get("close")
        if not (pc and o and hi and cl):
            continue
        if o < PFLOOR or o > PCEIL:
            continue
        gap = (o - pc) / pc * 100
        rows.append((tkr, gap, o, hi, cl))
    # rank by gap desc, take top-N per gate (mirrors candidates[:10])
    rows.sort(key=lambda r: -r[1])

    leg = [r for r in rows if GAP_MIN <= r[1] <= GAP_REJECT][:TOP_N]
    for tkr, gap, o, hi, cl in leg:
        record(LEG, o, hi, cl)

    vn = []
    for tkr, gap, o, hi, cl in rows:
        if gap < GAP_MIN or gap > GAP_REJECT:
            continue
        e20 = ema_asof(tkr, di - 1, 20)
        if e20 is not None and o <= e20:
            continue
        vn.append((tkr, gap, o, hi, cl))
    for tkr, gap, o, hi, cl in vn[:TOP_N]:
        record(VN, o, hi, cl)

    vt = []
    for tkr, gap, o, hi, cl in rows:
        if not (GF_T <= gap <= GC_T):
            continue
        e5 = ema_asof(tkr, di - 1, 5)
        if e5 is not None and o <= e5:
            continue
        vt.append((tkr, gap, o, hi, cl))
    for tkr, gap, o, hi, cl in vt[:TOP_N]:
        record(VT, o, hi, cl)

def summarize(name, a):
    n = a["picks"]
    if n == 0:
        print("  %-12s picks=0  (no candidates matched)" % name)
        return
    print("  %-12s picks=%-4d cont=%5.1f%%  avg open->close=%+6.2f%%  avg open->high=%+6.2f%%"
          % (name, n, 100.0 * a["cont"] / n, a["occ_sum"] / n, a["ooh_sum"] / n))

print("=" * 78)
print("FILTERS_V2 daily gap+EMA selection backtest  (%d replay days, top-%d/day)"
      % (len(dates) - 1, TOP_N))
print("HONEST: gap-window + EMA-alignment ONLY. Regime state & RVol NOT tested here")
print("        (intraday) -> validated by shadow-mode. No lookahead (EMA as-of D-1).")
print("=" * 78)
summarize("LEGACY", LEG)
summarize("V2_NORMAL", VN)
summarize("V2_TRENDING", VT)
print("=" * 78)
print("Read: if V2_* shows higher cont%% / avg open->close at similar pick counts,")
print("      the EMA-alignment gate is improving selection quality vs legacy gap-only.")
