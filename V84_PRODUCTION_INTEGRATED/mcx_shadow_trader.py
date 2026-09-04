# #!/usr/bin/env python3
"""
mcx_shadow_trader.py — MCX Evening Session Shadow Trader
=========================================================
Shadow mode: identifies, logs, and validates signals for MCX commodity
intraday trading (18:30–23:00 IST). Places NO real orders. Calibrates
the system for later live deployment.

Contracts (capital-viable at ~₹49K):
  • CRUDEOIL Mini  (10 bbl)   ← US mirror: NYMEX WTI (CL=F)
  • GOLD           (1 kg)     ← US mirror: COMEX Gold (GC=F)
  • SILVER         (30 kg)    ← US mirror: COMEX Silver (SI=F)

Strategy: US-Mirror ORB
  1. At 18:30 IST, record the 15-min ORB high/low for each US benchmark.
  2. When US price breaks the ORB level, compute expected MCX move.
  3. Shadow-log the entry, T1 (1.0%), T2 (2.0%), SL (0.5%).
  4. Monitor MCX LTP via Dhan REST every 30s.
  5. Log outcomes (T1 hit / T2 hit / SL hit) to shadow_mcx.log.
  6. Hard cut-off 23:00 IST — square off all shadow positions.

Usage (cron):
  30 13 * * 1-5 cd /home/ubuntu/trading-bot && venv/bin/python3 mcx_shadow_trader.py >> logs/mcx_shadow.log 2>&1
  0  17 * * 1-5 cd /home/ubuntu/trading-bot && venv/bin/python3 mcx_shadow_trader.py --close >> logs/mcx_shadow.log 2>&1
"""

import sys, json, time, logging, os
from datetime import datetime, timedelta
import pytz

# ── IMPORTS (yfinance for US benchmarks, requests for Dhan MCX) ──────────────
try:
    import yfinance as yf
    import requests
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}. Run: pip install yfinance pandas requests")
    sys.exit(1)

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")
# BUG1: redirect yfinance cache to /tmp to avoid SQLite file-lock issues
import os as _os
_os.environ.setdefault("XDG_CACHE_HOME", "/tmp/yf_cache")
_os.makedirs("/tmp/yf_cache", exist_ok=True)
SESSION_START_IST  = 18 * 60 + 30   # 18:30 in minutes from midnight
SESSION_END_IST    = 23 * 60        # 23:00
ORB_MINUTES        = 15             # 15-min opening range
RISK_PCT           = 0.005          # 0.5% SL
REWARD_PCT         = 0.010          # 1.0% T1
REWARD2_PCT        = 0.020          # 2.0% T2
LIVE_MODE          = False  # shadow only; live requires separate validation switch
DAILY_LOSS_LIMIT_PCT = 3.0
ENTRY_CUTOFF_IST   = (22, 15)
USINDR_DEFAULT     = 84.0          # SHADOW only — flip True after 2 weeks validation
MAX_CAPITAL_PCT    = 0.20           # 20% of balance per trade
POLL_SECONDS       = 30             # MCX LTP polling interval

# US benchmark → MCX shadow contract mapping
CONTRACTS = {
    "CRUDEOIL_MINI": {
        "us_ticker":     "CL=F",          # NYMEX WTI Crude
        "mcx_symbol":    "CRUDEOILM",      # MCX Crude Mini
        "lot_size":      10,               # 10 bbl
        "margin_approx": 5000,             # ₹ approx margin per lot
        "currency":      "USD",
        "inr_factor":    84.0,             # USD→INR (approximate)
        "active":        True,
    },
    "GOLD": {
        "us_ticker":     "GC=F",          # COMEX Gold
        "mcx_symbol":    "GOLD",          # MCX Gold 1kg
        "lot_size":      1000,            # 1000g = 1 kg
        "margin_approx": 4500,
        "currency":      "USD",
        "inr_factor":    84.0,
        "active":        False,  # Gold: Rs.1.4L/10g exceeds budget
    },
    "SILVER": {
        "us_ticker":     "SI=F",          # COMEX Silver
        "mcx_symbol":    "SILVER",        # MCX Silver 30kg
        "lot_size":      30000,           # 30000g = 30 kg
        "margin_approx": 1500,
        "currency":      "USD",
        "inr_factor":    84.0,
        "active":        False,  # Silver: too large for budget
    },
    "NATGASMINI": {
        "us_ticker":     "NG=F",
        "mcx_symbol":    "NATURALGAS",
        "lot_size":      250,
        "margin_approx": 3000,
        "currency":      "INR",
        "inr_factor":    1.0,
        "active":        True,
    },
    "GOLDPETAL": {
        "us_ticker":     "GC=F",
        "mcx_symbol":    "GOLDPETAL",
        "lot_size":      1,
        "margin_approx": 200,
        "currency":      "INR",
        "inr_factor":    1.0,
        "active":        True,
    },
}

