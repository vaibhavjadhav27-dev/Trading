#!/usr/bin/env python3
"""Swing Paper Trader - Simulates entries/exits, tracks to Google Sheets"""
import json, os, time, logging, requests
try:
    from catalyst_engine import get_cached_catalysts
except ImportError:
    get_cached_catalysts = lambda: []
from datetime import datetime, timedelta, date
try:
    from swing_exit import swing_exit_decision
except ImportError:
    swing_exit_decision = None
try:
    from swing_logger import log_event
except ImportError:
    def log_event(a, p): return False

log = logging.getLogger("swing_paper")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IST_OFFSET = timedelta(hours=5, minutes=30)
PAPER_FILE = "paper_trades.json"
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")

def ist_now():
    return datetime.utcnow() + IST_OFFSET

def load_paper_trades():
    if os.path.exists(PAPER_FILE):
        with open(PAPER_FILE, "r") as f:
            return json.load(f)
    return {"active": [], "closed": [], "watchlist": []}

def save_paper_trades(data):
    with open(PAPER_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def push_to_sheets(action, payload):
    """Log locally (EC2), Google Sheets kept as silent fallback"""
    log_event(action, payload)
    if not APPS_SCRIPT_URL:
        from secrets_manager import get_parameter
        url = get_parameter("/trading-engine/google/apps-script-url")
    else:
        url = APPS_SCRIPT_URL
    try:
        resp = requests.post(url, json={"action": action, "payload": payload}, timeout=30)
        log.info(f"Sheets push ({action}): {resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Sheets push failed: {e}")
        return False

def get_ltp_batch(tickers, watchlist_path="watchlist.csv"):
    """Get LTP for multiple tickers using WebSocket"""
    import pandas as pd
    wl = pd.read_csv(watchlist_path)
    sid_map = {}
    for _, row in wl.iterrows():
        vals = list(row)
        t = str(vals[0])
        s = str(int(float(vals[1])))
        if t in tickers:
            sid_map[t] = s
    if not sid_map:
        return {}
    from secrets_manager import get_dhan_token, get_parameter
    token = get_dhan_token()
    client_id = get_parameter("/trading-engine/dhan/client-id")
    pairs = [(t, sid_map[t]) for t in sid_map]
    try:
        from ws_ltp import get_bulk_ltp
        result = get_bulk_ltp(client_id, token, pairs)
        if isinstance(result, list) and len(result) > 0:
            result = result if isinstance(result, dict) else {}
        return result if isinstance(result, dict) else {}
    except Exception as e:
        log.warning(f"LTP fetch failed: {e}")
        return {}

def check_entries():
    """Check watchlist for entry signals"""
    data = load_paper_trades()
    if not data["watchlist"]:
        log.info("No stocks in watchlist to check")
        return
    tickers = [w["ticker"] for w in data["watchlist"]]
    ltp_map = get_ltp_batch(tickers)
    entries_made = []
    for w in data["watchlist"][:]:
        ticker = w["ticker"]
        ltp = ltp_map.get(ticker, 0)
        if ltp <= 0:
            continue
        entry_price = w.get("entry_above", 0)
        if ltp > entry_price and entry_price > 0:
            trade = {
                "ticker": ticker,
                "entry_price": ltp,
                "entry_time": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
                "qty": w.get("qty", 1),
                "sl": w.get("sl", 0),
                "target": w.get("target", 0),
                "peak": ltp,
                "status": "ACTIVE",
                "signal_date": w.get("signal_date", ""),
                "rs_10d": w.get("rs_10d", 0),
                "rvol": w.get("rvol", 0)
            }
            data["active"].append(trade)
            data["watchlist"].remove(w)
            entries_made.append(trade)
            log.info(f"PAPER ENTRY: {ticker} @ {ltp} (SL: {trade['sl']}, Target: {trade['target']})")
    if entries_made:
        save_paper_trades(data)
        rows = [[t["entry_time"], t["ticker"], "BUY", t["entry_price"], t["qty"],
                 t["sl"], t["target"], "PAPER", t["rs_10d"], t["rvol"]] for t in entries_made]
        push_to_sheets("swing_scan", {"scan_data": rows})
    return entries_made

def monitor_active():
    """Monitor active paper trades for SL/Target/Trailing"""
    data = load_paper_trades()
    if not data["active"]:
        log.info("No active paper trades")
        return
    tickers = [t["ticker"] for t in data["active"]]
    ltp_map = get_ltp_batch(tickers)
    exits_made = []
    for trade in data["active"][:]:
        ticker = trade["ticker"]
        ltp = ltp_map.get(ticker, 0)
        if ltp <= 0:
            continue
        entry = trade["entry_price"]
        sl = trade["sl"]
        peak = trade.get("peak", entry)
        if ltp > peak:
            trade["peak"] = ltp
            peak = ltp
        # days held from signal/entry date
        try:
            _ed = trade.get("entry_time", "")[:10] or trade.get("signal_date", "")
            days_held = (datetime.strptime(ist_now().strftime("%Y-%m-%d"), "%Y-%m-%d")
                         - datetime.strptime(_ed, "%Y-%m-%d")).days if _ed else 0
        except Exception:
            days_held = trade.get("days_held", 0)
        gain_pct = (ltp - entry) / entry * 100 if entry else 0
        trailing_sl = trade.get("trailing_sl", sl)
        # SINGLE SOURCE OF TRUTH: same rule as swing_monitor (3-15d / +15% no cap)
        action, trailing_sl = swing_exit_decision(days_held, gain_pct, trailing_sl,
                                                  entry, peak, ltp)
        trade["trailing_sl"] = trailing_sl
        exit_reason = None
        if action in ("HARD_SL", "TRAIL_SL", "TIME_STOP"):
            exit_reason = action
        if exit_reason:
            trade["exit_price"] = ltp
            trade["exit_time"] = ist_now().strftime("%Y-%m-%d %H:%M:%S")
            trade["exit_reason"] = exit_reason
            trade["pnl"] = (ltp - entry) * trade["qty"]
            trade["pnl_pct"] = (ltp - entry) / entry * 100
            trade["status"] = "CLOSED"
            data["closed"].append(trade)
            data["active"].remove(trade)
            exits_made.append(trade)
            log.info(f"PAPER EXIT: {ticker} @ {ltp} ({exit_reason}) PnL: {trade['pnl']:.2f}")
        else:
            trade["current_ltp"] = ltp
            trade["unrealized_pnl"] = (ltp - entry) * trade["qty"]
            trade["last_checked"] = ist_now().strftime("%Y-%m-%d %H:%M:%S")
    save_paper_trades(data)
    if exits_made:
        rows = [[t["exit_time"], t["ticker"], "SELL", t["exit_price"], t["qty"],
                 t["exit_reason"], t["pnl"], t["pnl_pct"], "", ""] for t in exits_made]
        push_to_sheets("swing_scan", {"scan_data": rows})
    return exits_made

def populate_watchlist_from_scanner():
    """Read latest swing scan results and add top candidates to watchlist"""
    data = load_paper_trades()
    try:
        from swing_scanner import run_swing_scan
        buy_signals, _watch = run_swing_scan()
        buy_signals.sort(key=lambda x: -x.get("rs_10d", 0))
        top5 = buy_signals[:5]
        # Filter by catalyst score (skip AVOID signals)
        catalysts = get_cached_catalysts()
        cat_map = {c['ticker']: c for c in catalysts}
        top5 = [s for s in top5 if cat_map.get(s['ticker'], {}).get('signal', 'NEUTRAL') != 'AVOID'][:5]
        existing_tickers = [w["ticker"] for w in data["watchlist"]]
        existing_tickers += [a["ticker"] for a in data["active"]]
        added = 0
        for s in top5:
            if s["ticker"] in existing_tickers:
                continue
            price = s.get("close", s.get("ltp", 0))
            sma20 = s.get("sma20", price * 0.95)
            watch_entry = {
                "ticker": s["ticker"],
                "signal_date": ist_now().strftime("%Y-%m-%d"),
                "entry_above": price * 1.005,
                "sl": sma20 * 0.98,
                "target": price * 1.15,
                "qty": max(1, int(50000 / price)) if price > 0 else 1,
                "rs_10d": s.get("rs_10d", 0),
                "rvol": s.get("rvol", 0),
                "pullback_pct": s.get("pullback_pct", 0)
            }
            data["watchlist"].append(watch_entry)
            added += 1
            log.info(f"WATCHLIST ADD: {s['ticker']} entry>{watch_entry['entry_above']:.2f} SL:{watch_entry['sl']:.2f}")
        save_paper_trades(data)
        log.info(f"Watchlist updated: {added} new, {len(data['watchlist'])} total")
        return added
    except Exception as e:
        log.error(f"Scanner failed: {e}")
        return 0

def daily_summary():
    """Generate daily summary"""
    data = load_paper_trades()
    summary = {
        "date": ist_now().strftime("%Y-%m-%d"),
        "active_trades": len(data["active"]),
        "watchlist": len(data["watchlist"]),
        "closed_today": 0,
        "total_closed": len(data["closed"]),
        "total_pnl": sum(t.get("pnl", 0) for t in data["closed"])
    }
    today = ist_now().strftime("%Y-%m-%d")
    summary["closed_today"] = len([t for t in data["closed"] if t.get("exit_time", "").startswith(today)])
    log.info(f"DAILY SUMMARY: Active={summary['active_trades']}, Watch={summary['watchlist']}, PnL={summary['total_pnl']:.2f}")
    return summary

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "scan":
            populate_watchlist_from_scanner()
        elif cmd == "check":
            check_entries()
        elif cmd == "monitor":
            monitor_active()
        elif cmd == "summary":
            daily_summary()
        elif cmd == "status":
            data = load_paper_trades()
            print(f"Active: {len(data['active'])}, Watchlist: {len(data['watchlist'])}, Closed: {len(data['closed'])}")
            for a in data["active"]:
                print(f"  ACTIVE: {a['ticker']} @ {a['entry_price']} (SL:{a['sl']})")
            for w in data["watchlist"]:
                print(f"  WATCH: {w['ticker']} entry>{w['entry_above']:.2f}")
    else:
        log.info("Running full cycle: scan -> check -> monitor -> summary")
        populate_watchlist_from_scanner()
        check_entries()
        monitor_active()
        daily_summary()
