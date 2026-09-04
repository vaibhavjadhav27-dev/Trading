#!/usr/bin/env python3
"""
shadow_orchestrator.py — V8.5.1 MCX + Swing Shadow Orchestration
Wires the new strategy modules into existing data infrastructure.
Usage:
  python3 shadow_orchestrator.py --mcx     (run during MCX session 19:00-23:00)
  python3 shadow_orchestrator.py --swing   (run after market close ~19:00)
  python3 shadow_orchestrator.py --report  (daily comparison report)
"""
import sys, json, os, time, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/trading-bot")
sys.path.insert(0, "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED")

IST = timezone(timedelta(hours=5, minutes=30))
SHADOW_DIR = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/shadow_comparison")
SHADOW_DIR.mkdir(parents=True, exist_ok=True)

# V12 FIX 3: Deduplicate log handlers (was causing 3x logging)
_log_configured = False
def _setup_logging():
    global _log_configured
    if _log_configured:
        return
    _log_configured = True
    root = logging.getLogger()
    # Remove any existing handlers to prevent duplicates
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler("/home/ubuntu/trading-bot/logs/shadow_orchestrator.log")
    fh.setFormatter(fmt)
    root.addHandler(fh)

_setup_logging()
log = logging.getLogger("shadow_orch")

# V10.1 Shadow Strategy (comparison logging)
try:
    from V10_1_STRATEGY_PATCH import acceleration_score as v10_score
    V10_AVAILABLE = True
except ImportError:
    V10_AVAILABLE = False


def now_ist(): return datetime.now(IST)
def today_str(): return now_ist().strftime("%Y-%m-%d")

