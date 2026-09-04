#!/usr/bin/env python3
"""top_movers_archive.py — Archive scoring data for today top 5 gainers/losers (V8.5 PATCH 17)
Runs at 15:30 IST after market close. Checks if top movers were in our universe and what they scored.
"""
import json, sys, os, csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, "/home/ubuntu/trading-bot")
sys.path.insert(0, "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED")

IST = timezone(timedelta(hours=5, minutes=30))
WATCHLIST_PATH = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/watchlist.csv")
SCANS_DIR = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trade_logs")
ARCHIVE_DIR = Path("/home/ubuntu/trading-bot/trade_logs/top_movers")
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

def now_ist(): return datetime.now(IST)
def today_str(): return now_ist().strftime("%Y-%m-%d")

def load_watchlist():
    wl = {}
    if WATCHLIST_PATH.exists():
        with open(WATCHLIST_PATH) as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    wl[row[0].upper()] = {"sid": row[1], "sector": row[2] if len(row) > 2 else ""}
    return wl

def load_todays_scores():
    """Load scan scores from today session"""
    scores = {}
    scan_path = SCANS_DIR / f"scans_{today_str()}.csv"
    if scan_path.exists():
        with open(scan_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = row.get("symbol", "")
                score = float(row.get("score", 0) or 0)
                if sym and score > scores.get(sym, {}).get("score", 0):
                    scores[sym] = row
    return scores

def get_top_movers_from_candidates():
    """Get today top movers from candidate_scores CSV (broadest data)"""
    path = Path(f"/home/ubuntu/trading-bot/candle_archive/candidate_scores_{today_str()}.csv")
    if not path.exists():
        return {}
    scores = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("ticker", "")
            if ticker:
                scores[ticker] = row
    return scores

def archive_top_movers(gainers, losers):
    """Archive top movers with scoring data"""
    watchlist = load_watchlist()
    scan_scores = load_todays_scores()
    candidate_scores = get_top_movers_from_candidates()

    records = []
    for category, stocks in [("GAINER", gainers), ("LOSER", losers)]:
        for rank, (ticker, change_pct) in enumerate(stocks, 1):
            ticker_upper = ticker.upper()
            in_universe = ticker_upper in watchlist
            wl_info = watchlist.get(ticker_upper, {})
            scan_data = scan_scores.get(ticker_upper, {})
            cand_data = candidate_scores.get(ticker_upper, {})

            record = {
                "date": today_str(),
                "category": category,
                "rank": rank,
                "ticker": ticker_upper,
                "change_pct": change_pct,
                "in_universe": in_universe,
                "sid": wl_info.get("sid", ""),
                "sector": wl_info.get("sector", ""),
                # From scan scores (if scored today)
                "v84_score": float(scan_data.get("score", 0) or 0),
                "rs": float(scan_data.get("rs", cand_data.get("rs", 0)) or 0),
                "rvol": float(scan_data.get("rvol", cand_data.get("rvol", 0)) or 0),
                "momentum_5m": float(scan_data.get("momentum_5m", 0) or 0),
                "momentum_15m": float(scan_data.get("momentum_15m", 0) or 0),
                "vwap": float(scan_data.get("vwap", 0) or 0),
                "atr_pct": float(scan_data.get("atr_pct", 0) or 0),
                "long_score": float(cand_data.get("long_score", 0) or 0),
                "short_score": float(cand_data.get("short_score", 0) or 0),
                # Rejection analysis
                "was_shortlisted": cand_data.get("in_shortlist", "") == "Y",
                "rejection_reason": "NOT_IN_UNIVERSE" if not in_universe else ("SCORE_BELOW_50" if float(scan_data.get("score", 0) or 0) < 50 and float(cand_data.get("long_score", 0) or 0) < 50 else "QUALIFIED"),
            }
            records.append(record)

    # Save archive
    archive_path = ARCHIVE_DIR / f"top_movers_{today_str()}.jsonl"
    with open(archive_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"Archived {len(records)} top movers to {archive_path}")
    return records

def main():
    # These would normally be fetched from web or pre-populated
    # For now, read from a manual input or today data
    # The daily_trade_review.py or a web scraper would populate this
    print(f"Top Movers Archive for {today_str()}")
    print("Note: Top movers list should be populated by web scraper or manual input")
    print(f"Archive dir: {ARCHIVE_DIR}")

    # Try to read if a gainers/losers file was pre-populated today
    input_path = ARCHIVE_DIR / f"input_{today_str()}.json"
    if input_path.exists():
        data = json.loads(input_path.read_text())
        gainers = [(g["ticker"], g["change_pct"]) for g in data.get("gainers", [])]
        losers = [(l["ticker"], l["change_pct"]) for l in data.get("losers", [])]
        records = archive_top_movers(gainers, losers)
        for r in records:
            status = "IN UNIVERSE" if r["in_universe"] else "NOT IN UNIVERSE"
            score_info = f"score={r['v84_score']:.0f}" if r["v84_score"] > 0 else f"long={r['long_score']:.0f}"
            print(f"  {r['category']} #{r['rank']}: {r['ticker']} {r['change_pct']:+.1f}% | {status} | {score_info} | {r['rejection_reason']}")
    else:
        print(f"No input file at {input_path}")
        print("Create it manually or via web scraper with format:")
        print(json.dumps({"gainers": [{"ticker": "GLAXO", "change_pct": 4.3}], "losers": [{"ticker": "MOIL", "change_pct": -3.2}]}, indent=2))

if __name__ == "__main__":
    main()
