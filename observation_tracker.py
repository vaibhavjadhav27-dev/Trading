import json
import boto3
import pandas as pd
from datetime import datetime, timedelta, timezone
from secrets_manager import get_parameter
import requests
import logging

log = logging.getLogger("observation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

class ObservationTracker:
    """Tracks bot performance during 20-day observation phase.
    ZERO strategy changes. Pure measurement."""

    def __init__(self):
        self.dynamo = boto3.resource("dynamodb", region_name="ap-south-1")
        self.trades_table = self.dynamo.Table("TradingBot_Trades")
        self.state_table = self.dynamo.Table("TradingBot_DailyState")
        self.token = get_parameter("/trading-engine/dhan/access-token")
        self.headers = {"Content-Type": "application/json", "access-token": self.token}

    def get_nse_top_gainers(self):
        """Fetch actual NSE top gainers for comparison."""
        try:
            resp = requests.get(
                "https://www.nseindia.com/api/live-analysis/gainers/allSec",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])[:10]
        except:
            pass
        return []

    def get_todays_candidates(self):
        """Read candidate log from DynamoDB."""
        today = now_ist().strftime("%Y-%m-%d")
        try:
            resp = self.state_table.get_item(Key={"date": today, "key": "candidates"})
            if "Item" in resp:
                return json.loads(resp["Item"].get("data", "[]"))
        except:
            pass
        return []

    def get_todays_trades(self):
        """Get trades executed today."""
        today = now_ist().strftime("%Y-%m-%d")
        try:
            resp = self.trades_table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("date").eq(today)
            )
            return resp.get("Items", [])
        except:
            return []

    def calculate_daily_metrics(self):
        """Calculate observation metrics for today."""
        today = now_ist().strftime("%Y-%m-%d")
        candidates = self.get_todays_candidates()
        trades = self.get_todays_trades()
        top_gainers = self.get_nse_top_gainers()

        metrics = {
            "date": today,
            "day_number": self.get_observation_day(),
            "candidates_shortlisted": len(candidates),
            "trades_executed": len(trades),
            "trades_won": sum(1 for t in trades if float(t.get("pnl", 0)) > 0),
            "trades_lost": sum(1 for t in trades if float(t.get("pnl", 0)) < 0),
            "total_pnl": sum(float(t.get("pnl", 0)) for t in trades),
            "top_gainers_caught": 0,
            "top_gainers_missed": 0,
            "false_signals": 0,
        }

        # Compare our shortlist vs actual top movers
        gainer_tickers = [g.get("symbol", "") for g in top_gainers[:5]]
        candidate_tickers = [c.get("ticker", "") for c in candidates]

        metrics["top_gainers_caught"] = len(set(candidate_tickers) & set(gainer_tickers))
        metrics["top_gainers_missed"] = len(set(gainer_tickers) - set(candidate_tickers))

        # Trades that lost = false signals
        metrics["false_signals"] = metrics["trades_lost"]

        # Calculate R-multiples
        r_multiples = []
        for t in trades:
            entry = float(t.get("entry_price", 0))
            sl = float(t.get("stop_loss", 0))
            exit_p = float(t.get("exit_price", 0))
            if entry and sl and entry != sl:
                r = (exit_p - entry) / abs(entry - sl)
                r_multiples.append(r)

        metrics["avg_r_multiple"] = sum(r_multiples) / len(r_multiples) if r_multiples else 0
        metrics["r_multiples"] = r_multiples

        return metrics

    def get_observation_day(self):
        """How many trading days since observation started."""
        try:
            resp = self.state_table.get_item(Key={"date": "CONFIG", "key": "observation_start"})
            if "Item" in resp:
                start = datetime.strptime(resp["Item"]["value"], "%Y-%m-%d")
                delta = (now_ist().date() - start.date()).days
                # Rough trading days (exclude weekends)
                return max(1, int(delta * 5 / 7))
        except:
            pass
        return 1

    def save_metrics(self, metrics):
        """Store in DynamoDB for historical analysis."""
        self.state_table.put_item(Item={
            "date": metrics["date"],
            "key": "observation_metrics",
            "data": json.dumps(metrics),
            "timestamp": now_ist().isoformat()
        })
        log.info(f"Observation Day {metrics['day_number']}: "
                 f"Candidates={metrics['candidates_shortlisted']}, "
                 f"Trades={metrics['trades_executed']}, "
                 f"PnL={metrics['total_pnl']:.2f}, "
                 f"Caught={metrics['top_gainers_caught']}/5, "
                 f"Avg R={metrics['avg_r_multiple']:.2f}")

    def get_cumulative_stats(self):
        """Get running totals across all observation days."""
        try:
            resp = self.state_table.scan(
                FilterExpression=boto3.dynamodb.conditions.Attr("key").eq("observation_metrics")
            )
            items = resp.get("Items", [])
            all_metrics = [json.loads(item["data"]) for item in items]

            if not all_metrics:
                return None

            total_trades = sum(m["trades_executed"] for m in all_metrics)
            total_wins = sum(m["trades_won"] for m in all_metrics)
            total_pnl = sum(m["total_pnl"] for m in all_metrics)
            total_caught = sum(m["top_gainers_caught"] for m in all_metrics)
            total_missed = sum(m["top_gainers_missed"] for m in all_metrics)
            all_r = []
            for m in all_metrics:
                all_r.extend(m.get("r_multiples", []))

            return {
                "days_observed": len(all_metrics),
                "total_trades": total_trades,
                "win_rate": (total_wins / total_trades * 100) if total_trades else 0,
                "profit_factor": "TBD (need win/loss amounts)",
                "total_pnl": total_pnl,
                "avg_r_multiple": sum(all_r) / len(all_r) if all_r else 0,
                "top_movers_catch_rate": (total_caught / (total_caught + total_missed) * 100) if (total_caught + total_missed) else 0,
                "avg_candidates_per_day": sum(m["candidates_shortlisted"] for m in all_metrics) / len(all_metrics),
            }
        except Exception as e:
            log.error(f"Cumulative stats error: {e}")
            return None

    def start_observation(self):
        """Mark today as observation start date."""
        today = now_ist().strftime("%Y-%m-%d")
        self.state_table.put_item(Item={
            "date": "CONFIG",
            "key": "observation_start",
            "value": today,
            "timestamp": now_ist().isoformat()
        })
        log.info(f"Observation phase started: {today}")
        log.info("RULES: Zero strategy changes for 20 trading days.")

    def run(self):
        """Daily observation run."""
        metrics = self.calculate_daily_metrics()
        self.save_metrics(metrics)

        cumulative = self.get_cumulative_stats()
        if cumulative:
            log.info(f"CUMULATIVE: {cumulative['days_observed']} days, "
                     f"Win Rate: {cumulative['win_rate']:.1f}%, "
                     f"PnL: {cumulative['total_pnl']:.2f}, "
                     f"Catch Rate: {cumulative['top_movers_catch_rate']:.1f}%")

        return metrics, cumulative


if __name__ == "__main__":
    import sys
    tracker = ObservationTracker()
    if len(sys.argv) > 1 and sys.argv == "start":
        tracker.start_observation()
    tracker.run()
