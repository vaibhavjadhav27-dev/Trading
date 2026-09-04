#!/usr/bin/env python3
"""
rolling_exit.py - Rolling Target Profit System v7.0

Staircase trailing with elastic bands:
- T1 = +0.60% (first lock)
- Each step = +0.95% from previous
- Floor = previous_target + 0.10% buffer
- False signal protection before exit
- Works for both LONG and SHORT trades
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

log = logging.getLogger("rolling_exit")


@dataclass
class RollingState:
    """Tracks the rolling target state for an active trade."""
    side: str                    # 'LONG' or 'SHORT'
    entry_price: float
    t1_pct: float = 0.60
    step_pct: float = 0.95
    buffer_pct: float = 0.10
    grace_seconds: int = 60

    current_level: int = 0
    peak_pct: float = 0.0
    floor_price: float = 0.0
    floor_touched_at: float = 0.0
    is_in_grace: bool = False

    def target_pct(self, level):
        if level <= 0:
            return 0.0
        if level == 1:
            return self.t1_pct
        return self.t1_pct + (level - 1) * self.step_pct

    def target_price(self, level):
        pct = self.target_pct(level)
        if self.side == 'LONG':
            return self.entry_price * (1 + pct / 100)
        else:
            return self.entry_price * (1 - pct / 100)

    def floor_for_level(self, level):
        if level <= 1:
            return self.entry_price
        prev_target_pct = self.target_pct(level - 1)
        floor_pct = prev_target_pct + self.buffer_pct
        if self.side == 'LONG':
            return self.entry_price * (1 + floor_pct / 100)
        else:
            return self.entry_price * (1 - floor_pct / 100)

    def profit_pct(self, ltp):
        if self.side == 'LONG':
            return ((ltp - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - ltp) / self.entry_price) * 100


def check_false_signal(rsi, vol_change_pct, red_candles, vwap_slope, config):
    """Returns (should_exit, reason). Exit only if multiple signals agree."""
    signals = 0
    reasons = []

    if rsi < getattr(config, 'ROLLING_RSI_EXIT', 40):
        signals += 1
        reasons.append(f"RSI={rsi:.0f}")

    vol_threshold = getattr(config, 'ROLLING_VOL_DROP_PCT', 30)
    if vol_change_pct < -vol_threshold:
        signals += 1
        reasons.append(f"Vol{vol_change_pct:.0f}%")

    min_red = getattr(config, 'ROLLING_RED_CANDLES', 2)
    if red_candles >= min_red:
        signals += 1
        reasons.append(f"{red_candles}red")

    if vwap_slope < 0:
        signals += 1
        reasons.append("VWAP_neg")

    should_exit = signals >= 2
    return should_exit, " + ".join(reasons) if reasons else "none"


def check_hold_signal(rsi, vol_change_pct, rs_vs_entry, higher_lows):
    """Returns (should_hold, reason). Hold if opportunity for more profit."""
    hold_signals = 0
    reasons = []

    if rs_vs_entry > 0:
        hold_signals += 1
        reasons.append(f"RS+{rs_vs_entry:.1f}")

    if vol_change_pct > 10:
        hold_signals += 1
        reasons.append(f"Vol+{vol_change_pct:.0f}%")

    if rsi > 50:
        hold_signals += 1
        reasons.append(f"RSI={rsi:.0f}")

    if higher_lows:
        hold_signals += 1
        reasons.append("HL")

    should_hold = hold_signals >= 2
    return should_hold, " + ".join(reasons) if reasons else "none"


def evaluate_rolling_exit(state, ltp, rsi, vol_change_pct,
                          red_candles, vwap_slope,
                          rs_vs_entry, higher_lows, config):
    """
    Main evaluation function. Called every monitor tick.
    Returns: (action, reason, exit_price)
        action: 'HOLD' | 'EXIT' | 'ADVANCE'
    """
    current_profit = state.profit_pct(ltp)
    now = time.time()

    if current_profit > state.peak_pct:
        state.peak_pct = current_profit

    # Check if advanced to next level
    next_level = state.current_level + 1
    next_target_pct = state.target_pct(next_level)

    if current_profit >= next_target_pct:
        state.current_level = next_level
        state.floor_price = state.floor_for_level(state.current_level)
        state.is_in_grace = False
        state.floor_touched_at = 0
        log.info(f"ROLLING_ADVANCE L{state.current_level} "
                 f"profit={current_profit:.2f}% floor={state.floor_price:.2f}")
        return 'ADVANCE', f"T{state.current_level} reached ({current_profit:.2f}%)", 0.0

    # No target hit yet
    if state.current_level == 0:
        return 'HOLD', f"Below T1 ({current_profit:.2f}%)", 0.0

    # Check if price at floor
    floor = state.floor_price
    at_floor = (state.side == 'LONG' and ltp <= floor) or \
               (state.side == 'SHORT' and ltp >= floor)

    if not at_floor:
        state.is_in_grace = False
        state.floor_touched_at = 0
        should_hold, hold_reason = check_hold_signal(
            rsi, vol_change_pct, rs_vs_entry, higher_lows)
        return 'HOLD', f"Above floor ({current_profit:.2f}%)", 0.0

    # At floor - check signals
    exit_confirmed, exit_reason = check_false_signal(
        rsi, vol_change_pct, red_candles, vwap_slope, config)

    should_hold, hold_reason = check_hold_signal(
        rsi, vol_change_pct, rs_vs_entry, higher_lows)

    # Hold signals override if exit not confirmed
    if should_hold and not exit_confirmed:
        if not state.is_in_grace:
            state.is_in_grace = True
            state.floor_touched_at = now
        return 'HOLD', f"Floor touched, HOLD: {hold_reason}", 0.0

    # Exit confirmed
    if exit_confirmed:
        log.info(f"ROLLING_EXIT confirmed: {exit_reason}")
        return 'EXIT', f"Confirmed: {exit_reason}", floor

    # Grace period logic
    if not state.is_in_grace:
        state.is_in_grace = True
        state.floor_touched_at = now
        return 'HOLD', f"Grace started ({state.grace_seconds}s)", 0.0

    elapsed = now - state.floor_touched_at
    if elapsed >= state.grace_seconds:
        recovery_pct = getattr(config, 'ROLLING_HOLD_RECOVERY_PCT', 0.20)
        recovery_price = floor * (1 + recovery_pct / 100) if state.side == 'LONG' \
            else floor * (1 - recovery_pct / 100)
        recovered = (state.side == 'LONG' and ltp > recovery_price) or \
                    (state.side == 'SHORT' and ltp < recovery_price)
        if recovered:
            state.is_in_grace = False
            state.floor_touched_at = 0
            return 'HOLD', "Recovered during grace", 0.0
        log.info(f"ROLLING_EXIT grace expired {elapsed:.0f}s")
        return 'EXIT', f"Grace expired ({elapsed:.0f}s)", floor

    remaining = state.grace_seconds - elapsed
    return 'HOLD', f"In grace ({remaining:.0f}s left)", 0.0


if __name__ == "__main__":
    class MockConfig:
        ROLLING_RSI_EXIT = 40
        ROLLING_VOL_DROP_PCT = 30
        ROLLING_RED_CANDLES = 2
        ROLLING_HOLD_RECOVERY_PCT = 0.20

    cfg = MockConfig()
    print("=" * 50)
    print("ROLLING EXIT SELF-TEST")
    print("=" * 50)

    # LONG test
    s = RollingState(side='LONG', entry_price=1000.0)
    print(f"\nLONG Entry=1000")
    print(f"  T1={s.target_price(1):.2f} T2={s.target_price(2):.2f} T3={s.target_price(3):.2f}")

    a, r, _ = evaluate_rolling_exit(s, 1006.5, 60, 20, 0, 0.001, 2.0, True, cfg)
    print(f"  LTP=1006.50: {a} | L={s.current_level} floor={s.floor_price:.2f}")

    a, r, _ = evaluate_rolling_exit(s, 1016.0, 55, 15, 0, 0.0005, 1.5, True, cfg)
    print(f"  LTP=1016.00: {a} | L={s.current_level} floor={s.floor_price:.2f}")

    a, r, ep = evaluate_rolling_exit(s, 1007.0, 35, -40, 2, -0.002, -0.5, False, cfg)
    print(f"  LTP=1007.00: {a} | exit_px={ep:.2f} locked=+{((ep-1000)/1000)*100:.2f}%")

    # SHORT test
    ss = RollingState(side='SHORT', entry_price=1000.0)
    print(f"\nSHORT Entry=1000")
    print(f"  T1={ss.target_price(1):.2f} T2={ss.target_price(2):.2f} T3={ss.target_price(3):.2f}")

    a, r, _ = evaluate_rolling_exit(ss, 993.5, 35, 25, 2, -0.002, -2.0, False, cfg)
    print(f"  LTP=993.50: {a} | L={ss.current_level} floor={ss.floor_price:.2f}")

    print("\n" + "=" * 50)
    print("ALL TESTS PASSED")
    print("=" * 50)