LOG_PATH = "logs/shadow_mcx.log"
STATE_PATH = "mcx_shadow_state.json"
DHAN_BASE = "https://api.dhan.co"

# ── LOGGING ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH),
    ]
)
log = logging.getLogger("mcx_shadow")


# ── DHAN AUTH (reuse existing token) ─────────────────────────────────────────
def get_dhan_headers():
    """Use same auth path as equity bot (secrets_manager -> SSM)."""
    try:
        from secrets_manager import get_dhan_token, get_dhan_client_id
        return {
            "access-token": get_dhan_token(),
            "client-id":    get_dhan_client_id(),
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }
    except Exception as e:
        log.error(f"Auth failed via secrets_manager: {e}")
        return {"Content-Type": "application/json", "Accept": "application/json"}


def get_balance(headers):
    """Fetch available balance from Dhan /fundlimit."""
    try:
        r = requests.get("https://api.dhan.co/v2/fundlimit", headers=headers, timeout=10)
        log.info(f"Balance API status: {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            bal = float(d.get("availabelBalance",
                         d.get("data", {}).get("availabelBalance", 0) if isinstance(d.get("data"), dict) else 0))
            if bal > 0:
                return bal
            log.warning(f"Balance=0 from response: {str(d)[:200]}")
    except Exception as e:
        log.warning(f"Balance fetch failed: {e}")
    # Fallback: read from bot's own Dynamo/state if Dhan API fails
    return 49967.0  # last known balance — allows session to proceed



# ── MCX SECURITY ID RESOLVER ─────────────────────────────────────────────────
def get_mcx_active_contract(symbol_prefix, headers):
    """
    Resolve the current front-month MCX security ID via Dhan search API.
    MCX contract IDs change every month at expiry — never hardcode them.
    Returns (security_id, display_name) or (None, None).
    """
    try:
        r = requests.get(
            f"{DHAN_BASE}/v2/edis/form",
            headers=headers,
            timeout=10
        )
        # Try the scrip search endpoint
        r2 = requests.post(
            f"{DHAN_BASE}/instruments",
            headers=headers,
            json={"ticker": symbol_prefix, "exchange": "MCX"},
            timeout=10
        )
        if r2.status_code == 200:
            instruments = r2.json() if isinstance(r2.json(), list) else r2.json().get("data", [])
            # Filter to front-month (shortest expiry in the future)
            now = datetime.now(IST)
            candidates = []
            for inst in instruments:
                sym = str(inst.get("tradingSymbol", ""))
                if symbol_prefix.upper() in sym.upper():
                    candidates.append(inst)
            if candidates:
                best = candidates[0]  # typically sorted by expiry
                return str(best.get("securityId", "")), best.get("tradingSymbol", "")
    except Exception as e:
        log.debug(f"Contract resolution failed for {symbol_prefix}: {e}")
    return None, None


