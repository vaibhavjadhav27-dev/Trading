#!/usr/bin/env python3
# add_filters_v2_config.py  --  STEP 3b-i  (append FILTERS_V2 config keys)
#
# Appends the FILTERS_V2 tunable block to config.py. Idempotent: if the block
# already exists, does nothing. Backup + py_compile + auto-rollback.
# Run:  venv/bin/python3 add_filters_v2_config.py   (system python3 also fine)

import shutil, py_compile, sys, datetime

F = "/home/ubuntu/trading-bot/config.py"
src = open(F).read()

if "FILTERS_V2" in src:
    print("ALREADY PRESENT: FILTERS_V2 block exists in config.py. No change.")
    sys.exit(0)

BLOCK = """

# ===== FILTERS_V2 (5-state regime redesign) -- default OFF, backtest-gated =====
FILTERS_V2 = False            # master flag. OFF = legacy behavior, module is no-op.
# RVol gates (time-adjusted vs adv_20d)
RVOL_TRENDING = 3.5
RVOL_NORMAL   = 4.0
RVOL_BEARISH  = 5.0
# TRENDING gap window (loosened ceiling)
GAP_FLOOR_TRENDING = 2.5
GAP_CEIL_TRENDING  = 7.5
# BEARISH-DEFENSIVE flat-opener window
BEARISH_GAP_LO = -0.5
BEARISH_GAP_HI =  0.5
# CHOPPY behavior
CHOPPY_PAUSE = True           # True = hard pause to cash (shadow-logs would-have trades)
# 2nd-position R-gate (Step 6, separate live opt-in)
R_GATE_TRENDING   = 1.5
R_GATE_NORMAL     = 1.0
POS2_LIVE_ENABLED = False     # leveraged 2nd position -- explicit opt-in required
# ===== end FILTERS_V2 =====
"""

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
bak = "%s.bak_%s" % (F, ts)
shutil.copy2(F, bak)
open(F, "w").write(src + BLOCK)
try:
    py_compile.compile(F, doraise=True)
    print("OK: FILTERS_V2 block appended, config.py compiles clean.")
    print("    Backup: %s" % bak)
    print("    Verify: venv/bin/python3 -c \"import config; print(config.FILTERS_V2, config.RVOL_TRENDING)\"")
except py_compile.PyCompileError as e:
    shutil.copy2(bak, F)
    print("COMPILE FAILED -- rolled back. config.py unchanged.")
    print(e)
    sys.exit(1)
