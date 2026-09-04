#!/usr/bin/env python3
"""
daily_research_export.py — Expert Review Research Package Generator
===================================================================
Builds a comprehensive daily research package for expert review.

READ-ONLY: Does NOT modify any trading engine files.
Runs after market close (post 15:30 IST for NSE, post 23:30 IST for MCX).

Output: /tmp/trading_review_YYYY-MM-DD.tgz emailed via SES.

Requirements (expert spec 13 categories):
  A. Complete code snapshot (secrets redacted)
  B. All candidate data (every candidate considered)
  C. Missed trade analysis (detection → peak → reason)
  D. Actual trades (entry + exit with MFE/MAE/R)
  E. Dhan broker truth (orders/trades/positions)
  F. Market data (OHLCV for traded + high-value candidates)
  G. NSE/MCX/Swing same structure
  H. Calculated metrics (MFE/MAE/R/capital)
  I. Daily summary JSON
  J. MANIFEST.json
  K. Auto-email

Usage:
  /home/ubuntu/trading-bot/venv/bin/python3 daily_research_export.py [--date 2026-08-25]

Cron (after MCX close, covers full day):
  35 18 * * 1-5  cd /home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED && /home/ubuntu/trading-bot/venv/bin/python3 daily_research_export.py
"""

import json, csv, os, sys, shutil, tarfile, re, glob, io, logging
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

# ============================================================================
# CONFIG
# ============================================================================
BASE_DIR = Path("/home/ubuntu/trading-bot")
PROD_DIR = BASE_DIR / "V84_PRODUCTION_INTEGRATED"
OUTPUT_BASE = Path("/tmp/trading_review")
LOG_DIR = PROD_DIR / "logs"
STATE_DIR = PROD_DIR / "mcx_state"
TRADE_LOG_DIR = PROD_DIR / "trade_logs"

# Email config (uses existing SES setup)
SES_SENDER = None  # loaded from secrets_manager
SES_RECIPIENT = None
REGION = "ap-south-1"

# Secrets patterns to redact
SECRET_PATTERNS = [
    r'(access_token\s*[=:]\s*)["\']?[\w\-\.]+["\']?',
    r'(api_key\s*[=:]\s*)["\']?[\w\-\.]+["\']?',
    r'(api_secret\s*[=:]\s*)["\']?[\w\-\.]+["\']?',
    r'(client_id\s*[=:]\s*)["\']?[\w\-\.]+["\']?',
    r'(password\s*[=:]\s*)["\']?[^\s,}\]]+["\']?',
    r'(AWS_ACCESS_KEY_ID\s*[=:]\s*)\S+',
    r'(AWS_SECRET_ACCESS_KEY\s*[=:]\s*)\S+',
    r'(DHAN_ACCESS_TOKEN\s*[=:]\s*)\S+',
    r'(token\s*[=:]\s*)["\']?[\w\-\.]{20,}["\']?',
    r'(Bearer\s+)\S{20,}',
    r'(ssh-rsa\s+)\S+',
    r'-----BEGIN[A-Z ]+KEY-----[\s\S]*?-----END[A-Z ]+KEY-----',
]

ENGINE_VERSIONS = {
    "nse": "V8.5.5",
    "mcx": "V8.5.4",
    "swing": "V8.5.1",
}

log = logging.getLogger("research_export")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ============================================================================
# HELPERS
# ============================================================================

def get_trade_date(override: Optional[str] = None) -> date:
    """Get the trading date (default: today, or --date arg)."""
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()
    return date.today()


def redact_secrets(text: str) -> str:
    """Remove all sensitive values from text."""
    for pattern in SECRET_PATTERNS:
        try:
            text = re.sub(
                pattern,
                lambda m: (m.group(1) if m.lastindex else '') + '[REDACTED]',
                text, flags=re.IGNORECASE
            )
        except Exception:
            pass
    text = re.sub(r'eyJ[A-Za-z0-9_\\-]{50,}', '[REDACTED_JWT]', text)
    return text
def safe_json_dump(obj, fp, **kwargs):
    """JSON dump with default serializer for dates/etc."""
    json.dump(obj, fp, default=str, indent=2, **kwargs)


def load_dhan_gateway():
    """Import the existing Dhan gateway with fresh token from SSM."""
    sys.path.insert(0, str(PROD_DIR))
    sys.path.insert(0, str(BASE_DIR))
    try:
        import boto3
        ssm = boto3.client('ssm', region_name='ap-south-1')
        token = ssm.get_parameter(Name='/trading-engine/dhan/access-token', WithDecryption=True)['Parameter']['Value'].strip()
        client_id = ssm.get_parameter(Name='/trading-engine/dhan/client-id', WithDecryption=True)['Parameter']['Value'].strip()
        from v82_dhan_gateway import DhanV82Gateway as DhanGateway
        gw = DhanGateway(client_id=client_id, access_token=token)
        return gw
    except Exception as e:
        log.warning(f"Could not load DhanGateway: {e}")
        return None


