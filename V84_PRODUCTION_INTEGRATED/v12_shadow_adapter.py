"""
V12 Shadow Adapter — maps existing V10.1-R pipeline data to V12 Candidate objects.
Deployed alongside V11_V12_HARDENED_COMBINED_PATCH.py for shadow evaluation.

ENGINEER NOTES:
- This adapter runs AFTER V10.1-R makes its decision
- V12 evaluates the same candidate and logs WOULD_ENTER / WOULD_WAIT / etc.
- V10.1-R remains the canonical decision authority until shadow validates V12
- All V12 results logged via v11_observability if available
- Wrapped in try/except — can never crash the trading bot
"""

import logging
import time
import math

log = logging.getLogger("v12_shadow")

# Features we CAN compute from existing pipeline data
# Features we CANNOT compute yet are marked ESTIMATED

def build_v12_candidate(symbol, side, price, score_components, v853_result,
                        snapshot, df=None, orb_data=None, atr=None):
    """
    Build a V12 Candidate features dict from existing V10.1-R pipeline data.
    
    Args:
        symbol: stock symbol
        side: "LONG" or "SHORT" 
        price: current LTP
        score_components: dict with MKT, SEC, RS, MOM, RVOL, VWAP, SETUP, ENTRY, OPP
        v853_result: dict with phase, quality, timing, fitness, action
        snapshot: dict with vwap, momentum, rvol, rs_val, etc.
        df: OHLCV dataframe if available (for ATR/extension calc)
        orb_data: dict with orb_high, orb_low if available
        atr: pre-computed ATR if available
    
    Returns:
        dict with discovery_score, setup_score, entry_score, features
    """
    
    # === SCORE DECOMPOSITION ===
    # Discovery: MKT + SEC + RS (market conditions + relative strength)
    # Setup: SETUP + VWAP + OPP (setup quality + opportunity)
    # Entry: MOM + RVOL + ENTRY (momentum + volume + entry timing)
    
    sc = score_components or {}
    mkt = float(sc.get("MKT", 0))
    sec = float(sc.get("SEC", 0))
    rs  = float(sc.get("RS", 0))
    mom = float(sc.get("MOM", 0))
    rvol_sc = float(sc.get("RVOL", 0))
    vwap_sc = float(sc.get("VWAP", 0))
    setup_sc = float(sc.get("SETUP", 0))
    entry_sc = float(sc.get("ENTRY", 0))
    opp = float(sc.get("OPP", 0))
    
    # Normalize to 0-100 scale
    # Max components: MKT=7, SEC=5, RS=15, MOM=18, RVOL=12, VWAP=10, SETUP=15, ENTRY=10, OPP=8
    discovery_raw = mkt + sec + rs  # max 27
    setup_raw = setup_sc + vwap_sc + opp  # max 33
    entry_raw = mom + rvol_sc + entry_sc  # max 40
    
    discovery_score = min(100.0, discovery_raw * (100.0 / 27.0))
    setup_score = min(100.0, setup_raw * (100.0 / 33.0))
    entry_score = min(100.0, entry_raw * (100.0 / 40.0))
    
    # === V8.5.3 MAPPING ===
    v853 = v853_result or {}
    phase = v853.get("phase", "UNKNOWN")
    quality = float(v853.get("quality", 0))
    timing = float(v853.get("timing", 0))
    fitness = float(v853.get("fitness", 0))
    
    # === SNAPSHOT MAPPING ===
    snap = snapshot or {}
    rvol = float(snap.get("rvol", 1.0))
    momentum = float(snap.get("momentum", 0))
    vwap = float(snap.get("vwap", 0))
    rs_val = float(snap.get("rs_val", 0))
    
    # === COMPUTED FEATURES ===
    
    # data_healthy: True if we have valid price + valid snapshot
    data_healthy = 1.0 if (price and price > 0 and math.isfinite(price) 
                           and rvol > 0) else 0.0
    
    # market_regime_allowed: from MKT score (>=0 = allowed)
    market_regime_allowed = 1.0 if mkt >= 0 else 0.0
    
    # execution_quality_ok: default True for NSE (we have separate spread checks)
    execution_quality_ok = 1.0
    
    # signal_age_seconds: 0 at creation (freshly computed)
    signal_age_seconds = 0.0
    
    # entry_drift_pct: 0 at creation (computed at order time)
    entry_drift_pct = 0.0
    
    # volume_expansion: from RVOL (relative volume)
    volume_expansion = rvol
    
    # ORB detection
    orb_breakout = 0.0
    breakout_accepted = 0.0
    if orb_data and price:
        orb_h = float(orb_data.get("orb_high", 0))
        orb_l = float(orb_data.get("orb_low", 0))
        if side == "LONG" and orb_h > 0 and price > orb_h:
            orb_breakout = 1.0
            breakout_accepted = min(1.0, max(0.0, (price - orb_h) / max(orb_h * 0.005, 0.01)))
        elif side == "SHORT" and orb_l > 0 and price < orb_l:
            orb_breakout = 1.0
            breakout_accepted = min(1.0, max(0.0, (orb_l - price) / max(orb_l * 0.005, 0.01)))
    
    # extension_atr: distance from breakout level in ATR — ESTIMATED without df
    extension_atr = 0.0
    if atr and atr > 0 and orb_data and price:
        if side == "LONG":
            ref = float(orb_data.get("orb_high", price))
            extension_atr = abs(price - ref) / atr
        else:
            ref = float(orb_data.get("orb_low", price))
            extension_atr = abs(ref - price) / atr
    
    # expected_r: ESTIMATED from ATR-based stop/target
    expected_r = 1.5  # conservative default
    if atr and atr > 0:
        stop_distance = atr * 0.5
        target_distance = atr * 1.5
        if stop_distance > 0:
            expected_r = target_distance / stop_distance
    
    # remaining_edge_pct: ESTIMATED
    remaining_edge_pct = 0.50  # conservative default
    if atr and atr > 0 and price and orb_data:
        ref = float(orb_data.get("orb_high" if side == "LONG" else "orb_low", price))
        move_so_far = abs(price - ref)
        expected_total = atr * 2.0
        if expected_total > 0:
            remaining_edge_pct = max(0.0, min(1.0, 1.0 - (move_so_far / expected_total)))
    
    # Setup-specific signals — ESTIMATED (not yet computed by our pipeline)
    consolidation_breakout = 0.0
    vwap_reclaim = 0.0
    reclaim_confirmed = 0.0
    pullback_held = 0.0
    trend_intact = 1.0 if momentum > 0 else 0.0
    
    features = {
        "data_healthy": data_healthy,
        "market_regime_allowed": market_regime_allowed,
        "execution_quality_ok": execution_quality_ok,
        "signal_age_seconds": signal_age_seconds,
        "entry_drift_pct": entry_drift_pct,
        "volume_expansion": volume_expansion,
        "expected_r": expected_r,
        "remaining_edge_pct": remaining_edge_pct,
        "extension_atr": extension_atr,
        "breakout_acceptance": breakout_accepted,
        "orb_breakout": orb_breakout,
        "breakout_accepted": breakout_accepted,
        "consolidation_breakout": consolidation_breakout,
        "vwap_reclaim": vwap_reclaim,
        "reclaim_confirmed": reclaim_confirmed,
        "pullback_held": pullback_held,
        "trend_intact": trend_intact,
        "setup_invalid": 0.0,
        # Audit: source tracking
        "_v853_phase": phase,
        "_v853_fitness": fitness,
        "_v853_quality": quality,
        "_v853_timing": timing,
        "_estimated_fields": "expected_r,remaining_edge_pct,extension_atr,breakout_acceptance,consolidation_breakout,vwap_reclaim,pullback_held",
    }
    
    return {
        "discovery_score": round(discovery_score, 1),
        "setup_score": round(setup_score, 1),
        "entry_score": round(entry_score, 1),
        "features": features,
    }


