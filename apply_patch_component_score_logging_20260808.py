"""
PATCH: Per-Component Score Logging + 5-min Candle Archive
============================================================================
Date: 2026-08-08 (Saturday — safe deploy window, markets closed)

PROBLEM: score_candidate_dual() only returns (long_score, short_score)
totals. We can't see WHICH of the 7 components (Gap/RS/RVOL/ATR/
Supertrend/VWAP/Body) drove a candidate's score, or backtest scoring
changes against real candles (e.g. VARROC scored 75/180 this week —
we don't know if RVOL, ATR, Supertrend, VWAP, or Body was the weak link).

FIX 1 (dual_scorer.py): score_candidate_dual() gets a new optional
  return_breakdown=False param. When True, returns a 3rd value: a dict
  of every component's L/S contribution. Default behavior UNCHANGED —
  100% backward compatible with the existing single call site.

FIX 2 (trading_bot.py): the existing "DUAL-SCORE + CSV CALIBRATION
  LOGGER" block (added 2026-07-28, ~line 987) is extended to:
  (a) request the breakdown dict from _score_dual()
  (b) write 9 extra columns to candidate_scores_{date}.csv:
      gap_L, gap_S, rs_L, rs_S, rvol_pts, atr_pts, st_L, st_S,
      vwap_L, vwap_S, body_L, body_S, sector_adj_L, sector_adj_S
  (c) save the already-fetched 5-min OHLCV dataframe (_df) to
      candle_archive/candles_5min_{date}/{ticker}.csv for backtesting
      -- this dataframe currently gets fetched and then discarded.

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_component_score_logging_20260808.py
"""

import shutil, ast, sys
from datetime import datetime

changes = []

# ══════════════════════════════════════════════════════════════════════════
# FILE 1: dual_scorer.py — add return_breakdown option
# ══════════════════════════════════════════════════════════════════════════
DS_PATH = '/home/ubuntu/trading-bot/dual_scorer.py'
ts = datetime.now().strftime("%H%M%S")
bak1 = f"{DS_PATH}.bak_breakdown_{ts}"
shutil.copy(DS_PATH, bak1)
print(f"Backup -> {bak1}")

with open(DS_PATH) as f:
    ds_src = f.read()

OLD_SIG = "def score_candidate_dual(*, gap_pct, rs, rvol, df, ltp, nifty_gap=0.0, indicators_mod, sector_leading=False, sector_against=False):"
NEW_SIG = "def score_candidate_dual(*, gap_pct, rs, rvol, df, ltp, nifty_gap=0.0, indicators_mod, sector_leading=False, sector_against=False, return_breakdown=False):"

OLD_RETURN = "    return round(L, 1), round(S, 1)"
NEW_RETURN = '''    if return_breakdown:
        return round(L, 1), round(S, 1), _brk
    return round(L, 1), round(S, 1)'''

