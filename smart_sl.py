#!/usr/bin/env python3
"""
smart_sl.py — Smart Stop Loss with Confirmation
Prevents false SL hits from spikes, errors, and manipulation.

Logic:
  SOFT SL (0.75%): Needs 2 of 4 confirmations + 30s hold
  HARD FLOOR (1.5%): Instant exit, no questions asked

Confirmations required (2 of 4):
  1. Time hold: price below SL for 30 continuous seconds
  2. Volume: sell volume > 1.5x average (real selling pressure)
  3. VWAP: price below VWAP (bearish structure confirmed)
  4. Candle close: current 5-min candle body closes below SL
"""

import time
import logging

log = logging.getLogger(__name__)

# ===== CONFIGURATION =====
SOFT_SL_PCT = 0.75          # First trigger level (%)
HARD_FLOOR_PCT = 1.50       # Emergency exit - no confirmation needed (%)
SL_CONFIRM_SECONDS = 30     # Must stay below SL for this long
SL_CONFIRM_CHECKS_NEEDED = 2  # Need 2 of 4 signals to confirm
RECOVERY_RESET = True       # If price goes back above SL, reset timer

# ===== STATE TRACKING =====
_sl_state = {
    "triggered": False,
    "trigger_time": 0,
    "trigger_price": 0,
    "confirmations": set(),
    "check_count": 0,
}


def reset_sl_state():
    """Reset SL confirmation state (call on new trade or recovery)."""
    global _sl_state
    _sl_state = {
        "triggered": False,
        "trigger_time": 0,
        "trigger_price": 0,
        "confirmations": set(),
        "check_count": 0,
    }
    return _sl_state


def check_smart_sl(
    entry_price,
    current_price,
    side,
    vwap=None,
    current_volume=None,
    avg_volume=None,
    candle_close=None,
    candle_open=None,
):
    """
    Check if SL should trigger with confirmation.

    Returns:
        (should_exit: bool, reason: str, details: dict)

    Usage:
        should_exit, reason, details = check_smart_sl(
            entry_price=1200,
            current_price=1190,
            side="LONG",
            vwap=1195,
            current_volume=50000,
            avg_volume=30000,
            candle_close=1189,
            candle_open=1198,
        )
        if should_exit:
            execute_exit(reason)
    """
    global _sl_state

    # Calculate SL levels
    if side == "LONG":
        soft_sl = entry_price * (1 - SOFT_SL_PCT / 100)
        hard_floor = entry_price * (1 - HARD_FLOOR_PCT / 100)
        below_soft = current_price <= soft_sl
        below_hard = current_price <= hard_floor
        drop_pct = (entry_price - current_price) / entry_price * 100
    else:  # SHORT
        soft_sl = entry_price * (1 + SOFT_SL_PCT / 100)
        hard_floor = entry_price * (1 + HARD_FLOOR_PCT / 100)
        below_soft = current_price >= soft_sl
        below_hard = current_price >= hard_floor
        drop_pct = (current_price - entry_price) / entry_price * 100

    details = {
        "entry": entry_price,
        "current": current_price,
        "soft_sl": round(soft_sl, 2),
        "hard_floor": round(hard_floor, 2),
        "drop_pct": round(drop_pct, 2),
        "confirmations": list(_sl_state["confirmations"]),
        "time_below": 0,
    }

    # ===== HARD FLOOR: INSTANT EXIT =====
    if below_hard:
        reset_sl_state()
        reason = f"HARD_FLOOR_EXIT ({drop_pct:.2f}% drop > {HARD_FLOOR_PCT}%)"
        log.warning(f"🚨 {reason} | price={current_price} floor={hard_floor:.2f}")
        return (True, reason, details)

    # ===== PRICE ABOVE SOFT SL: ALL CLEAR =====
    if not below_soft:
        if _sl_state["triggered"]:
            log.info(f"✅ SL RECOVERED: price={current_price} back above SL={soft_sl:.2f} — resetting timer")
            reset_sl_state()
        return (False, "ABOVE_SL", details)

    # ===== PRICE BELOW SOFT SL: START CONFIRMATION =====
    now = time.time()

    # First time touching SL
    if not _sl_state["triggered"]:
        _sl_state["triggered"] = True
        _sl_state["trigger_time"] = now
        _sl_state["trigger_price"] = current_price
        _sl_state["confirmations"] = set()
        _sl_state["check_count"] = 0
        log.info(f"⚠️ SOFT SL TOUCHED: price={current_price} < SL={soft_sl:.2f} — starting 30s confirmation")
        return (False, "SL_TOUCHED_CONFIRMING", details)

    # Already in confirmation mode — check signals
    _sl_state["check_count"] += 1
    time_below = now - _sl_state["trigger_time"]
    details["time_below"] = round(time_below, 1)

    # Signal 1: Time hold (30s below SL)
    if time_below >= SL_CONFIRM_SECONDS:
        _sl_state["confirmations"].add("TIME_30S")

    # Signal 2: Volume confirmation (sell pressure)
    if current_volume and avg_volume and avg_volume > 0:
        if current_volume > avg_volume * 1.5:
            _sl_state["confirmations"].add("VOLUME_SPIKE")

    # Signal 3: Below VWAP (bearish structure)
    if vwap and vwap > 0:
        if side == "LONG" and current_price < vwap:
            _sl_state["confirmations"].add("BELOW_VWAP")
        elif side == "SHORT" and current_price > vwap:
            _sl_state["confirmations"].add("ABOVE_VWAP")

    # Signal 4: Candle close below SL (not just a wick)
    if candle_close and candle_open:
        if side == "LONG" and candle_close < soft_sl and candle_close < candle_open:
            _sl_state["confirmations"].add("CANDLE_CLOSE")
        elif side == "SHORT" and candle_close > soft_sl and candle_close > candle_open:
            _sl_state["confirmations"].add("CANDLE_CLOSE")

    details["confirmations"] = list(_sl_state["confirmations"])
    num_confirmations = len(_sl_state["confirmations"])

    # ===== CONFIRMED EXIT =====
    if num_confirmations >= SL_CONFIRM_CHECKS_NEEDED:
        signals = ", ".join(_sl_state["confirmations"])
        reason = f"SMART_SL_CONFIRMED ({num_confirmations} signals: {signals})"
        log.warning(f"🔴 {reason} | price={current_price} | time_below={time_below:.0f}s")
        reset_sl_state()
        return (True, reason, details)

    # Still waiting for confirmation
    log.info(
        f"⏳ SL confirming: {num_confirmations}/{SL_CONFIRM_CHECKS_NEEDED} signals "
        f"[{', '.join(_sl_state['confirmations']) or 'none yet'}] "
        f"| time_below={time_below:.0f}s | price={current_price}"
    )
    return (False, "SL_CONFIRMING", details)