def load_ses_config():
    """Load SES email config from secrets_manager."""
    global SES_SENDER, SES_RECIPIENT
    sys.path.insert(0, str(PROD_DIR))
    try:
        from secrets_manager import get_ses_sender, get_ses_recipient
        SES_SENDER = get_ses_sender()
        SES_RECIPIENT = get_ses_recipient()
    except Exception:
        SES_SENDER = "trading-bot@vaibhavjadhav.com"
        SES_RECIPIENT = "vaibhavjadhav27@gmail.com"


# ============================================================================
# SECTION A: CODE SNAPSHOT (with redaction)
# ============================================================================

def export_code_snapshot(output_dir: Path, trade_date: date):
    """Copy all source code with secrets redacted."""
    code_dir = output_dir / "CODE" / "source_snapshot"

    # NSE engine files
    nse_dir = code_dir / "NSE"
    nse_dir.mkdir(parents=True, exist_ok=True)
    nse_files = [
        "trading_bot_v84.py", "trading_bot_v82.py", "v82_strategy.py",
        "v82_dhan_gateway.py", "v84_trade_logger.py", "V854_UNIFIED_PATCH.py",
        "V8_5_5_EARLY_ENTRY_PROFIT_PROTECTION_PATCH.py", "V85_1_PATCH.py",
        "v853_profit_engine_patch.py", "config.py", "trade_policy.py",
        "indicators.py", "dual_scorer.py", "orb_rescan.py",
    ]
    for f in nse_files:
        src = PROD_DIR / f
        if src.exists():
            content = redact_secrets(src.read_text(errors='replace'))
            (nse_dir / f).write_text(content)

    # MCX engine files
    mcx_dir = code_dir / "MCX"
    mcx_dir.mkdir(parents=True, exist_ok=True)
    mcx_files = [
        "mcx_v854_engine.py", "mcx_native_orb_v2.py", "mcx_shadow_trader.py",
        "mcx_v851_strategy.py", "mcx_policy_v8.py", "shadow_orchestrator.py",
    ]
    for f in mcx_files:
        src = PROD_DIR / f
        if src.exists():
            content = redact_secrets(src.read_text(errors='replace'))
            (mcx_dir / f).write_text(content)

    # Swing engine files
    swing_dir = code_dir / "SWING"
    swing_dir.mkdir(parents=True, exist_ok=True)
    swing_files = [
        "swing_scanner.py", "swing_monitor.py", "swing_paper_trader.py",
        "swing_daily.py", "swing_exit.py", "swing_v851_strategy.py",
        "swing_regime.py", "swing_policy_v8.py",
    ]
    for f in swing_files:
        src = PROD_DIR / f
        if src.exists():
            content = redact_secrets(src.read_text(errors='replace'))
            (swing_dir / f).write_text(content)

    # CONFIG
    config_dir = code_dir / "CONFIG"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_files = ["config.py", "trade_policy.py"]
    for f in config_files:
        src = PROD_DIR / f
        if src.exists():
            content = redact_secrets(src.read_text(errors='replace'))
            (config_dir / f).write_text(content)

    # requirements
    req_path = BASE_DIR / "requirements.txt"
    if req_path.exists():
        shutil.copy2(req_path, config_dir / "requirements.txt")
    else:
        # Generate from pip freeze
        try:
            result = subprocess.run(
                [str(BASE_DIR / "venv/bin/pip"), "freeze"],
                capture_output=True, text=True, timeout=15
            )
            (config_dir / "requirements.txt").write_text(result.stdout)
        except Exception:
            pass

    log.info(f"Code snapshot exported to {code_dir}")
    return code_dir


# ============================================================================
# SECTION B+C: CANDIDATE DATA + MISSED TRADES
# ============================================================================

