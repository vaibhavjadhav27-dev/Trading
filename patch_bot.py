#!/usr/bin/env python3
"""One-shot root-cause patcher for trading_bot.py.
Exact-string matching (no line numbers / no regex). AST-verified. Auto-revert on any failure."""
import ast, json, shutil, sys, time

TARGET = "trading_bot.py"
EDITS = json.loads(r'''[["#from clv_scorer import get_clv_scores, get_clv_bonus\n", "from clv_scorer import get_clv_scores, get_clv_bonus\n"], ["            c[\"rank_score\"] += get_rs_bonus(c.get(\"ticker\", \"\"), self._rs_scores)\n#            c[\"rank_score\"] += get_clv_bonus(str(c.get(\"security_id\", \"\")), self._clv_scores)", "            c[\"rank_score\"] += get_rs_bonus(c.get(\"ticker\", \"\"), self._rs_scores)\n            c[\"rank_score\"] += get_clv_bonus(str(c.get(\"sid\", \"\")), getattr(self, \"_clv_scores\", {}))"], ["#        if not hasattr(self, \"_clv_scores\"):\n#            self._clv_scores = get_clv_scores()\n", "        if not hasattr(self, \"_clv_scores\"):\n            self._clv_scores = get_clv_scores()\n"], ["            _ltp_result = [{}]\n", "            _ltp_result = {}   # ROOT FIX: dict init (was [{}] -> spawned 4 list-guards)\n"], ["                        if isinstance(_ltp_result, list): _ltp_result = _ltp_result if _ltp_result else {}\n", ""], ["            ltp_map = _ltp_result if _ltp_result and _ltp_result else {}\n", "            ltp_map = _ltp_result if isinstance(_ltp_result, dict) else {}\n"], ["        # Build ltp_data dict compatible with downstream code\n        ltp_data = ltp_map\n        # REST fallback DISABLED - use cache gaps instead\n        log.info(\"Skipping REST fallback - using cached prev_close for gaps\")", "        # Build ltp_data dict compatible with downstream code\n        ltp_data = ltp_map\n        # ROOT FIX: bounded REST fallback so a WS miss is not a dead session.\n        if not ltp_map:\n            log.warning(\"WebSocket returned 0 LTPs - bounded REST fallback (top 120 by prior gap)\")\n            try:\n                _ranked = sorted(\n                    self.watchlist,\n                    key=lambda s: self._prev_closes.get(str(int(s[\"security_id\"])), 0),\n                    reverse=True,\n                )[:120]\n                _sids = [str(int(s[\"security_id\"])) for s in _ranked]\n                ltp_map = self.fetch_ltp_concurrent(_sids) or {}\n                ltp_data = ltp_map\n                log.info(f\"REST fallback LTP: {len(ltp_map)} stocks (bounded to 120)\")\n            except Exception as _fb:\n                log.warning(f\"Bounded REST fallback failed: {_fb}\")\n        else:\n            log.info(\"Using WebSocket LTP - REST fallback not needed\")"], ["                if isinstance(ltp_map, list): ltp_map = ltp_map if ltp_map else {}\n", ""], ["                if isinstance(ltp_map, list) and len(ltp_map) > 0: ltp_map = ltp_map.pop()\n", ""], ["            breakout_deadline = time.time() + 5400\n", "            breakout_deadline = time.time() + 19800   # ROOT FIX: 90min->5.5h; real gate is ist_time(15,15)/ENTRY_CUTOFF\n"], ["log.info(\"Dead zone (11:30-13:45). Sleeping...\")", "log.info(\"Dead zone (12:30-13:45). Sleeping...\")"]]''')

src = open(TARGET, encoding="utf-8").read()
bak = TARGET + ".bak_" + time.strftime("%H%M%S")
shutil.copy(TARGET, bak)

already = sum(1 for o, n in EDITS if n and n in src and o not in src)
missing = [i for i, (o, n) in enumerate(EDITS) if o not in src]
if missing and already:
    print("Looks ALREADY PATCHED (%d/%d fixes present). No changes." % (already, len(EDITS)))
    sys.exit(0)
if missing:
    print("ABORT: %d anchor(s) not found: %s" % (len(missing), missing))
    print("File not modified. Are you on the right version?")
    sys.exit(1)

out = src
for o, n in EDITS:
    out = out.replace(o, n, 1)

try:
    ast.parse(out)
except SyntaxError as e:
    print("ABORT: patched result has SYNTAX ERROR: %s -- reverting." % e)
    shutil.copy(bak, TARGET)
    sys.exit(1)

open(TARGET, "w", encoding="utf-8").write(out)
ast.parse(open(TARGET, encoding="utf-8").read())
print("PATCHED OK: %d fixes applied. Backup=%s  bytes %d->%d" % (len(EDITS), bak, len(src), len(out)))
