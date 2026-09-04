"""
V8.4 Trade Logger — Full scoring + candle data capture for backtesting
Stores: entries, exits, and periodic scan snapshots with all 7 scoring factors.
Auto-cleanup: Retains 2 months (60 days) of data.

Storage: ~55 MB for 2 months — negligible.

INTEGRATION: Add to trading_bot_v84.py / trading_bot_v82.py
"""
import json, csv, os, time, glob
from pathlib import Path
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = Path("trade_logs")
LOG_DIR.mkdir(exist_ok=True)
RETENTION_DAYS = 60  # 2 months

def now_ist():
    return datetime.now(IST)

def _date_str():
    return now_ist().strftime("%Y-%m-%d")

def _ts():
    return now_ist().isoformat(timespec="seconds")

# ═══════════════════════════════════════════════════════════════
# 1. ENTRY LOG — written when V8.4 places a trade
# ═══════════════════════════════════════════════════════════════

def log_entry(candidate, decision, fill_info, score_breakdown, market_state):
    """
    Log full entry details when a trade is placed.
    
    Args:
        candidate: dict with security_id, symbol, ltp, gap_pct, rs, rvol, sector, etc.
        decision: dict with side, entry_price, stop, target, setup_type, score
        fill_info: dict with qty, price (fill price), order_id
        score_breakdown: dict with all 7 factors:
            {market, sector, rs, momentum, rvol, vwap_trend, setup_quality,
             entry_quality, total_score}
        market_state: dict with nifty, banknifty, regime, vix, leaders, laggards
    """
    entry = {
        "timestamp": _ts(),
        "date": _date_str(),
        "event": "ENTRY",
        # Stock info
        "symbol": candidate.get("symbol", "?"),
        "security_id": str(candidate.get("security_id", "")),
        "ltp_at_signal": float(candidate.get("ltp", 0)),
        "gap_pct": float(candidate.get("gap_pct", 0)),
        "rs_score": float(candidate.get("rs", 0)),
        "rvol": float(candidate.get("rvol", 0)),
        "sector": candidate.get("sector", ""),
        "atr_pct": float(candidate.get("atr_pct", 0)),
        # Decision
        "side": decision.get("side", ""),
        "setup_type": decision.get("setup_type", ""),
        "entry_price_signal": float(decision.get("entry_price", 0)),
        "stop_price": float(decision.get("stop", 0)),
        "target_price": float(decision.get("target", 0)),
        # Fill
        "fill_price": float(fill_info.get("price", 0)),
        "fill_qty": int(fill_info.get("qty", 0)),
        "order_id": fill_info.get("order_id", ""),
        # Full score breakdown (7 factors)
        "score_total": float(score_breakdown.get("total_score", 0)),
        "score_market": float(score_breakdown.get("market", 0)),
        "score_sector": float(score_breakdown.get("sector", 0)),
        "score_rs": float(score_breakdown.get("rs", 0)),
        "score_momentum": float(score_breakdown.get("momentum", 0)),
        "score_rvol": float(score_breakdown.get("rvol", 0)),
        "score_vwap_trend": float(score_breakdown.get("vwap_trend", 0)),
        "score_setup_quality": float(score_breakdown.get("setup_quality", 0)),
        "score_entry_quality": float(score_breakdown.get("entry_quality", 0)),
        # Market context
        "nifty": float(market_state.get("nifty", 0)),
        "banknifty": float(market_state.get("banknifty", 0)),
        "regime": market_state.get("regime", ""),
        "vix": float(market_state.get("vix", 0)),
        "sector_leaders": market_state.get("leaders", []),
        "sector_laggards": market_state.get("laggards", []),
        # Candle data at entry (5-min)
        "candle_open": float(candidate.get("candle_open", 0)),
        "candle_high": float(candidate.get("candle_high", 0)),
        "candle_low": float(candidate.get("candle_low", 0)),
        "candle_close": float(candidate.get("candle_close", 0)),
        "candle_volume": int(candidate.get("candle_volume", 0)),
        "vwap": float(candidate.get("vwap", 0)),
    }
    
    _append_jsonl("entries", entry)
    return entry


