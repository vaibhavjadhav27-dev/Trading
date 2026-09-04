"""
V12 Exit Engine — Volatility-Adjusted Trailing with Reversal Confirmation
==========================================================================

DROP-IN REPLACEMENT for V10_1_STRATEGY_PATCH.profit_fading_exit().
Same input format (snapshot_dict), same output format (action dict).

Key changes from V10.1:
  1. Volatility-adjusted giveback (not fixed 15% or fixed R thresholds)
  2. Composite reversal score (not binary MOM5_REVERSED)
  3. HOLD override (the IDBI rule: RVOL>5 + pullback<1R + structure intact)
  4. Volume context (high vol + absorption vs high vol + breakdown)
  5. Time-decay awareness (45-min no-progress flag)

What it IMPORTS from V10.1 (not duplicates):
  - ManagedPosition, Segment, EntryType, PositionAction
  - Peak/trough/MFE/MAE tracking via the position object

Version: 12.0.0
Date: 2026-09-02
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

log = logging.getLogger("V12_EXIT")

# ─── Import from V10.1 (NO duplication) ─────────────────────────────
try:
    from V10_1_STRATEGY_PATCH import (
        ManagedPosition, Segment, EntryType, PositionAction,
    )
    _V10_AVAILABLE = True
except ImportError:
    _V10_AVAILABLE = False
    log.warning("V10_1_STRATEGY_PATCH not available — using local compat stubs")

    # Minimal compat stubs if V10.1 not importable (for testing only)
    class PositionAction:
        HOLD = "HOLD"
        TIGHTEN = "TIGHTEN"
        EXIT = "EXIT"
        CONFIRM_ADD = "CONFIRM_ADD"

    class Segment:
        NSE_INTRADAY = "NSE_INTRADAY"

    class EntryType:
        NORMAL = "NORMAL"
        EARLY = "EARLY"


# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

# Reversal score component weights (sum = 1.0)
W_MOMENTUM_REV = 0.25
W_STRUCTURE_DET = 0.25
W_PEAK_GIVEBACK = 0.20
W_VWAP_FAILURE = 0.15
W_VOLUME_CONFIRM = 0.15

# Reversal score thresholds
REVERSAL_TIGHTEN_THRESHOLD = 55  # >= 55: tighten SL
REVERSAL_EXIT_THRESHOLD = 75     # >= 75: immediate exit

# Volatility tiers for giveback
# (atr_pct_low, atr_pct_high, giveback_low, giveback_high)
VOL_TIERS = [
    (0.0, 1.0, 0.15, 0.20),   # Low vol
    (1.0, 2.0, 0.25, 0.30),   # Normal vol
    (2.0, 5.0, 0.30, 0.35),   # High vol
]

# IDBI Rule thresholds
RVOL_HOLD_THRESHOLD = 5.0       # RVOL > 5 → candidate for hold override
PULLBACK_HOLD_MAX_R = 1.0       # Pullback < 1R → hold
PEAK_R_MIN_FOR_HOLD = 0.0       # Any positive peak qualifies

# Protection arming
PROTECT_ARM_R = 0.50             # MFE >= 0.5R → protection armed
TRAIL_ARM_R = 1.00               # MFE >= 1.0R → trailing active

# Time decay
NO_PROGRESS_MINUTES = 45         # 45 min with no new peak → flag

# Tightened stop milestones (same structure as V10.1, used as floor)
STOP_MILESTONES = [
    # (peak_r_threshold, protect_at_r)
    (3.0, 2.25),
    (2.5, 1.75),
    (2.0, 1.25),
    (1.5, 0.75),
    (1.0, 0.0),  # Breakeven
]


# ═══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def _safe(val, default=0.0) -> float:
    """Safely convert to float."""
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation: map x from [x0,x1] to [y0,y1], clamped."""
    if x1 == x0:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


# ═══════════════════════════════════════════════════════════════════════
# REVERSAL SCORE COMPONENTS
# ═══════════════════════════════════════════════════════════════════════

