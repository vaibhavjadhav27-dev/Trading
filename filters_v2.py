#!/usr/bin/env python3
# filters_v2.py  --  STEP 3a  (FILTERS_V2 5-state per-regime filter module)
#
# Standalone, side-effect-free. select_candidates() calls apply_regime_filters()
# behind `if config.FILTERS_V2`. Reads precomputed metrics from stock_metrics.json
# (ema5/ema20/adv_20d/structural_high_5d/prev_day_high -- built by pull_yf_history.py).
#
# 5-STATE MACHINE (state passed in; caller derives it from mode + self.regime):
#   NO_TRADE           -> caller returns [] before calling this (handled upstream)
#   BEARISH-DEFENSIVE  -> flat openers only, RVol>5.0x, +sector anchor, half-size
#   CHOPPY             -> PAUSE: return [] BUT shadow-log what it WOULD have taken
#   NORMAL             -> gap>=GAP_MIN (existing), RVol>4.0x, price>ema20
#   TRENDING           -> gap 2.5-7.5%, RVol>3.5x, price>ema5
#
# RVol: caller supplies rvol_fn(candidate) -> float (bounded REST on shortlist only,
#       feeding compute_time_adjusted_rvol vs adv_20d). If None, RVol gate is skipped
#       (gap+EMA only) so the module degrades safely.
#
# ALL thresholds via getattr(config, ...) so they are tunable + backtest-gated.
# Nothing here runs live until config.FILTERS_V2 = True.

import json
import logging

log = logging.getLogger(__name__)

_METRICS_PATH = "/home/ubuntu/trading-bot/stock_metrics.json"
_metrics_cache = None


def _load_metrics():
    global _metrics_cache
    if _metrics_cache is None:
        try:
            with open(_METRICS_PATH) as f:
                _metrics_cache = json.load(f).get("metrics", {})
        except Exception as e:
            log.warning("filters_v2: could not load metrics (%s) -- EMA gates skipped", e)
            _metrics_cache = {}
    return _metrics_cache


def _m(ticker):
    return _load_metrics().get(ticker, {})


def _passes_ema(cfg, ticker, ltp, span_key):
    """price > pre-computed EMA(span). Missing metric -> pass (fail-open, logged)."""
    ema = _m(ticker).get(span_key, 0)
    if not ema:
        return True
    return ltp >= ema


