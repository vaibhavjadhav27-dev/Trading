"""
V12 Shadow Comparator — structured side-by-side logging of V10.1-R vs V12 decisions.

Produces one SHADOW_COMPARE JSONL entry per candidate evaluation, enabling:
- Direct decision comparison at identical timestamp + symbol
- Classification: AGREE_ENTER, AGREE_REJECT, V12_CATCHES_MISSED, V12_AVOIDS_BAD, etc.
- Post-session analysis with actual market outcomes

Output: v11_data/ledger/shadow_compare_YYYY-MM-DD.jsonl
"""

import json
import logging
import os
import time
import math
import threading
from datetime import datetime, timezone

log = logging.getLogger("v12_compare")

_IST_OFFSET = 5.5 * 3600  # IST = UTC + 5:30


def _ist_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _safe_float(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


class ShadowComparator:
    """
    Logs structured SHADOW_COMPARE entries for every candidate evaluated
    by both V10.1-R and V12.
    """

    def __init__(self, base_path=None):
        self._base = base_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "v11_data", "ledger"
        )
        os.makedirs(self._base, exist_ok=True)
        self._lock = threading.Lock()
        self._file = None
        self._current_date = None

    def _get_writer(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
            path = os.path.join(self._base, f"shadow_compare_{today}.jsonl")
            self._file = open(path, "a")
            self._current_date = today
        return self._file

    def log_comparison(
        self,
        symbol,
        side,
        price,
        # V10.1-R decision
        v10_decision,       # "ENTER" / "WATCH" / etc.
        v10_score,          # float
        v10_reason,         # str (setup_type, stage, etc.)
        v10_stage,          # str
        v10_type,           # str
        v10_pct,            # float
        # V12 decision (None if V12 unavailable)
        v12_result=None,    # DecisionResult object or None
        # V12 mapped features (for audit)
        v12_features=None,  # dict
        # Candidate metadata
        score_components=None,
        v853_result=None,
        candidate_id=None,
    ):
        """
        Log a single SHADOW_COMPARE entry.
        Call this AFTER both V10.1-R and V12 have evaluated the same candidate.
        """
        try:
            ts = _ist_now()

            # V10 classification
            v10_would_trade = v10_decision in ("ENTER", "ENTER_NOW")

            # V12 classification
            v12_decision = None
            v12_strategy = None
            v12_score = None
            v12_reason = None
            v12_would_trade = False
            v12_expected_r = None
            v12_remaining_edge = None
            v12_entry_drift = None
            v12_data_freshness = None
            v12_exec_quality = None
            v12_side = None

            if v12_result is not None:
                v12_decision = v12_result.decision.value if hasattr(v12_result.decision, 'value') else str(v12_result.decision)
                v12_strategy = v12_result.strategy
                v12_score = round(v12_result.calibrated_score, 2)
                v12_reason = v12_result.reason
                v12_side = v12_result.side.value if hasattr(v12_result.side, 'value') else str(v12_result.side)
                v12_would_trade = v12_decision == "ENTER_NOW"

            if v12_features:
                v12_expected_r = _safe_float(v12_features.get("expected_r"), None)
                v12_remaining_edge = _safe_float(v12_features.get("remaining_edge_pct"), None)
                v12_entry_drift = _safe_float(v12_features.get("entry_drift_pct"), None)
                v12_data_freshness = _safe_float(v12_features.get("signal_age_seconds"), None)
                v12_exec_quality = _safe_float(v12_features.get("execution_quality_ok"), None)

            # === CLASSIFICATION ===
            if v12_result is None:
                classification = "V12_UNAVAILABLE"
            elif v10_would_trade and v12_would_trade:
                classification = "AGREE_ENTER"
            elif not v10_would_trade and not v12_would_trade:
                classification = "AGREE_REJECT"
            elif not v10_would_trade and v12_would_trade:
                # V12 says enter but V10 said watch/reject
                # This COULD be V12_CATCHES_MISSED_OPPORTUNITY or V12_FALSE_ENTRY
                # We can't know until we see the outcome — mark as pending
                classification = "V12_WOULD_ENTER_V10_REJECTED"
            elif v10_would_trade and not v12_would_trade:
                # V10 says enter but V12 says wait/reject
                # This COULD be V12_AVOIDS_BAD_V10_ENTRY or V12_FALSE_REJECTION
                classification = "V12_WOULD_REJECT_V10_ENTERED"
            else:
                classification = "UNKNOWN"

            entry = {
                "event": "SHADOW_COMPARE",
                "timestamp": ts,
                "symbol": symbol,
                "candidate_id": candidate_id or f"{symbol}_{ts}",
                "side": side,
                "market_price": _safe_float(price),
                # V10.1-R
                "v10_decision": v10_decision,
                "v10_score": _safe_float(v10_score),
                "v10_reason": v10_reason or "",
                "v10_stage": v10_stage or "",
                "v10_type": v10_type or "",
                "v10_pct": _safe_float(v10_pct),
                "v10_would_trade": v10_would_trade,
                # V12
                "v12_decision": v12_decision,
                "v12_strategy": v12_strategy,
                "v12_score": v12_score,
                "v12_reason": v12_reason,
                "v12_side": v12_side,
                "v12_would_trade": v12_would_trade,
                "v12_expected_r": v12_expected_r,
                "v12_remaining_edge": v12_remaining_edge,
                "v12_entry_drift": v12_entry_drift,
                "v12_data_freshness_ms": round(v12_data_freshness * 1000, 0) if v12_data_freshness is not None else None,
                "v12_execution_quality": v12_exec_quality,
                # Classification
                "classification": classification,
                "disagreement_reason": v12_reason if classification.startswith("V12_WOULD") else None,
                # Score components (for post-hoc analysis)
                "score_components": score_components or {},
                "v853_phase": (v853_result or {}).get("phase", ""),
                "v853_fitness": _safe_float((v853_result or {}).get("fitness", 0)),
            }

            # Write JSONL
            with self._lock:
                writer = self._get_writer()
                writer.write(json.dumps(entry, default=str) + "\n")
                writer.flush()

            # Also log summary to main log
            log.info(
                f"SHADOW_COMPARE: {symbol} {side} @{_safe_float(price):.2f} | "
                f"V10={v10_decision}(s={_safe_float(v10_score):.0f}) "
                f"V12={v12_decision or '?'}(s={v12_score or 0:.0f}) | "
                f"{classification}"
            )

        except Exception as e:
            log.debug(f"SHADOW_COMPARE error for {symbol}: {e}")

    def close(self):
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None


def generate_session_summary(jsonl_path):
    """
    Read a day's shadow_compare JSONL and produce a summary.
    Call after session ends.
    
    Returns dict with counts and lists per classification.
    """
    counts = {
        "AGREE_ENTER": 0,
        "AGREE_REJECT": 0,
        "V12_WOULD_ENTER_V10_REJECTED": 0,
        "V12_WOULD_REJECT_V10_ENTERED": 0,
        "V12_UNAVAILABLE": 0,
        "UNKNOWN": 0,
    }
    disagreements = []
    total = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("event") != "SHADOW_COMPARE":
                    continue
                total += 1
                cls = entry.get("classification", "UNKNOWN")
                counts[cls] = counts.get(cls, 0) + 1
                if cls.startswith("V12_WOULD"):
                    disagreements.append({
                        "symbol": entry["symbol"],
                        "side": entry["side"],
                        "price": entry["market_price"],
                        "v10": entry["v10_decision"],
                        "v12": entry["v12_decision"],
                        "v12_reason": entry["v12_reason"],
                        "v12_score": entry["v12_score"],
                        "classification": cls,
                    })
    except FileNotFoundError:
        return {"error": f"File not found: {jsonl_path}"}

    return {
        "total_candidates": total,
        "counts": counts,
        "agreement_rate": round(
            100.0 * (counts["AGREE_ENTER"] + counts["AGREE_REJECT"]) / max(total, 1), 1
        ),
        "disagreements": disagreements,
    }


