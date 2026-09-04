import time, logging
log = logging.getLogger(__name__)

class PeakDetector:
    def __init__(self):
        self.prices = []
        self.peak = 0
        self.peak_time = 0
        self.declining_count = 0

    def reset(self):
        self.prices = []
        self.peak = 0
        self.peak_time = 0
        self.declining_count = 0

    def update(self, price):
        self.prices.append((time.time(), price))
        # Keep last 10 prices (5 min of 30s polls)
        self.prices = self.prices[-10:]
        if price > self.peak:
            self.peak = price
            self.peak_time = time.time()
            self.declining_count = 0
        else:
            self.declining_count += 1

    def should_exit(self, entry_price, current_price):
        if len(self.prices) < 5:
            return False, 'Not enough data'
        # Exit if declining 3+ ticks from peak AND still profitable
        profit_r = (current_price - entry_price) / max(entry_price * 0.02, 1)
        if self.declining_count >= 3 and profit_r >= 0.5:
            return True, f'Peak exit: declining {self.declining_count} ticks from peak {self.peak:.1f}'
        # Exit if lost 50% from peak profit
        peak_profit = self.peak - entry_price
        curr_profit = current_price - entry_price
        if peak_profit > 0 and curr_profit < peak_profit * 0.5:
            return True, f'Lost 50%+ of peak profit ({peak_profit:.1f} -> {curr_profit:.1f})'
        return False, 'Holding'
