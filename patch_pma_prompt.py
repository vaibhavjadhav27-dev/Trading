#!/usr/bin/env python3
"""Patch post_market_analysis.py: inject grounded selection criteria + reframe Q1-Q5.
Exact-string match, timestamped backup, AST-verify. Idempotent (detects already-patched)."""
import ast, shutil, datetime, sys, os

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "post_market_analysis.py")

OLD = (
    "Answer these 5 questions with SPECIFIC stock names and numbers:\n"
    "\n"
    "Q1. MISSED OPPORTUNITIES: Which of the top gainers did we MISS? For each missed stock, explain exactly which filter rejected it (price/gap/ORB range/volume/score). Were they legitimate misses or filter errors?\n"
    "\n"
    "Q2. SELECTION ACCURACY: Were our shortlisted stocks correct? Did any shortlisted stock FAIL to move up? What was the actual gain/loss for each shortlisted stock by EOD?\n"
    "\n"
    "Q3. ENTRY/EXIT TIMING: If we traded, was the entry price optimal? What was the high-of-day after entry? How much of the move did we capture vs leave on the table? If Smart Exit triggered, was it too early or correct?\n"
    "\n"
    "Q4. REGIME ASSESSMENT: Was the market trending/choppy/bearish? Did our regime detection match reality? Should we have traded more or fewer stocks given today conditions?\n"
    "\n"
    "Q5. TOMORROW ACTIONABLE: Based on today patterns, give ONE specific filter change (with exact number) that would improve results. Also flag any sector rotation or momentum shift for tomorrow."
)

NEW = (
    "OUR ACTUAL SELECTION CRITERIA (do NOT guess thresholds - judge every gainer against THESE):\n"
    "  1. Opening gap: +0.3% to +8% (below 0.3% = flat-open reject; above 8% = too-extended reject)\n"
    "  2. RVOL: >= 2.0x (relative volume vs the stock's own average)\n"
    "  3. Price band: Rs.60 to Rs.5000\n"
    "  4. Liquidity (ADT, avg daily turnover): min Rs.5Cr, sweet spot Rs.50-70Cr\n"
    "  5. Universe: must be in our 552-stock sector-classified watchlist (gainers OUTSIDE it are never scanned)\n"
    "  6. Relative Strength: ranked vs NIFTY (higher RS = higher rank)\n"
    "  7. Regime gate (5-state): NO_TRADE / BEARISH-DEFENSIVE / CHOPPY(pause+shadow) / NORMAL(x1.25) / TRENDING(x1.50)\n"
    "ENTRY: ORB breakout - a 5-min candle CLOSES above opening-range high + buffer; size = regime multiplier.\n"
    "EXIT: SMART_EXIT_v2 + R-Skate v2 trail; hard SL -5%; dead-trade cut (<0.5R in 30min); mandatory 3:15PM square-off.\n"
    "COST: MIS round-trip ~Rs.24 min; a trade must clear a 3x cost gate. A NO-TRADE on a weak/flat tape is a CORRECT outcome, not a miss.\n"
    "\n"
    "Answer these 5 questions with SPECIFIC stock names and numbers. Judge ONLY against the criteria above - never invent a threshold:\n"
    "\n"
    "Q1. MISSED OPPORTUNITIES: For EACH top gainer, classify as (a) TRUE MISS - passed ALL 7 criteria, we should have caught it; or (b) NOT-OUR-SETUP - name the ONE criterion it failed + its value vs the threshold (e.g. \"gap +11.3% > 8% cap\", or \"not in 552 watchlist -> never scanned\"). If a field is missing, say which field is needed - do NOT guess vaguely.\n"
    "\n"
    "Q2. SELECTION ACCURACY: Compare OUR shortlisted stocks (listed above) vs the actual top gainers. Which shortlisted names gained, which failed to move? Which gainers were absent from our shortlist and why? If the shortlist is EMPTY, state whether NO-TRADE was the CORRECT call given the regime.\n"
    "\n"
    "Q3. ENTRY/EXIT TIMING: For any TRUE MISS or trade taken, at what ORB-break level would we enter, what was high-of-day after entry, how much of the move did we capture vs leave, and was Smart Exit too early or correct?\n"
    "\n"
    "Q4. REGIME ASSESSMENT: State the detected regime and whether today's gainer breadth (how many names, which sectors) CONFIRMS or CONTRADICTS it. Should we have traded more or fewer given the criteria?\n"
    "\n"
    "Q5. TOMORROW ACTIONABLE: ONE specific, testable threshold change (one criterion, one exact number) that would capture the most TRUE MISSES - WITH its trade-off (what noise/losers it also admits). Only if the cost economics justify it. No multi-change wishlists."
)

src = open(FILE, encoding="utf-8").read()

if "OUR ACTUAL SELECTION CRITERIA" in src:
    print("ALREADY PATCHED - no change made."); sys.exit(0)

n = src.count(OLD)
if n != 1:
    print(f"ABORT: anchor matched {n} times, expected exactly 1. No change made."); sys.exit(1)

bak = FILE + ".bak_" + datetime.datetime.now().strftime("%H%M%S")
shutil.copy2(FILE, bak)
new_src = src.replace(OLD, NEW)

try:
    ast.parse(new_src)
except SyntaxError as e:
    print(f"ABORT: patched source has SyntaxError ({e}). Nothing written. Backup at {bak}"); sys.exit(1)

open(FILE, "w", encoding="utf-8").write(new_src)
disk = open(FILE, encoding="utf-8").read()
ast.parse(disk)
assert "OUR ACTUAL SELECTION CRITERIA" in disk and "TRUE MISS" in disk
print(f"PATCHED OK. Backup: {bak}")
print("  - injected 7-criteria block + entry/exit/cost")
print("  - reframed Q1-Q5 grounded (no-guess). AST verified on disk.")