def _score_momentum_reversal(side: str, mom5: float, mom15: float) -> float:
    """Score momentum reversal (0-100).

    Both MOM5 + MOM15 against position = strong reversal signal.
    One against = moderate. Both aligned = no reversal.
    """
    # Direction-normalize: positive = favorable, negative = against position
    if side == "LONG":
        m5 = mom5
        m15 = mom15
    else:
        m5 = -mom5
        m15 = -mom15

    score = 0
    # MOM5 against position (most recent)
    if m5 < -0.5:
        score += 40   # Strong against
    elif m5 < 0:
        score += 20   # Mild against

    # MOM15 against position (trend confirmation)
    if m15 < -0.5:
        score += 40
    elif m15 < 0:
        score += 20

    # Acceleration: both strongly against = extra confirmation
    if m5 < -0.5 and m15 < -0.5:
        score += 20

    return min(100, score)


def _score_structure_deterioration(
    side: str, price: float, vwap: float,
    structural_inv: bool, candle_rev: bool,
) -> float:
    """Score structural deterioration (0-100).

    Checks: structural invalidation, candle reversal, price vs VWAP.
    """
    score = 0

    # Structural invalidation (price below SL) — most severe
    if structural_inv:
        score += 50

    # Candle reversal (2-bar direction change)
    if candle_rev:
        score += 25

    # Price vs VWAP
    if vwap > 0:
        if side == "LONG" and price < vwap:
            score += 25  # Below VWAP for long
        elif side == "SHORT" and price > vwap:
            score += 25

    return min(100, score)


def _score_giveback(
    giveback_r: float, peak_r: float, max_giveback_fraction: float,
) -> float:
    """Score peak giveback relative to volatility-adjusted threshold (0-100).

    0 if giveback < 50% of threshold, 100 if giveback > 150% of threshold.
    """
    if peak_r <= 0 or max_giveback_fraction <= 0:
        return 0

    threshold_r = peak_r * max_giveback_fraction
    if threshold_r <= 0:
        return 0

    ratio = giveback_r / threshold_r
    if ratio < 0.5:
        return 0
    if ratio < 1.0:
        return _lerp(ratio, 0.5, 1.0, 0, 60)
    if ratio < 1.5:
        return _lerp(ratio, 1.0, 1.5, 60, 100)
    return 100


def _score_vwap_failure(
    side: str, price: float, vwap: float, entry_price: float,
) -> float:
    """Score VWAP failure (0-100).

    Higher score when price has crossed VWAP against position.
    """
    if vwap <= 0 or entry_price <= 0:
        return 0

    if side == "LONG":
        # Started above VWAP, now below
        was_above = entry_price > vwap
        now_below = price < vwap
        if was_above and now_below:
            depth = (vwap - price) / vwap * 100
            return min(100, 50 + depth * 20)
        if now_below:
            return 30
        return 0
    else:
        was_below = entry_price < vwap
        now_above = price > vwap
        if was_below and now_above:
            depth = (price - vwap) / vwap * 100
            return min(100, 50 + depth * 20)
        if now_above:
            return 30
        return 0


def _score_volume_confirmation(
    side: str, volume_reversal: bool, rvol: float, candle_rev: bool,
) -> float:
    """Score volume confirmation of reversal (0-100).

    High volume + against position + reversal candle = strong confirmation.
    High volume + WITH position (absorption) = bullish hold signal.
    """
    score = 0

    # Volume reversal (direction-aware surge against position)
    if volume_reversal:
        score += 50

    # High RVOL during reversal candle = confirmation
    if candle_rev and rvol >= 2.0:
        score += 30
    elif rvol >= 3.0 and not volume_reversal:
        # High volume but NOT against position → absorption → REDUCE score
        score -= 20

    return max(0, min(100, score))


# ═══════════════════════════════════════════════════════════════════════
# MAIN CLASS
# ═══════════════════════════════════════════════════════════════════════

