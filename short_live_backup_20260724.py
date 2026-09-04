#!/usr/bin/env python3
"""
short_live.py — Live short/long side-selecting intraday module for the NSE bot.

4-tier architecture (all params confirmed by user 2026-07-24):
  Tier 1 DIRECTION : pick_side() — BEARISH→short bias; NORMAL/CHOPPY→compare
                     scores head-to-head with SHORT >= LONG + 15 guard.
  Tier 2 SIZING    : 95% of balance; 2X margin ONLY when score>=110 AND
                     regime != CHOPPY; else 1X.
  Tier 3 HARD SL   : Fixed 0.75% adverse move, placed as a SEPARATE server-side
                     STOP_LOSS_MARKET order right after entry (survives bot crash —
                     API has no bracket/Super Order).
  Tier 4 TRAIL SL  : profit-lock ladder (0.60→0.55, 1.0→0.75, 1.5→1.30, 1.8→1.60,
                     2.0→1.75, 2.30→2.15, then +0.30% checkpoints, buffer 0.15).

Noise filter (inverted for SHORT): hold the short while price is BELOW VWAP and
the sector index is RED; require a 1-min candle CLOSE beyond the floor to exit
(3–5s tick check impossible on ~15s polling).

SAFETY: MIS/intraday only (auto square-off ~15:15 IST — also what unlocks 2X).
First live session runs a PARALLEL shadow-log of fills vs. predictions.
"""

import time
import datetime

# ── Confirmed parameters ──────────────────────────────────────────────
CAPITAL_PCT      = 0.95      # deploy 95% of available balance
SCORE_2X         = 110       # score threshold for 2X margin
HARD_SL_PCT      = 0.75      # fixed hard stop (adverse move from entry), %
SHORT_MARGIN     = 15        # short must beat long by this in NORMAL/CHOPPY
SECTOR_STRONG    = 0.5       # sector-strength ignore threshold, %
REEARM_NOTE      = "MIS only; auto square-off ~15:15 IST"


# ── Tier 1: direction ─────────────────────────────────────────────────
def pick_side(regime, long_score, short_score,
              short_needs_margin=SHORT_MARGIN):
    """Return (side, reason). side in {LONG, SHORT, NO_TRADE}.
    Short must beat long by `short_needs_margin` in non-directional regimes
    (encodes the -25.5 short backtest — skeptical of shorts)."""
    L = long_score if long_score is not None else -1
    S = short_score if short_score is not None else -1
    if L < 0 and S < 0:
        return ("NO_TRADE", "no qualifier either side")
    r = regime.upper()
    if r in ("BULLISH", "TRENDING_UP"):
        return ("LONG", f"{r}: long-only") if L >= 0 else ("NO_TRADE", f"{r}, no long")
    if r in ("BEARISH", "TRENDING_DOWN"):
        return ("SHORT", f"{r}: short bias (S={S})") if S >= 0 else ("NO_TRADE", f"{r}, no short")
    # NORMAL / CHOPPY → head-to-head, short must clear margin
    if S >= 0 and (S - L) >= short_needs_margin:
        return ("SHORT", f"{r}: short wins by {S-L} (>= {short_needs_margin})")
    if L >= 0:
        why = "long higher" if L >= S else f"short only +{S-L} (< {short_needs_margin}, LONG-bias)"
        return ("LONG", f"{r}: {why}")
    return ("SHORT", f"{r}: short only side (S={S})")


