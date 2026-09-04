#!/usr/bin/env python3
# add_shadow_mode.py  --  STEP 4b  (SHADOW_MODE: live, non-trading, per-state logging)
#
# Extends FILTERS_V2 so it can run in SHADOW: compute what each state WOULD select
# (incl. RVol on the shortlist) and write one JSON line per scan to a dedicated file,
# WITHOUT changing self.candidates while FILTERS_V2=False. Legacy trades live; shadow
# just observes -> zero risk to capital, real intraday regime+RVol evidence.
#
# THREE coordinated edits (all exact-string, count-checked BEFORE any write):
#   1) config.py         : append SHADOW_MODE = False
#   2) filters_v2.py     : add `force` param (bypass master-flag guard) + shadow_log()
#   3) trading_bot.py    : rewrite the deployed hook to run under FILTERS_V2 OR SHADOW_MODE,
#                          log the would-keep set, and only assign candidates when LIVE.
#
# All-or-nothing: if ANY target does not match exactly once, nothing is written.
# Per-file backup + py_compile + full rollback on any compile failure.
# Run:  venv/bin/python3 add_shadow_mode.py

import shutil, py_compile, sys, datetime

BASE = "/home/ubuntu/trading-bot"
CFG  = BASE + "/config.py"
FV2  = BASE + "/filters_v2.py"
BOT  = BASE + "/trading_bot.py"

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# ---------- read ----------
cfg = open(CFG).read()
fv2 = open(FV2).read()
bot = open(BOT).read()

# ---------- pre-flight: idempotency ----------
if "SHADOW_MODE" in cfg and "def shadow_log" in fv2 and "FILTERS_V2 SHADOW" in bot:
    print("ALREADY PATCHED: SHADOW_MODE present across all three files. No change.")
    sys.exit(0)

# ---------- edit 1: config ----------
CFG_MARK = "CHOPPY_PAUSE = True"
if cfg.count(CFG_MARK) != 1:
    print("ABORT(config): anchor '%s' found %d times (expected 1)." % (CFG_MARK, cfg.count(CFG_MARK)))
    sys.exit(1)
CFG_OLD = "CHOPPY_PAUSE = True           # True = hard pause to cash (shadow-logs would-have trades)\n"
if cfg.count(CFG_OLD) != 1:
    # fall back to appending near the flag if the comment drifted
    CFG_OLD = CFG_MARK + "\n"
CFG_NEW = CFG_OLD + "SHADOW_MODE = False           # True = run FILTERS_V2 non-trading, log per-state to filters_v2_shadow.log\n"

# ---------- edit 2a: filters_v2 signature ----------
SIG_OLD = "def apply_regime_filters(candidates, state, cfg, rvol_fn=None, sector_ok_fn=None):"
SIG_NEW = "def apply_regime_filters(candidates, state, cfg, rvol_fn=None, sector_ok_fn=None, force=False):"
if fv2.count(SIG_OLD) != 1:
    print("ABORT(filters_v2): signature anchor found %d times (expected 1)." % fv2.count(SIG_OLD)); sys.exit(1)

# ---------- edit 2b: filters_v2 master-flag guard ----------
GUARD_OLD = ('    if not getattr(cfg, "FILTERS_V2", False):\n'
             '        return candidates  # master flag OFF -> no-op passthrough')
GUARD_NEW = ('    if not getattr(cfg, "FILTERS_V2", False) and not force:\n'
             '        return candidates  # master flag OFF (and not shadow) -> no-op passthrough')
if fv2.count(GUARD_OLD) != 1:
    print("ABORT(filters_v2): guard anchor found %d times (expected 1)." % fv2.count(GUARD_OLD)); sys.exit(1)

# ---------- edit 2c: append shadow_log writer ----------
SHADOW_FN = '''

def shadow_log(state, before, kept, rvol_fn=None,
               path="/home/ubuntu/trading-bot/filters_v2_shadow.log"):
    """Write one JSON line per scan: what FILTERS_V2 WOULD have selected. Non-trading."""
    import json as _j, datetime as _dt
    kept_ids = set(id(c) for c in kept)
    rec = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "state": state,
        "n_before": len(before),
        "n_kept": len(kept),
        "kept": [c.get("ticker", "?") for c in kept],
        "dropped": [c.get("ticker", "?") for c in before if id(c) not in kept_ids],
    }
    if rvol_fn is not None:
        rv = {}
        for c in before:
            try:
                rv[c.get("ticker", "?")] = round(float(rvol_fn(c)), 2)
            except Exception:
                rv[c.get("ticker", "?")] = None
        rec["rvol"] = rv
    try:
        with open(path, "a") as f:
            f.write(_j.dumps(rec) + "\\n")
    except Exception as e:
        log.warning("shadow_log write failed: %s", e)
'''

