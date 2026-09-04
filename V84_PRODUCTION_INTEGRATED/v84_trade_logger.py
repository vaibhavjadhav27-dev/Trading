"""
V8.4 Trade Logger — Full scoring + candle data capture for backtesting
Auto-cleanup: Retains 2 months (60 days) of data.
"""
import json, csv, os
from pathlib import Path
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = Path(__file__).parent / "trade_logs"
LOG_DIR.mkdir(exist_ok=True)
RETENTION_DAYS = 60

def now_ist():
    return datetime.now(IST)

def _date_str():
    return now_ist().strftime("%Y-%m-%d")

def _ts():
    return now_ist().isoformat(timespec="seconds")

def _append_jsonl(category, record):
    path = LOG_DIR / f"{category}_{_date_str()}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

def log_entry(candidate, decision, fill_info, score_breakdown, market_state):
    entry = {
        "timestamp": _ts(), "date": _date_str(), "event": "ENTRY",
        "symbol": candidate.get("symbol", "?"),
        "security_id": str(candidate.get("security_id", "")),
        "ltp_at_signal": float(candidate.get("ltp", 0)),
        "gap_pct": float(candidate.get("gap_pct", 0)),
        "rs_score": float(candidate.get("rs", 0)),
        "rvol": float(candidate.get("rvol", 0)),
        "sector": candidate.get("sector", ""),
        "atr_pct": float(candidate.get("atr_pct", 0)),
        "side": decision.get("side", ""),
        "setup_type": decision.get("setup_type", ""),
        "entry_price_signal": float(decision.get("entry_price", 0)),
        "stop_price": float(decision.get("stop", 0)),
        "target_price": float(decision.get("target", 0)),
        "fill_price": float(fill_info.get("price", 0)),
        "fill_qty": int(fill_info.get("qty", 0)),
        "order_id": fill_info.get("order_id", ""),
        "score_total": float(score_breakdown.get("total_score", 0)),
        "score_market": float(score_breakdown.get("market", 0)),
        "score_sector": float(score_breakdown.get("sector", 0)),
        "score_rs": float(score_breakdown.get("rs", 0)),
        "score_momentum": float(score_breakdown.get("momentum", 0)),
        "score_rvol": float(score_breakdown.get("rvol", 0)),
        "score_vwap_trend": float(score_breakdown.get("vwap_trend", 0)),
        "score_setup_quality": float(score_breakdown.get("setup_quality", 0)),
        "score_entry_quality": float(score_breakdown.get("entry_quality", 0)),
        "nifty": float(market_state.get("nifty", 0)),
        "banknifty": float(market_state.get("banknifty", 0)),
        "regime": market_state.get("regime", ""),
        "vix": float(market_state.get("vix", 0)),
        "sector_leaders": market_state.get("leaders", []),
        "sector_laggards": market_state.get("laggards", []),
        "candle_open": float(candidate.get("candle_open", 0)),
        "candle_high": float(candidate.get("candle_high", 0)),
        "candle_low": float(candidate.get("candle_low", 0)),
        "candle_close": float(candidate.get("candle_close", 0)),
        "candle_volume": int(candidate.get("candle_volume", 0)),
        "vwap": float(candidate.get("vwap", 0)),
    }
    entry["mode"] = decision.get("setup_type", decision.get("mode", ""))
    entry["expected_move_pct"] = float(decision.get("expected_move_pct", 0))
    entry["expected_r"] = float(decision.get("expected_r", decision.get("edge", 0)))
    entry["risk_pct"] = float(decision.get("risk_pct", 0))
    _append_jsonl("entries", entry)
    return entry

def log_exit(symbol, security_id, side, entry_price, exit_price, qty,
             reason, peak_price=0, duration_minutes=0, entry_time="", **kwargs):
    pnl_pct = 0.0
    if entry_price > 0:
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if side == "LONG" else ((entry_price - exit_price) / entry_price * 100)
    pnl_rs = pnl_pct / 100 * entry_price * qty
    peak_profit_pct = 0.0
    if entry_price > 0 and peak_price > 0:
        peak_profit_pct = ((peak_price - entry_price) / entry_price * 100) if side == "LONG" else ((entry_price - peak_price) / entry_price * 100)
    record = {
        "timestamp": _ts(), "date": _date_str(), "event": "EXIT",
        "symbol": symbol, "security_id": str(security_id), "side": side,
        "entry_price": float(entry_price), "exit_price": float(exit_price),
        "qty": int(qty), "pnl_pct": round(pnl_pct, 4), "pnl_rs": round(pnl_rs, 2),
        "peak_price": float(peak_price), "peak_profit_pct": round(peak_profit_pct, 4),
        "drawdown_from_peak_pct": round(peak_profit_pct - pnl_pct, 4),
        "exit_reason": reason, "duration_minutes": int(duration_minutes),
        "entry_time": entry_time,
        "mfe_pct": round(peak_profit_pct, 4),
        "mae_pct": round(kwargs.get("mae_pct", 0.0), 4),
        "expected_r": round(kwargs.get("expected_r", 0.0), 2),
        "mode": kwargs.get("mode", ""),
    }
    _append_jsonl("exits", record)
    return record

def log_scan_cycle(ranked_candidates, market_state, top_n=10):
    scan_time = _ts()
    rows = []
    for score, c in ranked_candidates[:top_n]:
        rows.append({
            "scan_time": scan_time, "symbol": c.get("symbol", "?"),
            "security_id": str(c.get("security_id", "")),
            "side": c.get("side", ""), "score": float(score),
            "ltp": float(c.get("ltp", 0)), "gap_pct": float(c.get("gap_pct", 0)),
            "rs": float(c.get("rs", 0)), "rvol": float(c.get("rvol", 0)),
            "vwap": float(c.get("vwap", 0)),
            "momentum_5m": float(c.get("momentum_5m", 0)),
            "momentum_15m": float(c.get("momentum_15m", 0)),
            "atr_pct": float(c.get("atr_pct", 0)),
            "setup_type": c.get("setup_type", ""),
            "regime": market_state.get("regime", ""),
            "nifty": float(market_state.get("nifty", 0)),
        })
    if rows:
        csv_path = LOG_DIR / f"scans_{_date_str()}.csv"
        write_header = not csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

def log_daily_summary(trades_today, total_pnl, balance, regime):
    summary = {
        "date": _date_str(), "timestamp": _ts(),
        "total_trades": len(trades_today),
        "winners": sum(1 for t in trades_today if t.get("pnl_pct", 0) > 0),
        "losers": sum(1 for t in trades_today if t.get("pnl_pct", 0) < 0),
        "total_pnl_pct": round(sum(t.get("pnl_pct", 0) for t in trades_today), 4),
        "total_pnl_rs": round(sum(t.get("pnl_rs", 0) for t in trades_today), 2),
        "regime": regime, "closing_balance": float(balance),
    }
    _append_jsonl("daily_summary", summary)
    return summary

def cleanup_old_logs():
    cutoff = (now_ist() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for f in LOG_DIR.glob("*_20*.*"):
        for p in f.stem.split("_"):
            if p.startswith("20") and len(p) == 10 and p < cutoff:
                f.unlink(); removed += 1; break
    if removed: print(f"[TRADE_LOGGER] Cleaned {removed} files older than {RETENTION_DAYS}d")

if __name__ == "__main__":
    cleanup_old_logs()
    print(f"[TRADE_LOGGER] Ready. Log dir: {LOG_DIR}")
    print(f"[TRADE_LOGGER] Retention: {RETENTION_DAYS} days")
