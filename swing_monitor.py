import json, os, time, logging, requests
from datetime import datetime, date
from secrets_manager import get_parameter, get_dhan_token
try:
    from swing_exit import swing_exit_decision, MIN_HOLD_DAYS as _MHD
except ImportError:
    swing_exit_decision = None

log = logging.getLogger("swing_monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

POSITIONS_FILE = "swing_positions.json"
JOURNAL_DIR = "journal/swing"
CHECK_INTERVAL = 120  # 2 minutes
TIME_STOP_DAYS = 15
MIN_HOLD_DAYS = 3          # no target/trail/time exit before this many days (hard SL always honored)
TARGET_GAIN_PCT = 15.0     # +15% activates tight trailing (no upper cap)
LOCK_TRAIL_PCT = 0.93      # after +15%, trail at 93% of peak (locks gains, rides upside)

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                _raw = f.read().strip()
            if _raw:
                _d = json.loads(_raw)
                if isinstance(_d, dict):
                    _d.setdefault("active", [])
                    _d.setdefault("closed", [])
                    return _d
            else:
                log.warning("swing_positions.json EMPTY (0 bytes) - using empty default")
        except (json.JSONDecodeError, ValueError) as _je:
            log.warning(f"swing_positions.json corrupt ({_je}) - using empty default")
    return {"active": [], "closed": []}

def save_positions(positions):
    # atomic: write to .tmp then os.replace so an interrupted write can never truncate the live file
    _tmp = POSITIONS_FILE + ".tmp"
    with open(_tmp, "w") as f:
        json.dump(positions, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(_tmp, POSITIONS_FILE)

def get_ltp_batch(sids):
    try:
        from secrets_manager import get_dhan_token, get_dhan_client_id
        token = get_dhan_token()
        client_id = get_dhan_client_id()
        url = "https://api.dhan.co/v2/marketfeed/ltp"
        headers = {"access-token": token, "client-id": client_id, "Content-Type": "application/json", "Accept": "application/json"}
        payload = {"NSE_EQ": [int(s) for s in sids]}
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            result = {}
            # Handle multiple Dhan response formats
            items = data if isinstance(data, list) else data.get("data", data) if isinstance(data, dict) else []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        sid = str(item.get("security_id", item.get("SID", item.get("securityId", ""))))
                        ltp = float(item.get("LTP", item.get("ltp", item.get("last_price", 0))) or 0)
                        if sid and ltp > 0:
                            result[sid] = ltp
            elif isinstance(items, dict):
                # Format: {"NSE_EQ": {"4655": {"LTP": 1488}}}
                for segment_data in items.values():
                    if isinstance(segment_data, dict):
                        for sid, vals in segment_data.items():
                            if isinstance(vals, dict):
                                ltp = float(vals.get("LTP", vals.get("ltp", 0)) or 0)
                                if ltp > 0: result[str(sid)] = ltp
                            elif isinstance(vals, (int, float)) and vals > 0:
                                result[str(sid)] = float(vals)
            return result
        else:
            log.warning("LTP HTTP " + str(resp.status_code) + ": " + resp.text[:200])
        return {}
    except Exception as e:
        log.warning("LTP fetch failed: " + str(e))
        return {}
        return {}

def get_sid_for_ticker(ticker):
    try:
        import pandas as pd
        wl = pd.read_csv("watchlist.csv")
        row = wl[wl["ticker"] == ticker]
        if not row.empty:
            return str(int(row.iloc[0]["security_id"]))
    except Exception:
        pass
    return None

def send_exit_email(position, exit_type, exit_price):
    try:
        import boto3
        ses = boto3.client("ses", region_name="ap-south-1")
        sender = get_parameter("/trading-engine/ses/sender-email")
        recipient = get_parameter("/trading-engine/ses/recipient-email")
        pnl = (exit_price - position["entry_price"]) * position["qty"]
        pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
        emoji = "GREEN" if pnl > 0 else "RED"
        subject = emoji + " PAPER EXIT: " + position["ticker"] + " @ Rs." + str(exit_price) + " (" + ("+" if pnl_pct > 0 else "") + str(round(pnl_pct, 1)) + "%)"
        body = "SWING PAPER EXIT" + chr(10) + "=" * 40 + chr(10)
        body += "Ticker: " + position["ticker"] + chr(10)
        body += "Exit Type: " + exit_type + chr(10)
        body += "Entry: Rs." + str(position["entry_price"]) + " on " + position["entry_date"] + chr(10)
        body += "Exit: Rs." + str(exit_price) + " on " + str(date.today()) + chr(10)
        body += "Days Held: " + str(position["days_held"]) + chr(10)
        body += "P&L: Rs." + str(round(pnl, 2)) + " (" + str(round(pnl_pct, 1)) + "%)" + chr(10)
        body += "Qty: " + str(position["qty"]) + chr(10)
        ses.send_email(Source=sender, Destination={"ToAddresses": [recipient]}, Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}})
        log.info("Exit email sent: " + position["ticker"])
    except Exception as e:
        log.warning("Exit email failed: " + str(e))

