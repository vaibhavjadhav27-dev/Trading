#!/usr/bin/env python3
"""swing_logger.py - Local EC2 logging to replace Google Sheets.
Append-only JSONL (human-greppable) + SQLite (queryable). Both live on the
EBS volume under LOG_DIR. Free-tier safe: no network, no external deps.
"""
import json, os, sqlite3, logging
from datetime import datetime, timedelta

log = logging.getLogger("swing_logger")
IST_OFFSET = timedelta(hours=5, minutes=30)
LOG_DIR = os.environ.get("SWING_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "swing_logs"))
os.makedirs(LOG_DIR, exist_ok=True)
JSONL_PATH = os.path.join(LOG_DIR, "swing_events.jsonl")
DB_PATH = os.path.join(LOG_DIR, "swing.db")

def _ist_now():
    return datetime.utcnow() + IST_OFFSET

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS swing_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, event_date TEXT, action TEXT, payload TEXT)""")
    return conn

def log_event(action, payload):
    """Append one event to JSONL and SQLite. Returns True on success."""
    rec = {"ts": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
           "date": _ist_now().strftime("%Y-%m-%d"),
           "action": action, "payload": payload}
    try:
        with open(JSONL_PATH, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        conn = _db()
        conn.execute("INSERT INTO swing_events (ts, event_date, action, payload) VALUES (?,?,?,?)",
                     (rec["ts"], rec["date"], action, json.dumps(payload, default=str)))
        conn.commit(); conn.close()
        log.info(f"Logged event: {action}")
        return True
    except Exception as e:
        log.warning(f"Local log failed ({action}): {e}")
        return False

def query_events(action=None, since_date=None, limit=200):
    """Read back events for review/summary."""
    try:
        conn = _db(); cur = conn.cursor()
        q = "SELECT ts, event_date, action, payload FROM swing_events WHERE 1=1"
        args = []
        if action:     q += " AND action = ?";     args.append(action)
        if since_date: q += " AND event_date >= ?"; args.append(since_date)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        rows = cur.execute(q, args).fetchall(); conn.close()
        return [{"ts": r[0], "date": r[1], "action": r[2], "payload": json.loads(r[3])} for r in rows]
    except Exception as e:
        log.warning(f"Query failed: {e}")
        return []

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "tail":
        for e in query_events(limit=20)[::-1]:
            print(f"{e['ts']} [{e['action']}] {json.dumps(e['payload'])[:120]}")
    else:
        log_event("selftest", {"ok": True})
        print("swing_logger OK ->", LOG_DIR)