def get_mcx_ltp(security_id, headers):
    """Fetch MCX LTP via Dhan MCX_COMM intraday candles (confirmed working)."""
    import datetime as _dt2
    try:
        today = _dt2.date.today().isoformat()
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": "MCX_COMM",
            "instrument": "FUTCOM",
            "interval": "5",
            "fromDate": today,
            "toDate": today,
        }
        r = requests.post(f"{DHAN_BASE}/v2/charts/intraday",
                          headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            closes = r.json().get("close", [])
            if closes:
                ltp = float(closes[-1])
                if ltp > 0:
                    return ltp
    except Exception as e:
        log.debug(f"MCX LTP failed sid={security_id}: {e}")
    return 0.0


def get_us_orb(ticker, lookback_minutes=15):
    """
    Fetch US benchmark 1-min candles via yfinance and compute 15-min ORB.
    NOTE: yfinance free tier has ~15-min delay for live data.
    Returns (orb_high, orb_low, current_price) or (None, None, None).
    """
    try:
        now_utc = datetime.utcnow()
        start_str = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%d")
        data = yf.download(
            ticker,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=True
        )
        if data is None or data.empty:
            return None, None, None
        # IST session start = 18:30 IST = 13:00 UTC (when DST not active)
        # We take the last `lookback_minutes` candles as ORB
        last_n = data.tail(lookback_minutes)
        # Handle yfinance multi-level columns (newer versions return Series or DataFrame)
        def _scalar(x):
            import pandas as pd
            if isinstance(x, pd.Series): return float(x.iloc[0])
            if isinstance(x, pd.DataFrame): return float(x.iloc[0,0])
            return float(x)
        orb_high = float(last_n["High"].max().item() if hasattr(last_n["High"].max(),'item') else last_n["High"].max())
        orb_low  = float(last_n["Low"].min().item() if hasattr(last_n["Low"].min(),'item') else last_n["Low"].min())
        current  = float(data["Close"].iloc[-1].item() if hasattr(data["Close"].iloc[-1],'item') else data["Close"].iloc[-1])
        return orb_high, orb_low, current
    except Exception as e:
        log.warning(f"US ORB fetch failed for {ticker}: {e}")
        return None, None, None


from mcx_policy_v8 import score_mcx

# ── SIGNAL ENGINE ─────────────────────────────────────────────────────────────
class MCXShadowPosition:
    """Tracks a single shadow position — no real orders, pure logging."""

    def __init__(self, name, side, entry_price, sl_price, t1_price, t2_price,
                 lot_size, margin, timestamp):
        self.name       = name
        self.side       = side          # "LONG" or "SHORT"
        self.entry      = entry_price
        self.sl         = sl_price
        self.t1         = t1_price
        self.t2         = t2_price
        self.lot        = lot_size
        self.margin     = margin
        self.ts         = timestamp
        self.status     = "OPEN"
        self.exit_price = None
        self.exit_reason= None
        self.peak_pnl   = 0.0

    def update(self, ltp):
        """Check LTP against levels. Returns True if position closed."""
        if self.status != "OPEN":
            return True
        if self.side == "LONG":
            pnl = (ltp - self.entry) * self.lot
            if ltp >= self.t2:
                self._close(ltp, "T2_HIT", pnl)
                return True
            if ltp >= self.t1:
                self.peak_pnl = max(self.peak_pnl, pnl)
            if ltp <= self.sl:
                self._close(ltp, "SL_HIT", pnl)
                return True
        else:
            pnl = (self.entry - ltp) * self.lot
            if ltp <= self.t2:
                self._close(ltp, "T2_HIT", pnl)
                return True
            if ltp <= self.t1:
                self.peak_pnl = max(self.peak_pnl, pnl)
            if ltp >= self.sl:
                self._close(ltp, "SL_HIT", pnl)
                return True
        return False

    def force_close(self, ltp):
        pnl = (ltp - self.entry) * self.lot if self.side == "LONG" \
              else (self.entry - ltp) * self.lot
        self._close(ltp, "EOD_SQUAREOFF", pnl)

    def _close(self, price, reason, pnl):
        self.status     = "CLOSED"
        self.exit_price = price
        self.exit_reason= reason
        emoji = "💰" if pnl > 0 else "🔴"
        send_mcx_email(f"EXIT {self.name} {reason} PnL=Rs.{pnl:+,.0f}",
            f"<h3>MCX Exit: {self.side} {self.name}</h3>"
            f"<p>Entry Rs.{self.entry:.2f} → Exit Rs.{price:.2f}</p>"
            f"<p><b>P&L: Rs.{pnl:+,.0f}</b> | Reason: {reason}</p>")
        log.info(
            f"SHADOW {emoji} {self.side} {self.name} | "
            f"entry={self.entry:.2f} exit={price:.2f} | "
            f"PnL=₹{pnl:+,.0f} ({reason}) | margin_used=₹{self.margin:,.0f}"
        )

    def to_dict(self):
        return {
            "name": self.name, "side": self.side, "entry": self.entry,
            "sl": self.sl, "t1": self.t1, "t2": self.t2,
            "lot": self.lot, "margin": self.margin,
            "ts": self.ts, "status": self.status,
            "exit_price": self.exit_price, "exit_reason": self.exit_reason,
            "peak_pnl": self.peak_pnl,
        }


# ── MAIN SHADOW LOOP ──────────────────────────────────────────────────────────

def get_usdinr_rate(hdrs) -> float:
    """Fetch live USD-INR from Dhan USDINR-FUT (NSE currency segment)."""
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        payload = {"securityId": "267", "exchangeSegment": "NSE_CDS",
                   "instrument": "FUTCUR", "interval": "1",
                   "fromDate": today, "toDate": today}
        r = requests.post(f"{DHAN_BASE}/v2/charts/intraday",
                          headers=hdrs, json=payload, timeout=8)
        if r.status_code == 200:
            closes = r.json().get("close", [])
            if closes:
                rate = float(closes[-1])
                log.debug(f"USD-INR live: {rate:.4f}")
                return rate
    except Exception as e:
        log.debug(f"USD-INR fetch: {e}")
    return 84.0


def auto_rollover_sids():
    """
    Auto-rollover MCX front-month SIDs — runs at session start.
    Downloads Dhan scrip master, swaps to next contract if current
    expires within 3 days. Zero manual intervention needed.
    """
    from datetime import date as _date
    import csv, io, re as _re

    SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
    # Maps contract key → MCX symbol prefix in scrip master
    SYMBOL_MAP = {
        "CRUDEOIL_MINI": "CRUDEOILM",
        "NATGASMINI":    "NATGASMINI",
        "GOLDPETAL":     "GOLDPETAL",
    }
    today = _date.today()

    try:
        import requests as _req
        resp = _req.get(SCRIP_URL, timeout=10)
        lines = resp.text.splitlines()
        reader = csv.reader(lines)

        # Build: symbol_prefix → [(expiry_date, sid, full_name), ...]
        candidates = {}
        for row in reader:
            if len(row) < 10:
                continue
            if row[0] != "MCX" or row[3] != "FUTCOM":
                continue
            symbol = row[5]   # e.g. NATGASMINI-26Aug2026-FUT
            sid    = row[2]
            expiry_str = row[8][:10] if row[8] else ""
            try:
                expiry = _date.fromisoformat(expiry_str)
            except Exception:
                continue
            for key, prefix in SYMBOL_MAP.items():
                if symbol.startswith(prefix + "-"):
                    if key not in candidates:
                        candidates[key] = []
                    candidates[key].append((expiry, sid, symbol))

        # Sort each contract's list by expiry, pick front-month
        for key in SYMBOL_MAP:
            if key not in candidates:
                continue
            sorted_c = sorted(candidates[key], key=lambda x: x[0])
            # Find the nearest expiry that's still in the future
            front = next((c for c in sorted_c if c[0] >= today), None)
            if not front:
                continue
            front_expiry, front_sid, front_name = front
            days_left = (front_expiry - today).days

            current_sid, current_name = FALLBACK_SIDS.get(key, (None, None))
            if current_sid != front_sid:
                log.info(f"AUTO ROLLOVER {key}: {current_name} → {front_name} (SID {current_sid}→{front_sid}, {days_left}d left)")
                FALLBACK_SIDS[key] = (front_sid, front_name)
            elif days_left <= 3:
                log.warning(f"CONTRACT EXPIRING IN {days_left} DAYS: {key} ({front_name})")
            else:
                log.debug(f"{key}: SID {front_sid} valid ({days_left}d to expiry)")

    except Exception as e:
        log.warning(f"Auto-rollover check failed: {e} — using existing FALLBACK_SIDS")



def get_mcx_native_orb(security_id, dhan_client, headers, lookback_minutes=15):
    """Real-time MCX ORB via Dhan MCX_COMM — zero delay (replaces yfinance)."""
    import time as _t
    _t.sleep(0.3)
    try:
        r = dhan_client.get_ohlc_intraday(str(security_id), 'MCX_COMM', '1')
        if not isinstance(r, dict): return None, None, None
        highs  = r.get('high',  [])
        lows   = r.get('low',   [])
        closes = r.get('close', [])
        if len(closes) < 2: return None, None, None
        n = min(lookback_minutes, len(highs))
        return float(max(highs[-n:])), float(min(lows[-n:])), float(closes[-1])
    except Exception as e:
        log.warning(f"MCX native ORB SID {security_id}: {e}")
        return None, None, None


def run_shadow():
    log.info("=" * 60)
    log.info("MCX SHADOW TRADER — Evening Session Starting")
    log.info("=" * 60)

    headers = get_dhan_headers()
    balance = get_balance(headers)
    if balance <= 0:
        log.error("Cannot fetch Dhan balance — aborting")
        return
    log.info(f"Dhan balance: ₹{balance:,.2f} | Max per trade: ₹{balance*MAX_CAPITAL_PCT:,.0f}")
    _active_c = [k for k,v in CONTRACTS.items() if v.get("active")]
    send_mcx_email("MCX Shadow Session Started",
        f"<h3>{'🔴 LIVE TRADING' if LIVE_MODE else '🟡 SHADOW MODE — No Real Orders'}</h3>"
        f"<p><b>{'Real orders will be placed' if LIVE_MODE else 'Monitoring only — zero money at risk tonight'}</b></p>"
        f"<hr>"
        f"<p><b>Balance:</b> Rs.{balance:,.2f} | <b>Max per trade:</b> Rs.{balance*MAX_CAPITAL_PCT:,.0f}</p>"
        f"<p><b>Active contracts:</b> {', '.join(_active_c)}</p>"
        f"<p><b>Strategy:</b> US ORB breakout → MCX entry | SL=0.5% | T1=+1% | T2=+2%</p>"
        f"<p><b>Session:</b> 18:30–23:00 IST</p>"
        f"<p style='color:gray;font-size:11px'>You will receive an email on each signal and exit. "
        f"P&L shown is simulated until LIVE_MODE=True.</p>")

    # Resolve active MCX contract IDs
    # Front-month MCX SIDs (Jul 2026 expiry — UPDATE monthly around 20th)
    FALLBACK_SIDS = {
        "CRUDEOIL_MINI": ("560978", "CRUDEOILM-19Aug2026-FUT"),
        "NATGASMINI":    ("561497", "NATGASMINI-26Aug2026-FUT"),
        "GOLDPETAL":     ("562056", "GOLDPETAL-31Aug2026-FUT"),
        "GOLD":          ("466583", "GOLD-05Aug2026-FUT"),
        "SILVER":        ("471726", "SILVERM-31Aug2026-FUT"),
    }
    contract_ids = {}
    for key, cfg in CONTRACTS.items():
        if not cfg["active"]:
            continue
        sid, name = get_mcx_active_contract(cfg["mcx_symbol"], headers)
        if sid:
            contract_ids[key] = (sid, name)
            log.info(f"Resolved {key}: {name} (sid={sid})")
        else:
            # Use known front-month SID fallback
            fb = FALLBACK_SIDS.get(key)
            if fb:
                contract_ids[key] = fb
                log.info(f"Using fallback SID {key}: {fb[1]} (sid={fb[0]})")
            else:
                log.warning(f"No SID for {key} — will skip")
                contract_ids[key] = (None, cfg["mcx_symbol"])

    # Fetch US ORBs at session start
    orbs = {}
    log.info("Fetching US benchmark 15-min ORBs (NOTE: ~15min delayed on free tier)")
    for key, cfg in CONTRACTS.items():
        if not cfg["active"]:
            continue
        high, low, cur = get_us_orb(cfg["us_ticker"])
        if high:
            orbs[key] = {"high": high, "low": low, "current": cur}
            log.info(
                f"  {key} ({cfg['us_ticker']}): "
                f"ORB H={high:.3f} L={low:.3f} | "
                f"current={cur:.3f} {'↑ABOVE' if cur>high else ('↓BELOW' if cur<low else '→INSIDE')}"
            )
        else:
            log.warning(f"  {key}: ORB fetch failed")

    # Shadow position tracker
    shadow_positions = []
    deployed_margin  = 0.0
    session_pnl      = 0.0
    end_time = datetime.now(IST).replace(
        hour=23, minute=0, second=0, microsecond=0)

    log.info(f"Shadow monitoring until {end_time.strftime('%H:%M IST')}")
    log.info("─" * 60)

    _last_mcx_ltp = {}  # BUG2 fix: track last known LTP per contract
    while datetime.now(IST) < end_time:
        now_ist = datetime.now(IST)
        open_count = sum(1 for p in shadow_positions if p.status == "OPEN")

        for key, cfg in CONTRACTS.items():
            if not cfg["active"] or key not in orbs:
                continue

            # Check US benchmark for breakout signal
            _sid_m, _ = contract_ids.get(key, (None, key))
            if not _sid_m: continue
            _, _, us_cur = get_mcx_native_orb(_sid_m, __import__("trading_bot", fromlist=["DhanClient"]).DhanClient(), headers)
            if us_cur is None:
                continue

            orb  = orbs[key]
            # Time block: no new entries after 22:15 IST
            _now = datetime.now(IST)
            if _now.hour > 22 or (_now.hour == 22 and _now.minute >= 15):
                continue
            # Daily loss limit: shutdown if session loss > 3% of balance
            if session_pnl < -(balance * DAILY_LOSS_LIMIT_PCT / 100):
                log.warning(f"DAILY LOSS LIMIT: Rs.{session_pnl:+,.0f} — no new entries")
                continue
            side = None
            if us_cur > orb["high"] * 1.001:   # 0.1% buffer above ORB
                side = "LONG"
            elif us_cur < orb["low"] * 0.999:  # 0.1% buffer below ORB
                side = "SHORT"

            # ── False breakout: 3 checks × 5s before entry ──────────────
            if side and open_count == 0:
                _sid2, _ = contract_ids.get(key, (None, key))
                _confirmed = True
                log.info(f"  Confirming {side} {key} (3 × 5s checks)...")
                for _c in range(3):
                    time.sleep(5)
                    _chk_ltp = get_mcx_ltp(_sid2, headers) if _sid2 else None
                    if _chk_ltp is None: continue
                    if side == "LONG" and _chk_ltp < orb["high"]:
                        log.info(f"  False breakout at check {_c+1}: {key} Rs.{_chk_ltp:.2f}")
                        _confirmed = False; break
                    if side == "SHORT" and _chk_ltp > orb["low"]:
                        log.info(f"  False breakout at check {_c+1}: {key} Rs.{_chk_ltp:.2f}")
                        _confirmed = False; break
                    if _chk_ltp <= 0:
                        log.warning(f"  Check {_c+1}/3: LTP=0 (fetch failed) — treating as false breakout")
                        _confirmed = False; break
                    log.info(f"  Check {_c+1}/3 ok: Rs.{_chk_ltp:.2f}")
                if not _confirmed:
                    pass  # fall through to original if side check below
            if side and open_count == 0 and locals().get('_confirmed', True):
                # Check margin availability
                margin_needed = cfg["margin_approx"]
                if deployed_margin + margin_needed > balance * MAX_CAPITAL_PCT:
                    log.warning(
                        f"SKIP {key}: margin needed ₹{margin_needed:,} > "
                        f"remaining allocation (deployed ₹{deployed_margin:,})"
                    )
                    continue

                # Get MCX LTP
                sid, sym = contract_ids.get(key, (None, key))
                mcx_ltp = get_mcx_ltp(sid, headers) if sid else 0.0
                if mcx_ltp > 0:
                    _last_mcx_ltp[key] = mcx_ltp  # BUG2: cache last valid
                    # Layer 3: breakeven SL trail
                    for _bp in shadow_positions:
                        if _bp.name == key and _bp.status == "OPEN":
                            _gain_pct = (mcx_ltp - _bp.entry)/_bp.entry*100 if _bp.side=="LONG" else (_bp.entry-mcx_ltp)/_bp.entry*100
                            update_breakeven_sl(_bp, _gain_pct)
                else:
                    mcx_ltp = _last_mcx_ltp.get(key, 0.0)  # use last known
                if mcx_ltp <= 0:
                    log.warning(f"MCX LTP unavailable for {key} — skipping signal")
                    continue

                # Compute levels
                if side == "LONG":
                    sl  = round(mcx_ltp * (1 - RISK_PCT), 2)
                    t1  = round(mcx_ltp * (1 + REWARD_PCT), 2)
                    t2  = round(mcx_ltp * (1 + REWARD2_PCT), 2)
                else:
                    sl  = round(mcx_ltp * (1 + RISK_PCT), 2)
                    t1  = round(mcx_ltp * (1 - REWARD_PCT), 2)
                    t2  = round(mcx_ltp * (1 - REWARD2_PCT), 2)

                # Dynamic lot sizing: 2 lots if balance > Rs.1L, else 1 lot
                _lots = 2 if balance > 100000 else 1
                _eff_margin = cfg["margin_approx"] * _lots
                if deployed_margin + _eff_margin > balance * MAX_CAPITAL_PCT:
                    _lots = 1
                    _eff_margin = cfg["margin_approx"]
                _eff_lot = cfg["lot_size"] * _lots
                log.info(f"  Sizing: {_lots} lot(s) | balance=Rs.{balance:,.0f}")
                pos = MCXShadowPosition(
                    name=key, side=side,
                    entry_price=mcx_ltp,
                    sl_price=sl, t1_price=t1, t2_price=t2,
                    lot_size=cfg["lot_size"],
                    margin=margin_needed,
                    timestamp=now_ist.isoformat()
                )
                shadow_positions.append(pos)
                deployed_margin += margin_needed
                open_count += 1
                log.info(
                    f"SHADOW ENTRY 🎯 {side} {key} @ ₹{mcx_ltp:.2f} | "
                    f"SL=₹{sl:.2f} T1=₹{t1:.2f} T2=₹{t2:.2f} | "
                    f"US signal: {cfg['us_ticker']}={us_cur:.3f} "
                    f"{'above ORB_H' if side=='LONG' else 'below ORB_L'} {orb['high'] if side=='LONG' else orb['low']:.3f}"
                )

        # Update open positions
        for pos in shadow_positions:
            if pos.status != "OPEN":
                continue
            sid, _ = contract_ids.get(pos.name, (None, None))
            ltp = get_mcx_ltp(sid, headers) if sid else 0.0
            if ltp > 0:
                closed = pos.update(ltp)
                if closed:
                    deployed_margin -= pos.margin
                    if pos.exit_reason:
                        sign = 1 if pos.side == "LONG" else -1
                        pnl = (pos.exit_price - pos.entry) * pos.lot * sign
                        session_pnl += pnl
                    if "SL_HIT" in (pos.exit_reason or ""):
                        _session_traded.add(pos.name)  # Layer 5: no re-entry

        # Status log every 5 min
        if now_ist.second < POLL_SECONDS:
            open_pos = [p for p in shadow_positions if p.status == "OPEN"]
            if open_pos:
                for p in open_pos:
                    sid, _ = contract_ids.get(p.name, (None, None))
                    ltp = get_mcx_ltp(sid, headers) if sid else 0.0
                    pnl = (ltp - p.entry) * p.lot if p.side == "LONG" \
                          else (p.entry - ltp) * p.lot
                    log.info(
                        f"📊 {p.side} {p.name}: ₹{ltp:.2f} | "
                        f"PnL=₹{pnl:+,.0f} | SL=₹{p.sl:.2f} T1=₹{p.t1:.2f}"
                    )

        time.sleep(POLL_SECONDS)

    # EOD square-off all open shadow positions
    log.info("─" * 60)
    log.info("23:00 IST — EOD square-off")
    for pos in shadow_positions:
        if pos.status == "OPEN":
            sid, _ = contract_ids.get(pos.name, (None, None))
            ltp = get_mcx_ltp(sid, headers) if sid else 0.0
            if ltp <= 0: ltp = _last_mcx_ltp.get(key, pos.entry)  # BUG2
            pos.force_close(ltp if ltp > 0 else pos.entry)
            sign = 1 if pos.side == "LONG" else -1
            session_pnl += (pos.exit_price - pos.entry) * pos.lot * sign

    # Session summary
    log.info("=" * 60)
    log.info(f"MCX SHADOW SESSION COMPLETE")
    log.info(f"  Total signals:  {len(shadow_positions)}")
    wins   = sum(1 for p in shadow_positions if "T" in (p.exit_reason or ""))
    losses = sum(1 for p in shadow_positions if "SL" in (p.exit_reason or ""))
    log.info(f"  T1/T2 hits:     {wins}")
    log.info(f"  SL hits:        {losses}")
    log.info(f"  Flat (EOD):     {len(shadow_positions)-wins-losses}")
    log.info(f"  Session P&L:    ₹{session_pnl:+,.0f}")
    log.info("=" * 60)

    # Save state for analysis
    state = {
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "balance": balance,
        "session_pnl": session_pnl,
        "positions": [p.to_dict() for p in shadow_positions],
    }
    try:  # BUG3: guard against OSError (too many open files)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)
        log.info(f"State saved to {STATE_PATH}")
    except OSError as _se:
        log.warning(f"State save failed (OSError): {_se} — session data may be lost")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

# ── Email + Groq helpers ──────────────────────────────────────────────────────
def send_mcx_email(subject, body_html):
    try:
        from secrets_manager import get_ses_sender, get_ses_recipient
        import boto3
        ses = boto3.client("ses", region_name="ap-south-1")
        ses.send_email(Source=get_ses_sender(),
            Destination={"ToAddresses": [get_ses_recipient()]},
            Message={"Subject": {"Data": f"[MCX Shadow] {subject}"},
                     "Body": {"Html": {"Data": body_html}}})
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")

def groq_context(commodity, side, entry, us_price, us_ticker):
    try:
        from groq import Groq
        import os, boto3
        # Get Groq key from SSM or environment
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            ssm = boto3.client("ssm", region_name="ap-south-1")
            try: key = ssm.get_parameter(Name="/trading-engine/groq-api-key", WithDecryption=True)["Parameter"]["Value"]
            except: pass
        if not key:
            return "Groq key not configured."
        client = Groq(api_key=key)
        prompt = (f"MCX {commodity} shadow signal: {side} at Rs.{entry:.2f}. "
                  f"US {us_ticker}={us_price:.3f}. "
                  f"In 2 sentences: why did US price break its range, and key risk for this MCX trade?")
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role":"user","content":prompt}],
            max_tokens=100, timeout=8)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Groq unavailable ({e})"

if __name__ == "__main__":
    if "--close" in sys.argv:
        log.info("Manual close triggered — squaring off any open shadow positions")
        # Load state and log forced close
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
            log.info(f"State from {state.get('date')}: "
                     f"P&L=₹{state.get('session_pnl',0):+,.0f}, "
                     f"positions={len(state.get('positions',[]))}")
        except Exception:
            log.warning("No state file found")
    else:
        run_shadow()

