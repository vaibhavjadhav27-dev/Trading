#!/usr/bin/env python3
"""
SHADOW TRADER v1.0 - Expert Philosophy
Runs alongside main bot, logs what IT would have done
Compare after 1 week to find the better approach
"""
import sys, os, time, json, logging
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/trading-bot")
from secrets_manager import get_parameter, get_dhan_token, get_dhan_client_id
from ws_ltp_scanner import get_bulk_ltp
import pandas as pd

# Logging
log = logging.getLogger("shadow")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/ubuntu/trading-bot/shadow_trade.log"),
        logging.StreamHandler()
    ]
)

# ═══ EXPERT CURATED WATCHLIST (25 stocks) ═══
EXPERT_STOCKS = [
    {"ticker": "INFY", "sector": "IT"},
    {"ticker": "TCS", "sector": "IT"},
    {"ticker": "WIPRO", "sector": "IT"},
    {"ticker": "HCLTECH", "sector": "IT"},
    {"ticker": "SBIN", "sector": "BANK"},
    {"ticker": "ICICIBANK", "sector": "BANK"},
    {"ticker": "AXISBANK", "sector": "BANK"},
    {"ticker": "KOTAKBANK", "sector": "BANK"},
    {"ticker": "TATAMOTORS", "sector": "AUTO"},
    {"ticker": "M&M", "sector": "AUTO"},
    {"ticker": "MARUTI", "sector": "AUTO"},
    {"ticker": "ADANIENT", "sector": "INFRA"},
    {"ticker": "ADANIPORTS", "sector": "INFRA"},
    {"ticker": "RELIANCE", "sector": "ENERGY"},
    {"ticker": "TATAPOWER", "sector": "ENERGY"},
    {"ticker": "SUNPHARMA", "sector": "PHARMA"},
    {"ticker": "DRREDDY", "sector": "PHARMA"},
    {"ticker": "TATASTEEL", "sector": "METAL"},
    {"ticker": "HINDALCO", "sector": "METAL"},
    {"ticker": "JSWSTEEL", "sector": "METAL"},
    {"ticker": "HAL", "sector": "DEFENCE"},
    {"ticker": "BEL", "sector": "DEFENCE"},
    {"ticker": "TITAN", "sector": "CONSUMER"},
    {"ticker": "ZOMATO", "sector": "CONSUMER"},
    {"ticker": "APOLLOTYRE", "sector": "AUTO"},
]

# ═══ SHADOW CONFIG (Expert Philosophy) ═══
CAPITAL = 51000  # Same as main bot
RISK_PCT = 2.0  # 2% per trade
MAX_TRADES_PER_DAY = 1  # KEY: Only 1 trade/day (best setup only)
MIN_GAP_PCT = 0.5
MAX_GAP_PCT = 4.0
ORB_MIN_RANGE_PCT = 0.8
ORB_MAX_RANGE_PCT = 3.0
ENTRY_DEADLINE_HOUR = 10  # No entries after 10:00 AM
ENTRY_DEADLINE_MIN = 30
DEAD_TRADE_MINUTES = 20  # Exit if no +0.3R in 20 min
TRAIL_START_R = 1.0  # Trail starts at 1R (not 1.5R)

