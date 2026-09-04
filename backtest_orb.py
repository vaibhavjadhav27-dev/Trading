"""
BACKTEST ENGINE - ORB Strategy on Historical NSE Data
=====================================================
All 3 reviewers say: "If the base pattern has no edge after costs,
no amount of filters fixes that."

This answers THE question:
"Does ORB breakout produce positive expectancy after costs on NSE?"
"""
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from secrets_manager import get_parameter
import config
import logging

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IST = timezone(timedelta(hours=5, minutes=30))

# COSTS (realistic as per all reviewers)
BROKERAGE_PER_ORDER = 20  # Rs flat per order
STT_PCT = 0.025  # Securities Transaction Tax
EXCHANGE_CHARGES_PCT = 0.00345
GST_PCT = 18  # On brokerage
STAMP_DUTY_PCT = 0.003
SLIPPAGE_BPS = 5  # 0.05% assumed slippage

class ORBBacktester:
    """Backtest ORB strategy with realistic costs."""

    def __init__(self):
        self.token = get_parameter("/trading-engine/dhan/access-token")
        self.headers = {"Content-Type": "application/json", "access-token": self.token}
        self.results = []
        self.capital = 6000
        self.max_risk_pct = 2.0

    def fetch_intraday(self, security_id, date_str, exchange="NSE_EQ"):
        """Fetch 5-min candles for a specific date."""
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange,
            "instrument": "EQUITY",
            "interval": "5",
            "fromDate": date_str,
            "toDate": date_str
        }
        try:
            resp = requests.post(
                "https://api.dhan.co/v2/charts/intraday",
                json=payload, headers=self.headers, timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if "open" in data and len(data["open"]) > 0:
                    df = pd.DataFrame({
                        "open": data["open"],
                        "high": data["high"],
                        "low": data["low"],
                        "close": data["close"],
                        "volume": data.get("volume", *len(data["open"]))
                    })
                    return df
            elif resp.status_code == 429:
                time.sleep(2)
        except:
            pass
        return None

    def fetch_historical(self, security_id, days=90, exchange="NSE_EQ"):
        """Fetch daily data to get trading dates."""
        from_date = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
        to_date = datetime.now(IST).strftime("%Y-%m-%d")
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange,
            "instrument": "EQUITY",
            "expiryCode": 0,
            "fromDate": from_date,
            "toDate": to_date
        }
        try:
            resp = requests.post(
                "https://api.dhan.co/v2/charts/historical",
                json=payload, headers=self.headers, timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None

    def calculate_costs(self, buy_price, sell_price, qty):
        """Realistic trading costs."""
        buy_value = buy_price * qty
        sell_value = sell_price * qty

        # Brokerage (flat Rs 20 per order, buy + sell)
        brokerage = BROKERAGE_PER_ORDER * 2

        # STT (sell side only for intraday)
        stt = sell_value * STT_PCT / 100

        # Exchange charges (both sides)
        exchange = (buy_value + sell_value) * EXCHANGE_CHARGES_PCT / 100

        # GST on brokerage + exchange
        gst = (brokerage + exchange) * GST_PCT / 100

        # Stamp duty (buy side only)
        stamp = buy_value * STAMP_DUTY_PCT / 100

        # Slippage (both sides)
        slippage = (buy_value + sell_value) * SLIPPAGE_BPS / 10000

        total_cost = brokerage + stt + exchange + gst + stamp + slippage
        return total_cost

    def simulate_orb_trade(self, df, prev_close):
        """Simulate one day of ORB trading.

        Rules:
        - ORB = first 3 candles (9:15-9:30) high/low
        - Entry: break above ORB high
        - SL: ATR * 1.5 below entry
        - Target: trailing (2R minimum)
        - Exit: 3:15 PM hard stop
        """
        if df is None or len(df) < 10:
            return None

        # First 3 candles = ORB (9:15, 9:20, 9:25 in 5-min)
        orb_candles = df.iloc[:3]
        orb_high = orb_candles["high"].max()
        orb_low = orb_candles["low"].min()
        orb_range = orb_high - orb_low

        # Skip if ORB range too small (< 0.5% of price)
        if orb_range < orb_high * 0.005:
            return {"result": "SKIP", "reason": "ORB range too small"}

        # Skip if ORB range too large (> 3% of price)
        if orb_range > orb_high * 0.03:
            return {"result": "SKIP", "reason": "ORB range too large"}

        # Calculate gap
        day_open = df.iloc["open"]
        if prev_close > 0:
            gap_pct = (day_open - prev_close) / prev_close * 100
        else:
            gap_pct = 0

        # Apply gap filter
        if abs(gap_pct) < config.GAP_MIN or abs(gap_pct) > config.GAP_REJECT:
            return {"result": "SKIP", "reason": f"Gap {gap_pct:.2f}% outside range"}

        # Scan remaining candles for breakout (from candle 4 onwards)
        entry_price = None
        entry_idx = None
        sl_distance = orb_range * 0.75  # SL = 75% of ORB range

        for i in range(3, len(df)):
            if df.iloc[i]["high"] > orb_high:
                entry_price = orb_high + (orb_high * SLIPPAGE_BPS / 10000)  # Slippage on entry
                entry_idx = i
                break

        if entry_price is None:
            return {"result": "SKIP", "reason": "No breakout"}

        # Now simulate trailing SL
        sl = entry_price - sl_distance
        highest = entry_price
        exit_price = None
        exit_reason = None

        for i in range(entry_idx + 1, len(df)):
            candle = df.iloc[i]

            # Check SL hit
            if candle["low"] <= sl:
                exit_price = sl
                exit_reason = "SL_HIT"
                break

            # Update trailing SL
            if candle["high"] > highest:
                highest = candle["high"]
                gain = highest - entry_price
                r_multiple = gain / sl_distance

                # Phase-based trailing
                if r_multiple >= 2.0:
                    sl = max(sl, entry_price + gain * 0.6)  # Lock 60% of gains
                elif r_multiple >= 1.5:
                    sl = max(sl, entry_price + sl_distance * 0.75)
                elif r_multiple >= 1.0:
                    sl = max(sl, entry_price + sl_distance * 0.25)

        # EOD exit at last candle if still holding
        if exit_price is None:
            exit_price = df.iloc[-1]["close"]
            exit_reason = "EOD_EXIT"

        # Calculate P&L
        qty = max(1, int(self.capital * (self.max_risk_pct / 100) / sl_distance))
        gross_pnl = (exit_price - entry_price) * qty
        costs = self.calculate_costs(entry_price, exit_price, qty)
        net_pnl = gross_pnl - costs
        r_multiple = (exit_price - entry_price) / sl_distance

        return {
            "result": "TRADE",
            "entry": entry_price,
            "exit": exit_price,
            "sl": entry_price - sl_distance,
            "qty": qty,
            "gross_pnl": gross_pnl,
            "costs": costs,
            "net_pnl": net_pnl,
            "r_multiple": r_multiple,
            "exit_reason": exit_reason,
            "gap_pct": gap_pct,
            "orb_range_pct": orb_range / orb_high * 100,
        }

    def run_backtest(self, watchlist_sample=20, days=60):
        """Run backtest on sample stocks."""
        log.info(f"Starting backtest: {watchlist_sample} stocks x {days} days")
        log.info(f"Capital: Rs.{self.capital}, Risk: {self.max_risk_pct}%/trade")
        log.info(f"Costs: Brokerage Rs.{BROKERAGE_PER_ORDER}x2, STT {STT_PCT}%, Slippage {SLIPPAGE_BPS}bps")

        # Load watchlist
        wl = pd.read_csv("watchlist.csv")
        sample = wl.head(watchlist_sample)

        all_trades = []
        skipped = {"ORB range too small": 0, "ORB range too large": 0,
                   "No breakout": 0, "Gap outside range": 0, "No data": 0}

        for idx, row in sample.iterrows():
            ticker = row["ticker"]
            sid = str(row["security_id"])
            log.info(f"  Backtesting {ticker} (SID: {sid})...")

            # Get historical dates
            hist = self.fetch_historical(sid, days=days)
            time.sleep(0.5)

            if not hist or "close" not in hist:
                skipped["No data"] += 1
                continue

            closes = hist["close"]
            # We need to fetch intraday for recent dates
            # Use last 20 trading days from historical
            num_days = min(20, len(closes))

            for day_idx in range(1, num_days):
                # Get prev close
                prev_close = float(closes[day_idx - 1])

                # We can only backtest dates we can fetch intraday for
                # Dhan intraday is limited to recent dates
                # Calculate date (approximate)
                target_date = (datetime.now(IST) - timedelta(days=num_days - day_idx)).strftime("%Y-%m-%d")

                intra = self.fetch_intraday(sid, target_date)
                time.sleep(0.5)  # Rate limit

                if intra is None or len(intra) < 10:
                    skipped["No data"] += 1
                    continue

                result = self.simulate_orb_trade(intra, prev_close)

                if result is None:
                    skipped["No data"] += 1
                elif result["result"] == "SKIP":
                    reason = result["reason"]
                    if "ORB range too small" in reason:
                        skipped["ORB range too small"] += 1
                    elif "ORB range too large" in reason:
                        skipped["ORB range too large"] += 1
                    elif "No breakout" in reason:
                        skipped["No breakout"] += 1
                    else:
                        skipped["Gap outside range"] += 1
                else:
                    result["ticker"] = ticker
                    result["date"] = target_date
                    all_trades.append(result)

        # Calculate final stats
        self.results = all_trades
        return self.generate_backtest_report(all_trades, skipped)

    def generate_backtest_report(self, trades, skipped):
        """Generate comprehensive backtest report."""
        report = []
        report.append("=" * 60)
        report.append("  ORB STRATEGY BACKTEST RESULTS")
        report.append(f"  Capital: Rs.6,000 | Risk: 2%/trade | Costs: REALISTIC")
        report.append("=" * 60)

        if not trades:
            report.append("  NO TRADES GENERATED - strategy may need adjustment")
            report.append(f"  Skipped reasons: {json.dumps(skipped, indent=4)}")
            return "".join(report)

        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]

        total_gross = sum(t["gross_pnl"] for t in trades)
        total_costs = sum(t["costs"] for t in trades)
        total_net = sum(t["net_pnl"] for t in trades)

        total_won = sum(t["net_pnl"] for t in wins)
        total_lost = abs(sum(t["net_pnl"] for t in losses))

        r_multiples = [t["r_multiple"] for t in trades]

        report.append("")
        report.append("--- CORE RESULTS ---")
        report.append(f"  Total Trades:      {len(trades)}")
        report.append(f"  Wins / Losses:     {len(wins)} / {len(losses)}")
        report.append(f"  Win Rate:          {len(wins)/len(trades)*100:.1f}%")
        report.append(f"  Profit Factor:     {total_won/total_lost:.2f}" if total_lost > 0 else "  Profit Factor: INF")
        report.append(f"  Expectancy:        {sum(r_multiples)/len(r_multiples):.2f}R per trade")
        report.append("")
        report.append(f"  Gross PnL:         Rs.{total_gross:.2f}")
        report.append(f"  Total Costs:       Rs.{total_costs:.2f}")
        report.append(f"  NET PnL:           Rs.{total_net:.2f}")
        report.append(f"  Cost Impact:       {total_costs/abs(total_gross)*100:.1f}% of gross" if total_gross != 0 else "")
        report.append("")
        report.append(f"  Avg R-Multiple:    {np.mean(r_multiples):.2f}")
        report.append(f"  Best Trade:        {max(r_multiples):.2f}R")
        report.append(f"  Worst Trade:       {min(r_multiples):.2f}R")
        report.append(f"  Avg Win:           Rs.{total_won/len(wins):.2f}" if wins else "  Avg Win: N/A")
        report.append(f"  Avg Loss:          Rs.{total_lost/len(losses):.2f}" if losses else "  Avg Loss: N/A")

        # Exit analysis
        report.append("")
        report.append("--- EXIT ANALYSIS ---")
        sl_exits = sum(1 for t in trades if t["exit_reason"] == "SL_HIT")
        eod_exits = sum(1 for t in trades if t["exit_reason"] == "EOD_EXIT")
        report.append(f"  SL Hit:            {sl_exits} ({sl_exits/len(trades)*100:.0f}%)")
        report.append(f"  EOD Exit:          {eod_exits} ({eod_exits/len(trades)*100:.0f}%)")

        # Skip analysis
        report.append("")
        report.append("--- SKIP REASONS ---")
        for reason, count in sorted(skipped.items(), key=lambda x: x, reverse=True):
            report.append(f"  {reason}: {count}")

        # VERDICT
        report.append("")
        report.append("=" * 60)
        if total_net > 0 and len(wins)/len(trades) > 0.4:
            report.append("  VERDICT: EDGE EXISTS - Strategy is profitable after costs")
            report.append(f"  Monthly expectation: ~Rs.{total_net/4:.0f} (extrapolated)")
        elif total_net > 0:
            report.append("  VERDICT: MARGINAL EDGE - Profitable but low win rate")
            report.append("  ACTION: Tighten filters to improve win rate")
        else:
            report.append("  VERDICT: NO EDGE AFTER COSTS")
            report.append("  ACTION: Re-evaluate strategy fundamentally")
        report.append("=" * 60)

        return "".join(report)


if __name__ == "__main__":
    import sys
    stocks = int(sys.argv.__getitem__(1)) if len(sys.argv) > 1 else 10
    days = int(sys.argv.__getitem__(2)) if len(sys.argv) > 2 else 30

    bt = ORBBacktester()
    report = bt.run_backtest(watchlist_sample=stocks, days=days)
    print(report)
