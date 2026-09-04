#!/usr/bin/env python3
# add_filters_v2_hook.py  --  STEP 3b-ii  (gated hook into select_candidates)
#
# Inserts a SINGLE gated block right after `self.candidates = candidates[:10]`.
# When config.FILTERS_V2 is False (default) the block is a no-op passthrough,
# so live behavior is IDENTICAL until the flag is flipped after backtest.
#
# State derivation (uses BOTH attrs correctly, per Step-1 unification):
#   market_regime == 'CONSERVATIVE' (Nifty < VWAP)  -> 'BEARISH-DEFENSIVE'
#   else self.regime (CHOPPY / TRENDING / NORMAL)
#
# RVol: bounded REST on the ~10 shortlisted candidates only (0.3s sleep),
#       fed into compute_time_adjusted_rvol vs adv_20d. Shortlist-only = rate-safe.
#
# SAFETY: exact-string match (must be EXACTLY 1). Backup + py_compile + auto-rollback.
# Run:  venv/bin/python3 add_filters_v2_hook.py

import shutil, py_compile, sys, datetime

F = "/home/ubuntu/trading-bot/trading_bot.py"
src = open(F).read()

OLD = (
    "        candidates.sort(key=lambda x: -x.get(\"rank_score\", 0))\n"
    "        self.candidates = candidates[:10]\n"
    "        log.info(f\"Candidates: {len(self.candidates)} selected from {len(candidates)} passing\")\n"
)

NEW = (
    "        candidates.sort(key=lambda x: -x.get(\"rank_score\", 0))\n"
    "        self.candidates = candidates[:10]\n"
    "        # ===== FILTERS_V2 gated hook (default OFF -> no-op) =====\n"
    "        if getattr(config, \"FILTERS_V2\", False):\n"
    "            try:\n"
    "                import filters_v2, time as _t\n"
    "                _mode = getattr(self, \"market_regime\", \"FULL\")\n"
    "                _reg = getattr(self, \"regime\", \"NORMAL\")\n"
    "                _state = \"BEARISH-DEFENSIVE\" if _mode == \"CONSERVATIVE\" else _reg\n"
    "                def _rvol_fn(c):\n"
    "                    # bounded REST on shortlist ONLY (rate-safe)\n"
    "                    try:\n"
    "                        _sid = str(int(c[\"security_id\"]))\n"
    "                        _t.sleep(0.3)\n"
    "                        _d = self.dhan.get_ohlc_intraday(_sid, \"NSE_EQ\", \"5\")\n"
    "                        _v = _d.get(\"volume\") if isinstance(_d, dict) else None\n"
    "                        _vol_now = float(_v[-1]) if _v else 0\n"
    "                        _adv = filters_v2._m(c.get(\"ticker\", \"\")).get(\"adv_20d\", 0)\n"
    "                        _mins = 15  # ~9:15-9:30 window; refine with real clock if needed\n"
    "                        return compute_time_adjusted_rvol(_vol_now, _adv, _mins) if _adv else None\n"
    "                    except Exception:\n"
    "                        return None\n"
    "                self.candidates = filters_v2.apply_regime_filters(\n"
    "                    self.candidates, _state, config, rvol_fn=_rvol_fn)\n"
    "                log.info(f\"FILTERS_V2 hook: state={_state} -> {len(self.candidates)} candidates\")\n"
    "            except Exception as _fe:\n"
    "                log.warning(f\"FILTERS_V2 hook error -> legacy passthrough: {_fe}\")\n"
    "        # ===== end FILTERS_V2 hook =====\n"
    "        log.info(f\"Candidates: {len(self.candidates)} selected from {len(candidates)} passing\")\n"
)

n = src.count(OLD)
if n == 0:
    if "FILTERS_V2 gated hook" in src:
        print("ALREADY PATCHED: FILTERS_V2 hook present. No change.")
    else:
        print("ABORT: hook anchor not found (expected 1). Code drifted -- "
              "paste sed -n '835,845p' trading_bot.py to re-target.")
    sys.exit(1)
if n > 1:
    print("ABORT: hook anchor found %d times (expected 1). Too ambiguous." % n)
    sys.exit(1)

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
bak = "%s.bak_%s" % (F, ts)
shutil.copy2(F, bak)
open(F, "w").write(src.replace(OLD, NEW))
try:
    py_compile.compile(F, doraise=True)
    print("OK: FILTERS_V2 hook inserted, trading_bot.py compiles clean.")
    print("    Backup: %s" % bak)
    print("    NOTE: verify `compute_time_adjusted_rvol` is imported/in scope in trading_bot.py")
    print("          (it's at line ~39). If NameError at runtime, we add the import.")
except py_compile.PyCompileError as e:
    shutil.copy2(bak, F)
    print("COMPILE FAILED -- rolled back. trading_bot.py unchanged.")
    print(e)
    sys.exit(1)