class ShadowTrader:
    """Expert philosophy shadow trader - logs only, no real orders"""

    def __init__(self):
        self.token = get_dhan_token()
        self.client_id = get_dhan_client_id()
        self.today = date.today().isoformat()
        self.shadow_trade = None
        self.decisions = []
        self.log_file = f"/home/ubuntu/trading-bot/shadow_logs/shadow_{self.today}.json"
        os.makedirs("/home/ubuntu/trading-bot/shadow_logs", exist_ok=True)
        # Map tickers to security_ids from main watchlist
        self.watchlist_df = pd.read_csv("/home/ubuntu/trading-bot/watchlist.csv")
        self.sid_map = {}
        for s in EXPERT_STOCKS:
            match = self.watchlist_df[self.watchlist_df["ticker"] == s["ticker"]]
            if len(match) > 0:
                self.sid_map[s["ticker"]] = str(int(match.iloc[0]["security_id"]))
        log.info(f"Shadow Trader initialized: {len(self.sid_map)}/{len(EXPERT_STOCKS)} stocks mapped")

    def get_nifty_context(self):
        """CONFIRMATION 1: Is today the right day?"""
        import requests
        headers = {"Content-Type": "application/json", "access-token": self.token, "client-id": self.client_id}
        today_str = date.today().isoformat()
        payload = {"securityId": "13", "exchangeSegment": "IDX_I", "instrument": "INDEX",
                   "interval": "15", "fromDate": today_str, "toDate": today_str}
        resp = requests.post("https://api.dhan.co/v2/charts/intraday", json=payload, headers=headers)
        if resp.status_code != 200:
            return {"bias": "UNKNOWN", "reason": "API error"}
        data = resp.json()
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        if not closes or len(closes) < 2:
            return {"bias": "UNKNOWN", "reason": "No data"}
        # Calculate VWAP approximation
        vols = data.get("volume", [])
        if vols and sum(vols) > 0:
            typical = [(h+l+c)/3 for h,l,c in zip(highs, lows, closes)]
            cum_tp_vol = sum(t*v for t,v in zip(typical, vols))
            cum_vol = sum(vols)
            vwap = cum_tp_vol / cum_vol
        else:
            vwap = sum(closes) / len(closes)
        nifty_ltp = closes[-1]
        nifty_open = opens[0] if opens else nifty_ltp
        gap_pct = ((nifty_ltp - nifty_open) / nifty_open) * 100 if nifty_open else 0
        # Determine bias
        if nifty_ltp > vwap * 1.003:  # > 0.3% above VWAP
            bias = "BULLISH"
        elif nifty_ltp < vwap * 0.997:  # > 0.3% below VWAP
            bias = "BEARISH"
        else:
            bias = "CHOPPY"
        # Range check
        day_range = ((max(highs) - min(lows)) / min(lows)) * 100
        context = {
            "bias": bias,
            "nifty_ltp": nifty_ltp,
            "vwap": round(vwap, 2),
            "day_range_pct": round(day_range, 2),
            "trending": day_range > 0.8
        }
        log.info(f"Market context: {bias} | NIFTY: {nifty_ltp} | VWAP: {vwap:.0f} | Range: {day_range:.2f}%")
        return context

    def scan_stocks(self):
        """CONFIRMATION 2: Structure check - gap + ORB quality"""
        import requests
        headers = {"Content-Type": "application/json", "access-token": self.token, "client-id": self.client_id}
        candidates = []
        today_str = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=3)).isoformat()

        for ticker, sid in self.sid_map.items():
            time.sleep(1.5)  # Rate limit safe
            # Get prev close
            payload = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                       "fromDate": yesterday, "toDate": today_str, "expiryCode": 0}
            resp = requests.post("https://api.dhan.co/v2/charts/historical", json=payload, headers=headers)
            if resp.status_code != 200:
                self.decisions.append({"ticker": ticker, "action": "SKIP", "reason": "API error (historical)"})
                continue
            hist = resp.json()
            hist_closes = hist.get("close", [])
            if not hist_closes or len(hist_closes) < 1:
                self.decisions.append({"ticker": ticker, "action": "SKIP", "reason": "No historical data"})
                continue
            prev_close = hist_closes[-1]

            time.sleep(1.0)
            # Get today intraday
            payload2 = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                        "interval": "5", "fromDate": today_str, "toDate": today_str}
            resp2 = requests.post("https://api.dhan.co/v2/charts/intraday", json=payload2, headers=headers)
            if resp2.status_code != 200:
                self.decisions.append({"ticker": ticker, "action": "SKIP", "reason": "API error (intraday)"})
                continue
            intra = resp2.json()
            opens = intra.get("open", [])
            highs = intra.get("high", [])
            lows = intra.get("low", [])
            closes = intra.get("close", [])
            volumes = intra.get("volume", [])
            if not opens or len(opens) < 3:
                self.decisions.append({"ticker": ticker, "action": "SKIP", "reason": "Insufficient intraday data"})
                continue

            # Day open and current price
            day_open = opens[0]
            ltp = closes[-1]
            gap_pct = ((day_open - prev_close) / prev_close) * 100

            # ORB (first 3 candles = 15 min)
            orb_candles = min(3, len(highs))
            orb_high = max(highs[:orb_candles])
            orb_low = min(lows[:orb_candles])
            orb_range_pct = ((orb_high - orb_low) / orb_low) * 100

            # ORB first candle quality (body > 50% of range)
            first_body = abs(closes[0] - opens[0]) if len(closes) > 0 and len(opens) > 0 else 0
            first_range = highs[0] - lows[0] if len(highs) > 0 and len(lows) > 0 else 1
            body_ratio = first_body / first_range if first_range > 0 else 0

            # Volume check (breakout candle vs ORB avg)
            orb_avg_vol = sum(volumes[:orb_candles]) / orb_candles if orb_candles > 0 and volumes else 0

            # === FILTER DECISIONS ===
            # Gap filter
            if abs(gap_pct) < MIN_GAP_PCT:
                self.decisions.append({"ticker": ticker, "action": "REJECT", "reason": f"Gap too small: {gap_pct:.2f}%", "gap": gap_pct})
                continue
            if abs(gap_pct) > MAX_GAP_PCT:
                self.decisions.append({"ticker": ticker, "action": "REJECT", "reason": f"Gap too large: {gap_pct:.2f}%", "gap": gap_pct})
                continue

            # Direction filter (gap must be positive for long)
            if gap_pct < 0:
                self.decisions.append({"ticker": ticker, "action": "REJECT", "reason": f"Negative gap: {gap_pct:.2f}% (long only)", "gap": gap_pct})
                continue

            # ORB range filter
            if orb_range_pct < ORB_MIN_RANGE_PCT:
                self.decisions.append({"ticker": ticker, "action": "REJECT", "reason": f"ORB range too tight: {orb_range_pct:.2f}%", "orb_range": orb_range_pct})
                continue
            if orb_range_pct > ORB_MAX_RANGE_PCT:
                self.decisions.append({"ticker": ticker, "action": "REJECT", "reason": f"ORB range too wide: {orb_range_pct:.2f}%", "orb_range": orb_range_pct})
                continue

            # First candle quality (body > 50%)
            if body_ratio < 0.5:
                self.decisions.append({"ticker": ticker, "action": "REJECT", "reason": f"Weak first candle (doji): body={body_ratio:.0%}", "body_ratio": body_ratio})
                continue

            # Check breakout
            breakout = ltp > orb_high * 1.001  # > 0.1% above ORB high

            # Volume confirmation on breakout
            post_orb_vols = volumes[orb_candles:] if len(volumes) > orb_candles else []
            vol_confirm = False
            if post_orb_vols and orb_avg_vol > 0:
                max_post_vol = max(post_orb_vols)
                vol_confirm = max_post_vol > orb_avg_vol * 1.2

            # VWAP check
            if volumes and sum(volumes) > 0:
                typical = [(h+l+c)/3 for h,l,c in zip(highs, lows, closes)]
                cum_tp_vol = sum(t*v for t,v in zip(typical, volumes))
                vwap = cum_tp_vol / sum(volumes)
                above_vwap = ltp > vwap
            else:
                vwap = 0
                above_vwap = True

            # Score (simple 3-confirmation)
            score = 0
            if breakout: score += 40
            if vol_confirm: score += 30
            if above_vwap: score += 20
            if body_ratio > 0.7: score += 10

            entry_price = orb_high * 1.001
            sl_price = orb_low
            sl_distance = entry_price - sl_price
            risk_amount = CAPITAL * (RISK_PCT / 100)
            qty = int(risk_amount / sl_distance) if sl_distance > 0 else 0
            target_1r = entry_price + sl_distance
            target_2r = entry_price + (2 * sl_distance)

            candidate = {
                "ticker": ticker,
                "sid": sid,
                "ltp": ltp,
                "prev_close": prev_close,
                "gap_pct": round(gap_pct, 2),
                "orb_high": orb_high,
                "orb_low": orb_low,
                "orb_range_pct": round(orb_range_pct, 2),
                "breakout": breakout,
                "vol_confirm": vol_confirm,
                "above_vwap": above_vwap,
                "body_ratio": round(body_ratio, 2),
                "score": score,
                "entry_price": round(entry_price, 2),
                "sl_price": sl_price,
                "target_1r": round(target_1r, 2),
                "target_2r": round(target_2r, 2),
                "qty": qty,
                "action": "SHORTLISTED"
            }
            candidates.append(candidate)
            self.decisions.append(candidate)
            log.info(f"  SHORTLISTED: {ticker} | Gap:{gap_pct:.1f}% | ORB:{orb_range_pct:.1f}% | Score:{score} | BO:{breakout}")

        return candidates

    def select_best(self, candidates, context):
        """Pick THE SINGLE BEST setup"""
        if not candidates:
            log.info("No candidates passed filters - NO TRADE today")
            return None

        # Only trade on BULLISH or strong TRENDING days
        if context.get("bias") == "CHOPPY":
            log.info(f"Market is CHOPPY (NIFTY near VWAP) - NO TRADE today")
            self.decisions.append({"action": "NO_TRADE", "reason": "Choppy market - expert sits out"})
            return None

        # Filter: must have breakout + volume confirmation
        confirmed = [c for c in candidates if c["breakout"] and c["vol_confirm"]]
        if not confirmed:
            # Fallback: breakout without volume (lower conviction)
            confirmed = [c for c in candidates if c["breakout"]]
            if not confirmed:
                log.info("No confirmed breakouts - NO TRADE")
                return None

        # Rank by score
        confirmed.sort(key=lambda x: -x["score"])
        best = confirmed
        log.info(f"BEST PICK: {best['ticker']} | Score: {best['score']} | Entry: {best['entry_price']}")
        return best

    def simulate_trade(self, trade, context):
        """Simulate entry/exit with escalator floors"""
        import requests
        headers = {"Content-Type": "application/json", "access-token": self.token, "client-id": self.client_id}
        today_str = date.today().isoformat()

        # Get full day candles to simulate
        time.sleep(1.5)
        payload = {"securityId": trade["sid"], "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                   "interval": "5", "fromDate": today_str, "toDate": today_str}
        resp = requests.post("https://api.dhan.co/v2/charts/intraday", json=payload, headers=headers)
        if resp.status_code != 200:
            return {"status": "ERROR", "reason": "Cannot get intraday for simulation"}

        data = resp.json()
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])

        entry = trade["entry_price"]
        sl = trade["sl_price"]
        sl_distance = entry - sl
        max_price = entry
        current_sl = sl
        exit_price = None
        exit_reason = None
        floor = 0

        # Simulate candle by candle (post-ORB)
        for i in range(3, len(highs)):  # Start after ORB
            candle_high = highs[i]
            candle_low = lows[i]
            candle_close = closes[i]

            # Check if SL hit
            if candle_low <= current_sl:
                exit_price = current_sl
                exit_reason = f"SL hit at Floor {floor}"
                break

            # Update max price
            if candle_high > max_price:
                max_price = candle_high

            # Escalator logic
            r_multiple = (max_price - entry) / sl_distance if sl_distance > 0 else 0

            # Floor 0 -> 1: Move SL to reduce risk
            if r_multiple >= 0.5 and floor < 1:
                current_sl = entry - (sl_distance * 0.3)
                floor = 1

            # Floor 1 -> 2: Breakeven
            if r_multiple >= 1.0 and floor < 2:
                current_sl = entry
                floor = 2

            # Floor 2 -> 3: Trailing
            if r_multiple >= 1.5 and floor < 3:
                current_sl = entry + (0.5 * sl_distance)
                floor = 3

            # Floor 3+: Dynamic trail
            if floor >= 3:
                trail_sl = max_price - (0.5 * sl_distance)
                if trail_sl > current_sl:
                    current_sl = trail_sl

            # Dead trade check (candle 6 = 30 min post entry)
            if i == 9 and r_multiple < 0.3:  # ~30 min, no movement
                exit_price = candle_close
                exit_reason = "Dead trade (no +0.3R in 30min)"
                break

        # EOD exit if still holding
        if exit_price is None and closes:
            exit_price = closes[-1]
            exit_reason = "EOD exit (3:15 PM)"

        # Calculate P&L
        if exit_price:
            pnl_per_share = exit_price - entry
            r_achieved = pnl_per_share / sl_distance if sl_distance > 0 else 0
            total_pnl = pnl_per_share * trade["qty"]
            # Deduct costs (0.1% round trip)
            costs = (entry + exit_price) * trade["qty"] * 0.001
            net_pnl = total_pnl - costs
        else:
            exit_price = entry
            r_achieved = 0
            net_pnl = 0
            exit_reason = "No simulation data"

        result = {
            "ticker": trade["ticker"],
            "entry": entry,
            "exit": round(exit_price, 2),
            "sl": sl,
            "qty": trade["qty"],
            "r_achieved": round(r_achieved, 2),
            "net_pnl": round(net_pnl, 2),
            "max_price": round(max_price, 2),
            "max_r": round((max_price - entry) / sl_distance, 2) if sl_distance > 0 else 0,
            "exit_reason": exit_reason,
            "floor_reached": floor,
            "win": net_pnl > 0
        }
        log.info(f"SHADOW TRADE: {trade['ticker']} | Entry:{entry:.1f} Exit:{exit_price:.1f} | R:{r_achieved:.2f} | PnL:Rs.{net_pnl:.0f} | {exit_reason}")
        return result

    def run(self):
        """Main execution flow"""
        log.info("=" * 60)
        log.info(f"  SHADOW TRADER v1.0 | {self.today}")
        log.info("=" * 60)

        # Step 1: Market context
        log.info("Step 1: Checking market context...")
        context = self.get_nifty_context()

        # Step 2: Scan stocks
        log.info(f"Step 2: Scanning {len(self.sid_map)} stocks...")
        candidates = self.scan_stocks()
        log.info(f"  Found {len(candidates)} candidates")

        # Step 3: Select best
        log.info("Step 3: Selecting best setup...")
        best = self.select_best(candidates, context)

        # Step 4: Simulate trade
        result = None
        if best:
            log.info(f"Step 4: Simulating trade on {best[chr(39)+chr(39)]}...")
            result = self.simulate_trade(best, context)
        else:
            log.info("Step 4: No trade today (expert sits out)")
            result = {"status": "NO_TRADE", "reason": context.get("bias", "unknown")}

        # Step 5: Save complete log
        daily_log = {
            "date": self.today,
            "market_context": context,
            "decisions": self.decisions,
            "candidates_count": len(candidates),
            "best_pick": best,
            "trade_result": result
        }
        with open(self.log_file, "w") as f:
            json.dump(daily_log, f, indent=2, default=str)
        log.info(f"Log saved: {self.log_file}")
        log.info("=" * 60)
        return daily_log


if __name__ == "__main__":
    trader = ShadowTrader()
    trader.run()