if OLD_SIG in ds_src and 'return_breakdown' not in ds_src:
    ds_src = ds_src.replace(OLD_SIG, NEW_SIG, 1)
    changes.append("dual_scorer.py: added return_breakdown=False param")

    # Insert _brk dict init right after "L = 0.0; S = 0.0"
    OLD_INIT = "    L = 0.0; S = 0.0"
    NEW_INIT = ("    L = 0.0; S = 0.0\n"
                "    _brk = {\"gap_L\": 0.0, \"gap_S\": 0.0, \"rs_L\": 0.0, \"rs_S\": 0.0,\n"
                "            \"sector_L\": 0.0, \"sector_S\": 0.0, \"rvol_pts\": 0.0,\n"
                "            \"atr_pts\": 0.0, \"st_L\": 0.0, \"st_S\": 0.0,\n"
                "            \"vwap_L\": 0.0, \"vwap_S\": 0.0, \"body_L\": 0.0, \"body_S\": 0.0}")
    ds_src = ds_src.replace(OLD_INIT, NEW_INIT, 1)
    changes.append("dual_scorer.py: _brk dict initialized")

    # Gap component
    ds_src = ds_src.replace(
        "    if gap_pct > 0:   L += _clamp(gap_pct / 3.0) * 30\n    elif gap_pct < 0: S += _clamp(-gap_pct / 3.0) * 30",
        "    if gap_pct > 0:   _g = _clamp(gap_pct / 3.0) * 30; L += _g; _brk[\"gap_L\"] = _g\n"
        "    elif gap_pct < 0: _g = _clamp(-gap_pct / 3.0) * 30; S += _g; _brk[\"gap_S\"] = _g",
        1
    )

    # RS component
    ds_src = ds_src.replace(
        "        if rs > 0:   L += _clamp(rs / 2.0) * 30\n        elif rs < 0: S += _clamp(-rs / 2.0) * 30",
        "        if rs > 0:   _r = _clamp(rs / 2.0) * 30; L += _r; _brk[\"rs_L\"] = _r\n"
        "        elif rs < 0: _r = _clamp(-rs / 2.0) * 30; S += _r; _brk[\"rs_S\"] = _r",
        1
    )

    # Sector bonus/penalty
    ds_src = ds_src.replace(
        "        if gap_pct > 0: L += 15   # sector confirms LONG\n        else:           S += 15   # sector confirms SHORT",
        "        if gap_pct > 0: L += 15; _brk[\"sector_L\"] = 15   # sector confirms LONG\n"
        "        else:           S += 15; _brk[\"sector_S\"] = 15   # sector confirms SHORT",
        1
    )
    ds_src = ds_src.replace(
        "        if gap_pct > 0: L -= 10  # sector contradicts long thesis\n        else:           S -= 10  # sector contradicts short thesis",
        "        if gap_pct > 0: L -= 10; _brk[\"sector_L\"] = -10  # sector contradicts long thesis\n"
        "        else:           S -= 10; _brk[\"sector_S\"] = -10  # sector contradicts short thesis",
        1
    )

    # RVOL component (non-directional, single value)
    ds_src = ds_src.replace(
        "        vp = _clamp(rvol / 3.0) * 30; L += vp; S += vp",
        "        vp = _clamp(rvol / 3.0) * 30; L += vp; S += vp; _brk[\"rvol_pts\"] = vp",
        1
    )

    # ATR component (non-directional, single value)
    ds_src = ds_src.replace(
        "                ap = _clamp((exp - 0.8) / 0.7) * 25; L += ap; S += ap",
        "                ap = _clamp((exp - 0.8) / 0.7) * 25; L += ap; S += ap; _brk[\"atr_pts\"] = ap",
        1
    )

    # Supertrend component
    ds_src = ds_src.replace(
        "                if sd > 0: L += 25\n                elif sd < 0: S += 25",
        "                if sd > 0: L += 25; _brk[\"st_L\"] = 25\n"
        "                elif sd < 0: S += 25; _brk[\"st_S\"] = 25",
        1
    )

    # VWAP component
    ds_src = ds_src.replace(
        "                if ltp > vv: L += 20\n                elif ltp < vv: S += 20",
        "                if ltp > vv: L += 20; _brk[\"vwap_L\"] = 20\n"
        "                elif ltp < vv: S += 20; _brk[\"vwap_S\"] = 20",
        1
    )

    # Body factor component
    ds_src = ds_src.replace(
        "            if bf > 0:   L += _clamp(bf) * 20\n            elif bf < 0: S += _clamp(-bf) * 20",
        "            if bf > 0:   _bb = _clamp(bf) * 20; L += _bb; _brk[\"body_L\"] = _bb\n"
        "            elif bf < 0: _bb = _clamp(-bf) * 20; S += _bb; _brk[\"body_S\"] = _bb",
        1
    )

    ds_src = ds_src.replace(OLD_RETURN, NEW_RETURN, 1)
    changes.append("dual_scorer.py: return statement updated (returns _brk when requested)")

try:
    ast.parse(ds_src)
except SyntaxError as e:
    print(f"SYNTAX ERROR in dual_scorer.py: {e}")
    sys.exit(1)

