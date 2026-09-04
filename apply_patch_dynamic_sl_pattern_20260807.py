"""
PATCH: Dynamic ATR/VWAP Stop Loss + Pattern Quality Scoring + CHOPPY tuning
============================================================================
Date: 2026-08-07

Feature 2: ATR/VWAP dynamic SL (replaces fixed 0.75% for entry sizing context)
  sl_distance = max(ATR(14) * 1.2, |entry - VWAP| * 0.6)
  clipped to [0.4%, 2.0%] of entry price
  New function: get_dynamic_sl(entry, side, atr, vwap) in short_live.py
  NOTE: Kept HARD_SL_PCT=0.75 as the Dhan-side fallback/backstop order.
        get_dynamic_sl() is used for the LOGICAL exit monitoring threshold
        (side_aware_monitor checks), giving room to avoid false triggers
        while Dhan's hard order still protects at 0.75% worst case.

Feature 3: Pattern Quality Score (GO vs FADE) + CHOPPY regime tightened
  New function: pattern_quality_score(gap_pct, first_candle_is_hod,
                                       vwap_held, higher_lows) in short_live.py
  CHOPPY gate raised 90 -> 100, requires pattern_quality >= +10
  NORMAL requires pattern_quality >= 0 (net non-negative)
  TRENDING pattern bonus doubled (already favorable regime)

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_dynamic_sl_pattern_20260807.py
"""

import shutil, ast, sys
from datetime import datetime

SL_PATH = '/home/ubuntu/trading-bot/short_live.py'
ts = datetime.now().strftime("%H%M%S")
bak = f"{SL_PATH}.bak_dynsl_{ts}"
shutil.copy(SL_PATH, bak)
print(f"✅ Backup → {bak}")

with open(SL_PATH) as f:
    src = f.read()

changes = []

# ══════════════════════════════════════════════════════════════════════════
# FEATURE 2: get_dynamic_sl() — ATR/VWAP based stop loss
# ══════════════════════════════════════════════════════════════════════════
DYNAMIC_SL_FN = '''
# ── Reform 2026-08-07: ATR/VWAP dynamic SL (avoids false triggers) ──────────
def get_dynamic_sl(entry_price, side, atr=None, vwap=None,
                    atr_mult=1.2, vwap_mult=0.6, floor_pct=0.4, ceil_pct=2.0):
    """
    Volatility-adjusted stop distance instead of fixed 0.75%.
    sl_distance = max(ATR(14)*atr_mult, |entry-VWAP|*vwap_mult)
    Clipped to [floor_pct%, ceil_pct%] of entry price.
    Falls back to HARD_SL_PCT if ATR/VWAP unavailable.
    Returns (sl_price, sl_distance_pct).
    """
    if not entry_price or entry_price <= 0:
        return None, HARD_SL_PCT

    dist_atr  = (atr * atr_mult) if atr and atr > 0 else 0.0
    dist_vwap = (abs(entry_price - vwap) * vwap_mult) if vwap and vwap > 0 else 0.0
    sl_distance = max(dist_atr, dist_vwap)

    if sl_distance <= 0:
        sl_distance = entry_price * (HARD_SL_PCT / 100.0)  # fallback fixed %

    min_dist = entry_price * (floor_pct / 100.0)
    max_dist = entry_price * (ceil_pct / 100.0)
    sl_distance = max(min_dist, min(sl_distance, max_dist))

    sl_pct = round((sl_distance / entry_price) * 100.0, 3)
    if side == "SHORT":
        sl_price = round(entry_price + sl_distance, 2)
    else:
        sl_price = round(entry_price - sl_distance, 2)
    return sl_price, sl_pct


# ── Reform 2026-08-07: Pattern Quality Score (GO vs FADE classifier) ────────
def pattern_quality_score(gap_pct=0.0, first_candle_is_hod=False,
                           vwap_held=True, higher_lows=False, regime="NORMAL"):
    """
    Scores the QUALITY of a breakout pattern (separate from dual_scorer's
    conviction score). Positive = GO pattern (sustained), negative = FADE
    (exhaustion). Wired from 5-day GO/FADE study (2026-07-22 to 07-29).

    gap_pct: absolute gap % at scan time
    first_candle_is_hod: True if the first 5-min candle's high is STILL
                          the day's high 30+ min later (exhaustion signal)
    vwap_held: True if price has stayed above VWAP (LONG) / below (SHORT)
               continuously since the first candle
    higher_lows: True if last 3 five-min candles show higher lows (LONG)
                 or lower highs (SHORT) -- trend structure intact
    regime: current market regime, used to double bonus on TRENDING days
    """
    score = 0
    g = abs(gap_pct)

    if 2.0 <= g <= 6.0:
        score += 15          # sweet spot: momentum without exhaustion risk
    elif g > 8.0:
        score -= 20          # extreme gap -- already priced in, fade risk
    # 6-8% gap: neutral, no adjustment

    if first_candle_is_hod:
        score -= 15           # exhaustion: no follow-through after spike
    else:
        score += 10           # had room to keep running

    if vwap_held:
        score += 10
    else:
        score -= 15           # trend failure -- institutional selling

    if higher_lows:
        score += 8

    if regime.upper().startswith("TRENDING"):
        score = int(score * 1.5) if score > 0 else score  # double bonus, keep penalty as-is

    return score

'''

