"""
V11/V12 HARDENED STRATEGY PATCH
Purpose: deterministic NSE + MCX decision layer. Broker execution remains external.
This patch fail-closes on missing critical data and separates strategy-specific entry logic.

ENGINEER CONTRACT:
- Candidate.side is mandatory.
- Critical features must be present and finite.
- ENTER_NOW is not an order: OMS must re-check quote and broker state before submission.
- Position becomes active only after broker-confirmed fill.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple
import math
import time


class Market(str, Enum):
    NSE = "NSE"
    MCX = "MCX"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Decision(str, Enum):
    ENTER_NOW = "ENTER_NOW"
    WAIT = "WAIT"
    PULLBACK = "PULLBACK"
    RECLAIM = "RECLAIM"
    CANCEL = "CANCEL"


class PositionAction(str, Enum):
    OBSERVE = "OBSERVE"
    HOLD = "HOLD"
    PROTECT = "PROTECT"
    TRAIL = "TRAIL"
    EXIT = "EXIT"
    RECONCILE = "RECONCILE"


@dataclass
class Candidate:
    symbol: str
    market: Market
    side: Side
    timestamp: str
    price: float
    discovery_score: float
    setup_score: float
    entry_score: float
    regime: str
    setup: str
    features: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionResult:
    decision: Decision
    side: Side
    confidence: float
    reason: str
    strategy: str
    calibrated_score: float
    audit: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionSnapshot:
    symbol: str
    market: Market
    side: Side
    entry_price: float
    current_price: float
    peak_price: float
    qty: int
    initial_risk_per_share: float
    entry_time: str
    minutes_in_trade: float
    state_unknown: bool = False
    broker_confirmed: bool = True
    momentum_score: float = 50.0
    structure_valid: bool = True


@dataclass
class StrategyProfile:
    min_setup_score: float
    min_entry_score: float
    min_volume_expansion: float
    min_acceptance: float
    min_expected_r: float
    max_extension_atr: float


@dataclass
class StrategyConfig:
    min_discovery_score: float = 45.0
    min_calibrated_score: float = 60.0
    min_remaining_edge_pct: float = 0.40
    max_signal_age_seconds: float = 5.0
    max_entry_drift_pct: float = 0.20

    # Position management
    no_progress_minutes: int = 45
    min_progress_r_after_decay: float = 0.15
    min_momentum_for_hold: float = 45.0
    protect_at_r: float = 0.50
    trail_at_r: float = 1.00
    giveback_fraction: float = 0.35

    # MCX execution
    mcx_max_quote_age_seconds: float = 2.0
    mcx_max_spread_ticks: float = 3.0
    mcx_max_slippage_ticks: float = 2.0
    mcx_min_depth_ratio: float = 1.0
    reject_expiry_day_after_hour_ist: int = 18

    profiles: Dict[str, StrategyProfile] = field(default_factory=lambda: {
        "ORB_CONTINUATION": StrategyProfile(55, 60, 1.00, 0.50, 1.50, 0.80),
        "CONSOLIDATION_EXPANSION": StrategyProfile(52, 58, 1.00, 0.60, 1.50, 1.00),
        "VWAP_RECLAIM": StrategyProfile(52, 56, 0.90, 0.65, 1.50, 1.20),
        "PULLBACK_CONTINUATION": StrategyProfile(45, 50, 0.80, 0.60, 1.80, 1.20),
        "MOMENTUM_CONTINUATION": StrategyProfile(45, 50, 1.00, 0.45, 2.00, 1.00),
    })
    version: str = "V11_V12_HARDENED_PATCH_2026_08_31"


CRITICAL_FEATURES = (
    "data_healthy",
    "market_regime_allowed",
    "execution_quality_ok",
    "signal_age_seconds",
    "entry_drift_pct",
    "volume_expansion",
    "expected_r",
    "remaining_edge_pct",
)


def _num(value: Any) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> Optional[bool]:
    x = _num(value)
    if x is None:
        return None
    return x >= 1.0


def _directional_r(side: Side, entry: float, price: float, risk: float) -> float:
    if risk <= 0:
        return float("-inf")
    return ((price - entry) if side == Side.LONG else (entry - price)) / risk


def _directional_efficiency(side: Side, entry: float, exit_price: float, peak: float) -> Optional[float]:
    best = (peak - entry) if side == Side.LONG else (entry - peak)
    realized = (exit_price - entry) if side == Side.LONG else (entry - exit_price)
    return None if best <= 0 else 100.0 * realized / best


class StrategyRouter:
    @staticmethod
    def route(c: Candidate) -> str:
        f = c.features
        if _truthy(f.get("orb_breakout")) and _truthy(f.get("breakout_accepted")):
            return "ORB_CONTINUATION"
        if _truthy(f.get("consolidation_breakout")):
            return "CONSOLIDATION_EXPANSION"
        if _truthy(f.get("vwap_reclaim")):
            return "VWAP_RECLAIM"
        if _truthy(f.get("pullback_held")) and _truthy(f.get("trend_intact")):
            return "PULLBACK_CONTINUATION"
        return "MOMENTUM_CONTINUATION"


class ScoreCalibrator:
    @staticmethod
    def score(c: Candidate, strategy: str) -> float:
        raw = 0.25 * c.discovery_score + 0.35 * c.setup_score + 0.40 * c.entry_score
        extension_atr = _num(c.features.get("extension_atr")) or 0.0
        acceptance = _num(c.features.get("breakout_acceptance"))
        penalty = min(20.0, max(0.0, extension_atr) * 5.0)
        if acceptance is not None and acceptance < 0.60:
            penalty += 10.0
        return max(0.0, min(100.0, raw - penalty))


class EntryStateEngine:
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()

    def _critical_gate(self, c: Candidate) -> Optional[str]:
        for name in CRITICAL_FEATURES:
            if _num(c.features.get(name)) is None:
                return f"MISSING_OR_INVALID_CRITICAL_FEATURE:{name}"
        if not _truthy(c.features["data_healthy"]):
            return "STALE_OR_UNHEALTHY_DATA"
        if not _truthy(c.features["market_regime_allowed"]):
            return "REGIME_NOT_ALLOWED"
        if not _truthy(c.features["execution_quality_ok"]):
            return "EXECUTION_QUALITY_REJECTED"
        if _num(c.features["signal_age_seconds"]) > self.config.max_signal_age_seconds:
            return "STALE_SIGNAL_REEVALUATION_REQUIRED"
        if _num(c.features["entry_drift_pct"]) > self.config.max_entry_drift_pct:
            return "ENTRY_DRIFT_REEVALUATION_REQUIRED"
        return None

    def _strategy_gate(self, c: Candidate, strategy: str, p: StrategyProfile) -> Optional[Tuple[Decision, str]]:
        f = c.features
        if c.setup_score < p.min_setup_score:
            return Decision.WAIT, "STRATEGY_SETUP_NOT_READY"
        if c.entry_score < p.min_entry_score:
            return Decision.WAIT, "STRATEGY_ENTRY_CONFIRMATION_NOT_READY"

        vol = _num(f["volume_expansion"])
        if vol is None or vol < p.min_volume_expansion:
            return Decision.WAIT, "STRATEGY_VOLUME_CONFIRMATION_INSUFFICIENT"

        ext_atr = _num(f.get("extension_atr"))
        if ext_atr is None:
            return Decision.CANCEL, "MISSING_OR_INVALID_CRITICAL_FEATURE:extension_atr"
        if ext_atr > p.max_extension_atr:
            return Decision.PULLBACK, "VALID_SETUP_TOO_EXTENDED_WAIT_FOR_PULLBACK"

        expected_r = _num(f["expected_r"])
        remaining_edge = _num(f["remaining_edge_pct"])
        if expected_r is None or expected_r < p.min_expected_r:
            return Decision.WAIT, "EXPECTED_R_INSUFFICIENT"
        if remaining_edge is None or remaining_edge < self.config.min_remaining_edge_pct:
            return Decision.WAIT, "REMAINING_EDGE_INSUFFICIENT"

        acceptance = _num(f.get("breakout_acceptance"))
        if strategy in ("ORB_CONTINUATION", "CONSOLIDATION_EXPANSION", "MOMENTUM_CONTINUATION"):
            if acceptance is None or acceptance < p.min_acceptance:
                return Decision.WAIT, "PRICE_ACCEPTANCE_INSUFFICIENT"

        if strategy == "VWAP_RECLAIM":
            if not _truthy(f.get("reclaim_confirmed")):
                return Decision.RECLAIM, "WAIT_FOR_VWAP_RECLAIM_CONFIRMATION"

        if strategy == "PULLBACK_CONTINUATION":
            if not (_truthy(f.get("pullback_held")) and _truthy(f.get("trend_intact"))):
                return Decision.PULLBACK, "WAIT_FOR_PULLBACK_HOLD"

        return None

    def evaluate(self, c: Candidate) -> DecisionResult:
        strategy = StrategyRouter.route(c)
        profile = self.config.profiles[strategy]
        score = ScoreCalibrator.score(c, strategy)
        audit = {
            "version": self.config.version,
            "strategy": strategy,
            "side": c.side.value,
            "discovery_score": c.discovery_score,
            "setup_score": c.setup_score,
            "entry_score": c.entry_score,
            "calibrated_score": score,
        }

        if not all(math.isfinite(x) for x in (c.price, c.discovery_score, c.setup_score, c.entry_score)):
            return DecisionResult(Decision.CANCEL, c.side, 0.0, "INVALID_CANDIDATE_NUMERIC_DATA", strategy, score, audit)

        critical = self._critical_gate(c)
        if critical:
            decision = Decision.WAIT if "REEVALUATION_REQUIRED" in critical else Decision.CANCEL
            return DecisionResult(decision, c.side, 0.0, critical, strategy, score, audit)

        if c.discovery_score < self.config.min_discovery_score:
            return DecisionResult(Decision.CANCEL, c.side, 0.20, "DISCOVERY_SCORE_TOO_LOW", strategy, score, audit)

        if _truthy(c.features.get("setup_invalid")):
            return DecisionResult(Decision.CANCEL, c.side, 0.0, "SETUP_INVALID", strategy, score, audit)

        gate = self._strategy_gate(c, strategy, profile)
        if gate:
            d, reason = gate
            return DecisionResult(d, c.side, 0.60, reason, strategy, score, audit)

        if score < self.config.min_calibrated_score:
            return DecisionResult(Decision.WAIT, c.side, 0.50, "CALIBRATED_SCORE_TOO_LOW", strategy, score, audit)

        return DecisionResult(Decision.ENTER_NOW, c.side, 0.90, "STRATEGY_SPECIFIC_ENTRY_CONFIRMED", strategy, score, audit)


class PositionManager:
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()

    def update(self, p: PositionSnapshot) -> Tuple[PositionAction, str, Dict[str, float]]:
        if p.state_unknown or not p.broker_confirmed:
            return PositionAction.RECONCILE, "BROKER_STATE_UNCERTAIN", {}

        risk = max(p.initial_risk_per_share, 1e-9)
        current_r = _directional_r(p.side, p.entry_price, p.current_price, risk)
        peak_r = _directional_r(p.side, p.entry_price, p.peak_price, risk)
        giveback = max(0.0, peak_r - current_r)
        m = {"current_r": current_r, "peak_r": peak_r, "giveback_r": giveback}

        if not p.structure_valid:
            return PositionAction.EXIT, "STRUCTURE_INVALIDATED", m

        if (p.minutes_in_trade >= self.config.no_progress_minutes
                and current_r < self.config.min_progress_r_after_decay
                and p.momentum_score < self.config.min_momentum_for_hold):
            return PositionAction.EXIT, "TIME_DECAY_NO_PROGRESS", m

        if peak_r >= self.config.trail_at_r:
            allowed = max(0.25, peak_r * self.config.giveback_fraction)
            m["allowed_giveback_r"] = allowed
            if giveback >= allowed and p.momentum_score < self.config.min_momentum_for_hold:
                return PositionAction.EXIT, "EXCESSIVE_GIVEBACK_AFTER_PROFIT", m
            return PositionAction.TRAIL, "RUNNER_TRAIL", m

        if peak_r >= self.config.protect_at_r:
            return PositionAction.PROTECT, "PROFIT_PROTECTION", m

        if p.momentum_score >= self.config.min_momentum_for_hold:
            return PositionAction.HOLD, "MOMENTUM_AND_STRUCTURE_HEALTHY", m

        return PositionAction.OBSERVE, "POSITION_STILL_FORMING", m


# ---------------- MCX-specific safety helpers ----------------

MCX_FUTURE_SESSIONS = {
    "MORNING": ("09:00", "15:30"),
    "EVENING": ("17:00", "20:30"),
}


def validate_mcx_contract(contract: Dict[str, Any], now_hour_ist: int,
                          expiry_today: bool, cfg: Optional[StrategyConfig] = None) -> Tuple[bool, str]:
    cfg = cfg or StrategyConfig()
    segment = str(contract.get("exchange_segment", "")).upper()
    instrument = str(contract.get("instrument_type", "")).upper()
    symbol = str(contract.get("symbol", "")).upper()
    option_type = str(contract.get("option_type", "")).upper()
    if "MCX" not in segment:
        return False, "MCX_SEGMENT_MISMATCH"
    if instrument not in ("FUTCOM", "FUTURES", "FUTURE"):
        return False, "NOT_MCX_FUTURE"
    if option_type in ("CE", "PE"):
        return False, "OPTION_CONTRACT_REJECTED"
    if "-CE" in symbol or "-PE" in symbol or " CE" in symbol or " PE" in symbol:
        return False, "OPTION_SYMBOL_REJECTED"
    if expiry_today and now_hour_ist >= cfg.reject_expiry_day_after_hour_ist:
        return False, "EXPIRY_DAY_CONTRACT_REJECTED"
    return True, "OK"


def parse_dhan_mcx_ltp(response: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Expected Dhan shape:
    data -> MCX_COMM -> security_id -> quote object.
    Wrong/unknown schemas return {} and must be logged as data failure, not 'no signal'.
    """
    data = response.get("data", {})
    if not isinstance(data, dict):
        return {}
    segment = data.get("MCX_COMM", {})
    return segment if isinstance(segment, dict) else {}


