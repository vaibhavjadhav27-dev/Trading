#!/usr/bin/env python3
# pdh_probe_and_resolve.py  --  READ-ONLY. Diagnose PDH storage + provide the
# canonical PDH resolver Path B will use. Places NO orders, writes NO files.
#
# WHY: pull_yf_history.py has code to write 'prev_day_high', but the live JSON
# sample (ABDL) shows stocks[ticker] is a PLAIN LIST OF BARS with no such field.
# Before Path B (early PDH-cross entry) can read PDH instantly, we must know:
#   (1) Does a dedicated 'prev_day_high' field actually exist in the saved file?
#   (2) If not, can we derive PDH reliably from the last completed session's high?
#   (3) What % of the universe has a usable PDH?
#
# This script answers all three and prints the resolver's source per ticker so we
# KNOW (not assume) the data foundation before wiring any live trigger.
#
# Run:  cd ~/trading-bot && venv/bin/python3 pdh_probe_and_resolve.py

import json, sys
from datetime import datetime

F = "/home/ubuntu/trading-bot/stock_history_30d.json"
try:
    d = json.load(open(F))
except Exception as e:
    print("ABORT: cannot load %s (%s)" % (F, e)); sys.exit(1)

stocks = d.get("stocks", {})
today = datetime.now().strftime("%Y-%m-%d")   # server is UTC; date rollover ~edge only
print("=" * 64)
print("TOP-LEVEL JSON KEYS:", list(d.keys()))
print("updated:", d.get("updated"))
print("stock count:", len(stocks))
# structure probe on first ticker
t0 = next(iter(stocks))
rec0 = stocks[t0]
print("stocks['%s'] type: %s" % (t0, type(rec0).__name__))
if isinstance(rec0, dict):
    print("  ticker-level keys:", list(rec0.keys()))
print("=" * 64)


def resolve_pdh(ticker):
    """Return (pdh_float_or_None, source_str). Works regardless of where PDH lives."""
    rec = stocks.get(ticker)
    if rec is None:
        return None, "no-record"
    # Case A: ticker record is a dict that may hold a dedicated field + bars
    if isinstance(rec, dict):
        if rec.get("prev_day_high"):
            return float(rec["prev_day_high"]), "field:ticker-dict"
        bars = rec.get("bars") or rec.get("history") or []
    else:
        bars = rec  # plain list of bars (current observed shape)
    if not bars:
        return None, "no-bars"
    # Case B: dedicated field embedded in the most recent bar
    last = bars[-1]
    if isinstance(last, dict) and last.get("prev_day_high"):
        return float(last["prev_day_high"]), "field:last-bar"
    # Case C: DERIVE from the last COMPLETED session's high.
    #   If the last bar is TODAY (intraday/partial), use the prior bar instead so
    #   PDH = yesterday's high, not a partial today-high.
    completed = [b for b in bars if b.get("date", "") < today]
    src_bar = completed[-1] if completed else last
    tag = "derived:last-completed-high" if completed else "derived:last-bar(today?)"
    try:
        return float(src_bar["high"]), tag
    except Exception:
        return None, "bad-bar"


# Resolve across the whole universe, tally sources
from collections import Counter
srcs = Counter()
usable = 0
samples = []
for t in stocks:
    pdh, src = resolve_pdh(t)
    srcs[src] += 1
    if pdh is not None:
        usable += 1
    if len(samples) < 8:
        samples.append((t, pdh, src))

print("PDH RESOLUTION SOURCES (how each ticker's PDH was obtained):")
for src, n in srcs.most_common():
    print("  %-32s %5d" % (src, n))
print("-" * 64)
print("USABLE PDH: %d / %d  (%.1f%%)" % (usable, len(stocks), 100.0 * usable / max(1, len(stocks))))
print("-" * 64)
print("SAMPLES (ticker, PDH, source):")
for t, pdh, src in samples:
    print("  %-12s PDH=%-10s %s" % (t, pdh, src))
print("=" * 64)

# VERDICT for Path B foundation
has_field = any(s.startswith("field:") for s in srcs)
if has_field:
    print("VERDICT: dedicated 'prev_day_high' FIELD present -> Path B can read it directly. ✅")
elif usable == len(stocks):
    print("VERDICT: NO dedicated field, but PDH is DERIVABLE for 100% of tickers")
    print("         from last-completed-session high. Path B can use resolve_pdh().")
    print("         RECOMMENDED: also add the dedicated field in pull_yf_history.py so")
    print("         the live loop doesn't parse bars per tick. (writer fix = next step)")
else:
    print("VERDICT: PDH missing/underivable for %d tickers -> DO NOT arm Path B until fixed."
          % (len(stocks) - usable))
print("NOTE: server clock is UTC; 'today' cutoff assumes date labels are trading-day dates.")
