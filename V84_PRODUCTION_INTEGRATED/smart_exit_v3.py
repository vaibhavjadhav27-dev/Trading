"""Smart Exit v3 - Priority-Based Exit Hierarchy"""
import logging
from datetime import datetime

log = logging.getLogger(__name__)

class SmartExitV3:
    def __init__(self, entry_price, sl_price, orb_high, orb_low, side="LONG"):
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.orb_high = orb_high
        self.orb_low = orb_low
        self.side = side
        self.entry_time = datetime.now()
        self.max_price = entry_price
        self.candle_closes = []
        self.candle_volumes = []
        self.vwap = None
        self.avg_volume = None
        self.trail_sl = sl_price
        self.r_value = (entry_price - sl_price) if self.side == "LONG" else (sl_price - entry_price)
        self.best_r = 0.0

    def update(self, ltp, candle_close=None, candle_volume=None, vwap=None):
        if ltp > self.max_price:
            self.max_price = ltp
        current_r = ((ltp - self.entry_price) if self.side == "LONG" else (self.entry_price - ltp)) / self.r_value if self.r_value > 0 else 0
        if current_r > self.best_r:
            self.best_r = current_r
        if vwap:
            self.vwap = vwap
        if candle_close is not None:
            self.candle_closes.append(candle_close)
            if len(self.candle_closes) > 10:
                self.candle_closes.pop(0)
        if candle_volume is not None:
            self.candle_volumes.append(candle_volume)
            if len(self.candle_volumes) > 20:
                self.candle_volumes.pop(0)
            self.avg_volume = sum(self.candle_volumes) / len(self.candle_volumes)

        # Priority 1: Hard SL Hit — DISABLED (smart_sl.py handles with confirmation)
        # if ltp <= self.sl_price:  # Disabled - smart_sl.py provides 30s confirmation
            # return True, f'HARD SL: {ltp} <= {self.sl_price}', 'SL_HIT'

        # Priority 2: Close below ORB_High on volume
        if candle_close is not None and candle_close < self.orb_high:
            if self.avg_volume and candle_volume and candle_volume > self.avg_volume * 1.2:
                return True, f'ORB REBREAK: {candle_close} < {self.orb_high}', 'ORB_REBREAK'

        # Priority 3: VWAP loss + high volume
        if self.vwap and candle_close is not None:
            if candle_close < self.vwap and current_r < 1.0:
                if self.avg_volume and candle_volume and candle_volume > self.avg_volume * 1.3:
                    return True, f'VWAP LOSS: {candle_close} < {self.vwap:.1f}', 'VWAP_LOSS'

        # Priority 4: Momentum decay (3 lower closes)
        if len(self.candle_closes) >= 3:
            lc = self.candle_closes[-3:]
            if lc[0] > lc[1] > lc[2] and current_r < 1.5:
                return True, f'MOMENTUM DECAY: 3 lower closes', 'MOMENTUM_DECAY'

        # Priority 5: Dead-trade (30min + below VWAP)
        elapsed = (datetime.now() - self.entry_time).total_seconds() / 60
        if elapsed >= 30 and self.best_r < 0.3:
            if self.vwap is None or ltp < self.vwap:
                return True, f'DEAD TRADE: {elapsed:.0f}min, R={self.best_r:.2f}', 'DEAD_TRADE'

        # Update trailing SL
        self._update_trail(current_r)

        # Check trailing SL
        if self.trail_sl > self.sl_price and ltp <= self.trail_sl:
            return True, f'TRAIL SL: {ltp} <= {self.trail_sl:.1f}', 'TRAIL_SL'

        return False, f'HOLDING: R={current_r:.2f}, Best={self.best_r:.2f}', 'HOLD'

    def _update_trail(self, current_r):
        if len(self.candle_closes) == 0:
            return
        close_r = (self.candle_closes[-1] - self.entry_price) / self.r_value if self.r_value > 0 else 0
        if close_r >= 3.0:
            new_trail = self.entry_price + (2.5 * self.r_value)
        elif close_r >= 2.0:
            new_trail = self.entry_price + (1.5 * self.r_value)
        elif close_r >= 1.5:
            new_trail = self.entry_price + (1.0 * self.r_value)
        elif close_r >= 1.0:
            new_trail = self.entry_price + (0.5 * self.r_value)
        else:
            return
        if new_trail > self.trail_sl:
            self.trail_sl = new_trail
            log.info(f'Trail updated: {self.trail_sl:.1f} (at {close_r:.1f}R)')