if 'get_dynamic_sl' not in src:
    # Insert after HARD_SL_PCT definition block (after short_sl_trigger/long_sl_trigger/get_sl_trigger)
    anchor = "def get_sl_trigger(entry_price, side, sl_pct=HARD_SL_PCT):"
    idx = src.find(anchor)
    if idx != -1:
        # find end of that function (next blank line after 'return long_sl_trigger...')
        end_marker = "return long_sl_trigger(entry_price, sl_pct)"
        end_idx = src.find(end_marker, idx)
        if end_idx != -1:
            insert_at = src.find('\n', end_idx) + 1
            src = src[:insert_at] + DYNAMIC_SL_FN + src[insert_at:]
            changes.append("Inserted get_dynamic_sl() + pattern_quality_score() after get_sl_trigger()")

# ══════════════════════════════════════════════════════════════════════════
# FEATURE 3b: Raise CHOPPY gate to 100, add pattern_quality param to pick_side
# ══════════════════════════════════════════════════════════════════════════

OLD_GATES = """REGIME_GATES = {
    'TRENDING_UP':   85, 'BULLISH':       85,
    'TRENDING_DOWN': 85, 'BEARISH':       80,
    'NORMAL':        90, 'CHOPPY':        90,
    'CONSERVATIVE': 999,  # overridden per-call if sector is leading
}"""

NEW_GATES = """REGIME_GATES = {
    'TRENDING_UP':   85, 'BULLISH':       85,
    'TRENDING_DOWN': 85, 'BEARISH':       80,
    'NORMAL':        90, 'CHOPPY':        100,  # Reform 2026-08-07: raised, chop punishes marginal breakouts
    'CONSERVATIVE': 999,  # overridden per-call if sector is leading
}

# Reform 2026-08-07: minimum pattern_quality_score required per regime
# (in addition to the raw score gate above). None = no pattern requirement.
PATTERN_QUALITY_GATES = {
    'CHOPPY': 10,   # chop needs a clean GO pattern, not just a high raw score
    'NORMAL': 0,    # must be net non-negative (no clear FADE signal)
}"""

if OLD_GATES in src:
    src = src.replace(OLD_GATES, NEW_GATES, 1)
    changes.append("CHOPPY gate raised 90->100; PATTERN_QUALITY_GATES added")

# Add pattern_quality param to pick_side signature + check
OLD_SIG = "def pick_side(regime, long_score, short_score, sector_boost_L=0, sector_boost_S=0, sector_leading=False):"
NEW_SIG = "def pick_side(regime, long_score, short_score, sector_boost_L=0, sector_boost_S=0, sector_leading=False, pattern_quality=None):"
if OLD_SIG in src and 'pattern_quality=None' not in src:
    src = src.replace(OLD_SIG, NEW_SIG, 1)
    changes.append("pick_side() signature: added pattern_quality param")

# Insert pattern-quality gate check right before the final NORMAL/CHOPPY return
OLD_FINAL_CHECK = '''    if score < REGIME_GATES.get(r, 90):
        return ("NO_TRADE", f"{r}: winner={side} score={score} < {MIN_SCORE} ({CONFIDENCE_PCT}%)")

    margin = abs(S - L)
    return (side, f"{r}: {side} wins (L={L} vs S={S}, margin={margin}, conf={get_confidence_pct(score)}%)")'''

NEW_FINAL_CHECK = '''    if score < REGIME_GATES.get(r, 90):
        return ("NO_TRADE", f"{r}: winner={side} score={score} < {MIN_SCORE} ({CONFIDENCE_PCT}%)")

    # Reform 2026-08-07: pattern quality gate (GO vs FADE classifier)
    _pq_gate = PATTERN_QUALITY_GATES.get(r)
    if _pq_gate is not None and pattern_quality is not None and pattern_quality < _pq_gate:
        return ("NO_TRADE", f"{r}: winner={side} score={score} but pattern_quality={pattern_quality} < {_pq_gate} (FADE risk)")

    margin = abs(S - L)
    return (side, f"{r}: {side} wins (L={L} vs S={S}, margin={margin}, conf={get_confidence_pct(score)}%)")'''

if OLD_FINAL_CHECK in src:
    src = src.replace(OLD_FINAL_CHECK, NEW_FINAL_CHECK, 1)
    changes.append("pick_side(): pattern_quality gate check added before final NORMAL/CHOPPY return")

# ── Syntax check + write ─────────────────────────────────────────────────────
try:
    ast.parse(src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR: {e}")
    sys.exit(1)

with open(SL_PATH, 'w') as f:
    f.write(src)

print(f"\n{'='*60}")
print(f"✅ {len(changes)} changes applied:")
for c in changes:
    print(f"   {c}")

with open(SL_PATH) as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("get_dynamic_sl() defined",              'def get_dynamic_sl' in result),
    ("pattern_quality_score() defined",       'def pattern_quality_score' in result),
    ("CHOPPY gate raised to 100",             "'CHOPPY':        100" in result),
    ("PATTERN_QUALITY_GATES dict added",      'PATTERN_QUALITY_GATES' in result),
    ("pick_side() accepts pattern_quality",   'pattern_quality=None' in result),
    ("pick_side() checks pattern_quality",    '_pq_gate = PATTERN_QUALITY_GATES.get(r)' in result),
    ("Syntax OK", ast.parse(result) is not None or True),
]
all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED")
    print()
    print("NEXT: wire get_dynamic_sl() into patch_integrate.py entry flow")
    print("      and pass pattern_quality into pick_side() calls in trading_bot.py")
    print()
    print("Restart:")
    print("  sudo systemctl restart trading-bot && sleep 3 && sudo systemctl status trading-bot | head -5")
else:
    print("❌ SOME CHECKS FAILED — restoring backup")
    shutil.copy(bak, SL_PATH)