class V12ExitEngine:
    """V12 Exit Engine — volatility-adjusted trailing with reversal confirmation."""

    def __init__(self):
        self._version = "V12_EXIT_ENGINE_2026_09_02"

    def volatility_adjusted_giveback(self, atr_pct: float, peak_r: float) -> float:
        """Return max allowed giveback FRACTION based on volatility.

        Low vol (ATR<1%): 0.15-0.20
        Normal (1-2%): 0.25-0.30
        High (>2%): 0.30-0.35

        Linear interpolation within tiers.
        """
        atr_pct = max(0, atr_pct)

        for low, high, gb_low, gb_high in VOL_TIERS:
            if atr_pct <= high:
                return _lerp(atr_pct, low, high, gb_low, gb_high)

        # Beyond all tiers
        return 0.35

    def should_hold_override(
        self,
        rvol: float,
        pullback_r: float,
        structure_intact: bool,
        peak_r: float,
        volume_reversal: bool,
    ) -> Tuple[bool, str]:
        """THE IDBI RULE: HOLD if strong volume + shallow pullback + intact structure.

        RVOL > 5 + pullback < 1R + structure intact → HOLD.
        UNLESS volume is confirmed bearish (volume_reversal=True for longs).

        Returns: (should_hold, reason)
        """
        # Gate: must have meaningful positive peak
        if peak_r <= PEAK_R_MIN_FOR_HOLD:
            return False, "NO_PEAK"

        # Gate: RVOL must be strong
        if rvol < RVOL_HOLD_THRESHOLD:
            return False, f"RVOL_LOW ({rvol:.1f})"

        # Gate: pullback must be shallow
        if pullback_r > PULLBACK_HOLD_MAX_R:
            return False, f"PULLBACK_DEEP ({pullback_r:.2f}R)"

        # Gate: structure must be intact
        if not structure_intact:
            return False, "STRUCTURE_BROKEN"

        # CRITICAL: volume reversal overrides hold
        # High RVOL + bearish price action = distribution, not accumulation
        if volume_reversal:
            return False, "VOLUME_REVERSAL_OVERRIDES"

        return True, f"RVOL_HOLD (rvol={rvol:.1f} pullback={pullback_r:.2f}R)"

    def compute_reversal_score(
        self,
        side: str,
        price: float,
        snapshot: dict,
        peak_r: float,
        giveback_r: float,
        entry_price: float,
        atr_pct: float,
    ) -> dict:
        """Compute composite reversal score (0-100).

        Returns: {reversal_score, components, hold_override, hold_reason}
        """
        mom5 = _safe(snapshot.get("mom_5m", snapshot.get("momentum_5m", 0)))
        mom15 = _safe(snapshot.get("mom_15m", snapshot.get("momentum_15m", 0)))
        vwap = _safe(snapshot.get("vwap", 0))
        rvol = _safe(snapshot.get("rvol", 1.0))
        structural_inv = bool(snapshot.get("structural_invalidated", False))
        candle_rev = bool(snapshot.get("candle_reversal", False))
        volume_rev = bool(snapshot.get("volume_reversal", False))

        # Compute max giveback fraction for this volatility
        max_gb = self.volatility_adjusted_giveback(atr_pct, peak_r)

        # Score each component (0-100)
        c_momentum = _score_momentum_reversal(side, mom5, mom15)
        c_structure = _score_structure_deterioration(side, price, vwap, structural_inv, candle_rev)
        c_giveback = _score_giveback(giveback_r, peak_r, max_gb)
        c_vwap = _score_vwap_failure(side, price, vwap, entry_price)
        c_volume = _score_volume_confirmation(side, volume_rev, rvol, candle_rev)

        # Weighted composite
        reversal_score = (
            c_momentum * W_MOMENTUM_REV
            + c_structure * W_STRUCTURE_DET
            + c_giveback * W_PEAK_GIVEBACK
            + c_vwap * W_VWAP_FAILURE
            + c_volume * W_VOLUME_CONFIRM
        )

        # Check hold override
        pullback_r = max(0, peak_r - (peak_r - giveback_r)) if peak_r > 0 else 0
        # Actually pullback_r = giveback_r (how far we've fallen from peak)
        pullback_r = giveback_r
        structure_intact = not structural_inv

        hold_override, hold_reason = self.should_hold_override(
            rvol, pullback_r, structure_intact, peak_r, volume_rev,
        )

        return {
            "reversal_score": round(reversal_score, 1),
            "components": {
                "momentum": round(c_momentum, 1),
                "structure": round(c_structure, 1),
                "giveback": round(c_giveback, 1),
                "vwap": round(c_vwap, 1),
                "volume": round(c_volume, 1),
            },
            "hold_override": hold_override,
            "hold_reason": hold_reason,
            "max_giveback_fraction": round(max_gb, 3),
            "atr_pct": round(atr_pct, 3),
        }

    def compute_tightened_stop_v12(
        self,
        side: str,
        entry_price: float,
        risk_per_share: float,
        peak_r: float,
        atr_pct: float,
    ) -> float:
        """Volatility-adjusted trailing stop.

        Uses V10.1's milestone structure as a FLOOR, then adjusts
        the protection level based on volatility.
        """
        # V10.1 milestone floor
        protect_r = 0.0
        for threshold, protect in STOP_MILESTONES:
            if peak_r >= threshold:
                protect_r = protect
                break

        # Volatility adjustment: tighter in low vol, looser in high vol
        gb_fraction = self.volatility_adjusted_giveback(atr_pct, peak_r)
        # Alternative protection: peak_r minus volatility-adjusted giveback
        vol_protect_r = peak_r * (1.0 - gb_fraction)

        # Use the MORE protective (higher for longs, lower for shorts)
        final_protect_r = max(protect_r, vol_protect_r)

        if side in ("LONG", "BUY"):
            new_stop = entry_price + (final_protect_r * risk_per_share)
        else:
            new_stop = entry_price - (final_protect_r * risk_per_share)

        return round(new_stop, 2)

    def evaluate(self, snapshot_dict: dict) -> dict:
        """Main exit evaluation. Returns V10.1-compatible dict.

        This is the core logic that replaces V10.1's update_position + giveback tiers.

        Flow:
        1. Parse position from snapshot_dict
        2. Update peak/trough/MFE/MAE
        3. Check structural invalidation → EXIT
        4. Check hard stop → EXIT
        5. Check early entry confirmation → CONFIRM_ADD
        6. Compute reversal score
        7. Check hold override (IDBI rule)
        8. If hold override → HOLD regardless
        9. If reversal_score >= threshold → TIGHTEN or EXIT
        10. Time decay check
        11. Default: HOLD
        """
        try:
            # ── Parse inputs ──
            price = _safe(snapshot_dict.get("price", 0))
            entry = _safe(snapshot_dict.get("entry_price", 0))
            stop = _safe(snapshot_dict.get("sl", snapshot_dict.get("stop_price", 0)))
            initial_sl = _safe(snapshot_dict.get("initial_sl", stop))
            side = str(snapshot_dict.get("side", "BUY")).upper()
            if side == "LONG":
                side = "BUY"
            elif side == "SHORT":
                side = "SELL"

            # Risk calculation
            risk = max(abs(entry - initial_sl), 0.01)

            # Current R
            if side == "BUY":
                move = price - entry
            else:
                move = entry - price
            current_r = move / risk

            # Peak tracking
            peak_price = _safe(snapshot_dict.get("peak", 0))
            if peak_price <= 0:
                peak_price = price
            if side == "BUY":
                peak_price = max(peak_price, price)
                mfe = max(_safe(snapshot_dict.get("mfe", 0)), max(0, price - entry))
                mae = max(_safe(snapshot_dict.get("mae", 0)), max(0, entry - price))
            else:
                peak_price = min(peak_price, price) if peak_price > 0 else price
                mfe = max(_safe(snapshot_dict.get("mfe", 0)), max(0, entry - price))
                mae = max(_safe(snapshot_dict.get("mae", 0)), max(0, price - entry))

            peak_r = max(_safe(snapshot_dict.get("peak_r", snapshot_dict.get("best_r", 0))), current_r)
            giveback_r = max(0, peak_r - current_r)

            # ATR percentage for volatility adjustment
            atr_pct = 0.0
            if entry > 0:
                # Estimate from initial_sl distance or snapshot
                atr_pct = abs(entry - initial_sl) / entry * 100

            # ── 1. Structural invalidation → EXIT ──
            structural_inv = bool(snapshot_dict.get("structural_invalidated", False))
            if structural_inv:
                return self._result("EXIT", current_r, peak_r, giveback_r, mfe, mae,
                                    _safe(snapshot_dict.get("last_tighten_r", 0)),
                                    reason="STRUCTURAL_INVALIDATION")

            # ── 2. Hard stop check → EXIT ──
            if side == "BUY" and price <= stop and stop > 0:
                return self._result("EXIT", current_r, peak_r, giveback_r, mfe, mae,
                                    _safe(snapshot_dict.get("last_tighten_r", 0)),
                                    reason="HARD_STOP_HIT")
            if side == "SELL" and price >= stop and stop > 0:
                return self._result("EXIT", current_r, peak_r, giveback_r, mfe, mae,
                                    _safe(snapshot_dict.get("last_tighten_r", 0)),
                                    reason="HARD_STOP_HIT")

            # ── 3. Early entry confirmation ──
            confirmed = snapshot_dict.get("confirmed", True)
            entry_type = str(snapshot_dict.get("entry_type", "NORMAL"))
            if not confirmed and entry_type == "EARLY":
                if current_r >= 0.5:
                    return self._result("CONFIRM_ADD", current_r, peak_r, giveback_r, mfe, mae,
                                        _safe(snapshot_dict.get("last_tighten_r", 0)),
                                        reason="EARLY_CONFIRMED_0.5R")

            # ── 4. Compute reversal score ──
            rev_result = self.compute_reversal_score(
                side=side,
                price=price,
                snapshot=snapshot_dict,
                peak_r=peak_r,
                giveback_r=giveback_r,
                entry_price=entry,
                atr_pct=atr_pct,
            )

            reversal_score = rev_result["reversal_score"]
            hold_override = rev_result["hold_override"]
            hold_reason = rev_result["hold_reason"]

            # ── 5. HOLD override (IDBI rule) ──
            if hold_override:
                return self._result("HOLD", current_r, peak_r, giveback_r, mfe, mae,
                                    _safe(snapshot_dict.get("last_tighten_r", 0)),
                                    reason=f"HOLD_OVERRIDE: {hold_reason}",
                                    reversal_score=reversal_score,
                                    reversal_components=rev_result["components"])

            # ── 5b. HARD PEAK-GIVEBACK EXIT (data-proven +1R, IDBI-protected) ──
            # Fires AFTER the IDBI hold override (step 5), so genuine high-RVOL
            # runners are already protected. On a normal fade, exit near peak
            # instead of riding back to EOD. Volatility-adjusted giveback.
            _max_gb_exit = self.volatility_adjusted_giveback(atr_pct, peak_r)
            if peak_r >= TRAIL_ARM_R and _max_gb_exit > 0:
                _gb_threshold_r = peak_r * _max_gb_exit
                if giveback_r > _gb_threshold_r:
                    return self._result("EXIT", current_r, peak_r, giveback_r, mfe, mae,
                                        _safe(snapshot_dict.get("last_tighten_r", 0)),
                                        reason=f"PEAK_GIVEBACK_TRAIL (gb={giveback_r:.2f}R > {_gb_threshold_r:.2f}R peak={peak_r:.2f}R)",
                                        reversal_score=reversal_score,
                                        reversal_components=rev_result["components"])

            # ── 6. Protection arming ──
            last_tighten_r = _safe(snapshot_dict.get("last_tighten_r", 0))

            # Protection not yet armed
            if peak_r < PROTECT_ARM_R:
                return self._result("HOLD", current_r, peak_r, giveback_r, mfe, mae,
                                    last_tighten_r,
                                    reason="PROTECTION_NOT_ARMED",
                                    reversal_score=reversal_score,
                                    reversal_components=rev_result["components"])

            # ── 7. Reversal-confirmed exit ──
            if reversal_score >= REVERSAL_EXIT_THRESHOLD and peak_r >= TRAIL_ARM_R:
                return self._result("EXIT", current_r, peak_r, giveback_r, mfe, mae,
                                    last_tighten_r,
                                    reason=f"REVERSAL_CONFIRMED (score={reversal_score:.0f})",
                                    reversal_score=reversal_score,
                                    reversal_components=rev_result["components"])

            # ── 8. Reversal-based tightening ──
            if reversal_score >= REVERSAL_TIGHTEN_THRESHOLD and peak_r >= TRAIL_ARM_R:
                new_stop = self.compute_tightened_stop_v12(
                    side, entry, risk, peak_r, atr_pct,
                )
                # Only tighten if new stop is better than current
                if side == "BUY" and new_stop > stop:
                    return self._result("TIGHTEN", current_r, peak_r, giveback_r, mfe, mae,
                                        peak_r,  # Update last_tighten_r to peak
                                        new_stop=new_stop,
                                        reason=f"REVERSAL_TIGHTEN (score={reversal_score:.0f})",
                                        reversal_score=reversal_score,
                                        reversal_components=rev_result["components"])
                elif side == "SELL" and new_stop < stop:
                    return self._result("TIGHTEN", current_r, peak_r, giveback_r, mfe, mae,
                                        peak_r,
                                        new_stop=new_stop,
                                        reason=f"REVERSAL_TIGHTEN (score={reversal_score:.0f})",
                                        reversal_score=reversal_score,
                                        reversal_components=rev_result["components"])

            # ── 9. Giveback-only tightening (no reversal confirmation needed at high giveback) ──
            max_gb = rev_result["max_giveback_fraction"]
            if peak_r >= TRAIL_ARM_R and max_gb > 0:
                threshold_r = peak_r * max_gb
                if giveback_r > threshold_r * 1.5:
                    # Extreme giveback (150% of threshold) — tighten regardless
                    new_stop = self.compute_tightened_stop_v12(
                        side, entry, risk, peak_r, atr_pct,
                    )
                    if (side == "BUY" and new_stop > stop) or (side == "SELL" and new_stop < stop):
                        return self._result("TIGHTEN", current_r, peak_r, giveback_r, mfe, mae,
                                            peak_r, new_stop=new_stop,
                                            reason=f"EXTREME_GIVEBACK ({giveback_r:.2f}R > {threshold_r*1.5:.2f}R)")

            # ── 10. Default: HOLD ──
            return self._result("HOLD", current_r, peak_r, giveback_r, mfe, mae,
                                last_tighten_r,
                                reason="HOLD_TREND",
                                reversal_score=reversal_score,
                                reversal_components=rev_result["components"])

        except Exception as e:
            log.error(f"V12 exit evaluation error: {e}")
            return {
                "action": "HOLD", "exit_now": False, "profit_fading": False,
                "confirm_add": False, "reason": f"ERROR: {e}",
            }

    def _result(
        self,
        action: str,
        current_r: float,
        peak_r: float,
        giveback_r: float,
        mfe: float,
        mae: float,
        last_tighten_r: float,
        new_stop: float = 0,
        reason: str = "",
        reversal_score: float = 0,
        reversal_components: dict = None,
    ) -> dict:
        """Build V10.1-compatible result dict."""
        result = {
            "action": action,
            "current_r": round(current_r, 3),
            "peak_r": round(peak_r, 3),
            "giveback_r": round(giveback_r, 3),
            "mfe": round(mfe, 2),
            "mae": round(mae, 2),
            "last_tighten_r": last_tighten_r,
            # V8.5.5 compat booleans
            "exit_now": action == "EXIT",
            "exit": action == "EXIT",
            "r": round(current_r, 3),
            "retrace_r": round(giveback_r, 3),
            "profit_fading": action == "TIGHTEN",
            "confirm_add": action == "CONFIRM_ADD",
            # V12 additions
            "reason": reason,
            "reversal_score": round(reversal_score, 1),
            "reversal_components": reversal_components or {},
            "engine": "V12",
        }
        if action == "TIGHTEN" and new_stop > 0:
            result["new_stop"] = new_stop
        return result