def get_balance():
    from secrets_manager import get_dhan_token, get_dhan_client_id
    import requests
    token = get_dhan_token(); cid = get_dhan_client_id()
    headers = {"access-token": token, "client-id": cid, "Content-Type": "application/json"}
    try:
        r = requests.get("https://api.dhan.co/v2/fundlimit", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return float(d.get("availabelBalance", d.get("data",{}).get("availabelBalance",0)) or 0)
    except Exception as e:
        log.warning(f"Balance fetch: {e}")
    return 0.0

def load_or_create_snapshot():
    path = SHADOW_DIR / f"snapshot_{today_str()}.json"
    if path.exists():
        return json.loads(path.read_text())
    from shadow_comparator import new_day_snapshot, create_ledgers
    balance = get_balance()
    if balance <= 0:
        log.error("Cannot get Dhan balance for snapshot")
        return None
    snapshot = new_day_snapshot(balance)
    path.write_text(json.dumps(snapshot, indent=2))
    log.info(f"Day snapshot: Rs.{balance:,.2f} → 3 virtual ledgers")
    return snapshot

def load_ledgers():
    path = SHADOW_DIR / f"ledgers_{today_str()}.json"
    if path.exists():
        data = json.loads(path.read_text())
        from shadow_comparator import ShadowLedger
        ledgers = {}
        for k, v in data.items():
            ledgers[k] = ShadowLedger(**v)
        return ledgers
    snapshot = load_or_create_snapshot()
    if not snapshot: return None
    from shadow_comparator import create_ledgers
    ledgers = create_ledgers(snapshot)
    save_ledgers(ledgers)
    return ledgers

def save_ledgers(ledgers):
    from dataclasses import asdict
    path = SHADOW_DIR / f"ledgers_{today_str()}.json"
    data = {k: asdict(v) for k, v in ledgers.items()}
    path.write_text(json.dumps(data, indent=2))

# ═══════════════════════════════════════════════════
# MCX SESSION
# ═══════════════════════════════════════════════════
import fcntl
import atexit

_LOCK_FILE = "/tmp/.mcx_shadow_orchestrator.lock"
_lock_fd = None

def _acquire_lock():
    global _lock_fd
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        atexit.register(_release_lock)
        return True
    except IOError:
        print(f"FATAL: Another MCX shadow process is already running (lock: {_LOCK_FILE})")
        return False

def _release_lock():
    global _lock_fd
    if _lock_fd:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        try:
            os.unlink(_LOCK_FILE)
        except Exception:
            pass


import fcntl
import atexit

# --- V12 MCX Validation ---
try:
    from V11_V12_HARDENED_COMBINED_PATCH import validate_mcx_contract, parse_dhan_mcx_ltp, mcx_execution_quality_ok
    _V12_MCX_AVAILABLE = True
except Exception:
    _V12_MCX_AVAILABLE = False
# --- end V12 MCX ---

_LOCK_FILE = "/tmp/.mcx_shadow_orchestrator.lock"
_lock_fd = None

def _acquire_lock():
    global _lock_fd
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        atexit.register(_release_lock)
        return True
    except IOError:
        print(f"FATAL: Another MCX shadow process is already running (lock: {_LOCK_FILE})")
        return False

def _release_lock():
    global _lock_fd
    if _lock_fd:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        try:
            os.unlink(_LOCK_FILE)
        except Exception:
            pass


def run_mcx_shadow():
    """MCX V12 Shadow — full session-aware engine with multi-session support."""
    if not _acquire_lock():
        return
    log.info("=" * 60)
    log.info("MCX V12 SHADOW SESSION")
    log.info("=" * 60)

    import os, json, boto3, requests as _req
    from datetime import date as _date

    # Fresh Dhan token from SSM
    ssm = boto3.client('ssm', region_name='ap-south-1')
    token = ssm.get_parameter(Name='/trading-engine/dhan/access-token', WithDecryption=True)['Parameter']['Value'].strip()
    client_id = ssm.get_parameter(Name='/trading-engine/dhan/client-id', WithDecryption=True)['Parameter']['Value'].strip()
    os.environ['DHAN_ACCESS_TOKEN'] = token
    os.environ['DHAN_CLIENT_ID'] = client_id

    # Resolve contracts via V8.5.4 NativeMCXData (proven resolver)
    from mcx_v854_engine import NativeMCXData, completed_orb

    session = _req.Session()
    headers = {'Content-Type': 'application/json', 'access-token': token, 'client-id': client_id}
    data_provider = NativeMCXData(session, headers)

    prefixes = {
        "CRUDEOIL": "CRUDEOIL", "GOLD": "GOLD", "GOLDPETAL": "GOLDPETAL",
        "SILVER": "SILVER", "SILVERM": "SILVERM",
        "NATURALGAS": "NATURALGAS", "NATGASMINI": "NATGASMINI",
    }
    contracts_raw = data_provider.resolve_contracts(prefixes, _date.today())
    if not contracts_raw:
        log.error("MCX V12: No contracts resolved from scrip master")
        return

    log.info(f"MCX V12: Resolved {len(contracts_raw)} contracts from scrip master")

    # Convert namedtuples to dicts for V12 compatibility
    available_contracts = {}
    for key, c in contracts_raw.items():
        available_contracts[key] = {
            "symbol": key,  # bare symbol (CRUDEOIL, GOLD, etc.)
            "security_id": int(c.security_id),
            "lot_size": getattr(c, "lot_size", 1),
            "tick_size": getattr(c, "tick_size", 0.05),
            "expiry": getattr(c, "expiry", ""),
            "exchange_segment": "MCX_COMM",
            "instrument_type": "FUTCOM",
            "option_type": "",
            "full_symbol": getattr(c, "symbol", key),
        }
        log.info(f"  {key}: SID={c.security_id} lot={getattr(c, 'lot_size', '?')}")

    # Load virtual capital from ledger
    ledgers = load_ledgers()
    mcx_cap = 200000.0  # default
    if ledgers and ledgers.get("MCX_SHADOW"):
        mcx_cap = ledgers["MCX_SHADOW"].starting_capital
    log.info(f"MCX V12: Virtual capital Rs.{mcx_cap:,.2f}")

    # Initialize V12 engine
    try:
        from mcx_v12_engine import MCXEngine, MCXConfig
        config = MCXConfig()
        config.live = False  # SHADOW ONLY
        config.capital = mcx_cap

        engine = MCXEngine(
            config=config,
            data_provider=data_provider,
            gateway=None,  # shadow mode — no broker interaction
        )
        log.info(f"MCX V12 Engine initialized (version={config.version})")
    except ImportError as e:
        log.error(f"MCX V12: Failed to import engine: {e}")
        log.error("Falling back to V8.5.4 is NOT available — MCX shadow aborted")
        return
    except Exception as e:
        log.error(f"MCX V12: Failed to initialize engine: {e}")
        return

    # Run the engine (blocks until session ends)
    try:
        engine.run(
            symbols=prefixes,
            capital=mcx_cap,
            available_contracts=available_contracts,
        )
    except KeyboardInterrupt:
        log.info("MCX V12: Interrupted by user")
    except Exception as e:
        log.error(f"MCX V12: Fatal error in engine.run(): {e}")
        import traceback
        log.error(traceback.format_exc())

    log.info("MCX V12 SHADOW SESSION COMPLETE")


def run_swing_shadow():
    log.info("=" * 50)
    log.info("SWING V8.5.1 SHADOW SESSION")
    log.info("=" * 50)
    ledgers = load_ledgers()
    if not ledgers: return
    swing_ledger = ledgers.get("SWING_SHADOW")
    if not swing_ledger:
        log.error("No Swing ledger"); return

    from swing_v851_strategy import evaluate_swing, size_swing, swing_exit_v851
    import csv

    log.info(f"Swing virtual capital: Rs.{swing_ledger.starting_capital:,.2f}")

    # Load watchlist for stock data
    watchlist_path = "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/watchlist.csv"
    stocks = []
    with open(watchlist_path) as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                stocks.append({"ticker": row[0], "sid": row[1], "sector": row[2] if len(row) > 2 else ""})

    # Build features from available data (prev_close cache + metrics)
    metrics_path = "/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/stock_metrics.json"
    metrics = {}
    if os.path.exists(metrics_path):
        metrics = json.loads(open(metrics_path).read())

    signals_log = []
    shadow_risk = 0.0

    for st in stocks[:100]:  # Top 100 for now
        ticker = st["ticker"]
        m = metrics.get(ticker, {})
        price = float(m.get("last_price", m.get("close", 0)) or 0)
        if price <= 0: continue

        features = {
            "ticker": ticker, "price": price,
            "sma20": float(m.get("sma20", price * 0.98) or price * 0.98),
            "sma50": float(m.get("sma50", price * 0.95) or price * 0.95),
            "sma200": float(m.get("sma200", price * 0.90) or price * 0.90),
            "atr": float(m.get("atr", price * 0.02) or price * 0.02),
            "rs10": float(m.get("rs_5d", 0) or 0),
            "rs20": float(m.get("rs_20d", 0) or 0),
            "rvol": float(m.get("rvol", 1.0) or 1.0),
            "sector_rs": float(m.get("sector_rs", 0) or 0),
            "market_rs": 0.0,
            "high20": float(m.get("high_20d", price * 1.05) or price * 1.05),
            "low20": float(m.get("low_20d", price * 0.95) or price * 0.95),
            "base_tightness": 0.5,
            "accumulation": float(m.get("clv", 0.5) or 0.5),
            "breakout": False, "retest": False,
            "close_strength": 0.6, "catalyst": 0.0,
        }

        signal = evaluate_swing(features)
        # V10.1 Shadow comparison
        if V10_AVAILABLE and signal:
            try:
                _v10s = {"symbol": ticker, "price": price,
                    "vwap": price * 0.99, "atr": features.get("atr", price*0.02),
                    "mom5": 0, "mom15": 0, "rvol": features.get("rvol", 1.0),
                    "rs_val": features.get("rs10", 0), "sector_rs": features.get("sector_rs", 0),
                    "volume_acceleration": 0,
                    "orb_high": features.get("high20", price*1.05),
                    "orb_low": features.get("low20", price*0.95),
                    "side": signal.side, "adx": 25, "adx_slope": 0,
                    "choppiness": 50, "atr_percentile": 50,
                    "segment": "NSE_INTRADAY", "ts": __import__("datetime").datetime.now()}
                _v10r = v10_score(_v10s)
                log.info(f"  [V10.1_SWING] {ticker}: det={_v10r.get('acceleration_detected')} stage={_v10r.get('move_stage')} score={_v10r.get('score',0):.1f}")
            except Exception as _e:
                log.warning(f"  [V10.1_SWING] {ticker} error: {_e}")

        if signal:
            sizing = size_swing(signal, swing_ledger.starting_capital, shadow_risk)
            log.info(f"SWING SIGNAL: {signal.side} {ticker} score={signal.score:.1f} expected={signal.expected_move_pct:.1f}% qty={sizing.get('qty',0)}")
            signals_log.append({
                "ticker": ticker, "side": signal.side,
                "score": signal.score, "price": price,
                "expected_move_pct": signal.expected_move_pct,
                "setup": signal.setup, "qty": sizing.get("qty", 0),
                "action": "SHADOW_ENTRY" if sizing.get("qty", 0) > 0 else "REJECTED_SIZE"
            })
            if sizing.get("qty", 0) > 0:
                shadow_risk += abs(price - signal.stop) * sizing["qty"]

    # Save swing results
    result_path = SHADOW_DIR / f"swing_{today_str()}.json"
    result_path.write_text(json.dumps({"date": today_str(),
        "signals": signals_log, "total_signals": len(signals_log),
        "qualified": sum(1 for s in signals_log if s.get("qty", 0) > 0)}, indent=2, default=str))
    log.info(f"Swing session: {len(signals_log)} signals, {sum(1 for s in signals_log if s.get('qty',0)>0)} qualified")
    save_ledgers(ledgers)

# ═══════════════════════════════════════════════════
# DAILY REPORT
# ═══════════════════════════════════════════════════
def run_report():
    log.info("Generating daily shadow comparison report...")
    ledgers = load_ledgers()
    if not ledgers: return
    from shadow_comparator import daily_report
    report = daily_report(ledgers)
    report_path = SHADOW_DIR / f"comparison_{today_str()}.json"
    report_path.write_text(json.dumps(report, indent=2))
    log.info(f"Comparison report: {report_path}")
    for r in report:
        log.info(f"  {r['engine']}: return={r['return_pct']:+.3f}% trades={r['trades']} fees={r['fees']:.2f}")

if __name__ == "__main__":
    if "--mcx" in sys.argv:
        run_mcx_shadow()
    elif "--swing" in sys.argv:
        run_swing_shadow()
    elif "--report" in sys.argv:
        run_report()
    else:
        print("Usage: shadow_orchestrator.py --mcx|--swing|--report")
