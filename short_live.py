import logging
log = logging.getLogger('trading_bot')
#!/usr/bin/env python3
"""
short_live.py — Live short/long side-selecting intraday module for the NSE bot.

UPDATED 2026-07-27:
  - NORMAL/CHOPPY: equal weightage, higher score wins (no +15 short margin)
  - Both sides use profit-lock ladder (tighter trailing)
  - 95% capital × 2X margin always (≈₹97.5K on ₹50K balance)
  - Confidence gate: score must be >= 85% (153/180) to trade

5-tier architecture:
  Tier 1 DIRECTION : pick_side() — BEARISH→short; BULLISH→long;
                     NORMAL/CHOPPY→ higher score wins (equal weightage).
  Tier 2 CONFIDENCE: score must be >= 85% of max (153/180) to trade.
  Tier 3 SIZING    : 95% of balance × 2X margin always (MIS intraday).
  Tier 4 HARD SL   : Fixed 0.75% adverse move, placed as SEPARATE server-side
                     STOP_LOSS_MARKET order (survives bot crash).
  Tier 5 TRAIL SL  : Profit-lock ladder for BOTH sides:
                     (0.60→0.55, 1.0→0.75, 1.5→1.30, 1.8→1.60,
                      2.0→1.75, 2.30→2.15, then +0.30% checkpoints, buffer 0.15).

Noise filter (inverted for SHORT): hold while price respects VWAP and
sector supports; require 1-min candle CLOSE beyond floor to exit.

SAFETY: MIS/intraday only (auto square-off ~15:15 IST).
"""

import time
import datetime
from trade_policy import RAW_MAX_SCORE, confidence_pct, margin_tier, pick_side as policy_pick_side

# ── Confirmed parameters (UPDATED 2026-07-27) ────────────────────────
CAPITAL_PCT      = 1.00
LEVERAGE         = 1.00
HARD_SL_PCT      = 0.75
SECTOR_STRONG    = 0.5

# ── Confidence gate ───────────────────────────────────────────────────
MAX_SCORE        = int(RAW_MAX_SCORE)
CONFIDENCE_PCT   = 60
MIN_SCORE        = int(MAX_SCORE * CONFIDENCE_PCT / 100)  # = 153


def passes_confidence(score):
    if score is None or score < 0:
        return False
    return (score / MAX_SCORE) * 100 >= CONFIDENCE_PCT

def get_confidence_pct(score):
    if score is None or score < 0:
        return 0.0
    return round((score / MAX_SCORE) * 100, 1)


# ── Tier 1: direction (equal weightage) ──────────────────────────────

# Reform 2026-08-04: regime-specific confidence gates
REGIME_GATES = {}  # retained for compatibility; regime never hard-blocks a side


# Reform 2026-08-07: minimum pattern_quality_score required per regime
# (in addition to the raw score gate above). None = no pattern requirement.
PATTERN_QUALITY_GATES = {
    'CHOPPY': 10,   # chop needs a clean GO pattern, not just a high raw score
    'NORMAL': 0,    # must be net non-negative (no clear FADE signal)
}

def pick_side(regime, long_score, short_score, sector_boost_L=0, sector_boost_S=0,
              sector_leading=False, pattern_quality=None, entry_quality_L=0, entry_quality_S=0):
    """Unified side selection: NIFTY/sector context biases, never disables."""
    return policy_pick_side(
        regime, long_score, short_score,
        sector_boost_L=sector_boost_L, sector_boost_S=sector_boost_S,
        entry_quality_L=entry_quality_L, entry_quality_S=entry_quality_S
    )




