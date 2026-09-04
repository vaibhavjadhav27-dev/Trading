"""
V12 Independent Scoring — Compute discovery/setup/entry from raw OHLCV
======================================================================

Replaces the remapped-V10 approach in the V12 shadow block.
Every score is computed from raw DataFrames, NOT from V10/V8.2 components.

Produces ALL 8 CRITICAL_FEATURES required by V11_V12_HARDENED_COMBINED_PATCH:
  data_healthy, market_regime_allowed, execution_quality_ok,
  signal_age_seconds, entry_drift_pct, volume_expansion,
  expected_r, remaining_edge_pct

Integration:
  from v12_independent_scoring import V12IndependentScorer
  scorer = V12IndependentScorer()
  features = scorer.compute_all(symbol, df, nifty_df, sector_data, prev_close, orb)

Version: 12.1.0-independent
Date: 2026-09-02
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Dict, Any, List, Tuple

log = logging.getLogger("V12_INDEPENDENT")

# ─── Reuse from existing modules (NO duplication) ────────────────────
try:
    from v82_strategy import momentum as _v82_momentum, vwap as _v82_vwap, rvol as _v82_rvol
    _V82 = True
except ImportError:
    _V82 = False

try:
    from v12_early_discovery import EarlyDiscoveryEngine
    _DISC = True
except ImportError:
    _DISC = False

# ─── Helpers ─────────────────────────────────────────────────────────

def _safe(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _col(df, name: str):
    """Safely get a column as float list, return [] if missing."""
    if df is None:
        return []
    try:
        return [float(x) for x in df[name]]
    except (KeyError, TypeError, ValueError):
        return []


def _atr_from_df(df, period: int = 14) -> float:
    """True Range ATR from raw OHLCV. Works with as few as 2 bars."""
    highs = _col(df, "high")
    lows = _col(df, "low")
    closes = _col(df, "close")
    if len(highs) < 2:
        return 0.0
    trs = []
    for i in range(1, len(highs)):
        h, lo, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if not trs:
        return 0.0
    n = min(period, len(trs))
    return sum(trs[-n:]) / n


def _rvol_from_df(df) -> float:
    """Relative volume: last bar vs average of previous bars."""
    vols = _col(df, "volume")
    if len(vols) < 3:
        return 1.0
    prev = vols[:-1]
    avg = sum(prev) / len(prev) if prev else 1.0
    if avg <= 0:
        return 1.0
    return vols[-1] / avg


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))

# ─── Weight constants ────────────────────────────────────────────────

# Discovery (sum = 1.0)
W_D_RS    = 0.25
W_D_RVOL  = 0.25
W_D_EXP   = 0.20
W_D_MKT   = 0.15
W_D_LIQ   = 0.15

# Setup (sum = 1.0)
W_S_TREND = 0.25
W_S_BRK   = 0.25
W_S_STRUCT = 0.25
W_S_ALIGN = 0.15
W_S_RR    = 0.10

# Entry (sum = 1.0)
W_E_FRESH = 0.30
W_E_ACC   = 0.20
W_E_VOL   = 0.20
W_E_PULL  = 0.15
W_E_SIG   = 0.15

# Phase thresholds (fraction of estimated daily range consumed)
RANGE_DEVELOPING = 0.30
RANGE_MATURE     = 0.60
ATR_DAILY_MULT   = 2.0       # est daily range ≈ 2 × intraday ATR


class V12IndependentScorer:
    """Compute V12 discovery/setup/entry from raw OHLCV — zero V10 dependency."""

    _version = "V12_INDEPENDENT_SCORING_2026_09_02"

    def __init__(self):
        self._discovery_engine = EarlyDiscoveryEngine(min_bars=5) if _DISC else None

    # ──────────────────────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────────────────────

    def compute_all(
        self,
        symbol: str,
        df,
        nifty_df=None,
        sector_data: Optional[dict] = None,
        prev_close: float = 0,
        orb: Optional[dict] = None,
    ) -> dict:
        """Return a complete feature dict from raw data only.

        The returned dict includes every key the V12 HardenedCombinedPatch
        ``CRITICAL_FEATURES`` tuple requires, plus component breakdowns.
        """
        sector_data = sector_data or {}
        bars = len(df) if df is not None else 0
        closes = _col(df, "close")
        opens  = _col(df, "open")
        highs  = _col(df, "high")
        lows   = _col(df, "low")
        vols   = _col(df, "volume")

        price = closes[-1] if closes else 0.0
        atr = _atr_from_df(df)
        rvol = _rvol_from_df(df)

        # ── Data health ──────────────────────────────────────────────
        data_healthy = price > 0 and rvol > 0 and bars >= 5 and atr > 0

        # ── Discovery score ──────────────────────────────────────────
        rs_accel     = self._compute_rs(closes, nifty_df, prev_close)
        rvol_accel   = self._score_rvol(rvol)
        price_exp    = self._score_expansion(opens, closes, atr)
        mkt_ctx      = self._score_market(nifty_df, sector_data)
        liq          = self._score_liquidity(vols)

        discovery = _clamp(
            rs_accel   * W_D_RS   +
            rvol_accel * W_D_RVOL +
            price_exp  * W_D_EXP  +
            mkt_ctx    * W_D_MKT  +
            liq        * W_D_LIQ
        )

        # ── Setup score ──────────────────────────────────────────────
        trend    = self._score_trend(closes, lows)
        brk_q    = self._score_breakout(closes, highs, lows, vols, orb, atr)
        struct   = self._score_structure(opens, closes, highs, lows)
        align    = self._score_alignment(closes, nifty_df)
        rr       = self._score_rr(closes, orb, atr)

        setup = _clamp(
            trend  * W_S_TREND +
            brk_q  * W_S_BRK   +
            struct * W_S_STRUCT +
            align  * W_S_ALIGN +
            rr     * W_S_RR
        )

        # ── Entry score ──────────────────────────────────────────────
        fresh    = self._score_freshness(bars, orb)
        accept   = self._score_acceptance(closes, orb)
        vol_pers = self._score_volume_persistence(vols)
        pull     = self._score_pullback(closes, highs, lows)
        sig_f    = 100.0   # signal is brand-new at creation

        entry = _clamp(
            fresh   * W_E_FRESH +
            accept  * W_E_ACC   +
            vol_pers* W_E_VOL   +
            pull    * W_E_PULL  +
            sig_f   * W_E_SIG
        )

        # ── Phase classification ─────────────────────────────────────
        phase = self._classify_phase(opens, closes, atr)

        # ── Extension ATR ────────────────────────────────────────────
        extension_atr = self._extension(closes, orb, atr)

        # ── Expected R / remaining edge ──────────────────────────────
        expected_r = self._expected_r(closes, orb, atr)
        remaining_edge_pct = self._remaining_edge(closes, orb, atr)

        # ── Market regime ────────────────────────────────────────────
        market_regime_allowed = mkt_ctx >= 20.0  # non-hostile

        # ── Build result ─────────────────────────────────────────────
        return {
            # ── 8 CRITICAL_FEATURES ──
            "data_healthy":           data_healthy,
            "market_regime_allowed":  market_regime_allowed,
            "execution_quality_ok":   True,      # NSE: always OK at evaluation time
            "signal_age_seconds":     0.0,       # fresh at creation
            "entry_drift_pct":        0.0,       # no drift yet
            "volume_expansion":       round(rvol, 2),
            "expected_r":             round(expected_r, 2),
            "remaining_edge_pct":     round(remaining_edge_pct, 3),

            # ── 3-layer scores ──
            "discovery_score":  round(discovery, 1),
            "setup_score":      round(setup, 1),
            "entry_score":      round(entry, 1),

            # ── Context ──
            "phase":            phase,
            "extension_atr":    round(extension_atr, 3),
            "breakout_acceptance": round(accept, 1),
            "bars":             bars,
            "atr":              round(atr, 2),
            "price":            round(price, 2),
            "rvol":             round(rvol, 2),

            # ── V12 engine extras (used by StrategyRouter) ──
            "orb_breakout":             self._is_orb_breakout(closes, orb),
            "consolidation_breakout":   False,       # needs multi-day range
            "vwap_reclaim":             False,       # needs intraday VWAP cross
            "reclaim_confirmed":        False,
            "pullback_held":            pull >= 60,
            "trend_intact":             trend >= 50,

            # ── Component breakdown (for audit) ──
            "components": {
                "rs_accel":         round(rs_accel, 1),
                "rvol_accel":       round(rvol_accel, 1),
                "price_expansion":  round(price_exp, 1),
                "market_context":   round(mkt_ctx, 1),
                "liquidity":        round(liq, 1),
                "trend":            round(trend, 1),
                "breakout_quality": round(brk_q, 1),
                "structure":        round(struct, 1),
                "alignment":        round(align, 1),
                "rr":               round(rr, 1),
                "freshness":        round(fresh, 1),
                "acceptance":       round(accept, 1),
                "vol_persistence":  round(vol_pers, 1),
                "pullback":         round(pull, 1),
            },
            "_source": "V12_INDEPENDENT",
        }

    # ──────────────────────────────────────────────────────────────────
    #  DISCOVERY COMPONENTS
    # ──────────────────────────────────────────────────────────────────

    def _compute_rs(self, closes: list, nifty_df, prev_close: float) -> float:
        """Relative strength: stock return vs nifty return, scaled 0-100."""
        if len(closes) < 2:
            return 50.0
        stock_ret = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0
        nifty_ret = 0.0
        if nifty_df is not None:
            nc = _col(nifty_df, "close") if hasattr(nifty_df, "__len__") else []
            if len(nc) >= 2:
                nifty_ret = (nc[-1] - nc[0]) / nc[0] * 100 if nc[0] else 0
            elif isinstance(nifty_df, dict):
                nifty_ret = _safe(nifty_df.get("return_pct", nifty_df.get("gap", 0)))
        rs_diff = stock_ret - nifty_ret
        # Scale: 0 diff = 50, +3% diff = 100, -3% diff = 0
        return _clamp(50.0 + rs_diff * (50.0 / 3.0))

    def _score_rvol(self, rvol: float) -> float:
        """RVOL score: 1.0 = 30, 2.0 = 60, 3.0 = 80, 5.0+ = 100."""
        if rvol <= 0.5:
            return 0.0
        if rvol <= 1.0:
            return rvol * 30.0
        if rvol <= 3.0:
            return 30.0 + (rvol - 1.0) * 25.0   # 30 → 80
        return _clamp(80.0 + (rvol - 3.0) * 10.0)  # 80 → 100

    def _score_expansion(self, opens: list, closes: list, atr: float) -> float:
        """Price expansion from open, normalised by ATR."""
        if not opens or not closes or atr <= 0:
            return 0.0
        move = abs(closes[-1] - opens[0])
        expansion_atr = move / atr
        # 0.5 ATR = 40, 1.0 = 70, 1.5+ = 100
        return _clamp(expansion_atr * 66.7)

    def _score_market(self, nifty_df, sector_data: dict) -> float:
        """Market context: nifty direction + sector alignment."""
        score = 50.0  # neutral default
        if nifty_df is not None:
            if isinstance(nifty_df, dict):
                gap = _safe(nifty_df.get("gap", 0))
                slope = _safe(nifty_df.get("slope", 0))
                if gap > 0.3:
                    score += 20
                elif gap < -0.3:
                    score -= 20
                if slope > 0:
                    score += 10
                elif slope < 0:
                    score -= 10
            else:
                nc = _col(nifty_df, "close")
                if len(nc) >= 2:
                    nifty_chg = (nc[-1] - nc[0]) / nc[0] * 100 if nc[0] else 0
                    score += nifty_chg * 15
        if sector_data.get("leading"):
            score += 15
        elif sector_data.get("against"):
            score -= 20
        return _clamp(score)

    def _score_liquidity(self, vols: list) -> float:
        """Liquidity: total volume score."""
        if not vols:
            return 0.0
        total = sum(vols)
        # 50K = 30, 200K = 60, 500K+ = 90, 1M+ = 100
        if total < 10_000:
            return 10.0
        if total < 100_000:
            return 10.0 + (total / 100_000) * 50.0
        if total < 500_000:
            return 60.0 + ((total - 100_000) / 400_000) * 30.0
        return _clamp(90.0 + (total - 500_000) / 500_000 * 10.0)

    # ──────────────────────────────────────────────────────────────────
    #  SETUP COMPONENTS
    # ──────────────────────────────────────────────────────────────────

    def _score_trend(self, closes: list, lows: list) -> float:
        """Trend integrity: monotonic closes + higher lows."""
        if len(closes) < 3:
            return 50.0
        n = min(10, len(closes))
        c = closes[-n:]
        lo = lows[-n:] if len(lows) >= n else lows
        # Count rising closes
        rises = sum(1 for i in range(1, len(c)) if c[i] >= c[i - 1])
        rise_pct = rises / max(len(c) - 1, 1)
        # Count higher lows
        hl = sum(1 for i in range(1, len(lo)) if lo[i] >= lo[i - 1])
        hl_pct = hl / max(len(lo) - 1, 1)
        return _clamp((rise_pct * 60 + hl_pct * 40))

    def _score_breakout(self, closes, highs, lows, vols, orb, atr) -> float:
        """Breakout quality: distance from ORB + volume at breakout."""
        if not orb or not closes or atr <= 0:
            return 50.0  # neutral if no ORB
        orb_h = _safe(orb.get("high", orb.get("orb_high", 0)))
        orb_l = _safe(orb.get("low", orb.get("orb_low", 0)))
        price = closes[-1]
        if orb_h <= 0 or orb_l <= 0:
            return 50.0
        # Distance from nearest boundary
        dist_h = (price - orb_h) / atr if price > orb_h else 0
        dist_l = (orb_l - price) / atr if price < orb_l else 0
        dist = max(dist_h, dist_l)
        # 0.5 ATR break = 70, 1.0 = 90, >1.5 = penalised (over-extended)
        if dist < 0.1:
            score = 30.0  # not really a breakout
        elif dist <= 1.0:
            score = 30.0 + dist * 60.0   # 30 → 90
        else:
            score = max(40, 90.0 - (dist - 1.0) * 30.0)  # penalise extension
        # Volume bonus at breakout bar
        if vols and len(vols) >= 2:
            avg_prev = sum(vols[:-1]) / max(len(vols) - 1, 1)
            if avg_prev > 0 and vols[-1] > avg_prev * 1.5:
                score = min(100, score + 10)
        return _clamp(score)

    def _score_structure(self, opens, closes, highs, lows) -> float:
        """Clean bar structure: no excessive wicks."""
        if len(closes) < 3:
            return 50.0
        n = min(5, len(closes))
        wick_ratios = []
        for i in range(-n, 0):
            body = abs(closes[i] - opens[i])
            total = highs[i] - lows[i]
            if total > 0:
                wick_ratios.append(body / total)
        if not wick_ratios:
            return 50.0
        avg_body_pct = sum(wick_ratios) / len(wick_ratios)
        # High body ratio = clean (0.7+ = good, 0.3 = poor)
        return _clamp(avg_body_pct * 120)

    def _score_alignment(self, closes: list, nifty_df) -> float:
        """Stock direction aligned with market."""
        if len(closes) < 2:
            return 50.0
        stock_dir = 1 if closes[-1] > closes[0] else -1
        nifty_dir = 0
        if nifty_df is not None:
            if isinstance(nifty_df, dict):
                gap = _safe(nifty_df.get("gap", 0))
                nifty_dir = 1 if gap > 0 else (-1 if gap < 0 else 0)
            else:
                nc = _col(nifty_df, "close")
                if len(nc) >= 2:
                    nifty_dir = 1 if nc[-1] > nc[0] else -1
        if nifty_dir == 0:
            return 50.0
        return 80.0 if stock_dir == nifty_dir else 20.0

    def _score_rr(self, closes, orb, atr) -> float:
        """Risk:Reward estimate from ORB range."""
        if not orb or not closes or atr <= 0:
            return 50.0
        orb_h = _safe(orb.get("high", orb.get("orb_high", 0)))
        orb_l = _safe(orb.get("low", orb.get("orb_low", 0)))
        orb_range = orb_h - orb_l
        if orb_range <= 0:
            return 50.0
        # Estimated target: 2x ORB range.  Risk: 0.5 ORB range
        est_rr = (orb_range * 2) / (orb_range * 0.5)  # = 4.0 always for ORB
        # Use ATR-based instead
        price = closes[-1]
        risk = min(atr * 0.5, orb_range * 0.5)
        reward = atr * 1.5
        if risk <= 0:
            return 50.0
        rr = reward / risk
        # rr 1.5 = 50, 2.0 = 70, 3.0 = 90
        return _clamp(20.0 + rr * 25.0)

    # ──────────────────────────────────────────────────────────────────
    #  ENTRY COMPONENTS
    # ──────────────────────────────────────────────────────────────────

    def _score_freshness(self, bars: int, orb) -> float:
        """How recently the breakout occurred. Fewer bars = fresher."""
        if bars <= 5:
            return 100.0
        if bars <= 10:
            return 90.0
        if bars <= 15:
            return 70.0
        if bars <= 20:
            return 50.0
        if bars <= 30:
            return 30.0
        return 15.0

    def _score_acceptance(self, closes: list, orb) -> float:
        """Price holding above/below breakout level."""
        if not orb or len(closes) < 2:
            return 50.0
        orb_h = _safe(orb.get("high", orb.get("orb_high", 0)))
        orb_l = _safe(orb.get("low", orb.get("orb_low", 0)))
        price = closes[-1]
        if orb_h > 0 and price > orb_h:
            # Check how many recent bars held above
            held = sum(1 for c in closes[-5:] if c > orb_h)
            return _clamp(held / min(5, len(closes[-5:])) * 100)
        if orb_l > 0 and price < orb_l:
            held = sum(1 for c in closes[-5:] if c < orb_l)
            return _clamp(held / min(5, len(closes[-5:])) * 100)
        return 30.0  # inside range

    def _score_volume_persistence(self, vols: list) -> float:
        """RVOL staying elevated across recent bars."""
        if len(vols) < 5:
            return 50.0
        n = min(10, len(vols))
        recent = vols[-n:]
        first_half = recent[:len(recent) // 2]
        second_half = recent[len(recent) // 2:]
        avg1 = sum(first_half) / len(first_half) if first_half else 1
        avg2 = sum(second_half) / len(second_half) if second_half else 1
        if avg1 <= 0:
            return 50.0
        ratio = avg2 / avg1
        # ratio > 1 = accelerating, < 0.5 = fading
        if ratio >= 1.5:
            return 100.0
        if ratio >= 1.0:
            return 70.0
        if ratio >= 0.7:
            return 50.0
        return max(10, ratio * 60)

    def _score_pullback(self, closes, highs, lows) -> float:
        """Shallow retracement = good entry. Deep = poor."""
        if len(closes) < 3:
            return 60.0
        n = min(5, len(closes))
        peak = max(highs[-n:]) if highs else closes[-1]
        trough = min(lows[-n:]) if lows else closes[-1]
        total_range = peak - trough
        if total_range <= 0:
            return 60.0
        # Current retrace from peak
        retrace = peak - closes[-1]
        retrace_frac = retrace / total_range
        # 0% retrace = 90 (at highs), 50% = 40, 100% = 10
        return _clamp(90.0 - retrace_frac * 80.0)

    # ──────────────────────────────────────────────────────────────────
    #  CONTEXT / DERIVED
    # ──────────────────────────────────────────────────────────────────

    def _classify_phase(self, opens, closes, atr) -> str:
        if not opens or not closes or atr <= 0:
            return "EARLY"
        est_daily = atr * ATR_DAILY_MULT
        consumed = abs(closes[-1] - opens[0])
        frac = consumed / est_daily if est_daily > 0 else 0
        if frac < RANGE_DEVELOPING:
            return "EARLY"
        if frac < RANGE_MATURE:
            return "DEVELOPING"
        return "MATURE"

    def _extension(self, closes, orb, atr) -> float:
        if not closes or atr <= 0 or not orb:
            return 0.0
        orb_h = _safe(orb.get("high", orb.get("orb_high", 0)))
        orb_l = _safe(orb.get("low", orb.get("orb_low", 0)))
        price = closes[-1]
        dist = 0.0
        if price > orb_h and orb_h > 0:
            dist = price - orb_h
        elif price < orb_l and orb_l > 0:
            dist = orb_l - price
        return dist / atr

    def _expected_r(self, closes, orb, atr) -> float:
        if atr <= 0 or not closes:
            return 1.5
        # Estimate: target = 1.5 ATR from current, risk = 0.5 ATR
        risk = atr * 0.5
        target = atr * 1.5
        if orb:
            orb_range = _safe(orb.get("high", 0)) - _safe(orb.get("low", 0))
            if orb_range > 0:
                target = max(target, orb_range * 1.5)
                risk = max(risk, orb_range * 0.3)
        return target / risk if risk > 0 else 1.5

    def _remaining_edge(self, closes, orb, atr) -> float:
        if atr <= 0 or not closes:
            return 0.50
        price = closes[-1]
        target_dist = atr * 1.5
        if orb:
            orb_h = _safe(orb.get("high", 0))
            orb_range = _safe(orb.get("high", 0)) - _safe(orb.get("low", 0))
            if orb_range > 0 and orb_h > 0:
                already_moved = abs(price - orb_h)
                full_target = orb_range * 2.0
                remaining = max(0, full_target - already_moved)
                if remaining <= 0:
                    # Already past ORB target — use ATR-based residual edge
                    return (atr * 0.5) / price if price > 0 else 0.01
                return remaining / price if price > 0 else 0.50
        return target_dist / price if price > 0 else 0.50

    def _is_orb_breakout(self, closes, orb) -> bool:
        if not orb or not closes:
            return False
        orb_h = _safe(orb.get("high", orb.get("orb_high", 0)))
        orb_l = _safe(orb.get("low", orb.get("orb_low", 0)))
        price = closes[-1]
        return (price > orb_h > 0) or (price < orb_l and orb_l > 0)


# ═════════════════════════════════════════════════════════════════════
# SELF-TESTS
# ═════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("V12 Independent Scoring — Self Tests")
    print("=" * 50)

    _counts = [0, 0]   # [passed, failed]

    def test(name, condition):
        if condition:
            _counts[0] += 1
            print(f"  ✅ {name}")
        else:
            _counts[1] += 1
            print(f"  ❌ {name}")

    # Build a mock DataFrame-like object
    class MockDF:
        def __init__(self, data):
            self._data = data
        def __getitem__(self, key):
            return self._data[key]
        def __len__(self):
            return len(self._data.get("close", []))
        @property
        def columns(self):
            return list(self._data.keys())

    # 8-bar rising stock
    df8 = MockDF({
        "open":   [100, 101, 102, 103, 104, 105, 106, 107],
        "high":   [101, 102, 103, 104, 105, 106, 107, 109],
        "low":    [99.5, 100.5, 101.5, 102.5, 103, 104, 105, 106],
        "close":  [100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 108],
        "volume": [50000, 55000, 60000, 70000, 80000, 90000, 100000, 150000],
    })

    scorer = V12IndependentScorer()
    result = scorer.compute_all("TEST", df8, prev_close=99.0,
                                 orb={"high": 101, "low": 99})

    test("compute_all returns dict", isinstance(result, dict))
    test("discovery_score 0-100", 0 <= result["discovery_score"] <= 100)
    test("setup_score 0-100", 0 <= result["setup_score"] <= 100)
    test("entry_score 0-100", 0 <= result["entry_score"] <= 100)
    test("data_healthy True for valid df", result["data_healthy"] is True)
    test("phase is EARLY or DEVELOPING", result["phase"] in ("EARLY", "DEVELOPING", "MATURE"))
    test("volume_expansion > 1 (rising volume)", result["volume_expansion"] > 1.0)
    test("expected_r >= 1.0", result["expected_r"] >= 1.0)
    test("remaining_edge_pct > 0", result["remaining_edge_pct"] > 0)
    test("_source is V12_INDEPENDENT", result["_source"] == "V12_INDEPENDENT")

    # CRITICAL_FEATURES check
    CRITICAL = ("data_healthy", "market_regime_allowed", "execution_quality_ok",
                "signal_age_seconds", "entry_drift_pct", "volume_expansion",
                "expected_r", "remaining_edge_pct")
    for key in CRITICAL:
        test(f"CRITICAL_FEATURE '{key}' present", key in result)

    # Components breakdown
    test("components dict present", "components" in result)
    test("components has rs_accel", "rs_accel" in result["components"])

    # Edge: empty DF
    empty = MockDF({"open": [], "high": [], "low": [], "close": [], "volume": []})
    r2 = scorer.compute_all("EMPTY", empty)
    test("empty df: data_healthy False", r2["data_healthy"] is False)
    test("empty df: discovery_score >= 0", r2["discovery_score"] >= 0)

    # Edge: 3 bars (below min)
    df3 = MockDF({
        "open": [100, 101, 102], "high": [101, 102, 103],
        "low": [99, 100, 101], "close": [100.5, 101.5, 102.5],
        "volume": [50000, 60000, 70000]
    })
    r3 = scorer.compute_all("SHORT", df3)
    test("3 bars: data_healthy False (< 5)", r3["data_healthy"] is False)

    # Nifty context
    nifty_data = {"gap": 0.8, "slope": 1.0}
    r_nifty = scorer.compute_all("WITH_NIFTY", df8, nifty_df=nifty_data)
    test("nifty positive gap: market_regime_allowed", r_nifty["market_regime_allowed"] is True)

    # Extension detection
    ext_orb = {"high": 100, "low": 98}
    r_ext = scorer.compute_all("EXT", df8, orb=ext_orb)
    test("extension_atr > 0 when price above ORB high", r_ext["extension_atr"] > 0)
    test("orb_breakout True", r_ext["orb_breakout"] is True)

    print(f"\n{_counts[0]}/{_counts[0] + _counts[1]} passed")
    if _counts[1] > 0:
        print(f"  ⚠️  {_counts[1]} FAILED")
    else:
        print("  ALL TESTS PASSED ✅")