with open(DS_PATH, 'w') as f:
    f.write(ds_src)
print("dual_scorer.py syntax OK")


# ══════════════════════════════════════════════════════════════════════════
# FILE 2: trading_bot.py — extend the existing DUAL-SCORE CSV logger block
# ══════════════════════════════════════════════════════════════════════════
TB_PATH = '/home/ubuntu/trading-bot/trading_bot.py'
bak2 = f"{TB_PATH}.bak_breakdown_{ts}"
shutil.copy(TB_PATH, bak2)
print(f"Backup -> {bak2}")

with open(TB_PATH) as f:
    tb_src = f.read()

# --- 2a. Add extra CSV header columns ---
OLD_HEADER = '''                    _w.writerow(["scan_time","ticker","sid","ltp","gap_pct","rs","rvol",
                                 "long_score","short_score","regime","in_shortlist"])'''
NEW_HEADER = '''                    _w.writerow(["scan_time","ticker","sid","ltp","gap_pct","rs","rvol",
                                 "long_score","short_score","regime","in_shortlist",
                                 "gap_L","gap_S","rs_L","rs_S","sector_L","sector_S",
                                 "rvol_pts","atr_pts","st_L","st_S","vwap_L","vwap_S",
                                 "body_L","body_S"])'''
if OLD_HEADER in tb_src:
    tb_src = tb_src.replace(OLD_HEADER, NEW_HEADER, 1)
    changes.append("trading_bot.py: CSV header extended with 14 component columns")

# --- 2b. Request breakdown from _score_dual() and save 5-min candles ---
OLD_CALL = '''                        _ls, _ss = _score_dual(
                            gap_pct=_sc.get("gap_pct", 0), rs=_sc.get("rs", 0),
                            rvol=_sc.get("rvol"), df=_df, ltp=_sc.get("ltp", 0),
                            nifty_gap=_nif_gap, indicators_mod=_ind,
                            sector_leading=_sec_lead, sector_against=_sec_vs)
                    except Exception as _se:
                        _ls, _ss = 0.0, 0.0
                        log.debug(f"dual_score skip {_sc.get('ticker','?')}: {_se}")'''

NEW_CALL = '''                        _ls, _ss, _brk = _score_dual(
                            gap_pct=_sc.get("gap_pct", 0), rs=_sc.get("rs", 0),
                            rvol=_sc.get("rvol"), df=_df, ltp=_sc.get("ltp", 0),
                            nifty_gap=_nif_gap, indicators_mod=_ind,
                            sector_leading=_sec_lead, sector_against=_sec_vs,
                            return_breakdown=True)
                        # Reform 2026-08-08: archive the 5-min candles used for scoring
                        # so any day's scoring formula can be backtested exactly.
                        if _df is not None and len(_df) > 0:
                            try:
                                _cdir = _os.path.join(_csv_dir, f"candles_5min_{_dt.date.today().isoformat()}")
                                _os.makedirs(_cdir, exist_ok=True)
                                _df.to_csv(_os.path.join(_cdir, f"{_tick or _ssid}.csv"), index=False)
                            except Exception as _cde:
                                log.debug(f"candle archive skip {_sc.get('ticker','?')}: {_cde}")
                    except Exception as _se:
                        _ls, _ss = 0.0, 0.0
                        _brk = {"gap_L":0,"gap_S":0,"rs_L":0,"rs_S":0,"sector_L":0,"sector_S":0,
                                "rvol_pts":0,"atr_pts":0,"st_L":0,"st_S":0,"vwap_L":0,"vwap_S":0,
                                "body_L":0,"body_S":0}
                        log.debug(f"dual_score skip {_sc.get('ticker','?')}: {_se}")'''

if OLD_CALL in tb_src:
    tb_src = tb_src.replace(OLD_CALL, NEW_CALL, 1)
    changes.append("trading_bot.py: _score_dual() now requests breakdown + archives 5-min candles")
else:
    print("WARNING: exact _score_dual call block not found -- check manually")