# ── Tier 2: sizing ────────────────────────────────────────────────────
def size_position(balance, price, score, regime):
    """Return (qty, leverage, deployed_capital)."""
    lev = 2 if (score >= SCORE_2X and regime.upper() != 'CHOPPY') else 1
    deploy = balance * CAPITAL_PCT * lev
    qty = int(deploy // price)
    return qty, lev, deploy


# ── Tier 3: hard stop (separate server-side order) ────────────────────
def short_sl_trigger(entry_price, sl_pct=HARD_SL_PCT):
    """SHORT loses when price RISES → SL trigger sits ABOVE entry."""
    return round(entry_price * (1 + sl_pct / 100.0), 2)

def long_sl_trigger(entry_price, sl_pct=HARD_SL_PCT):
    """LONG loses when price FALLS → SL trigger sits BELOW entry."""
    return round(entry_price * (1 - sl_pct / 100.0), 2)


# ── Tier 4: trailing profit-lock ladder ───────────────────────────────
def profit_lock_floor(peak_pct):
    """Guaranteed lock-in floor (profit %) given peak profit % reached.
    None => not armed yet (< 0.60%). Validated monotonic."""
    if peak_pct >= 2.30:
        n = int((peak_pct - 2.30) // 0.30)
        checkpoint = round(2.30 + n * 0.30, 2)
        return round(checkpoint - 0.15, 2)
    if peak_pct >= 2.00: return 1.75
    if peak_pct >= 1.80: return 1.60
    if peak_pct >= 1.50: return 1.30
    if peak_pct >= 1.00: return 0.75
    if peak_pct >= 0.60: return 0.55
    return None

def cover_floor_price(entry_price, floor_pct, side):
    """Price at which the trailing floor would trigger an exit."""
    if side == "SHORT":   # profit = price down; floor sits below entry
        return round(entry_price * (1 - floor_pct / 100.0), 2)
    else:                 # LONG: profit = price up; floor sits above entry
        return round(entry_price * (1 + floor_pct / 100.0), 2)


# ── Noise filter (inverted for SHORT) ─────────────────────────────────
def confirm_exit(side, price, floor_price, vwap, vol, vol_ma20, sector_chg_pct,
                 candle_closed_beyond):
    """Return (should_exit, reason). Applies VWAP / volume / sector / candle-close
    filters so flash-wicks and low-volume shakeouts don't stop us out.
    For SHORT: 'against us' = price rising back toward/through floor."""
    if side == "SHORT":
        breached = price >= floor_price
        trend_still_ours = price < vwap          # still below VWAP = bearish intact
        sector_supports  = sector_chg_pct < -SECTOR_STRONG   # sector strongly red
    else:
        breached = price <= floor_price
        trend_still_ours = price > vwap          # still above VWAP = bullish intact
        sector_supports  = sector_chg_pct > SECTOR_STRONG    # sector strongly green
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


# ── Order helpers (reuse DhanClient auth/rate-limit path) ──────────────
def place_entry(dhan, security_id, qty, price, side):
    """Entry via existing place_order (single-leg LIMIT)."""
    txn = "SELL" if side == "SHORT" else "BUY"
    return dhan.place_order(security_id, qty, price,
                            transaction_type=txn, order_type="LIMIT")

def place_hard_sl(dhan, security_id, qty, entry_price, side):
    """Separate server-side STOP_LOSS_MARKET so the stop survives a bot crash.
    place_order() doesn't send triggerPrice, so post directly via _request()."""
    if side == "SHORT":
        txn, trig = "BUY", short_sl_trigger(entry_price)   # cover if price rises
    else:
        txn, trig = "SELL", long_sl_trigger(entry_price)   # sell if price falls
    payload = {
        "dhanClientId": dhan.client_id,          # NOTE: self.client_id (confirmed)
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


# ── Shadow logger (first-session seatbelt) ────────────────────────────
def shadow_log(path, record):
    line = f"{datetime.datetime.now().isoformat()} | {record}\n"
    with open(path, "a") as f:
        f.write(line)


if __name__ == "__main__":
    # Self-test (no network): prove the engine end-to-end on a sample.
    regime, L, S = "TRENDING_DOWN", 60, 130
    side, why = pick_side(regime, L, S)
    print("pick_side:", side, "-", why)
    if side in ("LONG", "SHORT"):
        qty, lev, dep = size_position(49407.0, 250.0, S if side == "SHORT" else L, regime)
        sl = short_sl_trigger(250.0) if side == "SHORT" else long_sl_trigger(250.0)
        print(f"sizing: {qty} sh @ {lev}x (deploy ₹{dep:,.0f}); hard SL trigger ₹{sl}")
        for pk in (0.60, 1.50, 2.30, 2.90):
            f = profit_lock_floor(pk)
            print(f"  peak {pk:.2f}% -> lock {f:.2f}% -> cover px "
                  f"₹{cover_floor_price(250.0, f, side)}")
