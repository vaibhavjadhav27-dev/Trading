import json, os
from datetime import date

JOURNAL_DIR = "journal"

def log_candidates(candidates, regime, funnel):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    filename = os.path.join(JOURNAL_DIR, date.today().strftime("%Y-%m-%d") + ".json")
    entry = {"regime": regime, "candidates": candidates, "funnel": funnel}
    with open(filename, "w") as f:
        json.dump(entry, f, indent=2, default=str)

def log_trade(trade_data):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    filename = os.path.join(JOURNAL_DIR, date.today().strftime("%Y-%m-%d") + ".json")
    existing = {}
    if os.path.exists(filename):
        with open(filename, "r") as f:
            existing = json.load(f)
    existing["trade"] = trade_data
    with open(filename, "w") as f:
        json.dump(existing, f, indent=2, default=str)

def log_exit(exit_data):
    log_trade({"exit": exit_data})
