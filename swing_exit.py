#!/usr/bin/env python3
"""swing_exit.py - Single source of truth for swing exit logic.
3-15 day hold, +15% floor that STARTS a trailing ride (no upper cap), hard SL always fires.
Both swing_monitor.py and swing_paper_trader.py import swing_exit_decision() so their
exit behavior can never diverge.
"""

MIN_HOLD_DAYS = 3        # no target/trail/time exit before this (hard SL always honored)
TIME_STOP_DAYS = 15      # max hold
TRAIL_TRIGGER_PCT = 15.0 # +15% activates the tight lock/trail (no upper cap)
TRAIL_FACTOR = 0.93      # after trigger, trail at 93% of peak


def swing_exit_decision(days_held, gain_pct, trailing_sl, entry, peak_price, ltp,
                        min_hold_days=MIN_HOLD_DAYS, time_stop_days=TIME_STOP_DAYS,
                        trail_trigger_pct=TRAIL_TRIGGER_PCT, trail_factor=TRAIL_FACTOR):
    """Return (action, new_trailing_sl).

    action: HARD_SL | TIME_STOP | TRAIL_SL | HOLD(min-hold) | HOLD

    Rules:
      - Once gain >= trail_trigger_pct, ratchet trailing_sl up to
        max(peak*trail_factor, entry*(1+trigger/100)) -> locks >= +15%, rides upside.
      - Hard SL (ltp <= entry-derived stop, i.e. ltp <= trailing_sl when trailing_sl
        is still the original SL) fires ALWAYS, even inside min-hold.
      - Inside min-hold window: only hard SL can exit.
      - After min-hold: trailing SL, then time stop.
    """
    # 1) Ratchet the trailing stop once the +15% trigger is reached (never lowers it)
    if gain_pct >= trail_trigger_pct and entry > 0:
        lock = max(peak_price * trail_factor, entry * (1 + trail_trigger_pct / 100.0))
        if lock > trailing_sl:
            trailing_sl = round(lock, 2)

    # Was the trailing ride ever activated? (peak crossed the +trigger% level)
    trail_active = entry > 0 and peak_price >= entry * (1 + trail_trigger_pct / 100.0)

    # 2) Hard stop-loss always fires first (capital protection), even inside min-hold
    if ltp <= trailing_sl and days_held < min_hold_days:
        return "HARD_SL", trailing_sl

    if days_held < min_hold_days:
        return "HOLD(min-hold)", trailing_sl

    # 3) After min-hold: stop-out is a TRAIL_SL if the trail was ever activated,
    #    otherwise it's the original hard SL.
    if ltp <= trailing_sl:
        return ("TRAIL_SL" if trail_active else "HARD_SL"), trailing_sl

    # 4) Time stop
    if days_held >= time_stop_days:
        return "TIME_STOP", trailing_sl

    return "HOLD", trailing_sl


if __name__ == "__main__":
    # doctest-style smoke checks
    print(swing_exit_decision(1, 20.0, 95.0, 100.0, 120.0, 118.0))   # HOLD(min-hold), 115.0
    print(swing_exit_decision(6, 14.0, 120.9, 100.0, 130.0, 114.0))  # TRAIL_SL, 120.9
    print(swing_exit_decision(16, 8.0, 95.0, 100.0, 110.0, 108.0))   # TIME_STOP
    print(swing_exit_decision(1, -6.0, 95.0, 100.0, 100.0, 94.0))    # HARD_SL