def monitor_positions():
    positions = load_positions()
    if not positions["active"]:
        log.info("No active positions to monitor")
        return
    log.info("Monitoring " + str(len(positions["active"])) + " active positions")
    # === NIFTY REGIME EXIT OVERLAY: tighter give-back when market bearish (fail-open) ===
    try:
        from swing_regime import nifty_trend
        _nt = nifty_trend()
        _bearish = (_nt["regime"] == "BEARISH")
        log.info("SWING REGIME (monitor): NIFTY %s close=%.1f sma20=%.1f slope10=%+.2f%%"
                 % (_nt["regime"], _nt["close"], _nt["sma20"], _nt["slope10"]))
    except Exception as _re:
        _bearish = False
        log.warning("SWING REGIME (monitor): trend check failed (%s) -> no overlay" % _re)
    # Get SIDs for all active tickers
    sid_map = {}
    for p in positions["active"]:
        sid = get_sid_for_ticker(p["ticker"])
        if sid:
            sid_map[p["ticker"]] = sid
    if not sid_map:
        log.warning("No SIDs found for active positions")
        return
    # Fetch LTPs
    ltps = get_ltp_batch(list(sid_map.values()))
    # Check each position
    exits = []
    for i, p in enumerate(positions["active"]):
        sid = sid_map.get(p["ticker"])
        if not sid:
            continue
        ltp = ltps.get(sid, 0)
        if ltp == 0:
            continue
        # Update peak price
        if ltp > p.get("peak_price", p["entry_price"]):
            positions["active"][i]["peak_price"] = ltp
        # Trail SL: move up to breakeven after +1R, then trail at 50% of gains
        entry = p["entry_price"]
        risk = entry - p["sl"]
        gain = ltp - entry
        gain_pct = gain / entry * 100 if entry else 0
        peak = positions["active"][i].get("peak_price", entry)
        if gain_pct >= TARGET_GAIN_PCT:
            # +15% reached: lock at least +15%, trail tightly below peak, NO upper cap
            # REGIME OVERLAY: bearish -> tighter give-back (0.97 vs 0.93); only tightens, hard SL untouched
            _ltf = 0.97 if _bearish else LOCK_TRAIL_PCT
            lock_trail = max(peak * _ltf, entry * (1 + TARGET_GAIN_PCT / 100))
            if lock_trail > p.get("trailing_sl", p["sl"]):
                positions["active"][i]["trailing_sl"] = round(lock_trail, 2)
                positions["active"][i]["target_locked"] = True
                log.info("  " + p["ticker"] + ": +" + str(round(gain_pct,1)) + "% locked, trail Rs." + str(round(lock_trail, 2)))
        elif gain > risk * 1.5:
            new_trail = entry + (gain * 0.5)
            if new_trail > p.get("trailing_sl", p["sl"]):
                positions["active"][i]["trailing_sl"] = round(new_trail, 2)
                log.info("  " + p["ticker"] + ": Trail SL moved to Rs." + str(round(new_trail, 2)))
        elif gain > risk:
            if entry > p.get("trailing_sl", p["sl"]):
                positions["active"][i]["trailing_sl"] = entry
                log.info("  " + p["ticker"] + ": SL moved to breakeven Rs." + str(entry))
        # Check exits
        trailing_sl = positions["active"][i].get("trailing_sl", p["sl"])
        days_held = p.get("days_held", 0)
        hard_sl = p["sl"]
        if ltp <= hard_sl:
            # Hard stop-loss always fires, even inside the min-hold window (capital protection)
            exits.append((i, "SL_HIT", ltp))
        elif days_held < MIN_HOLD_DAYS:
            # Inside min-hold: ride through noise, no trail/target/time exit yet
            log.info("  " + p["ticker"] + ": Day " + str(days_held) + " (<" + str(MIN_HOLD_DAYS) + ", hold) LTP=" + str(ltp) + " | HardSL=" + str(hard_sl))
        elif ltp <= trailing_sl:
            exits.append((i, "TRAIL_SL", ltp))
        elif days_held >= TIME_STOP_DAYS:
            exits.append((i, "TIME_STOP", ltp))
        else:
            log.info("  " + p["ticker"] + ": LTP=" + str(ltp) + " | SL=" + str(trailing_sl) + " | Target=" + str(p["target"]))
    # Process exits (reverse order to maintain indices)
    for idx, exit_type, exit_price in sorted(exits, reverse=True):
        p = positions["active"][idx]
        pnl = (exit_price - p["entry_price"]) * p["qty"]
        p["exit_price"] = exit_price
        p["exit_date"] = str(date.today())
        p["exit_type"] = exit_type
        p["pnl"] = round(pnl, 2)
        p["pnl_pct"] = round((exit_price - p["entry_price"]) / p["entry_price"] * 100, 2)
        p["status"] = "CLOSED"
        positions["closed"].append(p)
        positions["active"].pop(idx)
        log.info("  EXIT: " + p["ticker"] + " | " + exit_type + " @ Rs." + str(exit_price) + " | P&L: Rs." + str(round(pnl, 2)))
        send_exit_email(p, exit_type, exit_price)
    save_positions(positions)