def export_candidates(output_dir: Path, trade_date: date):
    """Export all candidate data from scan logs and trade logger."""
    nse_dir = output_dir / "NSE"
    nse_dir.mkdir(parents=True, exist_ok=True)

    # Look for candidate score CSVs (from candidate_logger.py)
    date_str = trade_date.strftime("%Y-%m-%d")
    date_str2 = trade_date.strftime("%Y%m%d")

    candidates = []

    # Source 1: candle_archive/candidate_scores_{date}.csv
    score_files = glob.glob(str(BASE_DIR / f"candle_archive/candidate_scores_{date_str}*.csv"))
    score_files += glob.glob(str(PROD_DIR / f"candle_archive/candidate_scores_{date_str}*.csv"))
    score_files += glob.glob(str(PROD_DIR / f"logs/candidate_scores_{date_str}*.csv"))

    for sf in score_files:
        try:
            with open(sf) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    candidates.append(row)
        except Exception as e:
            log.warning(f"Error reading {sf}: {e}")

    # Source 2: Parse bot.log for candidate evaluations
    bot_log = PROD_DIR / "bot.log"
    if bot_log.exists():
        try:
            with open(bot_log) as f:
                for line in f:
                    if date_str in line or date_str2 in line:
                        # Extract V853 ENTER/SKIP lines
                        if "V853 ENTER:" in line or "V853 SKIP:" in line:
                            candidates.append({"raw_log": line.strip(), "source": "bot.log"})
                        # Extract V855 acceleration
                        if "v855_v853_filter" in line or "EARLY_ACCELERATION" in line:
                            candidates.append({"raw_log": line.strip(), "source": "bot.log"})
        except Exception as e:
            log.warning(f"Error parsing bot.log: {e}")

    # Source 3: JSON event logs (from _event() calls)
    event_log = PROD_DIR / "logs" / f"events_{date_str}.jsonl"
    if not event_log.exists():
        event_log = PROD_DIR / "logs" / "events.jsonl"

    event_candidates = []
    event_entries = []
    event_exits = []
    event_all = []

    for elog in [event_log, PROD_DIR / "logs" / "events.jsonl",
                 PROD_DIR / f"events_{date_str}.jsonl"]:
        if elog.exists():
            try:
                with open(elog) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                            evt_date = str(evt.get("timestamp", evt.get("time", "")))[:10]
                            if evt_date == date_str or date_str2 in evt_date:
                                event_all.append(evt)
                                evt_type = evt.get("event", evt.get("type", ""))
                                if "candidate" in evt_type.lower() or "scan" in evt_type.lower():
                                    event_candidates.append(evt)
                                elif "entry" in evt_type.lower() or "ENTRY" in evt_type.upper():
                                    event_entries.append(evt)
                                elif "exit" in evt_type.lower() or "EXIT" in evt_type.upper():
                                    event_exits.append(evt)
                                elif "v855_exit_eval" in evt_type:
                                    event_exits.append(evt)
                                elif "v855_v853_filter" in evt_type:
                                    event_candidates.append(evt)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                log.warning(f"Error reading {elog}: {e}")

    # Write candidates CSV
    if candidates:
        fieldnames = set()
        for c in candidates:
            fieldnames.update(c.keys())
        fieldnames = sorted(fieldnames)
        with open(nse_dir / "candidates.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(candidates)

    # Write event-based candidates
    if event_candidates:
        with open(nse_dir / "candidate_events.json", "w") as f:
            safe_json_dump(event_candidates, f)

    # Write all events for the day
    if event_all:
        with open(nse_dir / "all_events.jsonl", "w") as f:
            for evt in event_all:
                f.write(json.dumps(evt, default=str) + "\n")

    log.info(f"Candidates: {len(candidates)} CSV rows, {len(event_candidates)} events")
    return candidates, event_entries, event_exits


# ============================================================================
# SECTION D: ACTUAL TRADES (with MFE/MAE/R calculations)
# ============================================================================

def export_trades(output_dir: Path, trade_date: date, event_entries, event_exits):
    """Export trades with calculated metrics."""
    nse_dir = output_dir / "NSE"
    nse_dir.mkdir(parents=True, exist_ok=True)

    date_str = trade_date.strftime("%Y-%m-%d")

    # Source 1: trade_logs directory
    trades = []
    trade_log_patterns = [
        str(TRADE_LOG_DIR / f"*{date_str}*"),
        str(PROD_DIR / f"logs/trades_{date_str}*"),
        str(PROD_DIR / f"trade_logs/*{date_str}*"),
    ]
    for pattern in trade_log_patterns:
        for f in glob.glob(pattern):
            try:
                if f.endswith('.json'):
                    with open(f) as fp:
                        data = json.load(fp)
                        if isinstance(data, list):
                            trades.extend(data)
                        else:
                            trades.append(data)
                elif f.endswith('.csv'):
                    with open(f) as fp:
                        reader = csv.DictReader(fp)
                        trades.extend(list(reader))
            except Exception as e:
                log.warning(f"Error reading trade log {f}: {e}")

    # Source 2: Event log entries/exits
    for entry in event_entries:
        entry["_source"] = "event_log"
        trades.append(entry)

    # Calculate MFE/MAE/R for each trade
    enriched_trades = []
    for t in trades:
        enriched = dict(t)
        try:
            entry_px = float(t.get("entry_price", t.get("entry", t.get("fill_price", 0))) or 0)
            exit_px = float(t.get("exit_price", t.get("exit", 0)) or 0)
            sl = float(t.get("initial_sl", t.get("sl", 0)) or 0)
            peak = float(t.get("peak", t.get("peak_price", 0)) or 0)
            side = str(t.get("side", "LONG")).upper()

            if entry_px > 0 and sl > 0:
                risk = abs(entry_px - sl)
                if risk > 0:
                    if side == "LONG":
                        mfe = (peak - entry_px) if peak > entry_px else 0
                        mae = (entry_px - float(t.get("min_price", entry_px)))
                        final_pnl = (exit_px - entry_px) if exit_px > 0 else 0
                    else:
                        mfe = (entry_px - peak) if peak < entry_px and peak > 0 else 0
                        mae = (float(t.get("max_price", entry_px)) - entry_px)
                        final_pnl = (entry_px - exit_px) if exit_px > 0 else 0

                    enriched["risk_per_share"] = round(risk, 2)
                    enriched["mfe"] = round(mfe, 2)
                    enriched["mae"] = round(mae, 2)
                    enriched["peak_r"] = round(mfe / risk, 3) if risk > 0 else 0
                    enriched["final_r"] = round(final_pnl / risk, 3) if risk > 0 else 0
                    enriched["profit_given_back"] = round((mfe - final_pnl) / risk, 3) if risk > 0 and mfe > final_pnl else 0

                    qty = int(t.get("qty", t.get("quantity", 0)) or 0)
                    if qty > 0 and exit_px > 0:
                        from dhan_charges import dhan_charges_mis
                        charges = dhan_charges_mis(qty, entry_px, exit_px) if entry_px > 0 else 0
                        enriched["gross_pnl"] = round(final_pnl * qty, 2)
                        enriched["charges"] = round(charges, 2)
                        enriched["net_pnl"] = round(final_pnl * qty - charges, 2)
        except Exception as e:
            enriched["_calc_error"] = str(e)

        enriched_trades.append(enriched)

    # Deduplicate trades by (symbol + side + entry_time + entry_price)
    seen = set()
    deduped = []
    for t in enriched_trades:
        key = (
            t.get("symbol", ""),
            t.get("side", ""),
            str(t.get("entry_time", t.get("entry_time", ""))),
            str(t.get("entry_price", t.get("entry", "")))
        )
        if key not in seen:
            seen.add(key)
            deduped.append(t)
    enriched_trades = deduped

    # Write trades
    if enriched_trades:
        with open(nse_dir / "trades.json", "w") as f:
            safe_json_dump(enriched_trades, f)

        # Also CSV version
        fieldnames = set()
        for t in enriched_trades:
            fieldnames.update(t.keys())
        fieldnames = sorted(fieldnames)
        with open(nse_dir / "trades.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(enriched_trades)

    # Write exits separately
    exits = [e for e in event_exits if "exit" in str(e.get("event", "")).lower() or e.get("exit")]
    if exits:
        with open(nse_dir / "exits.json", "w") as f:
            safe_json_dump(exits, f)

    log.info(f"Trades exported: {len(enriched_trades)}")
    return enriched_trades


# ============================================================================
# SECTION E: DHAN BROKER TRUTH
# ============================================================================

def export_dhan_truth(output_dir: Path, trade_date: date):
    """Fetch orders, trades, positions from Dhan API."""
    dhan_dir = output_dir / "DHAN"
    dhan_dir.mkdir(parents=True, exist_ok=True)

    gw = load_dhan_gateway()
    if not gw:
        log.warning("DhanGateway not available — skipping broker truth")
        (dhan_dir / "ERROR.txt").write_text("DhanGateway could not be loaded")
        return {}

    broker_data = {}

    # Orders
    try:
        orders = gw.get_order_list() if hasattr(gw, 'get_order_list') else gw.s.get(
            f"{gw.API}/orders", headers=gw.headers, timeout=10
        ).json()
        broker_data["orders"] = orders
        with open(dhan_dir / "orders.json", "w") as f:
            safe_json_dump(orders, f)
        log.info(f"Dhan orders: {len(orders) if isinstance(orders, list) else 'dict'}")
    except Exception as e:
        log.error(f"Dhan orders fetch failed: {e}")
        (dhan_dir / "orders_error.txt").write_text(str(e))

    # Trades
    try:
        trades = gw.get_trade_book() if hasattr(gw, 'get_trade_book') else gw.s.get(
            f"{gw.API}/trades", headers=gw.headers, timeout=10
        ).json()
        broker_data["trades"] = trades
        with open(dhan_dir / "trades.json", "w") as f:
            safe_json_dump(trades, f)
        log.info(f"Dhan trades: {len(trades) if isinstance(trades, list) else 'dict'}")
    except Exception as e:
        log.error(f"Dhan trades fetch failed: {e}")

    # Positions
    try:
        positions = gw.get_positions() if hasattr(gw, 'get_positions') else gw.s.get(
            f"{gw.API}/positions", headers=gw.headers, timeout=10
        ).json()
        broker_data["positions"] = positions
        with open(dhan_dir / "positions.json", "w") as f:
            safe_json_dump(positions, f)
        log.info(f"Dhan positions: {len(positions) if isinstance(positions, list) else 'dict'}")
    except Exception as e:
        log.error(f"Dhan positions fetch failed: {e}")

    return broker_data


# ============================================================================
# SECTION F: MARKET DATA (OHLCV for traded + high-value candidates)
# ============================================================================

def export_market_data(output_dir: Path, trade_date: date, trades: List[Dict]):
    """Export OHLCV for traded stocks and top candidates."""
    nse_dir = output_dir / "NSE"
    nse_dir.mkdir(parents=True, exist_ok=True)

    # Collect security IDs of traded stocks
    traded_sids = set()
    for t in trades:
        sid = t.get("security_id", t.get("sid", ""))
        if sid:
            traded_sids.add(str(sid))

    # Also look for existing candle archives
    date_str = trade_date.strftime("%Y-%m-%d")
    archive_patterns = [
        str(BASE_DIR / f"candle_archive/*{date_str}*"),
        str(PROD_DIR / f"candle_archive/*{date_str}*"),
        str(PROD_DIR / f"logs/candles_*{date_str}*"),
    ]

    market_data = {}
    for pattern in archive_patterns:
        for f in glob.glob(pattern):
            try:
                fname = Path(f).name
                if f.endswith('.csv'):
                    with open(f) as fp:
                        market_data[fname] = fp.read()
                elif f.endswith('.json'):
                    with open(f) as fp:
                        market_data[fname] = json.load(fp)
            except Exception:
                pass

    # If we have the gateway, fetch OHLCV for traded stocks
    if traded_sids:
        gw = load_dhan_gateway()
        if gw and hasattr(gw, 'get_ohlc_intraday'):
            for sid in list(traded_sids)[:15]:  # Limit to 15 to avoid rate limits
                try:
                    import time
                    time.sleep(0.3)  # Rate limiting
                    ohlc = gw.get_ohlc_intraday(sid, "NSE_EQ", "5")
                    if ohlc:
                        market_data[f"ohlcv_{sid}_5m.json"] = ohlc
                except Exception as e:
                    log.warning(f"OHLCV fetch failed for {sid}: {e}")

    # Write market data
    if market_data:
        market_file = nse_dir / "market_data.json"
        with open(market_file, "w") as f:
            safe_json_dump(market_data, f)
        log.info(f"Market data: {len(market_data)} items")

    # Copy any existing candle archive files directly
    snapshots_dir = nse_dir / "snapshots"
    snapshots_dir.mkdir(exist_ok=True)
    for pattern in archive_patterns:
        for f in glob.glob(pattern):
            try:
                shutil.copy2(f, snapshots_dir / Path(f).name)
            except Exception:
                pass


# ============================================================================
# SECTION G: MCX + SWING
# ============================================================================

def export_mcx(output_dir: Path, trade_date: date):
    """Export MCX shadow data."""
    mcx_dir = output_dir / "MCX"
    mcx_dir.mkdir(parents=True, exist_ok=True)

    date_str = trade_date.strftime("%Y-%m-%d")

    # MCX state files
    for f in glob.glob(str(STATE_DIR / f"*{date_str}*")) + \
             glob.glob(str(STATE_DIR / "*.json")):
        try:
            shutil.copy2(f, mcx_dir / Path(f).name)
        except Exception:
            pass

    # MCX logs
    mcx_log = PROD_DIR / "logs" / "mcx_v854.log"
    if mcx_log.exists():
        try:
            content = mcx_log.read_text(errors='replace')
            # Filter to today's entries
            today_lines = [l for l in content.split('\n') if date_str in l]
            (mcx_dir / f"mcx_log_{date_str}.txt").write_text('\n'.join(today_lines))
        except Exception:
            pass

    # MCX events
    for f in glob.glob(str(PROD_DIR / f"logs/mcx_events*")):
        try:
            shutil.copy2(f, mcx_dir / Path(f).name)
        except Exception:
            pass

    log.info("MCX data exported")


def export_swing(output_dir: Path, trade_date: date):
    """Export Swing data."""
    swing_dir = output_dir / "SWING"
    swing_dir.mkdir(parents=True, exist_ok=True)

    date_str = trade_date.strftime("%Y-%m-%d")

    # Swing state/positions
    swing_patterns = [
        str(PROD_DIR / "swing_*.json"),
        str(PROD_DIR / "logs/swing_*"),
        str(BASE_DIR / "swing_*.json"),
    ]
    for pattern in swing_patterns:
        for f in glob.glob(pattern):
            try:
                shutil.copy2(f, swing_dir / Path(f).name)
            except Exception:
                pass

    log.info("Swing data exported")


# ============================================================================
# SECTION H+I: PERFORMANCE METRICS + DAILY SUMMARY
# ============================================================================

def calculate_performance(output_dir: Path, trades: List[Dict], broker_data: Dict, trade_date: date):
    """Calculate and export performance metrics."""
    perf_dir = output_dir / "PERFORMANCE"
    perf_dir.mkdir(parents=True, exist_ok=True)

    # MFE/MAE table
    mfe_mae_rows = []
    for t in trades:
        if t.get("mfe") is not None:
            mfe_mae_rows.append({
                "symbol": t.get("symbol", "?"),
                "side": t.get("side", "?"),
                "entry": t.get("entry_price", t.get("entry", "")),
                "exit": t.get("exit_price", t.get("exit", "")),
                "mfe": t.get("mfe"),
                "mae": t.get("mae"),
                "peak_r": t.get("peak_r"),
                "final_r": t.get("final_r"),
                "profit_given_back": t.get("profit_given_back"),
                "gross_pnl": t.get("gross_pnl"),
                "net_pnl": t.get("net_pnl"),
                "charges": t.get("charges"),
            })

    if mfe_mae_rows:
        with open(perf_dir / "mfe_mae.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(mfe_mae_rows[0].keys()))
            writer.writeheader()
            writer.writerows(mfe_mae_rows)

    # Daily summary
    winners = [t for t in trades if float(t.get("net_pnl", 0) or 0) > 0]
    losers = [t for t in trades if float(t.get("net_pnl", 0) or 0) < 0]
    total_gross = sum(float(t.get("gross_pnl", 0) or 0) for t in trades)
    total_charges = sum(float(t.get("charges", 0) or 0) for t in trades)
    total_net = sum(float(t.get("net_pnl", 0) or 0) for t in trades)
    avg_mfe = sum(float(t.get("peak_r", 0) or 0) for t in trades) / max(len(trades), 1)
    avg_mae = sum(float(t.get("mae", 0) or 0) for t in trades) / max(len(trades), 1)

    summary = {
        "date": str(trade_date),
        "NSE": {
            "candidates_evaluated": "see candidates.csv",
            "trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "gross_pnl": round(total_gross, 2),
            "net_pnl": round(total_net, 2),
            "total_charges": round(total_charges, 2),
            "average_peak_R": round(avg_mfe, 3),
            "average_MAE": round(avg_mae, 2),
            "win_rate": round(len(winners) / max(len(trades), 1) * 100, 1),
        },
        "MCX": {
            "mode": "SHADOW_ONLY",
            "note": "MCX shadow — no live trades",
        },
        "SWING": {
            "mode": "PAPER/SHADOW",
            "note": "Check swing positions file",
        },
        "ACCOUNT": {
            "broker_truth_available": bool(broker_data.get("orders")),
        "performance_status": "VERIFIED" if broker_data.get("orders") else "UNVERIFIED — Dhan broker truth unavailable",
        "broker_orders": len(broker_data.get("orders", [])) if isinstance(broker_data.get("orders"), list) else 0,
            "broker_trades": len(broker_data.get("trades", [])) if isinstance(broker_data.get("trades"), list) else 0,
        }
    }

    with open(perf_dir / "daily_summary.json", "w") as f:
        safe_json_dump(summary, f)

    # Also write to root for easy access
    with open(output_dir / "daily_summary.json", "w") as f:
        safe_json_dump(summary, f)

    log.info(f"Performance: {len(trades)} trades, net P&L = {total_net:.2f}")
    return summary


# ============================================================================
# SECTION J: MANIFEST
# ============================================================================

def generate_manifest(output_dir: Path, trade_date: date, summary: Dict):
    """Generate MANIFEST.json."""
    # Count files
    file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())

    # Get git commit if available
    git_commit = "N/A"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(PROD_DIR), timeout=5
        )
        if result.returncode == 0:
            git_commit = result.stdout.strip()
    except Exception:
        pass

    # Get deployment timestamp (newest .py file modification time)
    newest_py = max(
        (f.stat().st_mtime for f in PROD_DIR.glob("*.py")),
        default=0
    )
    deploy_time = datetime.fromtimestamp(newest_py).isoformat() if newest_py else "unknown"

    manifest = {
        "date": str(trade_date),
        "nse_engine": ENGINE_VERSIONS["nse"],
        "mcx_engine": ENGINE_VERSIONS["mcx"],
        "swing_engine": ENGINE_VERSIONS["swing"],
        "git_commit": git_commit,
        "deployment_time": deploy_time,
        "market_data_source": "Dhan",
        "broker": "Dhan",
        "timezone": "Asia/Kolkata",
        "files": file_count,
        "data_start": "09:15",
        "data_end": "15:30",
        "mcx_data_start": "18:30",
        "mcx_data_end": "23:00",
        "export_time": datetime.now().isoformat(),
        "patches_applied": [
            "Patch A: V8.5.3 bypass for V8.5.5 EARLY_ACCELERATION",
            "Patch B: Real-time exit snapshot (fresh RS/RVOL/volume/structure)",
            "Patch C: Broker-side progressive SL trailing (modify_pending_sl)",
            "Patch D: Enhanced audit logging in exit eval",
            "MCX: Strict FUTCOM contract resolver + CE/PE rejection",
        ],
    }

    with open(output_dir / "MANIFEST.json", "w") as f:
        safe_json_dump(manifest, f)

    log.info(f"MANIFEST: {file_count} files, engines NSE={ENGINE_VERSIONS['nse']} MCX={ENGINE_VERSIONS['mcx']}")
    return manifest


# ============================================================================
# SECTION K: COMPRESS + EMAIL
# ============================================================================

def compress_package(output_dir: Path, trade_date: date) -> Path:
    """Create .tgz archive."""
    date_str = trade_date.strftime("%Y-%m-%d")
    tgz_path = Path(f"/tmp/trading_review_{date_str}.tgz")

    with tarfile.open(tgz_path, "w:gz") as tar:
        tar.add(str(output_dir), arcname=f"trading_review_{date_str}")

    size_mb = tgz_path.stat().st_size / (1024 * 1024)
    log.info(f"Package: {tgz_path} ({size_mb:.1f} MB)")
    return tgz_path


def email_package(tgz_path: Path, trade_date: date, summary: Dict):
    """Email the package via AWS SES."""
    load_ses_config()

    import boto3
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    date_str = trade_date.strftime("%Y-%m-%d")
    size_mb = tgz_path.stat().st_size / (1024 * 1024)

    # Build email body
    nse = summary.get("NSE", {})
    body = f"""Trading Research Package — {date_str}

NSE: {nse.get('trades', 0)} trades | Net P&L: ₹{nse.get('net_pnl', 0):,.2f} | Win rate: {nse.get('win_rate', 0)}%
MCX: Shadow mode
Swing: Paper mode

Engines: NSE {ENGINE_VERSIONS['nse']} | MCX {ENGINE_VERSIONS['mcx']} | Swing {ENGINE_VERSIONS['swing']}
Package size: {size_mb:.1f} MB

Upload to expert for review.
"""

    # SES with attachment (if IAM allows SendRawEmail)
    # Fallback: SES SendEmail (text only) + save tgz locally
    try:
        ses = boto3.client("ses", region_name=REGION)

        # Try raw email with attachment
        msg = MIMEMultipart()
        msg["Subject"] = f"Trading Review {date_str} | {nse.get('trades', 0)} trades | ₹{nse.get('net_pnl', 0):,.0f}"
        msg["From"] = SES_SENDER
        msg["To"] = SES_RECIPIENT
        msg.attach(MIMEText(body, "plain"))

        # Attach if < 10MB (SES limit)
        if size_mb < 9.5:
            with open(tgz_path, "rb") as f:
                att = MIMEApplication(f.read(), Name=tgz_path.name)
                att["Content-Disposition"] = f'attachment; filename="{tgz_path.name}"'
                msg.attach(att)

        try:
            ses.send_raw_email(
                Source=SES_SENDER,
                Destinations=[SES_RECIPIENT],
                RawMessage={"Data": msg.as_string()}
            )
            log.info(f"Email sent (with attachment) to {SES_RECIPIENT}")
            return True
        except Exception as e:
            if "SendRawEmail" in str(e) or "AccessDenied" in str(e):
                # Fallback: text-only email
                log.warning(f"SendRawEmail denied, falling back to text: {e}")
                ses.send_email(
                    Source=SES_SENDER,
                    Destination={"ToAddresses": [SES_RECIPIENT]},
                    Message={
                        "Subject": {"Data": f"Trading Review {date_str} | {nse.get('trades', 0)} trades"},
                        "Body": {"Text": {"Data": body + f"\n\nPackage saved at: {tgz_path}\n(Use SCP to download)"}}
                    }
                )
                log.info(f"Text email sent to {SES_RECIPIENT} (no attachment — use SCP)")
                return True
            else:
                raise
    except Exception as e:
        log.error(f"Email failed: {e}")
        log.info(f"Package saved locally: {tgz_path}")
        return False


# ============================================================================
# SECTION: MISSED OPPORTUNITIES
# ============================================================================

def analyze_missed_opportunities(output_dir: Path, candidates: List[Dict], trades: List[Dict], trade_date: date):
    """Identify candidates that weren't traded but moved significantly."""
    perf_dir = output_dir / "PERFORMANCE"
    perf_dir.mkdir(parents=True, exist_ok=True)

    traded_symbols = set(t.get("symbol", "") for t in trades)
    missed = []

    for c in candidates:
        symbol = c.get("symbol", c.get("SEM_TRADING_SYMBOL", ""))
        if symbol and symbol not in traded_symbols:
            score = float(c.get("final_score", c.get("score", 0)) or 0)
            if score >= 50:  # Only high-scoring misses
                missed.append({
                    "symbol": symbol,
                    "score": score,
                    "side": c.get("side", ""),
                    "setup_type": c.get("setup_type", ""),
                    "rejection_reason": c.get("reason", c.get("rejection_reason", "")),
                    "price_at_detection": c.get("price", c.get("ltp", "")),
                    "timestamp": c.get("timestamp", c.get("time", "")),
                })

    if missed:
        missed.sort(key=lambda x: x.get("score", 0), reverse=True)
        with open(perf_dir / "missed_opportunities.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(missed[0].keys()))
            writer.writeheader()
            writer.writerows(missed[:50])  # Top 50

    log.info(f"Missed opportunities: {len(missed)} high-scoring non-trades")


# ============================================================================
# SECTION: RAW LOG EXTRACTION
# ============================================================================

def export_raw_logs(output_dir: Path, trade_date: date):
    """Export today's raw log lines (filtered)."""
    logs_dir = output_dir / "LOGS"
    logs_dir.mkdir(parents=True, exist_ok=True)

    date_str = trade_date.strftime("%Y-%m-%d")
    date_str2 = trade_date.strftime("%Y%m%d")

    # bot.log — filter to today only
    bot_log = PROD_DIR / "bot.log"
    if bot_log.exists():
        try:
            with open(bot_log, errors='replace') as f:
                today_lines = []
                for line in f:
                    # UTC timestamps in log, check both date formats
                    if date_str in line[:30] or date_str2 in line[:30]:
                        today_lines.append(line)
                    # Also capture lines from today by hour pattern
                    elif line[:4] == "2026" and line[:10] == date_str:
                        today_lines.append(line)

            if today_lines:
                (logs_dir / f"bot_{date_str}.log").write_text(''.join(today_lines))
                log.info(f"Bot log: {len(today_lines)} lines for {date_str}")
        except Exception as e:
            log.warning(f"Error extracting bot.log: {e}")

    # MCX log
    mcx_log = PROD_DIR / "logs" / "mcx_v854.log"
    if not mcx_log.exists():
        mcx_log = PROD_DIR / "mcx_v854.log"
    if mcx_log.exists():
        try:
            content = mcx_log.read_text(errors='replace')
            today_lines = [l for l in content.split('\n') if date_str in l[:30]]
            if today_lines:
                (logs_dir / f"mcx_{date_str}.log").write_text('\n'.join(today_lines))
        except Exception:
            pass


# ============================================================================
# MAIN
# ============================================================================

def main():
    # Parse args
    trade_date_override = None
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        if idx + 1 < len(sys.argv):
            trade_date_override = sys.argv[idx + 1]

    trade_date = get_trade_date(trade_date_override)
    date_str = trade_date.strftime("%Y-%m-%d")

    log.info(f"="*60)
    log.info(f"DAILY RESEARCH EXPORT — {date_str}")
    log.info(f"="*60)

    # Create output directory
    output_dir = OUTPUT_BASE / date_str
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # Execute all sections
    try:
        # A. Code snapshot
        export_code_snapshot(output_dir, trade_date)

        # B+C. Candidates + missed trades
        candidates, event_entries, event_exits = export_candidates(output_dir, trade_date)

        # D. Actual trades with metrics
        trades = export_trades(output_dir, trade_date, event_entries, event_exits)

        # E. Dhan broker truth
        broker_data = export_dhan_truth(output_dir, trade_date)

        # F. Market data
        export_market_data(output_dir, trade_date, trades)

        # G. MCX + Swing
        export_mcx(output_dir, trade_date)
        export_swing(output_dir, trade_date)

        # H+I. Performance + Summary
        summary = calculate_performance(output_dir, trades, broker_data, trade_date)

        # Missed opportunities
        analyze_missed_opportunities(output_dir, candidates, trades, trade_date)

        # Raw logs
        export_raw_logs(output_dir, trade_date)

        # J. Manifest
        generate_manifest(output_dir, trade_date, summary)

        # K. Compress
        tgz_path = compress_package(output_dir, trade_date)

        # L. Email
        email_package(tgz_path, trade_date, summary)

        log.info(f"="*60)
        log.info(f"COMPLETE: {tgz_path}")
        log.info(f"="*60)

    except Exception as e:
        log.error(f"Export failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
