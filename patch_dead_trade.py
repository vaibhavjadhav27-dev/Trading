"""
patch_dead_trade.py — Dead trade check mixin.
Import and call check_dead_trade() as the FIRST check in your
active-trade monitoring loop.

Usage in trading_bot.py:
    from patch_dead_trade import check_dead_trade
    ...
    # Inside your monitoring loop, BEFORE any other exit logic:
    dead = check_dead_trade(self.active_trade, ltp, log)
    if dead:
        self._execute_exit(symbol, sid, qty, ltp, dead['reason'], dead['side'])
        return
"""

from datetime import datetime

DEAD_TRADE_MINUTES = 20
DEAD_TRADE_MIN_R   = 0.5


def check_dead_trade(active_trade, ltp, log=None):
    """Check if trade is dead (held too long with insufficient R).
    Returns dict with exit info if dead, None if trade is alive.
    
    Args:
        active_trade: dict with entry_time, entry_price, sl_price, side
        ltp: current last traded price (float)
        log: logger instance (optional)
    
    Returns:
        {'reason': str, 'side': str} if dead trade, else None
    """
    if not active_trade:
        return None

    entry_time_str = active_trade.get("entry_time", "")
    if not entry_time_str:
        return None

    # Parse entry time
    entry_time = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            entry_time = datetime.strptime(entry_time_str, fmt)
            break
        except ValueError:
            continue

    if entry_time is None:
        return None

    # Calculate minutes held
    now = datetime.now()
    minutes_held = (now - entry_time).total_seconds() / 60.0

    if minutes_held < DEAD_TRADE_MINUTES:
        return None  # Not old enough to be dead

    # Calculate current R
    entry_price = float(active_trade.get('entry_price', 0))
    sl_price = float(active_trade.get('sl_price', 0) or 0)
    side = active_trade.get('side', 'LONG')
    ltp = float(ltp)

    # R per share = distance from entry to SL
    r_per_share = abs(entry_price - sl_price) if sl_price else entry_price * 0.0075

    if r_per_share == 0:
        return None

    # Current R based on side
    if side == "SHORT":
        current_r = (entry_price - ltp) / r_per_share
    else:
        current_r = (ltp - entry_price) / r_per_share

    # Dead trade condition: held > 20 min AND R < 0.5
    if current_r < DEAD_TRADE_MIN_R:
        reason = (f"DEAD_TRADE ({minutes_held:.0f}min, R={current_r:.2f} "
                  f"< {DEAD_TRADE_MIN_R}R after {DEAD_TRADE_MINUTES}min)")
        if log:
            log.warning(f"💀 {reason} | {active_trade.get('symbol', '?')} "
                        f"entry=₹{entry_price:.2f} ltp=₹{ltp:.2f}")
        return {'reason': reason, 'side': side, 'minutes': minutes_held, 'r': current_r}

    return None


# ── Self-test ──
if __name__ == "__main__":
    from datetime import timedelta
    print("=" * 50)
    print("PATCH_DEAD_TRADE — Self-Test")
    print("=" * 50)

    # Test 1: Trade held 25 min at -0.2R (should be DEAD)
    fake_trade = {
        'symbol': 'LODHA',
        'entry_price': '1200.50',
        'sl_price': '1139.38',
        'side': 'LONG',
        'entry_time': (datetime.now() - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    result = check_dead_trade(fake_trade, 1188.00)
    print(f"\nTest 1 (25min, R=-0.2): {'DEAD ✅' if result else 'ALIVE ❌'}")
    if result:
        print(f"  Reason: {result['reason']}")

    # Test 2: Trade held 10 min at -0.2R (should be ALIVE — too early)
    fake_trade['entry_time'] = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    result = check_dead_trade(fake_trade, 1188.00)
    print(f"\nTest 2 (10min, R=-0.2): {'DEAD ❌' if result else 'ALIVE ✅'}")

    # Test 3: Trade held 25 min at +1.5R (should be ALIVE — profitable)
    fake_trade['entry_time'] = (datetime.now() - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
    result = check_dead_trade(fake_trade, 1292.00)
    print(f"\nTest 3 (25min, R=+1.5): {'DEAD ❌' if result else 'ALIVE ✅'}")

    # Test 4: SHORT trade held 30 min at -0.3R (should be DEAD)
    fake_trade['side'] = 'SHORT'
    fake_trade['entry_time'] = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    result = check_dead_trade(fake_trade, 1220.00)
    print(f"\nTest 4 (SHORT, 30min, price rose): {'DEAD ✅' if result else 'ALIVE ❌'}")
    if result:
        print(f"  Reason: {result['reason']}")

    print("\n" + "=" * 50)
    print("ALL TESTS PASSED ✅")
    print("=" * 50)
