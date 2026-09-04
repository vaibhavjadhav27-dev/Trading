#!/usr/bin/env python3
# add_precompute_metrics.py  --  STEP 2 (FILTERS_V2 pre-compute layer)
#
# Extends the EXISTING metrics builder in pull_yf_history.py (writes stock_metrics.json)
# with FILTERS_V2 fields: ema5, ema20, adv_20d, structural_high_5d, prev_day_high.
#
# EXTEND, not replace: keeps rs_5d / avg_vol_10d / latest_close / days_available intact
# so any current reader of stock_metrics.json is unaffected.
#
# DATA LIMIT: yf period='1mo' ~= 22 sessions. ema5/ema20/adv_20d/5d_high/PDH OK.
# Per-stock ema50 deliberately NOT computed (needs 50 sessions; not used in filters).
#
# SAFETY: two exact-string edits, each must match EXACTLY once. Backup + py_compile
# + auto-rollback. Run with venv/bin/python3 (needs pandas):
#   venv/bin/python3 add_precompute_metrics.py

import shutil, py_compile, sys, datetime

F = "/home/ubuntu/trading-bot/pull_yf_history.py"
src = open(F).read()

# --- Edit A: after latest_close, compute EMAs / ADV / highs ---
OLD_A = "            # Latest close\n            latest_close = closes[-1]\n"
NEW_A = (
    "            # Latest close\n"
    "            latest_close = closes[-1]\n"
    "            # FILTERS_V2 pre-compute (2026-07-14): EMAs, ADV, structural high, PDH.\n"
    "            # period='1mo' ~= 22 sessions -> ema5/ema20/adv_20d OK. Per-stock ema50\n"
    "            # NOT computed (insufficient history; not used in filters).\n"
    "            _highs = [r['high'] for r in records if r['high'] > 0]\n"
    "            _cs = pd.Series(closes)\n"
    "            _ema5 = round(float(_cs.ewm(span=5, adjust=False).mean().iloc[-1]), 2)\n"
    "            _ema20 = round(float(_cs.ewm(span=20, adjust=False).mean().iloc[-1]), 2) if len(closes) >= 20 else _ema5\n"
    "            _adv20 = int(sum(volumes[-20:]) / len(volumes[-20:])) if len(volumes) >= 20 else int(avg_vol)\n"
    "            _shigh5 = round(max(_highs[-5:]), 2) if len(_highs) >= 5 else (round(max(_highs), 2) if _highs else 0)\n"
    "            _pdh = round(_highs[-1], 2) if _highs else 0\n"
)

# --- Edit B: extend the metrics dict with the new fields ---
OLD_B = (
    "            metrics[ticker] = {\n"
    "                'rs_5d': round(rs_5d, 2),\n"
    "                'avg_vol_10d': int(avg_vol),\n"
    "                'latest_close': latest_close,\n"
    "                'days_available': len(records)\n"
    "            }"
)
NEW_B = (
    "            metrics[ticker] = {\n"
    "                'rs_5d': round(rs_5d, 2),\n"
    "                'avg_vol_10d': int(avg_vol),\n"
    "                'latest_close': latest_close,\n"
    "                'days_available': len(records),\n"
    "                'ema5': _ema5,\n"
    "                'ema20': _ema20,\n"
    "                'adv_20d': _adv20,\n"
    "                'structural_high_5d': _shigh5,\n"
    "                'prev_day_high': _pdh\n"
    "            }"
)

for label, OLD in (("A", OLD_A), ("B", OLD_B)):
    c = src.count(OLD)
    if c != 1:
        if label == "B" and "'structural_high_5d'" in src:
            print("ALREADY PATCHED: precompute fields already present. No change.")
        else:
            print("ABORT: edit %s target found %d times (expected 1). "
                  "Paste sed -n '92,120p' pull_yf_history.py to re-target." % (label, c))
        sys.exit(1)

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
bak = "%s.bak_%s" % (F, ts)
shutil.copy2(F, bak)
src = src.replace(OLD_A, NEW_A).replace(OLD_B, NEW_B)
open(F, "w").write(src)
try:
    py_compile.compile(F, doraise=True)
    print("OK: both edits applied, compiles clean.")
    print("    Backup: %s" % bak)
    print("    Next: venv/bin/python3 pull_yf_history.py   # rebuild metrics with new fields")
    print("    Verify: python3 -c \"import json;m=json.load(open('stock_metrics.json'))['metrics'];k=next(iter(m));print(k,m[k])\"")
except py_compile.PyCompileError as e:
    shutil.copy2(bak, F)
    print("COMPILE FAILED -- rolled back. File unchanged.")
    print(e)
    sys.exit(1)