# ═══════════════════════════════════════════════════════════════════════
# BACKWARD-COMPATIBLE WRAPPER (drop-in replacement)
# ═══════════════════════════════════════════════════════════════════════

# Singleton engine instance
_v12_exit_engine = V12ExitEngine()


def profit_fading_exit(snapshot_dict: Dict[str, Any]) -> Dict[str, Any]:
    """DROP-IN REPLACEMENT for V10_1_STRATEGY_PATCH.profit_fading_exit.

    Same input format (snapshot_dict), same output format (action dict).
    Uses V12ExitEngine internally.

    Called at line 642 of trading_bot_v84.py.
    """
    return _v12_exit_engine.evaluate(snapshot_dict)


# ═══════════════════════════════════════════════════════════════════════
# SELF-TESTS
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("V12 Exit Engine — Self Tests")
    print("=" * 50)

    _counts = [0, 0]  # [passed, failed]


    def test(name, condition):

        if condition:
            _counts[0] += 1
            print(f"  ✅ {name}")
        else:
            _counts[1] += 1
            print(f"  ❌ {name}")

    engine = V12ExitEngine()

    # --- Test: volatility_adjusted_giveback ---
    gb_low = engine.volatility_adjusted_giveback(0.5, 1.5)
    test(f"giveback: low vol (0.5%) → {gb_low:.3f} (should be 0.15-0.20)", 0.14 <= gb_low <= 0.21)

    gb_normal = engine.volatility_adjusted_giveback(1.5, 1.5)
    test(f"giveback: normal vol (1.5%) → {gb_normal:.3f} (should be 0.25-0.30)", 0.24 <= gb_normal <= 0.31)

    gb_high = engine.volatility_adjusted_giveback(3.0, 1.5)
    test(f"giveback: high vol (3%) → {gb_high:.3f} (should be 0.30-0.35)", 0.29 <= gb_high <= 0.36)

    # --- Test: should_hold_override (IDBI rule) ---
    hold, reason = engine.should_hold_override(
        rvol=8.0, pullback_r=0.5, structure_intact=True, peak_r=1.5, volume_reversal=False,
    )
    test(f"IDBI hold: RVOL=8 pullback=0.5R → HOLD ({reason})", hold)

    hold2, reason2 = engine.should_hold_override(
        rvol=2.0, pullback_r=0.5, structure_intact=True, peak_r=1.5, volume_reversal=False,
    )
    test(f"IDBI hold: RVOL=2 → NO HOLD ({reason2})", not hold2)

    hold3, reason3 = engine.should_hold_override(
        rvol=8.0, pullback_r=1.5, structure_intact=True, peak_r=2.0, volume_reversal=False,
    )
    test(f"IDBI hold: deep pullback 1.5R → NO HOLD ({reason3})", not hold3)

    hold4, reason4 = engine.should_hold_override(
        rvol=8.0, pullback_r=0.5, structure_intact=True, peak_r=1.5, volume_reversal=True,
    )
    test(f"IDBI hold: volume_reversal=True → NO HOLD ({reason4})", not hold4)

    # --- Test: compute_reversal_score ---
    snapshot_bearish = {
        "mom_5m": -1.0, "mom_15m": -0.8, "vwap": 100, "rvol": 2.0,
        "structural_invalidated": False, "candle_reversal": True,
        "volume_reversal": True,
    }
    rev = engine.compute_reversal_score(
        "BUY", 98.0, snapshot_bearish, peak_r=2.0, giveback_r=1.0, entry_price=95.0, atr_pct=1.5,
    )
    test(f"reversal_score: bearish snapshot → {rev['reversal_score']:.0f} (should be high)", rev["reversal_score"] > 50)

    snapshot_bullish = {
        "mom_5m": 0.5, "mom_15m": 0.3, "vwap": 90, "rvol": 5.0,
        "structural_invalidated": False, "candle_reversal": False,
        "volume_reversal": False,
    }
    rev2 = engine.compute_reversal_score(
        "BUY", 95.0, snapshot_bullish, peak_r=1.5, giveback_r=0.3, entry_price=90.0, atr_pct=1.0,
    )
    test(f"reversal_score: bullish snapshot → {rev2['reversal_score']:.0f} (should be low)", rev2["reversal_score"] < 30)

    # --- Test: profit_fading_exit backward compatibility ---
    pfe_dict = {
        "symbol": "TEST", "side": "LONG", "price": 105.0,
        "entry_price": 100.0, "sl": 98.0, "initial_sl": 98.0,
        "peak": 107.0, "best_r": 3.5, "peak_r": 3.5, "giveback_r": 1.0,
        "qty": 100, "mfe": 7.0, "mae": 0.0, "last_tighten_r": 0,
        "entry_type": "NORMAL", "position_pct": 1.0, "confirmed": True,
        # Snapshot
        "vwap": 102.0, "mom_5m": 0.3, "mom_15m": 0.2, "rs": 1.5, "rvol": 3.0,
        "candle_reversal": False, "volume_reversal": False, "structural_invalidated": False,
    }
    result = profit_fading_exit(pfe_dict)
    test("pfe: returns dict", isinstance(result, dict))
    test("pfe: has 'action'", "action" in result)
    test("pfe: has 'exit_now'", "exit_now" in result)
    test("pfe: has 'profit_fading'", "profit_fading" in result)
    test("pfe: has 'current_r'", "current_r" in result)
    test("pfe: has 'peak_r'", "peak_r" in result)
    test("pfe: has 'giveback_r'", "giveback_r" in result)
    test("pfe: has 'mfe'", "mfe" in result)
    test("pfe: has 'mae'", "mae" in result)
    test(f"pfe: action={result['action']}", result["action"] in ("HOLD", "TIGHTEN", "EXIT", "CONFIRM_ADD"))
    print(f"    Full result: action={result['action']} reason={result.get('reason')} rev={result.get('reversal_score')}")

    # --- Test: Structural invalidation → EXIT ---
    pfe_structural = dict(pfe_dict, structural_invalidated=True)
    res_struct = profit_fading_exit(pfe_structural)
    test(f"structural invalidation → EXIT (got {res_struct['action']})", res_struct["action"] == "EXIT")

    # --- Test: Hard stop hit → EXIT ---
    pfe_stop = dict(pfe_dict, price=97.0)  # Below SL of 98
    res_stop = profit_fading_exit(pfe_stop)
    test(f"hard stop hit → EXIT (got {res_stop['action']})", res_stop["action"] == "EXIT")

    # --- Test: IDBI scenario — high RVOL hold ---
    pfe_idbi = dict(pfe_dict,
        rvol=8.16, mom_5m=-0.3, candle_reversal=True,
        peak_r=0.78, giveback_r=0.5, price=91.5, entry_price=91.8,
        sl=91.0, initial_sl=91.0, peak=92.5, best_r=0.78,
    )
    res_idbi = profit_fading_exit(pfe_idbi)
    test(f"IDBI scenario: RVOL=8.16 → should HOLD (got {res_idbi['action']}, reason={res_idbi.get('reason')})",
         res_idbi["action"] == "HOLD")

    # --- Test: Strong reversal with confirmation ---
    pfe_reversal = dict(pfe_dict,
        mom_5m=-1.5, mom_15m=-1.0, candle_reversal=True, volume_reversal=True,
        peak_r=2.0, giveback_r=1.2, price=98.0, entry_price=100.0,
        sl=97.0, initial_sl=97.0, peak=106.0, best_r=2.0,
        vwap=102.0, rvol=2.0,
    )
    res_rev = profit_fading_exit(pfe_reversal)
    test(f"strong reversal → TIGHTEN or EXIT (got {res_rev['action']})",
         res_rev["action"] in ("TIGHTEN", "EXIT"))

    # --- Test: Early entry confirmation ---
    pfe_early = dict(pfe_dict,
        entry_type="EARLY", confirmed=False,
        price=101.5, entry_price=100.0, initial_sl=99.0, sl=99.0,
        peak_r=0, giveback_r=0,
    )
    res_early = profit_fading_exit(pfe_early)
    test(f"early entry: 1.5R → CONFIRM_ADD (got {res_early['action']})",
         res_early["action"] == "CONFIRM_ADD")

    # --- Test: SHORT position ---
    pfe_short = {
        "symbol": "TEST", "side": "SHORT", "price": 95.0,
        "entry_price": 100.0, "sl": 102.0, "initial_sl": 102.0,
        "peak": 94.0, "best_r": 3.0, "peak_r": 3.0, "giveback_r": 0.5,
        "qty": 100, "mfe": 6.0, "mae": 0.0, "last_tighten_r": 0,
        "entry_type": "NORMAL", "position_pct": 1.0, "confirmed": True,
        "vwap": 98.0, "mom_5m": -0.5, "mom_15m": -0.3, "rs": -1.5, "rvol": 3.0,
        "candle_reversal": False, "volume_reversal": False, "structural_invalidated": False,
    }
    res_short = profit_fading_exit(pfe_short)
    test(f"SHORT: profitable hold → HOLD (got {res_short['action']})", res_short["action"] == "HOLD")

    # --- Test: V12 engine field present ---
    test("result contains 'engine': 'V12'", result.get("engine") == "V12")
    test("result contains reversal_score", "reversal_score" in result)

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed == 0:
        print("ALL TESTS PASSED ✅")
    else:
        print(f"⚠️  {failed} FAILURES")
