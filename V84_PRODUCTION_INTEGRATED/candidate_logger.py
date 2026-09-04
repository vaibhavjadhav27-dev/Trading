"""Candidate Logger, MFE/MAE Tracker, and Timing Profiler for Trading Bot v6.1.2"""
import logging
import time as _time
import boto3
from decimal import Decimal
from datetime import datetime, date


log = logging.getLogger("CandidateLogger")


class CandidateLogger:
    """Records every scored candidate (not just trades) for ML training data"""

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb", region_name="ap-south-1")
        self.table = self.dynamodb.Table("TradingBot_CandidateLog")

    def log_candidate(self, date_str, ticker, features):
        try:
            item = {
                "date": date_str,
                "ticker_time": f"{ticker}_{features.get('scan_time', '0000')}",
                "ticker": ticker,
                "tier": features.get("tier", "UNKNOWN"),
                "score": Decimal(str(round(features.get("score", 0), 4))),
                "action": features.get("action", "SKIPPED"),
                "gap_pct": Decimal(str(round(features.get("gap_pct", 0), 2))),
                "rvol": Decimal(str(round(features.get("rvol", 0), 2))),
                "atr": Decimal(str(round(features.get("atr", 0), 2))),
                "atr_expansion": Decimal(str(round(features.get("atr_expansion", 0), 3))),
                "rsi": Decimal(str(round(features.get("rsi", 0), 1))),
                "ema9": Decimal(str(round(features.get("ema9", 0), 2))),
                "ema21": Decimal(str(round(features.get("ema21", 0), 2))),
                "vwap": Decimal(str(round(features.get("vwap", 0), 2))),
                "vwap_distance_pct": Decimal(str(round(features.get("vwap_distance_pct", 0), 3))),
                "supertrend_dir": int(features.get("supertrend_dir", 0)),
                "rs_score": Decimal(str(round(features.get("rs_score", 0), 3))),
                "trend_quality": Decimal(str(round(features.get("trend_quality", 0), 3))),
                "sector": features.get("sector", "UNKNOWN"),
                "sector_rank": int(features.get("sector_rank", 0)),
                "orb_high": Decimal(str(round(features.get("orb_high", 0), 2))),
                "orb_low": Decimal(str(round(features.get("orb_low", 0), 2))),
                "orb_range_pct": Decimal(str(round(features.get("orb_range_pct", 0), 3))),
                "ltp": Decimal(str(round(features.get("ltp", 0), 2))),
                "prev_close": Decimal(str(round(features.get("prev_close", 0), 2))),
                "volume": int(features.get("volume", 0)),
                "avg_volume_20d": int(features.get("avg_volume_20d", 0)),
                "turnover_cr": Decimal(str(round(features.get("turnover_cr", 0), 2))),
                "market_regime": features.get("market_regime", "UNKNOWN"),
                "rejection_reason": features.get("rejection_reason", ""),
                "scan_time": features.get("scan_time", ""),
            }
            item = {k: v for k, v in item.items() if v is not None and v != ""}
            self.table.put_item(Item=item)
        except Exception as e:
            log.warning(f"CandidateLog write failed for {ticker}: {e}")

    def log_batch(self, date_str, candidates_list):
        count = 0
        for candidate in candidates_list:
            self.log_candidate(date_str, candidate.get("ticker", "?"), candidate)
            count += 1
        log.info(f"Logged {count} candidates to DynamoDB")
        return count


class MFEMAETracker:
    """Records peak/trough prices during active trade"""

    def __init__(self):
        self.entry_price = 0
        self.highest_since_entry = 0
        self.lowest_since_entry = float("inf")
        self.mfe = 0
        self.mae = 0
        self.price_history = []

    def reset(self, entry_price):
        self.entry_price = entry_price
        self.highest_since_entry = entry_price
        self.lowest_since_entry = entry_price
        self.mfe = 0
        self.mae = 0
        self.price_history = []
        log.info(f"MFE/MAE tracker reset. Entry: {entry_price}")

    def update(self, current_price, timestamp=None):
        if timestamp is None:
            timestamp = datetime.now().strftime("%H:%M:%S")
        self.price_history.append((timestamp, current_price))
        if current_price > self.highest_since_entry:
            self.highest_since_entry = current_price
        if current_price < self.lowest_since_entry:
            self.lowest_since_entry = current_price
        self.mfe = self.highest_since_entry - self.entry_price
        self.mae = self.entry_price - self.lowest_since_entry
        return self.mfe, self.mae

    def get_stats(self):
        return {
            "entry_price": self.entry_price,
            "highest_price": self.highest_since_entry,
            "lowest_price": self.lowest_since_entry,
            "mfe": round(self.mfe, 2),
            "mae": round(self.mae, 2),
            "mfe_pct": round((self.mfe / self.entry_price) * 100, 3) if self.entry_price else 0,
            "mae_pct": round((self.mae / self.entry_price) * 100, 3) if self.entry_price else 0,
            "mfe_r": round(self.mfe / max(self.mae, 0.01), 2),
            "price_samples": len(self.price_history),
        }


class TimingProfiler:
    """Measures execution latency of each step"""

    def __init__(self):
        self.timings = {}

    def start(self, step_name):
        self.timings[step_name] = {"start": _time.time(), "end": None, "duration": None}

    def stop(self, step_name):
        if step_name in self.timings:
            self.timings[step_name]["end"] = _time.time()
            self.timings[step_name]["duration"] = (
                self.timings[step_name]["end"] - self.timings[step_name]["start"]
            )
            duration = self.timings[step_name]["duration"]
            log.info(f"[TIMING] {step_name}: {duration:.3f}s")
            return duration
        return 0

    def get_summary(self):
        lines = []
        total = 0
        for step, data in self.timings.items():
            if data["duration"] is not None:
                lines.append(f"  {step}: {data[chr(39)+"duration"+chr(39)]:.3f}s")
                total += data["duration"]
        lines.append(f"  TOTAL: {total:.3f}s")
        return chr(10).join(lines)

    def get_bottleneck(self):
        if not self.timings:
            return "No data"
        valid = [(k, v["duration"]) for k, v in self.timings.items() if v["duration"] is not None]
        if not valid:
            return "No data"
        name, dur = max(valid, key=lambda x: x)
        return f"{name}: {dur:.3f}s"

    def reset(self):
        self.timings = {}