# ---------- edit 3: rewrite the deployed hook in trading_bot.py ----------
HOOK_OLD = '''        # ===== FILTERS_V2 gated hook (default OFF -> no-op) =====
        if getattr(config, "FILTERS_V2", False):
            try:
                import filters_v2, time as _t
                _mode = getattr(self, "market_regime", "FULL")
                _reg = getattr(self, "regime", "NORMAL")
                _state = "BEARISH-DEFENSIVE" if _mode == "CONSERVATIVE" else _reg
                def _rvol_fn(c):
                    # bounded REST on shortlist ONLY (rate-safe)
                    try:
                        _sid = str(int(c["security_id"]))
                        _t.sleep(0.3)
                        _d = self.dhan.get_ohlc_intraday(_sid, "NSE_EQ", "5")
                        _v = _d.get("volume") if isinstance(_d, dict) else None
                        _vol_now = float(_v[-1]) if _v else 0
                        _adv = filters_v2._m(c.get("ticker", "")).get("adv_20d", 0)
                        _mins = 15  # ~9:15-9:30 window; refine with real clock if needed
                        return compute_time_adjusted_rvol(_vol_now, _adv, _mins) if _adv else None
                    except Exception:
                        return None
                self.candidates = filters_v2.apply_regime_filters(
                    self.candidates, _state, config, rvol_fn=_rvol_fn)
                log.info(f"FILTERS_V2 hook: state={_state} -> {len(self.candidates)} candidates")
            except Exception as _fe:
                log.warning(f"FILTERS_V2 hook error -> legacy passthrough: {_fe}")
        # ===== end FILTERS_V2 hook ====='''

HOOK_NEW = '''        # ===== FILTERS_V2 hook: LIVE (FILTERS_V2) or SHADOW (non-trading) =====
        if getattr(config, "FILTERS_V2", False) or getattr(config, "SHADOW_MODE", False):
            try:
                import filters_v2, time as _t
                _live = getattr(config, "FILTERS_V2", False)
                _mode = getattr(self, "market_regime", "FULL")
                _reg = getattr(self, "regime", "NORMAL")
                _state = "BEARISH-DEFENSIVE" if _mode == "CONSERVATIVE" else _reg
                def _rvol_fn(c):
                    # bounded REST on shortlist ONLY (rate-safe)
                    try:
                        _sid = str(int(c["security_id"]))
                        _t.sleep(0.3)
                        _d = self.dhan.get_ohlc_intraday(_sid, "NSE_EQ", "5")
                        _v = _d.get("volume") if isinstance(_d, dict) else None
                        _vol_now = float(_v[-1]) if _v else 0
                        _adv = filters_v2._m(c.get("ticker", "")).get("adv_20d", 0)
                        _mins = 15  # ~9:15-9:30 window; refine with real clock if needed
                        return compute_time_adjusted_rvol(_vol_now, _adv, _mins) if _adv else None
                    except Exception:
                        return None
                _before = list(self.candidates)
                _kept = filters_v2.apply_regime_filters(
                    _before, _state, config, rvol_fn=_rvol_fn, force=True)
                filters_v2.shadow_log(_state, _before, _kept, rvol_fn=_rvol_fn)
                if _live:
                    self.candidates = _kept
                    log.info(f"FILTERS_V2 LIVE: state={_state} -> {len(self.candidates)} candidates")
                else:
                    log.info(f"FILTERS_V2 SHADOW: state={_state} -> would keep {len(_kept)}/{len(_before)} (candidates UNCHANGED)")
            except Exception as _fe:
                log.warning(f"FILTERS_V2 hook error -> legacy passthrough: {_fe}")
        # ===== end FILTERS_V2 hook ====='''

if bot.count(HOOK_OLD) != 1:
    print("ABORT(trading_bot): deployed hook block found %d times (expected 1). "
          "Was Step 3b applied unchanged?" % bot.count(HOOK_OLD)); sys.exit(1)

# ---------- all anchors verified -> apply ----------
targets = [
    (CFG, cfg, cfg.replace(CFG_OLD, CFG_NEW, 1)),
    (FV2, fv2, fv2.replace(SIG_OLD, SIG_NEW, 1).replace(GUARD_OLD, GUARD_NEW, 1) + SHADOW_FN),
    (BOT, bot, bot.replace(HOOK_OLD, HOOK_NEW, 1)),
]

backups = []
for path, old, new in targets:
    bak = "%s.bak_%s" % (path, ts)
    shutil.copy2(path, bak)
    backups.append((path, bak))
    open(path, "w").write(new)

# ---------- compile all; rollback everything on any failure ----------
ok = True
err = None
for path, _bak in backups:
    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as e:
        ok = False; err = e; break

if not ok:
    for path, bak in backups:
        shutil.copy2(bak, path)
    print("COMPILE FAILED -- ALL THREE files rolled back. No changes.")
    print(err); sys.exit(1)

print("OK: SHADOW_MODE deployed across config.py, filters_v2.py, trading_bot.py. All compile clean.")
for path, bak in backups:
    print("    backup: %s" % bak)
print("")
print("TO ENABLE SHADOW (non-trading observation):")
print("  set SHADOW_MODE = True in config.py, restart the service before 9:15 IST.")
print("  It logs would-select per state to  filters_v2_shadow.log  WITHOUT trading.")
print("  Leave FILTERS_V2 = False. Read a few sessions, THEN decide to flip live.")