# ═══════════════════════════════════════════════════════════════
# 2. EXIT LOG — written when V8.4 closes a position
# ═══════════════════════════════════════════════════════════════

def log_exit(symbol, security_id, side, entry_price, exit_price, qty, 
             reason, peak_price=0, duration_minutes=0, entry_time=""):
    """
    Log full exit details when a position is closed.
    
    Args:
        reason: "HARD_SL" | "PROFIT_TRAIL" | "PEAK_REVERSAL" | "MANDATORY_EOD" | 
                "EMERGENCY" | "VWAP_EXIT" | "MOMENTUM_FADE"
    """
    pnl_pct = 0.0
    if entry_price > 0:
        if side == "LONG":
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100
    
    pnl_rs = pnl_pct / 100 * entry_price * qty  # Approx P&L in Rs
    
    peak_profit_pct = 0.0
    if entry_price > 0 and peak_price > 0:
        if side == "LONG":
            peak_profit_pct = (peak_price - entry_price) / entry_price * 100
        else:
            peak_profit_pct = (entry_price - peak_price) / entry_price * 100

    exit_record = {
        "timestamp": _ts(),
        "date": _date_str(),
        "event": "EXIT",
        "symbol": symbol,
        "security_id": str(security_id),
        "side": side,
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "qty": int(qty),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_rs": round(pnl_rs, 2),
        "peak_price": float(peak_price),
        "peak_profit_pct": round(peak_profit_pct, 4),
        "drawdown_from_peak_pct": round(peak_profit_pct - pnl_pct, 4),
        "exit_reason": reason,
        "duration_minutes": int(duration_minutes),
        "entry_time": entry_time,
    }
    
    _append_jsonl("exits", exit_record)
    return exit_record


# ═══════════════════════════════════════════════════════════════
# 3. SCAN LOG — written every scoring cycle (top candidates)
# ═══════════════════════════════════════════════════════════════

def log_scan_cycle(ranked_candidates, market_state, top_n=10):
    """
    Log top N scored candidates every scan cycle.
    Called from the main scoring loop.
    
    Args:
        ranked_candidates: list of (score, candidate_dict) sorted desc
        market_state: dict with nifty, regime, etc.
        top_n: how many top candidates to log (default 10)
    """
    scan_time = _ts()
    rows = []
    
    for score, c in ranked_candidates[:top_n]:
        rows.append({
            "scan_time": scan_time,
            "symbol": c.get("symbol", "?"),
            "security_id": str(c.get("security_id", "")),
            "side": c.get("side", ""),
            "score": float(score),
            "ltp": float(c.get("ltp", 0)),
            "gap_pct": float(c.get("gap_pct", 0)),
            "rs": float(c.get("rs", 0)),
            "rvol": float(c.get("rvol", 0)),
            "vwap": float(c.get("vwap", 0)),
            "momentum_5m": float(c.get("momentum_5m", 0)),
            "momentum_15m": float(c.get("momentum_15m", 0)),
            "atr_pct": float(c.get("atr_pct", 0)),
            "setup_type": c.get("setup_type", ""),
            "regime": market_state.get("regime", ""),
            "nifty": float(market_state.get("nifty", 0)),
        })
    
    # Write to daily CSV
    date_str = _date_str()
    csv_path = LOG_DIR / f"scans_{date_str}.csv"
    write_header = not csv_path.exists()
    
    if rows:
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            if write_header:
                writer.writeheader()
            writer.writerows(rows)


# ═══════════════════════════════════════════════════════════════
# 4. DAILY SUMMARY — end-of-day aggregation
# ═══════════════════════════════════════════════════════════════

