import json, os, glob, logging
from datetime import date, datetime, timedelta

log = logging.getLogger("study_archive")
logging.basicConfig(level=logging.INFO)

JOURNAL_DIR = "journal"
LOG_DIR = "logs"
BOT_LOG = "bot.log"

def archive_today():
    today = date.today().isoformat()
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    
    archive = {
        "date": today,
        "timestamp": datetime.now().isoformat(),
        "bot_log": [],
        "candidates": [],
        "trades": [],
        "exits": [],
        "market_regime": None,
        "nifty_gap": None
    }
    
    # Extract today entries from bot.log
    if os.path.exists(BOT_LOG):
        with open(BOT_LOG, "r") as f:
            for line in f:
                if today.replace("-", "-") in line:
                    archive["bot_log"].append(line.rstrip())
                    # Extract regime
                    if "Market regime:" in line or "market_mode" in line:
                        archive["market_regime"] = line.rstrip()
                    # Extract candidates
                    if "Candidates:" in line or "candidate" in line.lower():
                        archive["candidates"].append(line.rstrip())
                    # Extract trades
                    if "ORDER" in line or "TRADE" in line or "place_order" in line:
                        archive["trades"].append(line.rstrip())
                    # Extract exits
                    if "EXIT" in line or "square" in line.lower() or "SL hit" in line:
                        archive["exits"].append(line.rstrip())
                    # Extract nifty gap
                    if "nifty" in line.lower() and "gap" in line.lower():
                        archive["nifty_gap"] = line.rstrip()
    
    # Load candidate journal if exists
    candidate_file = os.path.join(JOURNAL_DIR, f"{today}_candidates.json")
    if os.path.exists(candidate_file):
        with open(candidate_file, "r") as f:
            archive["candidates_detail"] = json.load(f)
    
    # Load trade journal if exists
    trade_file = os.path.join(JOURNAL_DIR, f"{today}_trades.json")
    if os.path.exists(trade_file):
        with open(trade_file, "r") as f:
            archive["trades_detail"] = json.load(f)
    
    # Save daily archive
    archive_file = os.path.join(JOURNAL_DIR, f"{today}.json")
    with open(archive_file, "w") as f:
        json.dump(archive, f, indent=2)
    log.info(f"Study archive saved: {archive_file} ({len(archive[chr(98)+chr(111)+chr(116)+chr(95)+chr(108)+chr(111)+chr(103)])} log lines)")
    
    # Rotate bot.log (copy to logs/bot_YYYY-MM-DD.log, then truncate)
    log_backup = os.path.join(LOG_DIR, f"bot_{today}.log")
    if os.path.exists(BOT_LOG):
        import shutil
        shutil.copy2(BOT_LOG, log_backup)
        # Truncate bot.log
        with open(BOT_LOG, "w") as f:
            f.write("")
        log.info(f"Bot log rotated to: {log_backup}")
    
    # Cleanup logs older than 30 days
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    for old_log in glob.glob(os.path.join(LOG_DIR, "bot_*.log")):
        fname = os.path.basename(old_log)
        file_date = fname.replace("bot_", "").replace(".log", "")
        if file_date < cutoff:
            os.remove(old_log)
            log.info(f"Removed old log: {old_log}")
    
    return archive_file

if __name__ == "__main__":
    result = archive_today()
    print(f"Archive complete: {result}")