def apply_regime_filters(candidates, state, cfg, rvol_fn=None, sector_ok_fn=None, force=False):
    """
    candidates: list[dict] with at least 'ticker','ltp','gap_pct'.
    state:      'BEARISH-DEFENSIVE' | 'CHOPPY' | 'NORMAL' | 'TRENDING'
    cfg:        the config module.
    rvol_fn:    optional fn(candidate)->float time-adjusted RVol. None -> skip RVol gate.
    sector_ok_fn: optional fn(candidate)->bool for BEARISH sector anchor. None -> skip.
    Returns:    filtered list[dict] (may be []). Sets c['_size_mult'] where relevant.
    """
    if not getattr(cfg, "FILTERS_V2", False) and not force:
        return candidates  # master flag OFF (and not shadow) -> no-op passthrough

    def _rvol(c):
        if rvol_fn is None:
            return None
        try:
            return float(rvol_fn(c))
        except Exception:
            return None

    # ---- CHOPPY: hard pause, but shadow-log what it WOULD have taken ----
    if state == "CHOPPY" and getattr(cfg, "CHOPPY_PAUSE", True):
        shadow = [c.get("ticker", "?") for c in candidates[:10]]
        log.info("FILTERS_V2 CHOPPY PAUSE -> 0 trades. Shadow (would-have): %s", shadow)
        return []

    kept = []
    for c in candidates:
        tkr = c.get("ticker", "")
        ltp = float(c.get("ltp", 0) or 0)
        gap = float(c.get("gap_pct", 0) or 0)
        rv = _rvol(c)

        if state == "TRENDING":
            floor = getattr(cfg, "GAP_FLOOR_TRENDING", 2.5)
            ceil = getattr(cfg, "GAP_CEIL_TRENDING", 7.5)
            rmin = getattr(cfg, "RVOL_TRENDING", 3.5)
            if not (floor <= gap <= ceil):
                continue
            if rv is not None and rv < rmin:
                continue
            if not _passes_ema(cfg, tkr, ltp, "ema5"):
                continue
            kept.append(c)

        elif state == "NORMAL":
            rmin = getattr(cfg, "RVOL_NORMAL", 4.0)
            # GAP BYPASS: strong gap (>=4%) overrides RVOL gate
            # PINELABS +4.53% had score 60/180 but was cut by RVOL — this fixes it
            _gap_bypass_threshold = getattr(cfg, "GAP_RVOL_BYPASS_PCT", 4.0)
            if abs(c.get("gap_pct", 0)) >= _gap_bypass_threshold:
                rmin = 0.0  # bypass RVOL gate for strong-gap stocks
            # keep existing gap behavior (GAP_MIN + volume bypass already applied upstream)
            if rv is not None and rv < rmin:
                continue
            # Direction-aware EMA: LONG needs price > EMA20, SHORT needs price < EMA20
            _direction = c.get("direction", "LONG")
            if _direction == "SHORT":
                # SHORT direction: ltp <= ema20 confirms weakness (reversed check)
                _ema20 = _m(tkr).get("ema20", 0)
                if _ema20 and ltp > _ema20:
                    continue  # price above EMA = not weak enough for short
            else:
                if not _passes_ema(cfg, tkr, ltp, "ema20"):
                    continue  # price below EMA = not strong enough for long
            kept.append(c)

        elif state == "BEARISH-DEFENSIVE":
            rmin = getattr(cfg, "RVOL_BEARISH", 5.0)
            lo = getattr(cfg, "BEARISH_GAP_LO", -0.5)
            hi = getattr(cfg, "BEARISH_GAP_HI", 0.5)
            if not (lo <= gap <= hi):          # flat openers only
                continue
            if rv is not None and rv < rmin:
                continue
            if sector_ok_fn is not None:
                try:
                    if not sector_ok_fn(c):
                        continue
                except Exception:
                    continue
            c["_size_mult"] = getattr(cfg, "BEARISH_SIZE_MULT", 0.5)  # half-size
            c["_bearish_rotation"] = True
            kept.append(c)

        else:
            # unknown state -> pass through unchanged (fail-open, logged once by caller)
            kept.append(c)

    log.info("FILTERS_V2 %s: %d/%d candidates kept", state, len(kept), len(candidates))
    if state == "BEARISH-DEFENSIVE":
        maxb = getattr(cfg, "BEARISH_MAX_CANDIDATES", 3)
        # Rank survivors by RVol (conviction) before the cap, so the top-N
        # are highest-RVol, not positional. None RVol sorts last (stable).
        kept.sort(key=lambda c: (_rvol(c) is not None, _rvol(c) if _rvol(c) is not None else 0.0), reverse=True)
        kept = kept[:maxb]
    return kept


def shadow_log(state, before, kept, rvol_fn=None,
               path="/home/ubuntu/trading-bot/filters_v2_shadow.log"):
    """Write one JSON line per scan: what FILTERS_V2 WOULD have selected. Non-trading."""
    import json as _j, datetime as _dt
    kept_ids = set(id(c) for c in kept)
    rec = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "state": state,
        "n_before": len(before),
        "n_kept": len(kept),
        "kept": [c.get("ticker", "?") for c in kept],
        "dropped": [c.get("ticker", "?") for c in before if id(c) not in kept_ids],
    }
    if rvol_fn is not None:
        rv = {}
        for c in before:
            try:
                rv[c.get("ticker", "?")] = round(float(rvol_fn(c)), 2)
            except Exception:
                rv[c.get("ticker", "?")] = None
        rec["rvol"] = rv
    try:
        with open(path, "a") as f:
            f.write(_j.dumps(rec) + "\n")
    except Exception as e:
        log.warning("shadow_log write failed: %s", e)