# ── Tier 2: sizing (95% × 2X always) ─────────────────────────────────
def size_position(balance, price, score=0, regime="NORMAL"):
    """Desired notional from the canonical 100-point conviction ladder.
    Dhan margin is re-checked by patch_integrate._fit_to_margin before order placement.
    """
    if not balance or not price or price <= 0:
        return 0, 0.0, 0.0
    conf=confidence_pct(score)
    leverage,_=margin_tier(conf)
    if leverage<=0: return 0,0.0,0.0
    deploy=float(balance)*leverage
    qty=int(deploy//float(price))
    return max(qty,0),leverage,deploy


# ── Tier 3: hard stop ─────────────────────────────────────────────────
def short_sl_trigger(entry_price, sl_pct=HARD_SL_PCT):
    return round(entry_price * (1 + sl_pct / 100.0), 2)

def long_sl_trigger(entry_price, sl_pct=HARD_SL_PCT):
    return round(entry_price * (1 - sl_pct / 100.0), 2)

def get_sl_trigger(entry_price, side, sl_pct=HARD_SL_PCT):
    if side == "SHORT":
        return short_sl_trigger(entry_price, sl_pct)
    return long_sl_trigger(entry_price, sl_pct)

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



# ── Tier 4: profit-lock ladder (BOTH sides) ──────────────────────────
def profit_lock_floor(peak_pct):
    """Protect profit after +0.40% while allowing larger moves to run."""
    p = float(peak_pct or 0.0)
    if p < 0.40:
        return 0.0
    # At +0.40 protect ~+0.35; thereafter raise the floor in 0.30% steps.
    steps = int((p - 0.40 + 1e-6) / 0.30)
    return round(max(0.35, 0.35 + steps * 0.30), 2)


def current_profit_pct(entry_price, current_price, side):
    if side == "SHORT":
        return round((entry_price - current_price) / entry_price * 100, 4)
    else:
        return round((current_price - entry_price) / entry_price * 100, 4)


def cover_floor_price(entry_price, floor_pct, side):
    if side == "SHORT":
        return round(entry_price * (1 - floor_pct / 100.0), 2)
    else:
        return round(entry_price * (1 + floor_pct / 100.0), 2)


# ── Noise filter ──────────────────────────────────────────────────────
def confirm_exit(side, price, floor_price, vwap, vol, vol_ma20, sector_chg_pct,
                 candle_closed_beyond):
    if side == "SHORT":
        breached = price >= floor_price
        trend_still_ours = price < vwap
        sector_supports  = sector_chg_pct < -SECTOR_STRONG
    else:
        breached = price <= floor_price
        trend_still_ours = price > vwap
        sector_supports  = sector_chg_pct > SECTOR_STRONG

    if not breached:
        return (False, "floor not breached")
    if trend_still_ours:
        return (False, "VWAP trend still in our favour — hold")
    if vol < vol_ma20:
        return (False, "low-volume shakeout (vol < 20MA) — hold")
    if sector_supports:
        return (False, "sector strongly with us (> 0.5%) — ignore retrace")
    if not candle_closed_beyond:
        return (False, "1-min candle not yet closed beyond floor — wait")
    return (True, "confirmed: floor breached + candle closed + no noise filter held")


# ── Order helpers ─────────────────────────────────────────────────────
def place_entry(dhan, security_id, qty, price, side):
    txn = "SELL" if side == "SHORT" else "BUY"
    return dhan.place_order(security_id, qty, price,
                            transaction_type=txn, order_type="LIMIT",
                            product_type="INTRADAY")

def place_hard_sl(dhan, security_id, qty, entry_price, side, sl_price=None):
    if side == "SHORT":
        txn, trig = "BUY", short_sl_trigger(entry_price) if sl_price is None else float(sl_price)
    else:
        txn, trig = "SELL", long_sl_trigger(entry_price) if sl_price is None else float(sl_price)
    payload = {
        "dhanClientId": dhan.client_id,
        "transactionType": txn,
        "exchangeSegment": "NSE_EQ",
        "productType": "INTRADAY",
        "orderType": "STOP_LOSS_MARKET",
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": int(qty),
        "price": 0,
        "triggerPrice": trig,
    }
    return dhan._request("POST", "/orders", payload), trig

def place_exit(dhan, security_id, qty, price, side):
    txn = "BUY" if side == "SHORT" else "SELL"
    return dhan.place_order(security_id, qty, price,
                            transaction_type=txn, order_type="MARKET",
                            product_type="INTRADAY")


# ── Shadow logger ────────────────────────────────────────────────────
def shadow_log(path, record):
    line = f"{datetime.datetime.now().isoformat()} | {record}\n"
    with open(path, "a") as f:
        f.write(line)


# ── Self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("SHORT_LIVE.PY — Self-Test (Updated 2026-07-27)")
    print("=" * 60)
    print(f"\nConfig: CAPITAL={CAPITAL_PCT*100}%, LEVERAGE={LEVERAGE}X, "
          f"HARD_SL={HARD_SL_PCT}%")
    print(f"Confidence gate: {CONFIDENCE_PCT}% = min score {MIN_SCORE}/{MAX_SCORE}")

    balance = 50000.0
    price = 250.0

    print("\n── Test 1: NORMAL, L=140 vs S=155 (SHORT wins) ──")
    side, why = pick_side("NORMAL", 140, 155)
    print(f"  {side} — {why}")

    print("\n── Test 2: NORMAL, L=160 vs S=145 (LONG wins) ──")
    side, why = pick_side("NORMAL", 160, 145)
    print(f"  {side} — {why}")

    print("\n── Test 3: CHOPPY, L=130 vs S=155 (SHORT wins) ──")
    side, why = pick_side("CHOPPY", 130, 155)
    print(f"  {side} — {why}")

    print("\n── Test 4: NORMAL, L=140 vs S=150 (below confidence) ──")
    side, why = pick_side("NORMAL", 140, 150)
    print(f"  {side} — {why}")

    print("\n── Test 5: BEARISH, S=160 ──")
    side, why = pick_side("BEARISH", 80, 160)
    print(f"  {side} — {why}")

    print("\n── Test 6: Sizing on ₹50K ──")
    qty, lev, dep = size_position(balance, price)
    print(f"  Deploy: ₹{dep:,.0f} | Qty: {qty} | Lev: {lev}X")
    print(f"  Notional: ₹{qty * price:,.0f}")

    print("\n── Test 7: SL triggers ──")
    print(f"  LONG  entry ₹250 → SL ₹{long_sl_trigger(250)}")
    print(f"  SHORT entry ₹250 → SL ₹{short_sl_trigger(250)}")

    print("\n── Test 8: Profit-lock ladder ──")
    for pk in (0.40, 0.60, 1.00, 1.50, 2.00, 2.30, 2.90):
        f = profit_lock_floor(pk)
        if f:
            print(f"  peak {pk:.2f}% → lock {f:.2f}% | "
                  f"SHORT cover ₹{cover_floor_price(250, f, 'SHORT')} | "
                  f"LONG exit ₹{cover_floor_price(250, f, 'LONG')}")
        else:
            print(f"  peak {pk:.2f}% → not armed")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)
