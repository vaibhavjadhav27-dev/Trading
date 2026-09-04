#!/usr/bin/env python3
"""Guard the HTML-email copy of the 'all in shortlist' false-positive in market_intel.py.

The text-prompt copy (~line 377) was guarded on 2026-07-21. This closes the SECOND copy
in the HTML email body (~line 529) inside _send_email(self, date, gainers, our_stocks,
missed, market, analysis, provider). Here the in-scope var is `gainers` (NOT top_gainers)
and the missed list is `missed`. When the feed is empty, show a failure notice instead of
the false 'all in shortlist' win.

Anchor is the unique colspan=3 HTML line (distinct from the text copy).
Exact-string anchor, backup, AST-verified, idempotent."""
import ast, shutil, datetime, sys, os

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_intel.py")

OLD = (
    "            ]) if missed else '<tr><td colspan=\"3\">All top gainers were in our shortlist!</td></tr>'\n"
)
NEW = (
    "            ]) if missed else (\n"
    "                '<tr><td colspan=\"3\">\u26a0\ufe0f Gainer feed unavailable - could not fetch (not a real result)</td></tr>'\n"
    "                if not gainers\n"
    "                else '<tr><td colspan=\"3\">All top gainers were in our shortlist!</td></tr>')\n"
)

src = open(FILE, encoding="utf-8").read()

if "\u26a0\ufe0f Gainer feed unavailable - could not fetch (not a real result)" in src:
    print("ALREADY PATCHED - no change made."); sys.exit(0)

n = src.count(OLD)
if n != 1:
    print(f"ABORT: anchor matched {n} times, expected exactly 1. No change made."); sys.exit(1)

bak = FILE + ".bak_htmlfp_" + datetime.datetime.now().strftime("%H%M%S")
shutil.copy2(FILE, bak)

new_src = src.replace(OLD, NEW)

try:
    ast.parse(new_src)
except SyntaxError as e:
    print(f"ABORT: patched source has SyntaxError ({e}). Nothing written. Backup at {bak}"); sys.exit(1)

open(FILE, "w", encoding="utf-8").write(new_src)
disk = open(FILE, encoding="utf-8").read()
ast.parse(disk)
assert disk.count("Gainer feed unavailable") >= 2, "expected both text + HTML guards present now"
print(f"PATCHED OK. Backup: {bak}")
print("  HTML email body: empty feed now shows failure notice instead of false 'all in shortlist'")
print("  guard uses in-scope var `gainers` (not top_gainers). AST verified on disk.")
