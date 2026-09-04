#!/usr/bin/env python3
# wire_pathb_pdhlog.py
#   PIECE 1 of Path B wiring: build + LOG the PDH map at candidate selection.
#   This is the "keep logs of PDH" piece. It does NOT add the per-tick cross
#   detection (that needs the 1666-1754 read to find the pre-09:30 host loop).
#
# SAFETY: inserts a FILTERS_V2-gated, try-wrapped, NON-BLOCKING block right after
#   self.select_candidates(). No order calls. If FILTERS_V2 is False (tomorrow's
#   legacy path) the block is inert -> no logs, no behavior change. If build fails,
#   it logs a warning and continues (shadow must never break the live loop).
#
# ROBUSTNESS: locates the line by strip()=='self.select_candidates()' and reuses
#   its exact indentation, so it works regardless of nesting depth. Idempotent
#   (marker check). Backup -> py_compile -> auto-rollback.
# Run:  cd ~/trading-bot && venv/bin/python3 wire_pathb_pdhlog.py

import shutil, py_compile, sys, datetime

F = "/home/ubuntu/trading-bot/trading_bot.py"
MARKER = "[SHADOW-PATHB] PDH map built"

lines = open(F).read().split("\n")
if any(MARKER in ln for ln in lines):
    print("ALREADY WIRED: PDH-log block present. No change."); sys.exit(0)

# find the selection call
idx = next((i for i, ln in enumerate(lines)
            if ln.strip() == "self.select_candidates()"), None)
if idx is None:
    print("ABORT: could not find a line `self.select_candidates()`.")
    print("       Paste: grep -n 'select_candidates' trading_bot.py")
    sys.exit(1)

indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
block = [
    "",
    indent + "# --- PATH B SHADOW: log resolved PDH at selection (FILTERS_V2-gated, no orders) ---",
    indent + "if getattr(config, \"FILTERS_V2\", False):",
    indent + "    try:",
    indent + "        from pdh_cache import build_pdh_map",
    indent + "        from path_b_shadow import PathBShadow",
    indent + "        self._pathb_pdh_map, _pathb_missing = build_pdh_map(self.candidates)",
    indent + "        self._pathb = PathBShadow(log, self._pathb_pdh_map, getattr(self, \"market_regime\", \"NORMAL\"))",
    indent + "        log.info(\"[SHADOW-PATHB] PDH map built: %d with PDH, %d missing\" % (len(self._pathb_pdh_map), len(_pathb_missing)))",
    indent + "        for _sid, _pdh in list(self._pathb_pdh_map.items())[:50]:",
    indent + "            log.info(\"[SHADOW-PATHB] PDH sid=%s = %.2f\" % (_sid, _pdh))",
    indent + "    except Exception as _e:",
    indent + "        log.warning(\"[SHADOW-PATHB] PDH map build failed (shadow, non-blocking): %s\" % _e)",
]
new = lines[:idx + 1] + block + lines[idx + 1:]

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
bak = "%s.bak_%s" % (F, ts)
shutil.copy2(F, bak)
open(F, "w").write("\n".join(new))
try:
    py_compile.compile(F, doraise=True)
    print("OK: PDH-log block inserted after self.select_candidates() (line %d)." % (idx + 1))
    print("    Gated by FILTERS_V2; inert on tomorrow's legacy path (no logs, no orders).")
    print("    When FILTERS_V2=True: logs '[SHADOW-PATHB] PDH map built' + per-sid PDH.")
    print("    NOTE: requires pdh_cache.py + path_b_shadow.py present in ~/trading-bot/.")
    print("    Backup: %s" % bak)
except py_compile.PyCompileError as e:
    shutil.copy2(bak, F)
    print("COMPILE FAILED -- rolled back. trading_bot.py unchanged.")
    print(e); sys.exit(1)