def get_sl_levels(entry_price, side):
    """Get the SL price levels for display/logging."""
    if side == "LONG":
        return {
            "soft_sl": round(entry_price * (1 - SOFT_SL_PCT / 100), 2),
            "hard_floor": round(entry_price * (1 - HARD_FLOOR_PCT / 100), 2),
        }
    else:
        return {
            "soft_sl": round(entry_price * (1 + SOFT_SL_PCT / 100), 2),
            "hard_floor": round(entry_price * (1 + HARD_FLOOR_PCT / 100), 2),
        }


# ===== SELF-TEST =====
if __name__ == "__main__":
    print("=" * 60)
    print("SMART SL — Self Test")
    print("=" * 60)

    # Test 1: Price above SL
    reset_sl_state()
    exit, reason, d = check_smart_sl(1200, 1195, "LONG")
    assert not exit, "Should not exit above SL"
    print(f"Test 1 PASS: Above SL -> {reason}")

    # Test 2: Price touches soft SL (first time)
    reset_sl_state()
    exit, reason, d = check_smart_sl(1200, 1190, "LONG")
    assert not exit, "Should not exit on first touch"
    print(f"Test 2 PASS: First touch -> {reason}")

    # Test 3: Hard floor instant exit
    reset_sl_state()
    exit, reason, d = check_smart_sl(1200, 1180, "LONG")
    assert exit, "Should exit at hard floor"
    print(f"Test 3 PASS: Hard floor -> {reason}")

    # Test 4: Recovery resets timer
    reset_sl_state()
    check_smart_sl(1200, 1190, "LONG")  # Touch SL
    exit, reason, d = check_smart_sl(1200, 1195, "LONG")  # Recover
    assert not exit and reason == "ABOVE_SL", "Should reset on recovery"
    print(f"Test 4 PASS: Recovery -> {reason}")

    # Test 5: Confirmed exit with signals
    reset_sl_state()
    check_smart_sl(1200, 1190, "LONG")  # Touch SL
    # Simulate 30s passing
    _sl_state["trigger_time"] = time.time() - 35
    exit, reason, d = check_smart_sl(
        1200, 1188, "LONG",
        vwap=1195,
        current_volume=50000,
        avg_volume=30000,
    )
    assert exit, "Should exit with 3 confirmations"
    print(f"Test 5 PASS: Confirmed exit -> {reason}")

    # Test 6: SHORT side hard floor
    reset_sl_state()
    exit, reason, d = check_smart_sl(1200, 1220, "SHORT")
    assert exit, "Should exit SHORT at hard floor"
    print(f"Test 6 PASS: SHORT hard floor -> {reason}")

    # Test 7: Not enough confirmations
    reset_sl_state()
    check_smart_sl(1200, 1190, "LONG")  # Touch SL
    exit, reason, d = check_smart_sl(1200, 1190, "LONG")  # No time, no signals
    assert not exit, "Should not exit without confirmations"
    print(f"Test 7 PASS: Insufficient signals -> {reason}")

    print("")
    print("=" * 60)
    print("ALL 7 TESTS PASSED ✅")
    print("=" * 60)
    print("")
    print("Configuration:")
    print(f"  SOFT SL:     {SOFT_SL_PCT}% (needs {SL_CONFIRM_CHECKS_NEEDED} of 4 confirmations)")
    print(f"  HARD FLOOR:  {HARD_FLOOR_PCT}% (instant exit, no confirmation)")
    print(f"  TIME HOLD:   {SL_CONFIRM_SECONDS}s below SL")
    print(f"  SIGNALS:     TIME_30S, VOLUME_SPIKE, BELOW_VWAP, CANDLE_CLOSE")
    print("")
    levels = get_sl_levels(1200, "LONG")
    print(f"  Example (LONG @ 1200):")
    print(f"    Soft SL:    {levels['soft_sl']}")
    print(f"    Hard Floor: {levels['hard_floor']}")
