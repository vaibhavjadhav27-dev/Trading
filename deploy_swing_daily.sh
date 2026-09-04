# BACKUP
cp swing_daily.py swing_daily.py.bak.$(date +%Y%m%d_%H%M%S)

# WRITE (quoted heredoc = no shell expansion)
cat > swing_daily.py <<'PYEOF'
import json, os, time, logging, requests
from datetime import datetime, date, timedelta
from secrets_manager import get_parameter
try:
    from swing_logger import log_event
except ImportError:
    def log_event(a, p): return False

log = logging.getLogger("swing_daily")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

POSITIONS_FILE = "swing_positions.json"
JOURNAL_DIR = "journal/swing"
HISTORY_FILE = "stock_history_30d.json"
MAX_POSITIONS = 2
VIRTUAL_CAPITAL = 100000
RISK_PER_TRADE = 0.02
SCORE_THRESHOLD = 60
RESERVE_PCT = 0.05      # keep 5% cash reserve at EACH sizing step
CAP_1_PCT = 0.60        # favoured pick (#1) capped at 60% of usable so #2 funds

os.makedirs(JOURNAL_DIR, exist_ok=True)

def cnc_buy_charges(qty, price):
    """Dhan CNC buy-leg charges: STT 0.1%, exch 0.0030699%, SEBI 0.0001%,
    stamp 0.015%, +18% GST on (exch+sebi). Brokerage Rs.0. DP is sell-side only."""
    val = qty * price
    stt = 0.001 * val
    exch = 0.000030699 * val
    sebi = 0.000001 * val
    stamp = 0.00015 * val
    gst = 0.18 * (exch + sebi)
    return round(stt + exch + sebi + stamp + gst, 2)

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r") as f:
            p = json.load(f)
        p.setdefault("active", [])
        p.setdefault("closed", [])
        p.setdefault("cash", VIRTUAL_CAPITAL)   # running paper cash ledger
        return p
    return {"active": [], "closed": [], "cash": VIRTUAL_CAPITAL}

def save_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

def load_history():
    with open(HISTORY_FILE, "r") as f:
        data = json.load(f)
    return data.get("stocks", data)

def calculate_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def calculate_rs(closes, period=10):
    if len(closes) < period + 1:
        return 0
    return (closes[-1] - closes[-period]) / closes[-period] * 100

def calculate_rvol(volumes, period=20):
    if len(volumes) < period:
        return 0
    avg_vol = sum(volumes[-period:]) / period
    if avg_vol == 0:
        return 0
    return volumes[-1] / avg_vol

def scan_candidates():
    stocks = load_history()
    results = []
    for ticker, hist in stocks.items():
        try:
            if isinstance(hist, list):
                closes = [d["close"] for d in hist if "close" in d]
                highs = [d["high"] for d in hist if "high" in d]
                lows = [d["low"] for d in hist if "low" in d]
                volumes = [d.get("volume", 0) for d in hist]
            elif isinstance(hist, dict):
                closes = hist.get("close", [])
                highs = hist.get("high", [])
                lows = hist.get("low", [])
                volumes = hist.get("volume", [])
            else:
                continue
            if len(closes) < 20:
                continue
            cmp = closes[-1]
            if cmp < 60 or cmp > 5000:
                continue
            sma20 = calculate_sma(closes, 20)
            avg_vol = sum(v.get("volume", v.get("vol", 0)) if isinstance(v, dict) else 0 for v in hist[-20:]) / 20
            if avg_vol < 100000:
                continue
            if sma20 is None or cmp < sma20:
                continue
            high_20d = max(highs[-20:]) if highs else cmp
            pullback_pct = (high_20d - cmp) / high_20d * 100
            if pullback_pct < 2 or pullback_pct > 15:
                continue
            rs_10d = calculate_rs(closes, 10)
            if rs_10d < 0:
                continue
            rvol = calculate_rvol(volumes)
            score = 0
            score += min(rs_10d * 2, 40)
            score += min(rvol * 10, 25)
            score += min((15 - pullback_pct) * 1.5, 20)
            above_sma_pct = (cmp - sma20) / sma20 * 100
            score += min(above_sma_pct * 2, 15)
            sl = min(lows[-3:]) if lows else cmp * 0.95
            sl = max(sl, cmp * 0.92)
            risk = cmp - sl
            target = cmp + (risk * 2.5)
            results.append({"ticker": ticker, "cmp": round(cmp, 2), "score": round(score, 1), "rs_10d": round(rs_10d, 2), "rvol": round(rvol, 2), "pullback_pct": round(pullback_pct, 2), "sma20": round(sma20, 2), "high_20d": round(high_20d, 2), "sl": round(sl, 2), "target": round(target, 2), "risk_pct": round(risk / cmp * 100, 2), "rr_ratio": 2.5})
        except Exception:
            continue
    results.sort(key=lambda x: -x["score"])
    return results