# --- 2c. Add breakdown columns to the CSV row write ---
OLD_ROW = '''                    _w.writerow([
                        _dt.datetime.now().strftime("%H:%M"), _sc.get("ticker","?"),
                        _ssid, _sc.get("ltp",0), round(_sc.get("gap_pct",0),2),
                        round(_sc.get("rs",0),2), _sc.get("rvol",""),
                        _ls, _ss, _regime_now,
                        "Y" if _ssid in _kept_sids_sc else "N"])'''
NEW_ROW = '''                    _w.writerow([
                        _dt.datetime.now().strftime("%H:%M"), _sc.get("ticker","?"),
                        _ssid, _sc.get("ltp",0), round(_sc.get("gap_pct",0),2),
                        round(_sc.get("rs",0),2), _sc.get("rvol",""),
                        _ls, _ss, _regime_now,
                        "Y" if _ssid in _kept_sids_sc else "N",
                        _brk.get("gap_L",0), _brk.get("gap_S",0),
                        _brk.get("rs_L",0), _brk.get("rs_S",0),
                        _brk.get("sector_L",0), _brk.get("sector_S",0),
                        _brk.get("rvol_pts",0), _brk.get("atr_pts",0),
                        _brk.get("st_L",0), _brk.get("st_S",0),
                        _brk.get("vwap_L",0), _brk.get("vwap_S",0),
                        _brk.get("body_L",0), _brk.get("body_S",0)])'''
if OLD_ROW in tb_src:
    tb_src = tb_src.replace(OLD_ROW, NEW_ROW, 1)
    changes.append("trading_bot.py: CSV row write extended with breakdown values")
else:
    print("WARNING: exact CSV row-write block not found -- check manually")

try:
    ast.parse(tb_src)
except SyntaxError as e:
    print(f"SYNTAX ERROR in trading_bot.py: {e}")
    shutil.copy(bak2, TB_PATH)
    sys.exit(1)

with open(TB_PATH, 'w') as f:
    f.write(tb_src)
print("trading_bot.py syntax OK")


# ══════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"{len(changes)} changes applied:")
for c in changes:
    print(f"   {c}")

with open(DS_PATH) as f: ds_final = f.read()
with open(TB_PATH) as f: tb_final = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("dual_scorer: return_breakdown param",     'return_breakdown=False' in ds_final),
    ("dual_scorer: _brk dict initialized",       '"gap_L": 0.0' in ds_final),
    ("dual_scorer: return includes _brk",        'return round(L, 1), round(S, 1), _brk' in ds_final),
    ("trading_bot: CSV header has 14 new cols",  '"body_L","body_S"' in tb_final and 'gap_L' in tb_final),
    ("trading_bot: _score_dual called w/ breakdown", 'return_breakdown=True' in tb_final),
    ("trading_bot: 5-min candles archived",       'candles_5min_' in tb_final),
    ("trading_bot: CSV row includes breakdown",   '_brk.get("body_S"' in tb_final),
    ("Syntax OK dual_scorer.py",  ast.parse(ds_final) is not None or True),
    ("Syntax OK trading_bot.py",  ast.parse(tb_final) is not None or True),
]
all_ok = True
for label, passed in checks:
    icon = "OK" if passed else "FAIL"
    print(f"   [{icon}] {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("ALL CHECKS PASSED")
    print()
    print("WHAT HAPPENS NOW:")
    print("  Every day, candidate_scores_{date}.csv gains 14 columns showing")
    print("  exactly how many points each of the 7 components contributed.")
    print("  candle_archive/candles_5min_{date}/{ticker}.csv captures the")
    print("  5-min OHLCV data used for scoring -- run any historical day")
    print("  through the (now-transparent) scoring formula for backtesting.")
    print()
    print("Restart (safe on Saturday, market closed):")
    print("  sudo systemctl restart trading-bot && sleep 3 && sudo systemctl status trading-bot | head -5")
else:
    print("SOME CHECKS FAILED -- restoring backups")
    shutil.copy(bak1, DS_PATH)
    shutil.copy(bak2, TB_PATH)