def run():
    log.info("Swing Monitor started - checking every " + str(CHECK_INTERVAL) + "s")
    positions = load_positions()
    # Derive days_held from entry_date (idempotent) — weekday count in [entry, today).
    # Replaces the old per-run increment, which double-counted on monitor restarts / manual
    # dry-runs. Weekday count faithfully reproduces the old once-per-trading-day semantics
    # (cron ran Mon-Fri), so weekends are skipped and behavior is unchanged.
    _today = date.today()
    for i, p in enumerate(positions["active"]):
        _ed = p.get("entry_date")
        if not _ed:
            continue
        try:
            _d0 = date.fromisoformat(str(_ed))
            _n = (_today - _d0).days
            if _n <= 0:
                _wd = 0
            else:
                _fw, _rem = divmod(_n, 7)
                _wd = _fw * 5
                _st = _d0.weekday()
                for _k in range(_rem):
                    if (_st + _k) % 7 < 5:
                        _wd += 1
            positions["active"][i]["days_held"] = _wd
        except Exception as _e:
            log.warning("days_held derive failed for %s (%s) - keeping stored value" % (p.get("ticker"), _e))
    save_positions(positions)
    # Monitor loop during market hours (9:15 AM - 3:30 PM IST)
    while True:
        now = datetime.utcnow()
        ist_hour = now.hour + 5
        ist_min = now.minute + 30
        if ist_min >= 60:
            ist_hour += 1
            ist_min -= 60
        if ist_hour < 9 or (ist_hour == 9 and ist_min < 15):
            log.info("Market not open yet. Waiting...")
            time.sleep(60)
            continue
        if ist_hour >= 15 and ist_min >= 30:
            log.info("Market closed. Exiting monitor.")
            break
        monitor_positions()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
