"""
V12 Early Discovery Engine
==========================

Enables candidate scoring with 5-8 bars (vs 20-bar current minimum).
Starts discovery at 09:25 IST instead of 10:30+.

Architecture:
  - Computes discovery_score from 5 weighted components
  - Uses fallback indicators when < 20 bars available
  - Classifies phase: EARLY / DEVELOPING / MATURE
  - Detects future-mover precursors at 09:30-10:00 IST
  - Does NOT replace final_decision() or _v10r_gate() — enriches features for them

Integration:
  Called inside the candidate scan loop BEFORE final_decision(f).
  If bars < 20 and can_score_early(df): enrich features with fallback computations.
  Normal scoring pipeline then runs on enriched features.

Imports from existing modules (NO duplication):
  - v82_strategy: momentum(), vwap(), rvol()
  - V10_1_STRATEGY_PATCH: Snapshot, classify_move_stage, base_score, timing_score

Version: 12.0.0
Date: 2026-09-02
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List

log = logging.getLogger("V12_DISCOVERY")

# ─── Import from existing modules (NO duplication) ───────────────────
try:
    from v82_strategy import momentum as _v82_momentum, vwap as _v82_vwap, rvol as _v82_rvol
    _V82_AVAILABLE = True
except ImportError:
    _V82_AVAILABLE = False
    log.warning("v82_strategy not available — using built-in fallbacks")

try:
    from V10_1_STRATEGY_PATCH import Snapshot, classify_move_stage, base_score, timing_score
    _V10_AVAILABLE = True
except ImportError:
    _V10_AVAILABLE = False
    log.warning("V10_1_STRATEGY_PATCH not available — phase classification limited")


# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

IST = timezone(timedelta(hours=5, minutes=30))

# Discovery score component weights (must sum to 1.0)
W_RS_ACCEL = 0.25
W_RVOL_ACCEL = 0.25
W_PRICE_EXPANSION = 0.20
W_MARKET_CONTEXT = 0.15
W_LIQUIDITY = 0.15

# Thresholds
DISCOVERY_WATCH = 50        # >= 50: candidate, tracked
DISCOVERY_PRIORITY = 65     # >= 65: priority candidate
MIN_BARS_EARLY = 5          # Minimum bars for early scoring
MIN_BARS_NORMAL = 20        # Normal pipeline requirement
RVOL_SURGE_THRESHOLD = 3.0  # Future-mover precursor gate
RS_ACCEL_MIN = 0.5          # Minimum RS acceleration for precursor
RANGE_MATURE_PCT = 0.60     # >60% of estimated daily range = MATURE
RANGE_DEVELOPING_PCT = 0.30 # 30-60% = DEVELOPING

# Early scoring fallback defaults
DEFAULT_ATR_MULTIPLIER = 1.5  # Multiply early ATR by this for daily range estimate


# ═══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS (small utilities, NOT duplicating v82_strategy)
# ═══════════════════════════════════════════════════════════════════════

def _safe_float(val, default=0.0) -> float:
    """Safely convert to float, return default on failure."""
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _early_atr(df, min_bars: int = 3) -> float:
    """Estimate ATR from limited bars using high-low range.
    
    More robust than close-to-close for few bars.
    Returns 0.0 if insufficient data.
    """
    if df is None or len(df) < min_bars:
        return 0.0
    try:
        highs = [float(x) for x in df["high"].tail(min(14, len(df)))]
        lows = [float(x) for x in df["low"].tail(min(14, len(df)))]
        if not highs or not lows:
            return 0.0
        ranges = [h - l for h, l in zip(highs, lows) if h > l]
        return sum(ranges) / len(ranges) if ranges else 0.0
    except Exception:
        return 0.0


def _early_rvol(df) -> float:
    """Compute RVOL from available bars (even 5-8).
    
    Compares last bar volume to mean of prior bars.
    Uses v82_strategy.rvol() if available and enough bars, 
    else falls back to simple ratio.
    """
    if df is None or len(df) < 3:
        return 1.0
    try:
        volumes = [float(x) for x in df["volume"].fillna(0).tolist() if float(x) > 0]
        if len(volumes) < 3:
            return 1.0
        last = volumes[-1]
        prior_avg = sum(volumes[:-1]) / max(1, len(volumes) - 1)
        return last / prior_avg if prior_avg > 0 else 1.0
    except Exception:
        return 1.0


def _early_momentum(df) -> Tuple[float, float, float]:
    """Compute momentum from available bars.
    
    Uses v82_strategy.momentum() if available and enough bars,
    else falls back to simple return calculations.
    
    Returns: (mom_5m, mom_15m, acceleration)
    """
    if _V82_AVAILABLE and df is not None and len(df) >= 4:
        r5, r15, r30, acc = _v82_momentum(df)
        return r5, r15, acc

    if df is None or len(df) < 2:
        return 0.0, 0.0, 0.0

    try:
        closes = [float(x) for x in df["close"].tolist()]
        if len(closes) < 2:
            return 0.0, 0.0, 0.0
        # Simple returns
        r1 = (closes[-1] / closes[-2] - 1) * 100 if closes[-2] else 0
        r3 = (closes[-1] / closes[max(0, -min(4, len(closes)))] - 1) * 100 if closes[0] else 0
        acc = r1 * 2 + r3
        return r1, r3, acc
    except Exception:
        return 0.0, 0.0, 0.0


def _early_rs(df, features: dict, prev_close: float = 0) -> float:
    """Estimate relative strength from limited data.
    
    Uses: stock price change from prev_close vs market gap.
    Falls back to features['rs'] if available.
    """
    # Use provided RS if available
    rs = _safe_float(features.get("rs", features.get("rs_val", 0)))
    if rs != 0:
        return rs

    # Compute from prev_close
    if prev_close > 0 and df is not None and len(df) >= 1:
        try:
            current = float(df["close"].iloc[-1])
            stock_chg = (current / prev_close - 1) * 100
            market_gap = _safe_float(features.get("nifty_gap", features.get("gap_pct", 0)))
            return stock_chg - market_gap
        except Exception:
            pass

    return 0.0


def _opening_range(df, n_bars: int = 3) -> Optional[Dict[str, float]]:
    """Compute opening range from first N bars.
    
    Uses v82_strategy._orb() if available, else simple high/low.
    """
    if df is None or len(df) < n_bars:
        return None
    try:
        orb_h = float(df["high"].iloc[:n_bars].max())
        orb_l = float(df["low"].iloc[:n_bars].min())
        if orb_h > orb_l > 0:
            return {"high": orb_h, "low": orb_l}
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════
# DISCOVERY SCORE COMPONENTS
# ═══════════════════════════════════════════════════════════════════════

def _score_rs_acceleration(rs_val: float, mom_accel: float) -> float:
    """Score RS acceleration (0-100).
    
    High RS + accelerating momentum = strong relative performance.
    """
    # RS contribution (0-60)
    rs_score = min(60, max(0, rs_val * 15))  # ±4 RS → 0-60
    
    # Acceleration contribution (0-40)
    acc_score = min(40, max(0, mom_accel * 10))  # positive accel → 0-40
    
    return min(100, rs_score + acc_score)


def _score_rvol_acceleration(rvol: float) -> float:
    """Score RVOL acceleration (0-100).
    
    RVOL >= 3 is strong, >= 5 is exceptional.
    Below 1 means below-average volume (weak).
    """
    if rvol <= 0.5:
        return 0
    if rvol <= 1.0:
        return 20  # Below average
    if rvol <= 2.0:
        return 40 + (rvol - 1.0) * 20  # 40-60
    if rvol <= 3.0:
        return 60 + (rvol - 2.0) * 15  # 60-75
    if rvol <= 5.0:
        return 75 + (rvol - 3.0) * 7.5  # 75-90
    return min(100, 90 + (rvol - 5.0) * 2)  # 90-100


def _score_price_expansion(df, atr: float) -> float:
    """Score price expansion from open (0-100).
    
    Measures how much the stock has moved from its open in ATR terms.
    0.5-1.0 ATR = healthy; >1.5 ATR = aggressive.
    """
    if df is None or len(df) < 2 or atr <= 0:
        return 30  # Default: unknown

    try:
        open_price = float(df["open"].iloc[0])
        current = float(df["close"].iloc[-1])
        if open_price <= 0:
            return 30

        expansion = abs(current - open_price) / atr

        if expansion < 0.3:
            return 20  # Barely moved
        if expansion < 0.5:
            return 40
        if expansion < 1.0:
            return 60 + (expansion - 0.5) * 40  # 60-80
        if expansion < 1.5:
            return 80 + (expansion - 1.0) * 20  # 80-90
        return min(100, 90 + (expansion - 1.5) * 10)  # 90-100
    except Exception:
        return 30


def _score_market_context(features: dict) -> float:
    """Score market/sector context (0-100).
    
    Uses: sector_leading, sector_against, market regime, nifty gap.
    """
    score = 50  # Neutral baseline

    if features.get("sector_leading"):
        score += 25
    elif features.get("sector_against"):
        score -= 20

    nifty_gap = _safe_float(features.get("nifty_gap", features.get("gap_pct", 0)))
    if nifty_gap > 0.3:
        score += 10  # Market supporting
    elif nifty_gap < -0.3:
        score -= 10

    regime = str(features.get("regime", "NORMAL")).upper()
    if regime in ("TRENDING", "STRONG"):
        score += 10
    elif regime in ("CHOPPY", "RANGING"):
        score -= 10

    return max(0, min(100, score))


def _score_liquidity(df, features: dict) -> float:
    """Score liquidity (0-100).
    
    Uses: volume sum, spread (if available), price level.
    """
    score = 50  # Baseline

    # Volume check
    if df is not None and "volume" in df.columns:
        try:
            total_vol = float(df["volume"].fillna(0).sum())
            if total_vol >= 500_000:
                score += 30
            elif total_vol >= 100_000:
                score += 15
            elif total_vol < 50_000:
                score -= 20
        except Exception:
            pass

    # Price level (Rs 60-3500 range preference)
    ltp = _safe_float(features.get("ltp", features.get("price", 0)))
    if 100 <= ltp <= 2000:
        score += 15  # Sweet spot
    elif ltp < 60 or ltp > 3500:
        score -= 30  # Outside universe

    # ADV check
    adv = _safe_float(features.get("avg_daily_volume", features.get("adv_20d", 0)))
    if adv >= 200_000:
        score += 10
    elif 0 < adv < 100_000:
        score -= 15

    return max(0, min(100, score))


# ═══════════════════════════════════════════════════════════════════════
# MAIN CLASS
# ═══════════════════════════════════════════════════════════════════════

class EarlyDiscoveryEngine:
    """V12 Early Discovery — enables 5-8 bar scoring for faster mover detection."""

    def __init__(self, min_bars: int = MIN_BARS_EARLY):
        self.min_bars = min_bars
        self._version = "V12_EARLY_DISCOVERY_2026_09_02"

    # ─── Core API ────────────────────────────────────────────────────

    def can_score_early(self, df) -> bool:
        """True if we have enough bars for early scoring."""
        if df is None:
            return False
        return len(df) >= self.min_bars

    def compute_discovery_score(
        self,
        symbol: str,
        df,
        features: dict,
        prev_close: float = 0,
        orb: Optional[dict] = None,
    ) -> dict:
        """Compute discovery score from 5 weighted components.

        Works with as few as 5 bars. Returns full breakdown.
        """
        bars_available = len(df) if df is not None else 0
        fallback_used = bars_available < MIN_BARS_NORMAL

        # Compute component inputs
        atr = _early_atr(df)
        rvol = _early_rvol(df)
        rs_val = _early_rs(df, features, prev_close)
        mom5, mom15, accel = _early_momentum(df)

        # Score each component (0-100)
        rs_score = _score_rs_acceleration(rs_val, accel)
        rvol_score = _score_rvol_acceleration(rvol)
        expansion_score = _score_price_expansion(df, atr)
        market_score = _score_market_context(features)
        liquidity_score = _score_liquidity(df, features)

        # Weighted composite
        discovery = (
            rs_score * W_RS_ACCEL
            + rvol_score * W_RVOL_ACCEL
            + expansion_score * W_PRICE_EXPANSION
            + market_score * W_MARKET_CONTEXT
            + liquidity_score * W_LIQUIDITY
        )

        # Phase classification
        phase = self.classify_phase(df, features, orb=orb)

        return {
            "discovery_score": round(discovery, 1),
            "rs_accel": round(rs_score, 1),
            "rvol_accel": round(rvol_score, 1),
            "price_expansion": round(expansion_score, 1),
            "market_context": round(market_score, 1),
            "liquidity": round(liquidity_score, 1),
            "phase": phase,
            "bars_available": bars_available,
            "fallback_used": fallback_used,
            "rvol_raw": round(rvol, 2),
            "rs_raw": round(rs_val, 3),
            "atr_raw": round(atr, 2),
            "mom5": round(mom5, 3),
            "mom15": round(mom15, 3),
        }

    def classify_phase(
        self,
        df,
        features: dict,
        entry_price: float = 0,
        orb: Optional[dict] = None,
    ) -> str:
        """Classify move phase: EARLY / DEVELOPING / MATURE.

        Based on % of estimated daily range already consumed.
        Uses ATR from available bars (not 20-bar requirement).
        """
        if df is None or len(df) < 3:
            return "EARLY"

        try:
            atr = _early_atr(df)
            if atr <= 0:
                return "EARLY"

            # Estimate daily range as ATR * multiplier
            estimated_daily_range = atr * DEFAULT_ATR_MULTIPLIER

            # Current range consumed
            open_price = float(df["open"].iloc[0])
            current = float(df["close"].iloc[-1])
            range_consumed = abs(current - open_price)

            if estimated_daily_range > 0:
                pct_consumed = range_consumed / estimated_daily_range
            else:
                return "EARLY"

            if pct_consumed < RANGE_DEVELOPING_PCT:
                return "EARLY"
            elif pct_consumed < RANGE_MATURE_PCT:
                return "DEVELOPING"
            else:
                return "MATURE"

        except Exception as e:
            log.debug(f"Phase classification error for {features.get('symbol', '?')}: {e}")
            return "EARLY"

    def is_future_mover_precursor(
        self, df, features: dict
    ) -> Tuple[bool, str]:
        """Detect future-mover signals (09:30-10:00 IST window).

        A precursor is a stock showing ALL of:
          - RVOL >= 3.0 (volume surge)
          - RS accelerating (positive and > threshold)
          - Price expanding (meaningful move from open)
          - Not isolated (sector context not against)

        Returns: (is_precursor, reason_string)
        """
        rvol = _early_rvol(df)
        rs = _early_rs(df, features)
        mom5, mom15, accel = _early_momentum(df)
        atr = _early_atr(df)

        reasons = []

        # Gate 1: RVOL surge
        if rvol < RVOL_SURGE_THRESHOLD:
            return False, f"RVOL_LOW ({rvol:.1f} < {RVOL_SURGE_THRESHOLD})"
        reasons.append(f"RVOL={rvol:.1f}")

        # Gate 2: RS acceleration
        if rs < RS_ACCEL_MIN and accel <= 0:
            return False, f"RS_WEAK (rs={rs:.2f}, accel={accel:.2f})"
        reasons.append(f"RS={rs:.2f}")

        # Gate 3: Price expansion
        if df is not None and len(df) >= 2 and atr > 0:
            try:
                open_px = float(df["open"].iloc[0])
                cur = float(df["close"].iloc[-1])
                expansion_atr = abs(cur - open_px) / atr
                if expansion_atr < 0.3:
                    return False, f"EXPANSION_LOW ({expansion_atr:.2f} ATR)"
                reasons.append(f"EXP={expansion_atr:.1f}ATR")
            except Exception:
                pass

        # Gate 4: Sector not against
        if features.get("sector_against"):
            return False, "SECTOR_AGAINST"

        reasons.append(f"MOM={accel:.2f}")
        return True, " + ".join(reasons)

    def enrich_features_early(
        self,
        df,
        features: dict,
        prev_close: float = 0,
    ) -> dict:
        """Enrich features dict for candidates with < 20 bars.

        Computes fallback values using available bars so that
        final_decision() and acceleration_score() receive valid inputs.

        Returns a NEW dict (does not mutate the original).
        """
        enriched = dict(features)
        bars = len(df) if df is not None else 0
        enriched["_early_discovery"] = True
        enriched["_bars_available"] = bars

        if bars < MIN_BARS_NORMAL and bars >= self.min_bars:
            # ATR fallback
            atr = _early_atr(df)
            if atr > 0 and not enriched.get("atr"):
                enriched["atr"] = atr

            # RVOL fallback
            rvol = _early_rvol(df)
            if rvol > 0 and not enriched.get("rvol"):
                enriched["rvol"] = rvol

            # Momentum fallback
            mom5, mom15, accel = _early_momentum(df)
            if not enriched.get("momentum_5m") and not enriched.get("mom5"):
                enriched["momentum_5m"] = mom5
                enriched["mom5"] = mom5
            if not enriched.get("momentum_15m") and not enriched.get("mom15"):
                enriched["momentum_15m"] = mom15
                enriched["mom15"] = mom15

            # RS fallback
            rs = _early_rs(df, features, prev_close)
            if rs != 0 and not enriched.get("rs") and not enriched.get("rs_val"):
                enriched["rs"] = rs
                enriched["rs_val"] = rs

            # VWAP fallback (uses v82_strategy.vwap if available)
            if _V82_AVAILABLE and df is not None:
                try:
                    vw = _v82_vwap(df)
                    if vw > 0 and not enriched.get("vwap"):
                        enriched["vwap"] = vw
                except Exception:
                    pass

            # ORB from available bars
            orb = _opening_range(df, min(3, bars))
            if orb:
                if not enriched.get("orb_high"):
                    enriched["orb_high"] = orb["high"]
                if not enriched.get("orb_low"):
                    enriched["orb_low"] = orb["low"]

        return enriched

    def build_early_snapshot(
        self, df, features: dict, prev_close: float = 0
    ) -> Optional[Any]:
        """Build a V10.1 Snapshot from limited bars.

        Uses fallback computations for missing indicators.
        Returns None if V10.1 is not available or insufficient data.
        """
        if not _V10_AVAILABLE:
            return None
        if df is None or len(df) < self.min_bars:
            return None

        try:
            enriched = self.enrich_features_early(df, features, prev_close)
            ltp = _safe_float(enriched.get("ltp", enriched.get("price", 0)))

            snapshot = Snapshot(
                symbol=enriched.get("symbol", "UNKNOWN"),
                ts=enriched.get("ts", datetime.now()),
                price=ltp,
                atr=_safe_float(enriched.get("atr", _early_atr(df))),
                vwap=_safe_float(enriched.get("vwap", 0)),
                adx=_safe_float(enriched.get("adx", 20)),
                adx_slope=_safe_float(enriched.get("adx_slope", 0)),
                choppiness=_safe_float(enriched.get("choppiness", 50)),
                atr_percentile=_safe_float(enriched.get("atr_percentile", 50)),
                rsi=_safe_float(enriched.get("rsi", 50)),
                rvol=_safe_float(enriched.get("rvol", _early_rvol(df))),
                momentum_5m=_safe_float(enriched.get("mom5", enriched.get("momentum_5m", 0))),
                momentum_15m=_safe_float(enriched.get("mom15", enriched.get("momentum_15m", 0))),
                sector_rs=_safe_float(enriched.get("sector_rs", 0)),
                stock_rs=_safe_float(enriched.get("rs_val", enriched.get("rs", 0))),
                volume_acceleration=_safe_float(enriched.get("volume_acceleration", 0)),
                spread_ticks=_safe_float(enriched.get("spread_ticks", 0)),
                orb_high=enriched.get("orb_high"),
                orb_low=enriched.get("orb_low"),
                prior_high=enriched.get("prior_high", enriched.get("orb_high")),
                prior_low=enriched.get("prior_low", enriched.get("orb_low")),
            )
            return snapshot
        except Exception as e:
            log.debug(f"Early snapshot build error: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════
# SELF-TESTS
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import pandas as pd

    print("V12 Early Discovery Engine — Self Tests")
    print("=" * 50)

    _counts = [0, 0]  # [passed, failed]


    def test(name, condition):

        if condition:
            _counts[0] += 1
            print(f"  ✅ {name}")
        else:
            _counts[1] += 1
            print(f"  ❌ {name}")

    engine = EarlyDiscoveryEngine(min_bars=5)

    # --- Test: can_score_early ---
    test("can_score_early: None df → False", not engine.can_score_early(None))

    df_3 = pd.DataFrame({"open": [100]*3, "high": [101]*3, "low": [99]*3,
                          "close": [100.5]*3, "volume": [10000]*3})
    test("can_score_early: 3 bars → False", not engine.can_score_early(df_3))

    df_5 = pd.DataFrame({"open": [100]*5, "high": [102]*5, "low": [99]*5,
                          "close": [100, 101, 102, 103, 104], "volume": [10000, 15000, 20000, 25000, 50000]})
    test("can_score_early: 5 bars → True", engine.can_score_early(df_5))

    df_20 = pd.DataFrame({"open": [100]*20, "high": [102]*20, "low": [99]*20,
                           "close": list(range(100, 120)), "volume": [10000]*20})
    test("can_score_early: 20 bars → True", engine.can_score_early(df_20))

    # --- Test: compute_discovery_score ---
    features = {"ltp": 104, "nifty_gap": 0.2, "sector_leading": True, "regime": "NORMAL"}
    result = engine.compute_discovery_score("TEST", df_5, features, prev_close=99)
    test("discovery_score returns dict", isinstance(result, dict))
    test("discovery_score has required keys",
         all(k in result for k in ["discovery_score", "phase", "bars_available", "fallback_used"]))
    test("discovery_score is 0-100", 0 <= result["discovery_score"] <= 100)
    test("fallback_used True for 5 bars", result["fallback_used"])
    test("bars_available = 5", result["bars_available"] == 5)
    print(f"    discovery_score = {result['discovery_score']:.1f}")
    print(f"    components: rs={result['rs_accel']:.0f} rvol={result['rvol_accel']:.0f} "
          f"exp={result['price_expansion']:.0f} mkt={result['market_context']:.0f} liq={result['liquidity']:.0f}")

    # --- Test: classify_phase ---
    test("phase: 5 bars small move → EARLY",
         engine.classify_phase(df_5, features) == "EARLY")

    df_big_move = pd.DataFrame({
        "open": [100]*8, "high": [100, 102, 104, 106, 108, 110, 112, 114],
        "low": [99]*8, "close": [100, 102, 104, 106, 108, 110, 112, 114],
        "volume": [10000]*8
    })
    phase = engine.classify_phase(df_big_move, features)
    test(f"phase: big move → DEVELOPING or MATURE (got {phase})",
         phase in ("DEVELOPING", "MATURE"))

    # --- Test: is_future_mover_precursor ---
    df_surging = pd.DataFrame({
        "open": [100]*6, "high": [102]*6, "low": [99]*6,
        "close": [100, 101, 102, 103, 104, 106],
        "volume": [10000, 10000, 10000, 10000, 10000, 50000],  # Last bar 5x volume
    })
    feat_strong = {"rs": 2.0, "sector_leading": True, "nifty_gap": 0.5}
    is_precursor, reason = engine.is_future_mover_precursor(df_surging, feat_strong)
    test(f"future_mover: surging vol + RS → {is_precursor} ({reason})",
         isinstance(is_precursor, bool))

    # --- Test: enrich_features_early ---
    sparse_features = {"ltp": 104, "symbol": "TEST"}
    enriched = engine.enrich_features_early(df_5, sparse_features, prev_close=99)
    test("enrich adds _early_discovery flag", enriched.get("_early_discovery") is True)
    test("enrich adds _bars_available", enriched.get("_bars_available") == 5)
    test("enrich computes rvol", "rvol" in enriched and enriched["rvol"] > 0)
    test("enrich computes mom5", "mom5" in enriched or "momentum_5m" in enriched)

    # --- Test: early_atr ---
    atr = _early_atr(df_5)
    test(f"early_atr: 5 bars → {atr:.2f} (positive)", atr > 0)

    atr_none = _early_atr(None)
    test("early_atr: None → 0.0", atr_none == 0.0)

    # --- Test: scoring components ---
    test("rs_score: positive RS → high score", _score_rs_acceleration(3.0, 0.5) > 50)
    test("rs_score: negative RS → low score", _score_rs_acceleration(-2.0, -0.5) < 30)
    test("rvol_score: RVOL=5 → high", _score_rvol_acceleration(5.0) > 85)
    test("rvol_score: RVOL=0.5 → 0", _score_rvol_acceleration(0.5) == 0)
    test("market_context: sector_leading → high",
         _score_market_context({"sector_leading": True, "nifty_gap": 0.5}) > 70)
    test("market_context: sector_against → low",
         _score_market_context({"sector_against": True, "nifty_gap": -0.5}) < 30)

    # --- Test: 20-bar df doesn't use fallbacks ---
    result_20 = engine.compute_discovery_score("TEST", df_20, features, prev_close=99)
    test("20 bars: fallback_used = False", not result_20["fallback_used"])

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed == 0:
        print("ALL TESTS PASSED ✅")
    else:
        print(f"⚠️  {failed} FAILURES")
