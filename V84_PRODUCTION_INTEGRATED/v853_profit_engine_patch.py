"""
V8.5.3 PROFIT / TIMING / EXIT ENGINE
====================================

Engineer-ready strategy layer for the existing V8.2/V8.4/V8.5.x engine.

CALIBRATED AGAINST THE 21-AUG-2026 LOG PATTERNS PROVIDED IN CHAT:
- SOUTHWEST: strong early continuation that should not be lost to late entry
- VIPIND: strong RS + RVOL + momentum
- ZUARIIND: strong SHORT despite lower raw score
- RGL / COCHINSHIP: strong SHORT participation
- SUPREMEINF: >80 score with strong RS/RVOL/momentum
- POWERGRID: high raw score but very weak initial RVOL; do not blindly enter

This is a strategy module, NOT a replacement for the Dhan gateway.
Broker-side SL/order-status/restart/partial-fill tests remain mandatory.

CORE PRINCIPLE
--------------
Raw V8.2 score identifies a candidate.
V8.5.3 determines whether the candidate is becoming actionable NOW.

The engine deliberately separates:
    QUALITY  -> is this a good trade?
    TIMING   -> is NOW a good entry?
    ROOM     -> is enough of the move still available?
    RISK     -> where is structural invalidation?
    EXIT     -> has the thesis actually reversed?

No profit percentage is guaranteed.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any, Dict, Tuple
from math import isfinite, floor


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    # Directional raw-score floors. These are not the final score.
    long_score_floor: float = 65.0
    short_score_floor: float = 63.0

    # Actionable score after evidence combination.
    enter_score: float = 66.0
    strong_score: float = 80.0

    # Timing / extension.
    early_extension_atr: float = 0.60
    developing_extension_atr: float = 1.00
    mature_extension_atr: float = 1.40
    exhausted_extension_atr: float = 1.80

    # Participation.
    rvol_watch: float = 0.80
    rvol_actionable: float = 1.15
    rvol_strong: float = 2.00
    rvol_exceptional: float = 4.00

    # Momentum.
    m5_actionable: float = 0.30
    m15_actionable: float = 0.45
    m5_strong: float = 0.55
    m15_strong: float = 0.80

    # Remaining move.
    minimum_remaining_move_pct: float = 0.40

    # Structural stop.
    initial_atr_mult: float = 1.20
    structural_buffer_atr: float = 0.10
    minimum_stop_atr: float = 0.25

    # Peak/reversal.
    warning_reversal_score: int = 2
    exit_reversal_score: int = 4
    minimum_peak_r_for_exit: float = 0.80
    warning_retracement_r: float = 0.35
    exit_retracement_r: float = 0.75

    # Dhan SL recovery.
    sl_recovery_buffer_atr: float = 0.10

    # Cost control.
    minimum_expected_net_edge_pct: float = 0.10


class Phase(str, Enum):
    EARLY = "EARLY"
    DEVELOPING = "DEVELOPING"
    MATURE = "MATURE"
    EXHAUSTED = "EXHAUSTED"


class EntryAction(str, Enum):
    ENTER_NOW = "ENTER_NOW"
    ENTER_ON_PULLBACK = "ENTER_ON_PULLBACK"
    WATCH = "WATCH"
    DO_NOT_TRADE = "DO_NOT_TRADE"


class ExitAction(str, Enum):
    HOLD = "HOLD"
    PROTECT = "PROTECT"
    EXIT = "EXIT"


@dataclass
class Candidate:
    symbol: str
    side: str                    # LONG / SHORT
    ltp: float
    atr: float
    raw_score: float
    rs: float
    rvol: float
    vwap: Optional[float]
    momentum_5m: float
    momentum_15m: float
    trigger: Optional[float]
    setup_type: str
    regime: str

    expected_move_pct: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None

    # Changes from previous scan.
    rs_delta: Optional[float] = None
    rvol_delta: Optional[float] = None
    momentum_delta: Optional[float] = None

    # Optional market/sector alignment.
    sector_rs: Optional[float] = None
    market_direction: Optional[str] = None  # LONG / SHORT / NEUTRAL

    data_quality: str = "FULL"


@dataclass
class EntryDecision:
    action: EntryAction
    final_score: float
    quality_score: float
    timing_score: float
    phase: Phase
    reason: str
    diagnostics: Dict[str, Any]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if isfinite(x) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Entry engine
# ---------------------------------------------------------------------------

class V853EntryEngine:
    """
    Evidence-weighted entry engine.

    IMPORTANT:
    Do not calculate one giant weighted average from the raw V8.2 score.
    We deliberately split QUALITY and TIMING.

    This fixes the problem seen in today's data where:
        - a strong early move can be missed because it has not yet become
          the highest-scoring candidate;
        - a high raw score with weak participation can be traded too early.
    """

    def __init__(self, cfg: Config = Config()):
        self.cfg = cfg

    def extension_atr(self, c: Candidate) -> Optional[float]:
        if c.trigger is None or c.atr <= 0:
            return None
        return abs(c.ltp - c.trigger) / c.atr

    def phase(self, c: Candidate) -> Phase:
        e = self.extension_atr(c)

        if e is None:
            return Phase.DEVELOPING
        if e <= self.cfg.early_extension_atr:
            return Phase.EARLY
        if e <= self.cfg.developing_extension_atr:
            return Phase.DEVELOPING
        if e <= self.cfg.mature_extension_atr:
            return Phase.MATURE
        if e <= self.cfg.exhausted_extension_atr:
            return Phase.EXHAUSTED
        return Phase.EXHAUSTED

    def direction(self, c: Candidate) -> int:
        return 1 if c.side == "LONG" else -1

    def quality_score(self, c: Candidate) -> float:
        """
        0..60.

        Strongest inputs:
            RS
            RVOL
            5m momentum
            15m momentum
            VWAP relationship
            setup type

        The raw V8.2 score is intentionally capped in influence.
        """
        d = self.direction(c)
        s = 0.0

        # RS: 0..15
        directional_rs = d * c.rs
        if directional_rs >= 3.0:
            s += 15
        elif directional_rs >= 2.0:
            s += 12
        elif directional_rs >= 1.0:
            s += 8
        elif directional_rs >= 0.5:
            s += 4

        # RVOL: 0..15
        if c.rvol >= self.cfg.rvol_exceptional:
            s += 15
        elif c.rvol >= self.cfg.rvol_strong:
            s += 12
        elif c.rvol >= self.cfg.rvol_actionable:
            s += 8
        elif c.rvol >= self.cfg.rvol_watch:
            s += 3

        # 5m momentum: 0..10
        m5 = d * c.momentum_5m
        if m5 >= self.cfg.m5_strong:
            s += 10
        elif m5 >= self.cfg.m5_actionable:
            s += 7
        elif m5 > 0:
            s += 3

        # 15m momentum: 0..10
        m15 = d * c.momentum_15m
        if m15 >= self.cfg.m15_strong:
            s += 10
        elif m15 >= self.cfg.m15_actionable:
            s += 7
        elif m15 > 0:
            s += 3

        # VWAP: 0..5
        if c.vwap is not None and ((c.ltp > c.vwap) == (c.side == "LONG")):
            s += 5

        # Setup: 0..5
        s += {
            "ORB_BREAKOUT": 5,
            "ORB_BREAKDOWN": 5,
            "MOMENTUM_CONTINUATION": 5,
            "VWAP_REVERSAL": 4,
            "LATE_MOMENTUM": 2,
        }.get(c.setup_type, 1)

        return clamp(s, 0, 60)

    def timing_score(self, c: Candidate) -> float:
        """
        0..40.

        This is the critical new layer.

        It rewards:
            acceleration
            early/developing phase
            RVOL expansion
            momentum expansion

        It penalises:
            exhausted extension
            weak participation
        """
        d = self.direction(c)
        s = 0.0
        phase = self.phase(c)

        # Freshness: 0..12
        s += {
            Phase.EARLY: 12,
            Phase.DEVELOPING: 10,
            Phase.MATURE: 5,
            Phase.EXHAUSTED: 0,
        }[phase]

        # RVOL: 0..8
        if c.rvol_delta is not None:
            if c.rvol_delta >= 1.0:
                s += 8
            elif c.rvol_delta >= 0.30:
                s += 5
            elif c.rvol_delta > 0:
                s += 2

        # Momentum acceleration: 0..8
        if c.momentum_delta is not None and d * c.momentum_delta > 0:
            if abs(c.momentum_delta) >= 0.50:
                s += 8
            elif abs(c.momentum_delta) >= 0.20:
                s += 5
            else:
                s += 2

        # RS acceleration: 0..6
        if c.rs_delta is not None and d * c.rs_delta > 0:
            if abs(c.rs_delta) >= 1.0:
                s += 6
            elif abs(c.rs_delta) >= 0.30:
                s += 4
            else:
                s += 2

        # Strong participation can itself make the move actionable.
        if c.rvol >= self.cfg.rvol_exceptional:
            s += 6
        elif c.rvol >= self.cfg.rvol_strong:
            s += 4

        return clamp(s, 0, 40)

    def final_score(self, c: Candidate, quality: float, timing: float) -> float:
        """
        70% evidence quality + 30% timing.

        Raw V8.2 score contributes only as a small calibration factor.
        This prevents POWERGRID-like high raw-score/low-RVOL cases from
        dominating, while allowing ZUARIIND-like strong shorts through.
        """
        raw_component = clamp(c.raw_score, 0, 100) * 0.10
        evidence_component = (quality / 60.0 * 100.0) * 0.60
        timing_component = (timing / 40.0 * 100.0) * 0.30
        return clamp(raw_component + evidence_component + timing_component, 0, 100)

    def evaluate(self, c: Candidate) -> EntryDecision:
        if c.data_quality != "FULL":
            return EntryDecision(
                EntryAction.WATCH, 0, 0, 0, self.phase(c),
                "INSUFFICIENT_DATA", {"data_quality": c.data_quality}
            )

        p = self.phase(c)
        q = self.quality_score(c)
        t = self.timing_score(c)
        f = self.final_score(c, q, t)

        directional_raw_floor = (
            self.cfg.long_score_floor
            if c.side == "LONG"
            else self.cfg.short_score_floor
        )

        d = self.direction(c)

        # Strong directional exception:
        # This specifically prevents a strong SHORT from being rejected
        # simply because its raw score is below the generic long threshold.
        strong_direction = (
            d * c.rs >= 2.0
            and c.rvol >= self.cfg.rvol_actionable
            and d * c.momentum_5m >= self.cfg.m5_actionable
        )

        # Strong raw-score candidates still need actual evidence.
        if c.raw_score < directional_raw_floor and not strong_direction:
            return EntryDecision(
                EntryAction.WATCH, f, q, t, p,
                "RAW_SCORE_FLOOR_NOT_MET",
                {"raw_score": c.raw_score,
                 "floor": directional_raw_floor}
            )

        # POWERGRID-type protection:
        # High score alone with poor participation must not enter.
        if c.rvol < self.cfg.rvol_watch:
            return EntryDecision(
                EntryAction.WATCH, f, q, t, p,
                "PARTICIPATION_TOO_WEAK",
                {"rvol": c.rvol}
            )

        # If expected move is known and nearly exhausted, do not chase.
        if (
            c.expected_move_pct is not None
            and c.expected_move_pct < self.cfg.minimum_remaining_move_pct
        ):
            return EntryDecision(
                EntryAction.ENTER_ON_PULLBACK if p == Phase.MATURE else EntryAction.WATCH,
                f, q, t, p,
                "INSUFFICIENT_REMAINING_MOVE",
                {"remaining_move_pct": c.expected_move_pct}
            )

        # Exhausted move: do not chase.
        if p == Phase.EXHAUSTED:
            return EntryDecision(
                EntryAction.DO_NOT_TRADE, f, q, t, p,
                "MOVE_EXHAUSTED_DO_NOT_CHASE",
                {}
            )

        # Strong directional exception:
        # ZUARIIND-type setups can be actionable even when the raw-score
        # composite is modest, provided RS/RVOL/momentum agree strongly.
        if (
            strong_direction
            and q >= 36
            and t >= 10
            and p in (Phase.EARLY, Phase.DEVELOPING)
        ):
            return EntryDecision(
                EntryAction.ENTER_NOW, f, q, t, p,
                "STRONG_DIRECTIONAL_EARLY_EXCEPTION",
                {"quality": q, "timing": t,
                 "raw_score": c.raw_score,
                 "strong_direction": True}
            )

        # Immediate entry requires BOTH quality and timing.
        if f >= self.cfg.enter_score and q >= 32:
            if p in (Phase.EARLY, Phase.DEVELOPING):
                return EntryDecision(
                    EntryAction.ENTER_NOW, f, q, t, p,
                    "FRESH_ACTIONABLE_OPPORTUNITY",
                    {"quality": q, "timing": t,
                     "raw_score": c.raw_score,
                     "strong_direction": strong_direction}
                )

            if p == Phase.MATURE and t >= 12:
                return EntryDecision(
                    EntryAction.ENTER_NOW, f, q, t, p,
                    "MATURE_BUT_ACCELERATING",
                    {"quality": q, "timing": t}
                )

            if p == Phase.MATURE:
                return EntryDecision(
                    EntryAction.ENTER_ON_PULLBACK, f, q, t, p,
                    "MATURE_WAIT_FOR_FRESH_TRIGGER",
                    {"quality": q, "timing": t}
                )

        return EntryDecision(
            EntryAction.WATCH, f, q, t, p,
            "NOT_YET_ACTIONABLE",
            {"quality": q, "timing": t,
             "raw_score": c.raw_score}
        )


# ---------------------------------------------------------------------------
# Structural SL
# ---------------------------------------------------------------------------

def structural_stop(
    c: Candidate,
    cfg: Config = Config(),
) -> float:
    """
    Structural stop, not arbitrary 0.x%.

    LONG:
        below support/trigger + ATR buffer.

    SHORT:
        above resistance/trigger + ATR buffer.

    The minimum distance prevents an unrealistically tight stop.
    """
    if c.atr <= 0:
        raise ValueError("ATR must be positive")

    b = cfg.structural_buffer_atr * c.atr

    if c.side == "LONG":
        levels = [x for x in (c.support, c.trigger) if x is not None]
        raw = max(levels) - b if levels else c.ltp - cfg.initial_atr_mult * c.atr
        return round(
            min(raw, c.ltp - cfg.minimum_stop_atr * c.atr), 2
        )

    levels = [x for x in (c.resistance, c.trigger) if x is not None]
    raw = min(levels) + b if levels else c.ltp + cfg.initial_atr_mult * c.atr
    return round(
        max(raw, c.ltp + cfg.minimum_stop_atr * c.atr), 2
    )


# ---------------------------------------------------------------------------
# Dhan SL rejection recovery
# ---------------------------------------------------------------------------

def recover_stop_after_dhan_rejection(
    side: str,
    ltp: float,
    intended_stop: float,
    atr: float,
    cfg: Config = Config(),
) -> Tuple[Optional[float], str]:
    """
    DH-906 recovery.

    We do NOT automatically market-exit just because the original
    structural stop has crossed current LTP.

    LONG protection must remain below LTP.
    SHORT protection must remain above LTP.

    The caller must:
        1. cancel/replace the invalid pending order;
        2. submit a valid Dhan SL-M order with price=0 and valid triggerPrice;
        3. verify order status;
        4. continue normal thesis-based exit management.

    If protection cannot be established after recovery, production code
    must invoke its broker-failure safety policy.
    """
    if atr <= 0 or ltp <= 0:
        return None, "INVALID_ATR_OR_LTP"

    b = cfg.sl_recovery_buffer_atr * atr

    if side == "LONG":
        if intended_stop < ltp:
            return round(intended_stop, 2), "ORIGINAL_STOP_VALID"

        new_stop = ltp - b
        if new_stop <= 0:
            return None, "RECOVERY_STOP_INVALID"
        return round(new_stop, 2), "STOP_CROSSED_RECOVERED_BELOW_LTP"

    if intended_stop > ltp:
        return round(intended_stop, 2), "ORIGINAL_STOP_VALID"

    return round(ltp + b, 2), "STOP_CROSSED_RECOVERED_ABOVE_LTP"


# ---------------------------------------------------------------------------
# Exit engine
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    side: str
    entry: float
    structural_sl: float
    atr: float
    peak: float
    setup_type: str = ""
    entry_score: float = 0.0


class ExitEngine:
    """
    Exit philosophy:

        HOLD while thesis remains intact.

        PROTECT when profit is meaningful and reversal evidence begins.

        EXIT only when:
            - structural/setup invalidation is confirmed, OR
            - peak retracement + multi-factor reversal confirms.

    A single weak candle or a single indicator crossing does not exit.
    """

    def __init__(self, cfg: Config = Config()):
        self.cfg = cfg

    def r_multiple(self, p: Position, ltp: float) -> float:
        risk = abs(p.entry - p.structural_sl)
        if risk <= 0:
            return 0.0

        if p.side == "LONG":
            return (ltp - p.entry) / risk
        return (p.entry - ltp) / risk

    def update_peak(self, p: Position, ltp: float):
        if p.side == "LONG":
            p.peak = max(p.peak, ltp)
        else:
            p.peak = min(p.peak, ltp)

    def reversal_score(
        self,
        p: Position,
        ltp: float,
        f: Dict[str, Any],
    ) -> Tuple[int, Dict[str, bool]]:
        d = 1 if p.side == "LONG" else -1

        m5 = num(f.get("momentum_5m"), 0.0) or 0.0
        m15 = num(f.get("momentum_15m"), 0.0) or 0.0
        rs = num(f.get("rs"), 0.0) or 0.0
        vwap = num(f.get("vwap"))

        checks = {
            # Need BOTH short and medium momentum to reverse.
            "momentum_reversal": d * m5 < 0 and d * m15 < 0,

            # VWAP loss is supporting evidence only.
            "vwap_reversal": (
                vwap is not None and
                ((ltp < vwap) if p.side == "LONG" else (ltp > vwap))
            ),

            "rs_reversal": d * rs < 0,

            "structure_break": bool(f.get("structure_break")),

            "volume_climax_no_progress": bool(
                f.get("volume_climax")
            ) and bool(f.get("price_progress_stalling")),

            "setup_invalidated": bool(f.get("setup_invalidated")),
        }

        return sum(checks.values()), checks

    def protected_stop(
        self,
        p: Position,
        ltp: float,
        reversal_score: int,
    ) -> float:
        """
        Profit-locking schedule.

        This does NOT set a fixed profit target.
        The position remains open while the trend continues.
        """
        risk = abs(p.entry - p.structural_sl)
        r = self.r_multiple(p, ltp)
        peak_r = self.r_multiple(p, p.peak)

        if r < 1.0:
            target_r = 0.0
        elif r < 2.0:
            target_r = 0.50
        elif r < 2.5:
            target_r = 1.25
        elif r < 3.0:
            target_r = 1.75
        else:
            target_r = 2.25

        if peak_r >= 1.0 and reversal_score >= self.cfg.warning_reversal_score:
            target_r = max(
                target_r,
                peak_r - self.cfg.warning_retracement_r
            )

        if peak_r >= 1.5 and reversal_score >= self.cfg.exit_reversal_score:
            target_r = max(
                target_r,
                peak_r - self.cfg.exit_retracement_r
            )

        if p.side == "LONG":
            return round(max(
                p.structural_sl,
                p.entry + target_r * risk
            ), 2)

        return round(min(
            p.structural_sl,
            p.entry - target_r * risk
        ), 2)

    def evaluate(
        self,
        p: Position,
        ltp: float,
        features: Dict[str, Any],
    ) -> Dict[str, Any]:

        self.update_peak(p, ltp)

        current_r = self.r_multiple(p, ltp)
        peak_r = self.r_multiple(p, p.peak)
        retracement = max(0.0, peak_r - current_r)

        rev, checks = self.reversal_score(p, ltp, features)
        new_stop = self.protected_stop(p, ltp, rev)

        if checks["setup_invalidated"]:
            return {
                "action": ExitAction.EXIT.value,
                "reason": "CONFIRMED_SETUP_INVALIDATION",
                "reversal_score": rev,
                "current_r": current_r,
                "peak_r": peak_r,
                "retracement_r": retracement,
                "stop": new_stop,
                "checks": checks,
            }

        if (
            peak_r >= self.cfg.minimum_peak_r_for_exit
            and rev >= self.cfg.exit_reversal_score
            and retracement >= self.cfg.exit_retracement_r
        ):
            return {
                "action": ExitAction.EXIT.value,
                "reason": "CONFIRMED_PEAK_REVERSAL",
                "reversal_score": rev,
                "current_r": current_r,
                "peak_r": peak_r,
                "retracement_r": retracement,
                "stop": new_stop,
                "checks": checks,
            }

        if (
            rev >= self.cfg.warning_reversal_score
            or retracement >= self.cfg.warning_retracement_r
        ):
            return {
                "action": ExitAction.PROTECT.value,
                "reason": "PROTECT_AND_LET_EXIT_ENGINE_WORK",
                "reversal_score": rev,
                "current_r": current_r,
                "peak_r": peak_r,
                "retracement_r": retracement,
                "stop": new_stop,
                "checks": checks,
            }

        return {
            "action": ExitAction.HOLD.value,
            "reason": "THESIS_INTACT",
            "reversal_score": rev,
            "current_r": current_r,
            "peak_r": peak_r,
            "retracement_r": retracement,
            "stop": new_stop,
            "checks": checks,
        }


# ---------------------------------------------------------------------------
# Multi-position selection / allocation
# ---------------------------------------------------------------------------

def rank_actionable(
    candidates: list[Candidate],
    engine: Optional[V853EntryEngine] = None,
) -> list[Tuple[Candidate, EntryDecision]]:
    engine = engine or V853EntryEngine()

    out = []
    for c in candidates:
        d = engine.evaluate(c)
        if d.action in (
            EntryAction.ENTER_NOW,
            EntryAction.ENTER_ON_PULLBACK,
        ):
            out.append((c, d))

    # Highest opportunity first, not first scanned.
    out.sort(key=lambda x: x[1].final_score, reverse=True)
    return out


def allocate_margin(
    actionable: list[Tuple[Candidate, EntryDecision]],
    available_margin: float,
    risk_budget: float,
    margin_per_share_fn,
    risk_per_share_fn,
) -> list[Dict[str, Any]]:
    """
    No arbitrary "maximum 3 trades" rule.

    Every qualified position can compete for available capital.

    The actual Dhan margin-per-share function MUST be supplied by the
    production gateway because leverage/margin varies by instrument,
    product type and broker state.

    Allocation priority:
        1. actionable score
        2. risk affordability
        3. margin availability

    This avoids splitting capital mechanically while still allowing
    multiple simultaneous high-quality trades when capital supports them.
    """
    remaining_margin = max(0.0, available_margin)
    remaining_risk = max(0.0, risk_budget)
    result = []

    for c, decision in actionable:
        mps = max(0.01, float(margin_per_share_fn(c)))
        rps = max(0.01, float(risk_per_share_fn(c)))

        qty_by_margin = floor(remaining_margin / mps)
        qty_by_risk = floor(remaining_risk / rps)
        qty = min(qty_by_margin, qty_by_risk)

        if qty <= 0:
            continue

        used_margin = qty * mps
        used_risk = qty * rps

        result.append({
            "symbol": c.symbol,
            "side": c.side,
            "qty": qty,
            "score": decision.final_score,
            "quality_score": decision.quality_score,
            "timing_score": decision.timing_score,
            "phase": decision.phase.value,
            "estimated_margin": used_margin,
            "estimated_risk": used_risk,
        })

        remaining_margin -= used_margin
        remaining_risk -= used_risk

        if remaining_margin <= 0 or remaining_risk <= 0:
            break

    return result


# ---------------------------------------------------------------------------
# Required audit output
# ---------------------------------------------------------------------------

def audit_record(c: Candidate, d: EntryDecision) -> Dict[str, Any]:
    return {
        "symbol": c.symbol,
        "side": c.side,
        "ltp": c.ltp,
        "raw_score": c.raw_score,
        "quality_score": round(d.quality_score, 2),
        "timing_score": round(d.timing_score, 2),
        "final_score": round(d.final_score, 2),
        "phase": d.phase.value,
        "setup_type": c.setup_type,
        "rs": c.rs,
        "rvol": c.rvol,
        "momentum_5m": c.momentum_5m,
        "momentum_15m": c.momentum_15m,
        "entry_action": d.action.value,
        "reason": d.reason,
        "diagnostics": d.diagnostics,
    }


# ---------------------------------------------------------------------------
# Replay-oriented tests from the supplied 21-Aug log patterns
# ---------------------------------------------------------------------------

def run_self_tests() -> str:
    e = V853EntryEngine()

    # POWERGRID: raw score high, but initial RVOL 0.368.
    # It must NOT become an immediate trade from raw score alone.
    powergrid = Candidate(
        "POWERGRID", "LONG", 274.40, 1.11, 75.60,
        3.70, 0.368, 269.43, 0.183, 0.406,
        274.0, "ORB_BREAKOUT", "NORMAL"
    )
    pd = e.evaluate(powergrid)
    assert pd.action != EntryAction.ENTER_NOW, pd

    # SOUTHWEST: strong early continuation profile.
    southwest = Candidate(
        "SOUTHWEST", "LONG", 219.74, 0.81, 74.88,
        2.03, 2.17, 218.71, 0.573, 0.587,
        219.0, "ORB_BREAKOUT", "NORMAL",
        expected_move_pct=2.0
    )
    sd = e.evaluate(southwest)
    assert sd.action == EntryAction.ENTER_NOW, sd

    # ZUARIIND: strong bearish evidence despite raw score only 66.72.
    zuari = Candidate(
        "ZUARIIND", "SHORT", 278.70, 1.0, 66.72,
        -2.54, 1.91, 281.05, -0.464, -0.429,
        279.0, "MOMENTUM_CONTINUATION", "NORMAL",
        expected_move_pct=1.5
    )
    zd = e.evaluate(zuari)
    assert zd.action == EntryAction.ENTER_NOW, zd

    # COCHINSHIP: exceptionally strong participation + bearish momentum.
    cochin = Candidate(
        "COCHINSHIP", "SHORT", 1473.90, 7.0, 75.46,
        -1.01, 9.06, 1482.16, -0.88, -1.49,
        1475.0, "ORB_BREAKDOWN", "NORMAL",
        expected_move_pct=2.5
    )
    cd = e.evaluate(cochin)
    assert cd.action == EntryAction.ENTER_NOW, cd

    # SUPREMEINF: high score + strong RS/RVOL/momentum.
    supreme = Candidate(
        "SUPREMEINF", "LONG", 89.51, 0.56, 80.83,
        4.13, 2.04, 88.87, 1.88, 0.56,
        89.0, "ORB_BREAKOUT", "NORMAL",
        expected_move_pct=1.5
    )
    sd2 = e.evaluate(supreme)
    assert sd2.action == EntryAction.ENTER_NOW, sd2

    # Dhan SL recovery.
    s, reason = recover_stop_after_dhan_rejection(
        "LONG", 101.0, 102.0, 2.0
    )
    assert s is not None and s < 101.0
    assert reason == "STOP_CROSSED_RECOVERED_BELOW_LTP"

    s, reason = recover_stop_after_dhan_rejection(
        "SHORT", 99.0, 98.0, 2.0
    )
    assert s is not None and s > 99.0
    assert reason == "STOP_CROSSED_RECOVERED_ABOVE_LTP"

    # One weak indicator must not force exit.
    p = Position(
        "TEST", "LONG", 100.0, 97.0, 2.0, 106.0
    )
    x = ExitEngine().evaluate(
        p, 105.5,
        {
            "momentum_5m": -0.1,
            "momentum_15m": 0.3,
            "vwap": 104.0,
            "rs": 1.5,
            "structure_break": False,
            "volume_climax": False,
            "price_progress_stalling": False,
            "setup_invalidated": False,
        }
    )
    assert x["action"] != ExitAction.EXIT.value, x

    return "7 self-tests passed"


if __name__ == "__main__":
    print(run_self_tests())
