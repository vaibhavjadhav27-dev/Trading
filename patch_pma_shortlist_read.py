#!/usr/bin/env python3
"""Wire post_market_analysis.py shortlist/rejected text to pending_{date}.json.
The candidate logger writes candle_archive/pending_{date}.json (10 real candidates,
kept flag). Current code reads bot_log['shortlisted'] which is empty -> 'No data' ->
Gemini says 'no stock shortlisted'. This sources the real candidates, with graceful
fallback to bot_log if the pending file is missing. Exact-string, backup, AST-verified, idempotent."""
import ast, shutil, datetime, sys, os

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "post_market_analysis.py")

OLD = (
    '    shortlist_text = NL.join(bot_log.get("shortlisted", ["No data"])[:10])\n'
    '    rejected_text = NL.join(bot_log.get("rejected", ["No data"])[:10])\n'
)

NEW = (
    '    # --- wired 2026-07-21: source shortlist/rejected from pending_{date}.json (real logged candidates) ---\n'
    '    import os as _pos, json as _pjson\n'
    '    from datetime import date as _pdate\n'
    '    _pf = _pos.path.join(_pos.path.dirname(_pos.path.abspath(__file__)), "candle_archive", f"pending_{_pdate.today()}.json")\n'
    '    _kept, _rej = [], []\n'
    '    try:\n'
    '        with open(_pf) as _pff:\n'
    '            _pd = _pjson.load(_pff)\n'
    '        for _c in _pd.get("candidates", []):\n'
    '            _line = ("%s: gap +%.1f%%, RS %.1f, score %.1f, %s, LTP %s" % (\n'
    "                _c.get('symbol', '?'), (_c.get('gap') or 0), (_c.get('rs') or 0),\n"
    "                (_c.get('score') or 0), _c.get('tier', '?'), _c.get('ltp') or 0))\n"
    '            (_kept if _c.get("kept") else _rej).append(_line)\n'
    '    except Exception:\n'
    '        _kept, _rej = [], []\n'
    '    shortlist_text = NL.join(_kept[:10]) if _kept else NL.join(bot_log.get("shortlisted", ["No data"])[:10])\n'
    '    rejected_text = NL.join(_rej[:10]) if _rej else NL.join(bot_log.get("rejected", ["No data"])[:10])\n'
)

src = open(FILE, encoding="utf-8").read()

if "wired 2026-07-21: source shortlist/rejected from pending" in src:
    print("ALREADY PATCHED - no change made."); sys.exit(0)

n = src.count(OLD)
if n != 1:
    print(f"ABORT: anchor matched {n} times, expected exactly 1. No change made."); sys.exit(1)

bak = FILE + ".bak_shortlist_" + datetime.datetime.now().strftime("%H%M%S")
shutil.copy2(FILE, bak)
new_src = src.replace(OLD, NEW)

try:
    ast.parse(new_src)
except SyntaxError as e:
    print(f"ABORT: patched source has SyntaxError ({e}). Nothing written. Backup at {bak}"); sys.exit(1)

open(FILE, "w", encoding="utf-8").write(new_src)
disk = open(FILE, encoding="utf-8").read()
ast.parse(disk)
assert "pending_{_pdate.today()}.json" in disk
print(f"PATCHED OK. Backup: {bak}")
print("  - shortlist_text/rejected_text now read candle_archive/pending_{date}.json")
print("  - kept=True -> shortlisted, kept=False -> rejected, with real gap/RS/score/tier/LTP")
print("  - falls back to bot_log if pending file missing. AST verified on disk.")