def auto_select_paper_trades(candidates, positions):
    """Sequential sizing against the RUNNING cash ledger.
    #1 (favoured) capped at CAP_1_PCT of usable; #2 sized from remaining cash.
    5% reserve applied at EACH step. Buy-leg CNC charges debited on entry."""
    available_slots = MAX_POSITIONS - len(positions["active"])
    if available_slots <= 0:
        log.info("Max positions reached. No new entries.")
        return []
    active_tickers = [p["ticker"] for p in positions["active"]]
    # eligible, best-first (candidates already score-sorted)
    eligible = [c for c in candidates
                if c["ticker"] not in active_tickers
                and c["score"] >= SCORE_THRESHOLD
                and c["risk_pct"] <= 8
                and (c["cmp"] - c["sl"]) > 0]
    new_entries = []
    cash = positions.get("cash", VIRTUAL_CAPITAL)
    for rank, c in enumerate(eligible):
        if len(new_entries) >= available_slots:
            break
        usable = cash * (1 - RESERVE_PCT)          # 5% reserve on CURRENT cash
        # cap the favoured pick ONLY when placing 2 at once; solo fill uses full usable
        cap_active = (available_slots >= 2 and len(new_entries) == 0)
        alloc = usable * CAP_1_PCT if cap_active else usable
        qty = int(alloc / c["cmp"])
        if qty < 1:
            continue
        charges = cnc_buy_charges(qty, c["cmp"])
        cost = qty * c["cmp"] + charges
        if cost > cash:                            # safety: never overspend
            qty = int((cash - charges) / c["cmp"])
            if qty < 1:
                continue
            charges = cnc_buy_charges(qty, c["cmp"])
            cost = qty * c["cmp"] + charges
        cash -= cost                               # debit running ledger
        new_entries.append({"ticker": c["ticker"], "entry_price": c["cmp"],
            "entry_date": str(date.today()), "qty": qty, "sl": c["sl"],
            "target": c["target"], "trailing_sl": c["sl"], "score": c["score"],
            "status": "PAPER_ACTIVE", "peak_price": c["cmp"],
            "notional": round(qty * c["cmp"], 2), "entry_charges": charges,
            "risk_pct": c["risk_pct"], "days_held": 0, "alloc_rank": rank + 1})
    positions["cash"] = round(cash, 2)             # persist remaining cash
    return new_entries

def push_to_sheets(candidates, positions):
    try:
        url = get_parameter("/trading-engine/google/apps-script-url")
        payload = {"action": "swing_signals", "data": candidates[:20], "date": str(date.today())}
        log_event("swing_signals", payload)
        resp = requests.post(url, json=payload, timeout=30)
        log.info("Sheets (signals): " + str(resp.status_code))
        if positions["active"]:
            payload2 = {"action": "swing_active", "data": positions["active"], "date": str(date.today())}
            log_event("swing_active", payload2)
            resp2 = requests.post(url, json=payload2, timeout=30)
            log.info("Sheets (active): " + str(resp2.status_code))
    except Exception as e:
        log.warning("Sheets push failed: " + str(e))

