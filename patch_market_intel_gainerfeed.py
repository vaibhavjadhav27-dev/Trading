#!/usr/bin/env python3
"""Fix market_intel.py empty gainer feed.
NSE endpoint returns HTTP 200 with empty body on server IPs (blocked) -> get_top_gainers()
returns []. Current code has NO fallback, so the 'Daily Market Intelligence' email shows an
empty Top-5 table AND the misleading 'All top gainers were in our shortlist!' false-positive.

Fix (mirrors the WORKING post_market_analysis.py:310 pattern):
  1) On empty NSE result, fall back to get_top_gainers_from_dhan(), with a field ADAPTER
     (ticker->symbol, gain_pct->pChange, fill open/high/low/volume/value_cr=0) because
     market_intel.py direct-indexes g['symbol']/g['pChange']/g['volume']/g['value_cr']
     (KeyError otherwise -- Dhan emits ticker/gain_pct/ltp/prev_close/sid only).
  2) Guard the 'all in shortlist' message: empty feed -> explicit failure, not a false win.

Exact-string anchors, backup, AST-verified, idempotent."""
import ast, shutil, datetime, sys, os

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_intel.py")

# ---- Change 1: Dhan fallback + adapter at the gainers assignment ----
OLD1 = (
    "        top_gainers = self.nse.get_top_gainers(10)\n"
    "        fii_dii = self.nse.get_fii_dii_data()\n"
)
NEW1 = (
    "        top_gainers = self.nse.get_top_gainers(10)\n"
    "        if not top_gainers:\n"
    "            # wired 2026-07-21: NSE returns 200-empty on server IP -> Dhan fallback (mirror post_market_analysis.py:310)\n"
    "            log.warning('NSE gainers empty -> Dhan fallback (get_top_gainers_from_dhan)')\n"
    "            try:\n"
    "                from post_market_analysis import get_top_gainers_from_dhan as _dhan_gainers\n"
    "                _dg = _dhan_gainers() or []\n"
    "                top_gainers = [{\n"
    "                    'symbol': _d.get('ticker', ''),\n"
    "                    'ltp': _d.get('ltp', 0),\n"
    "                    'change': round((_d.get('ltp', 0) or 0) - (_d.get('prev_close', 0) or 0), 2),\n"
    "                    'pChange': _d.get('gain_pct', 0),\n"
    "                    'open': 0, 'high': 0, 'low': 0,\n"
    "                    'prev_close': _d.get('prev_close', 0),\n"
    "                    'volume': 0, 'value_cr': 0,\n"
    "                } for _d in _dg]\n"
    "                log.info(f'Dhan fallback: got {len(top_gainers)} gainers')\n"
    "            except Exception as _fe:\n"
    "                log.error(f'Dhan gainer fallback failed: {_fe}')\n"
    "                top_gainers = []\n"
    "        fii_dii = self.nse.get_fii_dii_data()\n"
)

# ---- Change 2: guard the false-positive 'all in shortlist' text message ----
OLD2 = "        ]) if missed_gainers else '  All top gainers were in our shortlist!'\n"
NEW2 = (
    "        ]) if missed_gainers else (\n"
    "            '  \u26a0\ufe0f Gainer feed unavailable - NSE returned empty and Dhan fallback returned nothing '\n"
    "            '(this is NOT a real \"all shortlisted\" result)' if not top_gainers\n"
    "            else '  All top gainers were in our shortlist!')\n"
)

src = open(FILE, encoding="utf-8").read()

if "wired 2026-07-21: NSE returns 200-empty on server IP -> Dhan fallback" in src:
    print("ALREADY PATCHED - no change made."); sys.exit(0)

for tag, OLD in (("change1", OLD1), ("change2", OLD2)):
    n = src.count(OLD)
    if n != 1:
        print(f"ABORT: {tag} anchor matched {n} times, expected exactly 1. No change made."); sys.exit(1)

bak = FILE + ".bak_gainerfeed_" + datetime.datetime.now().strftime("%H%M%S")
shutil.copy2(FILE, bak)

new_src = src.replace(OLD1, NEW1).replace(OLD2, NEW2)

try:
    ast.parse(new_src)
except SyntaxError as e:
    print(f"ABORT: patched source has SyntaxError ({e}). Nothing written. Backup at {bak}"); sys.exit(1)

open(FILE, "w", encoding="utf-8").write(new_src)
disk = open(FILE, encoding="utf-8").read()
ast.parse(disk)
assert "get_top_gainers_from_dhan as _dhan_gainers" in disk
assert "Gainer feed unavailable" in disk
print(f"PATCHED OK. Backup: {bak}")
print("  1) NSE-empty -> Dhan fallback with field adapter (ticker->symbol, gain_pct->pChange, fill volume/value_cr=0)")
print("  2) empty feed now reports failure instead of 'all in shortlist' false-positive")
print("  AST verified on disk. NSE stays primary; Dhan only fires on empty.")
