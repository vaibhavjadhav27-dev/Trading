"""
V8.5.5 EARLY-ENTRY + PROFIT-PROTECTION PATCH
For integration into the existing V8.5.1/V8.5.3/V8.5.4 NSE engine.

Purpose
-------
1. Catch fast developing breakouts/breakdowns earlier without simply lowering
   the normal entry-score threshold.
2. Keep the existing V8.5.3 quality/timing/room/risk framework.
3. Improve exits when a profitable move starts fading, without exiting on one
   weak candle.
4. Work with the V8.5.4 structural-stop/reconciliation layer.

IMPORTANT
---------
This is a strategy/integration module. It is NOT a claim of live-broker
validation. Engineers must run the supplied unit tests plus server/Dhan
integration tests before enabling live execution.

Expected snapshot fields (aliases are accepted):
price, vwap, atr, support, resistance,
rvol, rvol_prev, rs, rs_prev,
mom_5m, mom_5m_prev, mom_15m, mom_15m_prev,
close, open, high, low, volume, avg_volume,
prior_close, prior_high, prior_low,
sector_rs, nifty_rs,
bars_since_break, distance_from_break_pct

For production, use the exact candle/snapshot that caused the decision.
Never silently replace missing values with zeros in the audit record.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
import math


def f(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def pct_change(a, b):
    a, b = f(a), f(b)
    if a == 0:
        return 0.0
    return (b - a) / abs(a) * 100.0


@dataclass(frozen=True)
class EarlyConfig:
    # Entry
    min_quality_score: float = 60.0
    early_score_threshold: float = 75.0
    min_room_pct: float = 0.40
    min_rvol: float = 1.50
    rvol_accel_min: float = 0.20
    momentum_accel_min: float = 0.15
    rs_accel_min: float = 0.25

    # Avoid chasing already exhausted moves.
    max_distance_from_break_pct: float = 1.25
    max_bars_since_break: int = 4

    # Exit
    meaningful_profit_r: float = 0.80
    profit_lock_r: float = 0.50
    peak_retrace_r: float = 0.35
    reversal_confirmations: int = 2


@dataclass
class ExitState:
    peak_price: float
    best_r: float = 0.0
    locked_r: float = 0.0


def acceleration_score(side: str, s: Dict[str, Any],
                       quality_score: float,
                       cfg: EarlyConfig = EarlyConfig()):
    """
    Early entry score: detects a transition into a directional move.

    This is deliberately separate from the normal mature score. It should
    catch the beginning of acceleration rather than wait for all indicators
    to become extreme.
    """
    side = side.upper()
    price = f(s.get("price"))
    vwap = f(s.get("vwap"))
    rvol = f(s.get("rvol"))
    rvol_prev = f(s.get("rvol_prev"))
    rs = f(s.get("rs"))
    rs_prev = f(s.get("rs_prev"))
    m5 = f(s.get("mom_5m"))
    m5_prev = f(s.get("mom_5m_prev"))
    m15 = f(s.get("mom_15m"))
    support = f(s.get("support"))
    resistance = f(s.get("resistance"))
    room = f(s.get("remaining_room_pct"))
    distance = abs(f(s.get("distance_from_break_pct")))
    bars = int(f(s.get("bars_since_break")))

    score = 0.0
    reasons = []

    if side == "SHORT":
        structure_break = resistance <= 0 and False
        # Prefer support breakdown; resistance is irrelevant for the short
        # trigger unless the strategy supplies another explicit breakdown flag.
        support_break = support > 0 and price < support
        explicit_break = bool(s.get("breakdown", False))
        if support_break or explicit_break:
            score += 20
            reasons.append("SUPPORT_BREAK")

        if m5 < 0:
            score += 15
            reasons.append("MOM5_NEG")
        if m15 < 0:
            score += 10
            reasons.append("MOM15_NEG")

        if rvol >= cfg.min_rvol:
            score += 10
            reasons.append("RVOL_ACTIVE")
        if rvol - rvol_prev >= cfg.rvol_accel_min:
            score += 5
            reasons.append("RVOL_ACCEL")

        if rs < 0:
            score += 10
            reasons.append("RS_WEAK")
        if (rs_prev - rs) >= cfg.rs_accel_min:
            score += 10
            reasons.append("RS_ACCEL")

        if price < vwap:
            score += 10
            reasons.append("BELOW_VWAP")

        # Strong negative candle / participation expansion.
        if bool(s.get("volume_expansion", False)):
            score += 10
            reasons.append("VOLUME_EXPANSION")

        # Momentum acceleration gets a separate bonus.
        if (m5_prev - m5) >= cfg.momentum_accel_min:
            score += 10
            reasons.append("MOM5_ACCEL")

    else:  # LONG
        support_break = False
        breakout = (resistance > 0 and price > resistance)
        explicit_break = bool(s.get("breakout", False))
        if breakout or explicit_break:
            score += 20
            reasons.append("RESISTANCE_BREAK")

        if m5 > 0:
            score += 15
            reasons.append("MOM5_POS")
        if m15 > 0:
            score += 10
            reasons.append("MOM15_POS")

        if rvol >= cfg.min_rvol:
            score += 10
            reasons.append("RVOL_ACTIVE")
        if rvol - rvol_prev >= cfg.rvol_accel_min:
            score += 5
            reasons.append("RVOL_ACCEL")

        if rs > 0:
            score += 10
            reasons.append("RS_STRONG")
        if (rs - rs_prev) >= cfg.rs_accel_min:
            score += 10
            reasons.append("RS_ACCEL")

        if price > vwap:
            score += 10
            reasons.append("ABOVE_VWAP")

        if bool(s.get("volume_expansion", False)):
            score += 10
            reasons.append("VOLUME_EXPANSION")

        if (m5 - m5_prev) >= cfg.momentum_accel_min:
            score += 10
            reasons.append("MOM5_ACCEL")

    # Quality and room are mandatory safeguards.
    if quality_score < cfg.min_quality_score:
        return {
            "enter": False, "score": round(score, 2),
            "reason": "QUALITY_TOO_LOW", "reasons": reasons
        }

    if room > 0 and room < cfg.min_room_pct:
        return {
            "enter": False, "score": round(score, 2),
            "reason": "INSUFFICIENT_ROOM", "reasons": reasons
        }

    if bars > cfg.max_bars_since_break:
        return {
            "enter": False, "score": round(score, 2),
            "reason": "MOVE_TOO_MATURE", "reasons": reasons
        }

    if distance > cfg.max_distance_from_break_pct:
        return {
            "enter": False, "score": round(score, 2),
            "reason": "CHASE_PROTECTION", "reasons": reasons
        }

    return {
        "enter": score >= cfg.early_score_threshold,
        "score": round(score, 2),
        "reason": "EARLY_ACCELERATION" if score >= cfg.early_score_threshold
                  else "WAIT_CONFIRMATION",
        "reasons": reasons
    }


def update_peak(state: ExitState, side: str, price: float):
    price = f(price)
    if side.upper() == "LONG":
        state.peak_price = max(state.peak_price, price)
    else:
        state.peak_price = min(state.peak_price, price)


def profit_fading_exit(side: str,
                       entry: float,
                       price: float,
                       initial_risk: float,
                       state: ExitState,
                       snapshot: Dict[str, Any],
                       cfg: EarlyConfig = EarlyConfig()):
    """
    Profit-protection engine.

    It does NOT exit because profit merely falls from its maximum.
    It requires either:
      A) structural invalidation, OR
      B) meaningful profit + peak retracement + 2-factor reversal.

    This is designed to avoid the Friday-style premature profit exit while
    still protecting a mature winning move.
    """
    side = side.upper()
    entry = f(entry)
    price = f(price)
    risk = abs(f(initial_risk))

    if entry <= 0 or price <= 0 or risk <= 0:
        return {"exit": False, "reason": "INVALID_EXIT_INPUT"}

    if side == "LONG":
        r = (price - entry) / risk
        peak_r = (state.peak_price - entry) / risk
        retrace_r = (state.peak_price - price) / risk
    else:
        r = (entry - price) / risk
        peak_r = (entry - state.peak_price) / risk
        retrace_r = (price - state.peak_price) / risk

    state.best_r = max(state.best_r, peak_r)

    # Structural invalidation always has priority.
    structural_invalid = bool(snapshot.get("structural_invalidated", False))
    if structural_invalid:
        return {
            "exit": True,
            "reason": "STRUCTURAL_INVALIDATION",
            "r": round(r, 3),
            "peak_r": round(peak_r, 3),
            "retrace_r": round(retrace_r, 3)
        }

    # Build reversal confirmation.
    confirmations = 0
    reasons = []

    vwap = f(snapshot.get("vwap"))
    mom5 = f(snapshot.get("mom_5m"))
    mom15 = f(snapshot.get("mom_15m"))
    rs = f(snapshot.get("rs"))
    candle_reversal = bool(snapshot.get("candle_reversal", False))
    volume_reversal = bool(snapshot.get("volume_reversal", False))

    if side == "LONG":
        if vwap > 0 and price < vwap:
            confirmations += 1
            reasons.append("VWAP_LOST")
        if mom5 < 0:
            confirmations += 1
            reasons.append("MOM5_REVERSED")
        if mom15 < 0:
            confirmations += 1
            reasons.append("MOM15_REVERSED")
        if rs < 0:
            confirmations += 1
            reasons.append("RS_REVERSED")
    else:
        if vwap > 0 and price > vwap:
            confirmations += 1
            reasons.append("VWAP_RECLAIMED")
        if mom5 > 0:
            confirmations += 1
            reasons.append("MOM5_REVERSED")
        if mom15 > 0:
            confirmations += 1
            reasons.append("MOM15_REVERSED")
        if rs > 0:
            confirmations += 1
            reasons.append("RS_REVERSED")

    if candle_reversal:
        confirmations += 1
        reasons.append("CANDLE_REVERSAL")
    if volume_reversal:
        confirmations += 1
        reasons.append("VOLUME_REVERSAL")

    # Once a meaningful winner exists, lock a floor, but don't exit merely
    # because the profit has decreased from the peak.
    if peak_r >= cfg.meaningful_profit_r:
        locked_r = max(cfg.profit_lock_r, peak_r - cfg.peak_retrace_r)
        state.locked_r = max(state.locked_r, locked_r)

        # Exit only when BOTH the retracement and reversal confirmation exist.
        if retrace_r >= cfg.peak_retrace_r and confirmations >= cfg.reversal_confirmations:
            return {
                "exit": True,
                "reason": "PROFIT_FADING_CONFIRMED",
                "r": round(r, 3),
                "peak_r": round(peak_r, 3),
                "retrace_r": round(retrace_r, 3),
                "locked_r": round(state.locked_r, 3),
                "confirmations": confirmations,
                "reasons": reasons
            }

    return {
        "exit": False,
        "reason": "HOLD_TREND",
        "r": round(r, 3),
        "peak_r": round(peak_r, 3),
        "retrace_r": round(retrace_r, 3),
        "locked_r": round(state.locked_r, 3),
        "confirmations": confirmations,
        "reasons": reasons
    }


def validate_entry(snapshot: Dict[str, Any]) -> tuple[bool, str]:
    """Prevent zero/garbled audit records from being accepted."""
    required = ("price", "volume")
    if any(f(snapshot.get(k)) <= 0 for k in required):
        return False, "MISSING_PRICE_OR_VOLUME"

    if "score_total" in snapshot and "decision_score" in snapshot:
        if abs(f(snapshot["score_total"]) - f(snapshot["decision_score"])) > 0.01:
            return False, "SCORE_MAPPING_ERROR"

    if "expected_r" in snapshot:
        expected_r = f(snapshot["expected_r"])
        if expected_r > 20:
            return False, "EXPECTED_R_MAPPING_ERROR"

    return True, "OK"


def run_tests():
    # Early SHORT: developing breakdown should qualify when room/quality are valid.
    s = {
        "price": 98, "vwap": 100, "atr": 1,
        "support": 99, "resistance": 0,
        "rvol": 2.0, "rvol_prev": 1.5,
        "rs": -1.2, "rs_prev": -0.5,
        "mom_5m": -0.7, "mom_5m_prev": -0.3,
        "mom_15m": -0.4,
        "remaining_room_pct": 1.5,
        "bars_since_break": 1,
        "distance_from_break_pct": 0.2,
        "breakdown": True,
        "volume_expansion": True,
    }
    r = acceleration_score("SHORT", s, quality_score=72)
    assert r["enter"] is True, r

    # A mature/extended move must not be chased.
    s["distance_from_break_pct"] = 2.0
    r = acceleration_score("SHORT", s, quality_score=90)
    assert r["enter"] is False
    assert r["reason"] == "CHASE_PROTECTION"

    # A winner with only mild fading should continue.
    st = ExitState(peak_price=95)
    r = profit_fading_exit(
        "SHORT", entry=100, price=96.0, initial_risk=2.0,
        state=st,
        snapshot={"vwap": 98, "mom_5m": -0.2, "mom_15m": -0.3, "rs": -1.0}
    )
    assert r["exit"] is False

    # Confirmed reversal after a meaningful winner should exit.
    st = ExitState(peak_price=90)
    r = profit_fading_exit(
        "SHORT", entry=100, price=94.0, initial_risk=2.0,
        state=st,
        snapshot={
            "vwap": 93, "mom_5m": 0.4, "mom_15m": 0.3,
            "rs": 0.5, "candle_reversal": True
        }
    )
    assert r["exit"] is True, r

    print("V8.5.5 early-entry/profit-protection tests: PASS")


if __name__ == "__main__":
    run_tests()