def log_daily_summary(trades_today, total_pnl, balance, regime):
    """Write end-of-day summary."""
    summary = {
        "date": _date_str(),
        "timestamp": _ts(),
        "total_trades": len(trades_today),
        "winners": sum(1 for t in trades_today if t.get("pnl_pct", 0) > 0),
        "losers": sum(1 for t in trades_today if t.get("pnl_pct", 0) < 0),
        "total_pnl_pct": round(sum(t.get("pnl_pct", 0) for t in trades_today), 4),
        "total_pnl_rs": round(sum(t.get("pnl_rs", 0) for t in trades_today), 2),
        "max_win_pct": round(max((t.get("pnl_pct", 0) for t in trades_today), default=0), 4),
        "max_loss_pct": round(min((t.get("pnl_pct", 0) for t in trades_today), default=0), 4),
        "avg_duration_min": round(sum(t.get("duration_minutes", 0) for t in trades_today) / max(len(trades_today), 1), 1),
        "regime": regime,
        "closing_balance": float(balance),
    }
    _append_jsonl("daily_summary", summary)
    return summary


# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════

def _append_jsonl(category, record):
    """Append a JSON record to the daily JSONL file."""
    date_str = _date_str()
    path = LOG_DIR / f"{category}_{date_str}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def cleanup_old_logs():
    """Remove trade logs older than RETENTION_DAYS (60 days = 2 months)."""
    cutoff = now_ist() - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed = 0
    
    for f in LOG_DIR.glob("*_20*.*"):
        # Extract date from filename (e.g., entries_2026-08-17.jsonl)
        parts = f.stem.split("_")
        date_part = None
        for p in parts:
            if p.startswith("20") and len(p) == 10:
                date_part = p
                break
        if date_part and date_part < cutoff_str:
            f.unlink()
            removed += 1
    
    if removed:
        print(f"[TRADE_LOGGER] Cleaned up {removed} files older than {RETENTION_DAYS} days")
    return removed


def get_todays_entries():
    """Read today's entry logs."""
    path = LOG_DIR / f"entries_{_date_str()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def get_todays_exits():
    """Read today's exit logs."""
    path = LOG_DIR / f"exits_{_date_str()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ═══════════════════════════════════════════════════════════════
# INTEGRATION HOOKS (call these from trading_bot_v84.py)
# ═══════════════════════════════════════════════════════════════
"""
INTEGRATION GUIDE — Add these calls to trading_bot_v84.py:

1. AFTER successful entry (after verify_fill + hard_sl):
   
   from v84_trade_logger import log_entry
   log_entry(
       candidate=c,
       decision=d,
       fill_info={"price": fp, "qty": fq, "order_id": oid},
       score_breakdown={"total_score": score, "market": ..., "sector": ..., ...},
       market_state={"nifty": nifty_ltp, "regime": regime, ...}
   )

2. AFTER exit (in close_position):
   
   from v84_trade_logger import log_exit
   log_exit(
       symbol=p["symbol"], security_id=sid, side=p["side"],
       entry_price=p["entry"], exit_price=exit_px, qty=p["qty"],
       reason=reason, peak_price=p["peak"],
       duration_minutes=(now-entry_time).seconds//60,
       entry_time=p["entry_time"]
   )

3. AFTER each scoring cycle (in the scan loop):
   
   from v84_trade_logger import log_scan_cycle
   log_scan_cycle(ranked, {"nifty": nifty, "regime": regime}, top_n=10)

4. AT BOT STARTUP (once):
   
   from v84_trade_logger import cleanup_old_logs
   cleanup_old_logs()  # Prune files older than 60 days

OUTPUT FILES (in trade_logs/ folder):
  trade_logs/entries_2026-08-18.jsonl    — full entry details
  trade_logs/exits_2026-08-18.jsonl      — full exit details  
  trade_logs/scans_2026-08-18.csv        — top 10 scores every cycle
  trade_logs/daily_summary_2026-08-18.jsonl — EOD P&L summary
"""