def mcx_execution_quality_ok(quote_age_s: float, spread_ticks: float,
                             estimated_slippage_ticks: float, depth_ratio: float,
                             cfg: Optional[StrategyConfig] = None) -> Tuple[bool, str]:
    cfg = cfg or StrategyConfig()
    vals = (quote_age_s, spread_ticks, estimated_slippage_ticks, depth_ratio)
    if any((not math.isfinite(float(x))) for x in vals):
        return False, "INVALID_MCX_EXECUTION_DATA"
    if quote_age_s > cfg.mcx_max_quote_age_seconds:
        return False, "STALE_MCX_QUOTE"
    if spread_ticks > cfg.mcx_max_spread_ticks:
        return False, "WIDE_MCX_SPREAD"
    if estimated_slippage_ticks > cfg.mcx_max_slippage_ticks:
        return False, "EXCESSIVE_MCX_SLIPPAGE"
    if depth_ratio < cfg.mcx_min_depth_ratio:
        return False, "INSUFFICIENT_MCX_DEPTH"
    return True, "OK"


def should_retry_after_http(status_code: int) -> Tuple[bool, str]:
    if status_code == 429:
        return True, "RATE_LIMIT_BACKOFF_AND_CIRCUIT_BREAKER"
    if 500 <= status_code <= 599:
        return True, "TRANSIENT_SERVER_ERROR_BACKOFF"
    return False, "DO_NOT_BLIND_RETRY"


def evaluate_candidate(candidate: Candidate, config: Optional[StrategyConfig] = None) -> DecisionResult:
    return EntryStateEngine(config).evaluate(candidate)


def exit_efficiency(side: Side, entry_price: float, exit_price: float, peak_price: float) -> Optional[float]:
    return _directional_efficiency(side, entry_price, exit_price, peak_price)


# Optional compatibility adapter for legacy callers. Engineer should migrate to Candidate.side.
def evaluate_candidate_legacy(candidate, config=None):
    side = getattr(candidate, "side", None)
    if side is None:
        raise ValueError("Legacy candidate has no side. Direction inference is forbidden.")
    return evaluate_candidate(candidate, config)