def send_email(candidates, new_entries, positions):
    try:
        import boto3
        ses = boto3.client("ses", region_name="ap-south-1")
        sender = get_parameter("/trading-engine/ses/sender-email")
        recipient = get_parameter("/trading-engine/ses/recipient-email")
        today = date.today().strftime("%d %b %Y")
        body = "SWING TRADING DAILY SCAN - " + today + chr(10)
        body += "=" * 50 + chr(10) + chr(10)
        if new_entries:
            body += "NEW PAPER ENTRIES:" + chr(10) + "-" * 30 + chr(10)
            for e in new_entries:
                body += "  " + e["ticker"] + " @ Rs." + str(e["entry_price"]) + " | Qty: " + str(e["qty"]) + " | SL: Rs." + str(e["sl"]) + " | Target: Rs." + str(e["target"]) + " | Hold: 3-15 days" + chr(10)
            body += chr(10)
        body += "PAPER CASH LEFT: Rs." + str(positions.get("cash", VIRTUAL_CAPITAL)) + " / Rs." + str(VIRTUAL_CAPITAL) + chr(10)
        body += "ACTIVE POSITIONS: " + str(len(positions["active"])) + chr(10)
        for p in positions["active"]:
            body += "  " + p["ticker"] + " @ Rs." + str(p["entry_price"]) + " (Day " + str(p["days_held"]) + ") | SL: Rs." + str(p["trailing_sl"]) + chr(10)
        body += chr(10) + "TOP 15 CANDIDATES:" + chr(10) + "-" * 30 + chr(10)
        for c in candidates[:15]:
            body += "  " + c["ticker"] + " Score:" + str(c["score"]) + " | CMP:" + str(c["cmp"]) + " | RS:" + str(c["rs_10d"]) + "%" + chr(10)
        body += chr(10) + "Closed trades: " + str(len(positions["closed"])) + chr(10)
        if positions["closed"]:
            wins = [p for p in positions["closed"] if p.get("pnl", 0) > 0]
            body += "Win rate: " + str(len(wins)) + "/" + str(len(positions["closed"])) + chr(10)
        ses.send_email(Source=sender, Destination={"ToAddresses": [recipient]}, Message={"Subject": {"Data": "Swing Scan: " + str(len(new_entries)) + " new entries | " + str(len(positions["active"])) + " active | " + today}, "Body": {"Text": {"Data": body}}})
        log.info("Email sent successfully")
    except Exception as e:
        log.warning("Email failed: " + str(e))

def save_journal(candidates, new_entries, positions):
    journal = {"date": str(date.today()), "scan_time": datetime.now().strftime("%H:%M:%S"), "candidates_found": len(candidates), "new_entries": new_entries, "active_positions": positions["active"], "closed_positions": positions["closed"], "top_20": candidates[:20]}
    filepath = os.path.join(JOURNAL_DIR, str(date.today()) + ".json")
    with open(filepath, "w") as f:
        json.dump(journal, f, indent=2)
    log.info("Journal saved: " + filepath)

def run():
    log.info("=" * 50)
    log.info("SWING DAILY SCAN - Starting")
    log.info("=" * 50)
    positions = load_positions()
    candidates = scan_candidates()
    log.info("Candidates found: " + str(len(candidates)))
    new_entries = auto_select_paper_trades(candidates, positions)
    if new_entries:
        positions["active"].extend(new_entries)
        save_positions(positions)
        log.info("New paper entries: " + str(len(new_entries)))
        for e in new_entries:
            log.info("  PAPER ENTRY: " + e["ticker"] + " @ Rs." + str(e["entry_price"]))
    else:
        log.info("No new entries (max positions or no qualifying candidates)")
    push_to_sheets(candidates, positions)
    send_email(candidates, new_entries, positions)
    save_journal(candidates, new_entries, positions)
    log.info("SWING DAILY SCAN - Complete")
    return candidates

if __name__ == "__main__":
    run()
PYEOF

# VERIFY
venv/bin/python3 -m py_compile swing_daily.py && echo '✅ swing_daily.py OK' || echo '❌ swing_daily.py FAILED — restore .bak'