def v12_shadow_evaluate(symbol, side, price, score_components, v853_result,
                        snapshot, df=None, orb_data=None, atr=None,
                        v12_engine=None, obs_engine=None):
    """
    Run V12 shadow evaluation on a candidate.
    
    Returns the DecisionResult (or None on error).
    Logs to v11_observability if available.
    NEVER raises — all errors caught and logged.
    """
    try:
        if v12_engine is None:
            return None
        
        from V11_V12_HARDENED_COMBINED_PATCH import (
            Market, Side, Candidate
        )
        
        mapped = build_v12_candidate(
            symbol, side, price, score_components, v853_result,
            snapshot, df, orb_data, atr
        )
        
        candidate = Candidate(
            symbol=symbol,
            market=Market.NSE,
            side=Side.LONG if side == "LONG" else Side.SHORT,
            timestamp=str(time.time()),
            price=float(price),
            discovery_score=mapped["discovery_score"],
            setup_score=mapped["setup_score"],
            entry_score=mapped["entry_score"],
            regime=v853_result.get("phase", "UNKNOWN") if v853_result else "UNKNOWN",
            setup="ORB_BREAKOUT",
            features=mapped["features"],
        )
        
        result = v12_engine.evaluate(candidate)
        
        log.info(
            f"[V12_SHADOW] {symbol} {side} | "
            f"decision={result.decision.value} | "
            f"strategy={result.strategy} | "
            f"score={result.calibrated_score:.1f} | "
            f"reason={result.reason} | "
            f"disc={mapped['discovery_score']:.0f} "
            f"setup={mapped['setup_score']:.0f} "
            f"entry={mapped['entry_score']:.0f}"
        )
        
        # Log to observability if available
        if obs_engine:
            try:
                obs_engine.log_candidate({
                    "symbol": symbol,
                    "side": side,
                    "price": price,
                    "v12_decision": result.decision.value,
                    "v12_strategy": result.strategy,
                    "v12_score": result.calibrated_score,
                    "v12_reason": result.reason,
                    "v12_audit": result.audit,
                    "estimated_fields": mapped["features"].get("_estimated_fields", ""),
                })
            except Exception:
                pass
        
        return result
        
    except Exception as e:
        log.warning(f"[V12_SHADOW] Error evaluating {symbol}: {e}")
        return None
