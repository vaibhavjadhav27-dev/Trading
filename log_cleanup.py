import os, glob
from datetime import datetime, timedelta

TRADING_DIR = "/home/ubuntu/trading-bot"
MAX_LOG_DAYS = 7
MAX_LOG_SIZE_MB = 5
deleted = 0

cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
for log_file in glob.glob(os.path.join(TRADING_DIR, "*.log")):
    mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
    if mtime < cutoff:
        os.remove(log_file)
        deleted += 1

for log_file in glob.glob(os.path.join(TRADING_DIR, "*.log")):
    size_mb = os.path.getsize(log_file) / (1024 * 1024)
    if size_mb > MAX_LOG_SIZE_MB:
        with open(log_file, "r") as f:
            lines = f.readlines()
        with open(log_file, "w") as f:
            f.writelines(lines[-1000:])

archive_dir = os.path.join(TRADING_DIR, "candle_archive")
if os.path.exists(archive_dir):
    cutoff_90 = datetime.now() - timedelta(days=90)
    for fn in os.listdir(archive_dir):
        fpath = os.path.join(archive_dir, fn)
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
        if mtime < cutoff_90:
            os.remove(fpath)
            deleted += 1

print(f"Cleanup done: {deleted} files removed")
