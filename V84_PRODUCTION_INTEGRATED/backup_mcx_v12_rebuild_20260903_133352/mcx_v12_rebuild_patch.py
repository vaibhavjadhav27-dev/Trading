"""MCX V12.4 SHADOW REBUILD PATCH — drop-in overlay for mcx_v12_engine.

Fixes found in the 2026-09-03 server audit:
- real candle volume instead of LTP pseudo-volume
- robust Dhan intraday payload normalization
- reachable ORB / pullback / VWAP reclaim / momentum / US-open routing
- structural expected-R instead of constant 4R
- shadow-safe execution quality: verified when bid/ask exists, explicit UNVERIFIED otherwise
- late-start ORB reconstruction from candles
- round-robin candle refresh to reduce rate-limit pressure
- no live trading: this patch force-disables live mode

Install by placing this file beside mcx_v12_engine.py and changing the MCX import in
shadow_orchestrator.py to import MCXEngine, MCXConfig from mcx_v12_rebuild_patch.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple
import logging, math, time

import mcx_v12_engine as base

log = logging.getLogger("mcx_v12_rebuild")
IST = base.IST


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _series(payload: Dict[str, Any], *names: str) -> List[Any]:
    for n in names:
        v = payload.get(n)
        if isinstance(v, list):
            return v
    return []


def normalize_intraday(payload: Any) -> List[Dict[str, Any]]:
    """Accept common Dhan chart response shapes and return sorted OHLCV bars."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict) and isinstance(data.get("candles"), list):
        data = data["candles"]
    if isinstance(data, list):
        rows = data
    else:
        ts = _series(data, "timestamp", "timestamps", "time")
        op = _series(data, "open")
        hi = _series(data, "high")
        lo = _series(data, "low")
        cl = _series(data, "close")
        vo = _series(data, "volume")
        n = min(len(ts), len(op), len(hi), len(lo), len(cl), len(vo))
        rows = [dict(timestamp=ts[i], open=op[i], high=hi[i], low=lo[i], close=cl[i], volume=vo[i]) for i in range(n)]
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = r.get("timestamp", r.get("time", r.get("ts", r.get("date"))))
        try:
            if isinstance(ts, (int, float)):
                # Dhan timestamps are normally epoch seconds.
                dt = datetime.fromtimestamp(float(ts), tz=IST)
            elif isinstance(ts, str):
                s = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=IST)
                else:
                    dt = dt.astimezone(IST)
            else:
                continue
        except Exception:
            continue
        c = {
            "ts": dt.isoformat(), "minute": dt.strftime("%Y-%m-%d %H:%M"),
            "open": _f(r.get("open")), "high": _f(r.get("high")),
            "low": _f(r.get("low")), "close": _f(r.get("close")),
            "volume": _f(r.get("volume")),
        }
        if c["high"] > 0 and c["low"] > 0 and c["close"] > 0 and c["volume"] >= 0:
            out.append(c)
    out.sort(key=lambda x: x["ts"])
    dedup = {}
    for c in out:
        dedup[c["minute"]] = c
    return list(dedup.values())


class MCXDataManager(base.MCXDataManager):
    def __init__(self, config):
        super().__init__(config)
        self._last_candle_refresh: Dict[str, float] = {}
        self._refresh_cursor = 0
        self.candle_refresh_seconds = 75.0
        self.candle_refresh_batch = 2

    def update_prices(self, raw_response: Dict[str, Any]) -> Dict[str, float]:
        # LTP updates price only. Never turn cumulative/missing LTP fields into candle volume.
        if base._V12_AVAILABLE:
            parsed = base.parse_dhan_mcx_ltp(raw_response)
        else:
            data = raw_response.get("data", {}) if isinstance(raw_response, dict) else {}
            parsed = data.get("MCX_COMM", {}) if isinstance(data, dict) else {}
        now = time.monotonic()
        prices = {}
        for key, qs in self.quotes.items():
            quote = parsed.get(str(qs.security_id), parsed.get(qs.security_id, {}))
            if not isinstance(quote, dict):
                continue
            px = base._num(quote.get("last_price"))
            if px is None or px <= 0:
                continue
            qs.last_price = px
            qs.last_update = now
            qs.volume = 0.0
            # Some quote providers may include bid/ask. Preserve them if present, but do not assume.
            bid = base._num(quote.get("bid_price", quote.get("best_bid")))
            ask = base._num(quote.get("ask_price", quote.get("best_ask")))
            if bid and bid > 0: qs.bid = bid
            if ask and ask > 0: qs.ask = ask
            prices[key] = px
        self._consecutive_429 = 0
        return prices

    def hydrate_candles(self, provider, contracts: Dict[str, Any], max_contracts: Optional[int] = None):
        if not provider or not hasattr(provider, "intraday"):
            return
        keys = list(contracts.keys())
        if not keys:
            return
        now = time.monotonic()
        due = [k for k in keys if now - self._last_candle_refresh.get(k, 0) >= self.candle_refresh_seconds]
        if not due:
            return
        limit = max_contracts or self.candle_refresh_batch
        start = self._refresh_cursor % len(keys)
        ordered = keys[start:] + keys[:start]
        selected = [k for k in ordered if k in due][:limit]
        self._refresh_cursor = (start + max(1, len(selected))) % len(keys)
        today = date.today().isoformat()
        for key in selected:
            qs = self.quotes.get(key)
            if not qs:
                continue
            try:
                raw = provider.intraday(str(qs.security_id), instrument="FUTCOM", interval="1",
                                         from_dt=today, to_dt=today)
                bars = normalize_intraday(raw)
                real = [b for b in bars if _f(b.get("volume")) > 0]
                if len(bars) >= 15 and real:
                    qs.candles_1m = bars[-450:]
                    # baseline for same-session fallback uses real completed candle volumes only
                    qs.tod_volume_history = [b["volume"] for b in bars[-121:-1] if b["volume"] > 0]
                    self._last_candle_refresh[key] = now
                    log.info("MCX V12.4 candles %s: bars=%d real_volume=%d", key, len(bars), len(real))
                else:
                    log.warning("MCX V12.4 candles unusable %s: bars=%d real_volume=%d", key, len(bars), len(real))
            except Exception as e:
                log.warning("MCX V12.4 candle refresh %s failed: %s", key, e)

    def record_orb_for_session(self, key: str, state: base.SessionState, orb_bars: int) -> Optional[Tuple[float, float]]:
        qs = self.quotes.get(key)
        if not qs:
            return None
        bars = qs.candles_1m
        if state in (base.SessionState.MORNING_ORB, base.SessionState.MORNING_ACTIVE, base.SessionState.AFTERNOON_SELECTIVE):
            hh, mm = 9, 0
        else:
            hh, mm = 17, 0
        session_bars = []
        for b in bars:
            try:
                dt = datetime.fromisoformat(str(b.get("ts", "")).replace("Z", "+00:00"))
                dt = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
                if dt.date() == date.today() and (dt.hour, dt.minute) >= (hh, mm):
                    session_bars.append(b)
            except Exception:
                continue
        if len(session_bars) < orb_bars:
            return None
        orb = session_bars[:orb_bars]
        qs.orb_high = max(_f(c["high"]) for c in orb)
        qs.orb_low = min(_f(c["low"]) for c in orb)
        qs.orb_complete = qs.orb_high > qs.orb_low > 0
        return (qs.orb_high, qs.orb_low) if qs.orb_complete else None


def _atr(candles):
    return base.atr14(candles)


def _crossed_vwap(candles, vwap, side):
    if len(candles) < 3 or vwap <= 0:
        return False
    prev = _f(candles[-2].get("close")); cur = _f(candles[-1].get("close"))
    return (prev <= vwap < cur) if side == "LONG" else (prev >= vwap > cur)


class MCXScorer(base.MCXScorer):
    def score(self, key: str, side: str, qs: base.QuoteState) -> Dict[str, Any]:
        candles = qs.candles_1m
        px = qs.last_price
        if len(candles) < 20:
            return {"score": 0.0, "valid": False, "reason": "INSUFFICIENT_CANDLES", "side": side}
        atr = _atr(candles)
        if atr <= 0:
            return {"score": 0.0, "valid": False, "reason": "NO_ATR", "side": side}
        real_vol = [b for b in candles if _f(b.get("volume")) > 0]
        if len(real_vol) < 5:
            return {"score": 0.0, "valid": False, "reason": "NO_REAL_VOLUME", "side": side}
        vw = base.vwap_calc(candles)
        mom5 = base.momentum_pct(candles, 5)
        mom15 = base.momentum_pct(candles, 15)
        rv = base.rvol_tod(candles, qs.tod_volume_history)
        latest = candles[-1]
        aligned = px > vw if side == "LONG" else px < vw
        mom_ok = (mom5 > 0 and mom15 > 0) if side == "LONG" else (mom5 < 0 and mom15 < 0)
        breakout = qs.orb_complete and ((px > qs.orb_high) if side == "LONG" else (px < qs.orb_low))
        # Pullback = prior trend/breakout, price near VWAP, fresh directional resumption.
        near_vwap = abs(px - vw) <= max(0.45 * atr, 1e-9)
        prior_break = any(((c["high"] > qs.orb_high) if side == "LONG" else (c["low"] < qs.orb_low)) for c in candles[-8:-1]) if qs.orb_complete else False
        pullback = prior_break and near_vwap and aligned and ((px > _f(candles[-2]["close"])) if side == "LONG" else (px < _f(candles[-2]["close"])))
        reclaim = _crossed_vwap(candles, vw, side) and (abs(mom5) > 0.02)
        continuation = aligned and mom_ok and rv >= 1.15 and not breakout
        ref = qs.orb_high if side == "LONG" else qs.orb_low
        ext_atr = abs(px - ref) / atr if qs.orb_complete and ref > 0 else 0.0
        tier = base.classify_extension(ext_atr, self.cfg)
        # Structural stop: beyond recent structure but at least 0.35 ATR, capped at 1.5 ATR.
        look = candles[-8:]
        if side == "LONG":
            structural_stop = min(_f(c["low"]) for c in look) - 0.10 * atr
            risk = px - structural_stop
            projection = qs.orb_high + max(qs.orb_high - qs.orb_low, 1.5 * atr) if qs.orb_complete else px + 2.0 * atr
            target = max(projection, px + 1.5 * atr)
        else:
            structural_stop = max(_f(c["high"]) for c in look) + 0.10 * atr
            risk = structural_stop - px
            projection = qs.orb_low - max(qs.orb_high - qs.orb_low, 1.5 * atr) if qs.orb_complete else px - 2.0 * atr
            target = min(projection, px - 1.5 * atr)
        risk = max(0.35 * atr, min(risk, 1.5 * atr))
        structural_stop = px - risk if side == "LONG" else px + risk
        reward = max(0.0, (target - px) if side == "LONG" else (px - target))
        expected_r = reward / risk if risk > 0 else 0.0
        remaining = max(0.0, min(1.0, reward / max(abs(target - ref), atr))) if qs.orb_complete else min(1.0, reward / max(2.0 * atr, 1e-9))
        acceptance = min(1.0, abs(px - ref) / max(0.30 * atr, 1e-9)) if breakout and qs.orb_complete else 0.70 if (pullback or reclaim) else 0.55
        breakout_pts = 24 if breakout else 0
        vwap_pts = 18 if aligned else 0
        mom_pts = min(22, 22 * (abs(mom5) + abs(mom15)) / 0.40)
        vol_pts = min(18, 18 * max(0, rv - 1) / 1.5)
        setup_bonus = 12 if pullback or reclaim else (8 if continuation else 0)
        ext_penalty = 0 if tier == base.ExtensionTier.FRESH else 5 if tier == base.ExtensionTier.EXTENDED_TRADABLE else 12 if tier == base.ExtensionTier.CONTINUATION_ONLY else 25
        score = max(0.0, min(100.0, breakout_pts + vwap_pts + mom_pts + vol_pts + setup_bonus - ext_penalty))
        valid = (breakout or pullback or reclaim or continuation) and tier != base.ExtensionTier.EXHAUSTED
        return {
            "score": round(score, 2), "valid": valid, "side": side, "price": px,
            "atr": round(atr, 6), "vwap": round(vw, 6), "mom5": round(mom5, 6), "mom15": round(mom15, 6),
            "rvol": round(rv, 4), "breakout": breakout, "pullback": pullback, "vwap_reclaim": reclaim,
            "continuation": continuation, "extension_atr": round(ext_atr, 4), "extension_tier": tier.value,
            "acceptance": round(acceptance, 4), "expected_r": round(expected_r, 4),
            "remaining_edge": round(remaining, 4), "structural_stop": round(structural_stop, 6), "target": round(target, 6),
            "volume_source": "INTRADAY_CANDLES", "real_volume_bars": len(real_vol),
        }


class MCXStrategyRouter(base.MCXStrategyRouter):
    def route(self, snapshot, session):
        allowed = session.allowed_strategies()
        if not allowed:
            return None
        if session.is_us_open_window and snapshot.get("breakout") and base.MCXStrategy.US_OPEN_REACTION in allowed:
            return base.MCXStrategy.US_OPEN_REACTION
        if snapshot.get("vwap_reclaim") and base.MCXStrategy.VWAP_RECLAIM in allowed:
            return base.MCXStrategy.VWAP_RECLAIM
        if snapshot.get("pullback") and base.MCXStrategy.PULLBACK_ENTRY in allowed:
            return base.MCXStrategy.PULLBACK_ENTRY
        if snapshot.get("breakout") and base.MCXStrategy.ORB_BREAKOUT in allowed:
            return base.MCXStrategy.ORB_BREAKOUT
        if snapshot.get("continuation") and base.MCXStrategy.MOMENTUM_CONTINUATION in allowed:
            return base.MCXStrategy.MOMENTUM_CONTINUATION
        return None


class MCXEngine(base.MCXEngine):
    def __init__(self, config=None, data_provider=None, gateway=None):
        cfg = config or base.MCXConfig()
        cfg.live = False  # hard safety: patch is shadow-only
        cfg.version = "MCX_V12_4_REBUILD_2026_09_03"
        super().__init__(cfg, data_provider=data_provider, gateway=None)
        self.data = MCXDataManager(self.cfg)
        self.scorer = MCXScorer(self.cfg)
        self.router = MCXStrategyRouter(self.cfg)

    def _execution_quality(self, qs):
        # Real check when quote fields exist. In shadow mode, unknown depth is explicitly logged,
        # not fabricated as a successful live execution-quality check.
        if qs.bid > 0 and qs.ask > qs.bid:
            tick = 0.05
            spread_ticks = (qs.ask - qs.bid) / tick
            return (spread_ticks <= self.cfg.max_spread_ticks, True, "VERIFIED_LTP_QUOTE")
        return (True, False, "UNVERIFIED_SHADOW_ONLY")

    def _scan_cycle(self):
        # Refresh a limited number of candle histories first; LTP remains one batched call.
        self.data.hydrate_candles(self.data_provider, self.contracts)
        # Call base only for the LTP path and position management, but replace its signal loop.
        if self.data.circuit_breaker_active:
            return
        if not self.data_provider:
            return
        sids = [self.data.quotes[k].security_id for k in self.contracts if k in self.data.quotes]
        try:
            raw = self.data_provider.marketfeed_ltp(sids)
            if isinstance(raw, dict) and "data" not in raw:
                wrapped = {str(sid): ({"last_price": float(v)} if isinstance(v, (int, float)) else v) for sid, v in raw.items()}
                raw = {"data": {"MCX_COMM": wrapped}}
            self.data.update_prices(raw)
            self.data.report_success()
        except RuntimeError as e:
            if "429" in str(e): self.data.report_429()
            return
        except Exception as e:
            log.warning("MCX V12.4 LTP error: %s", e); return
        # Reconstruct ORB even after a late process restart.
        for key in self.contracts:
            if not self._orb_recorded.get(key):
                result = self.data.record_orb_for_session(key, self.session.state, self.cfg.orb_bars)
                if result:
                    self._orb_recorded[key] = True
                    log.info("MCX V12.4 ORB %s H=%s L=%s", key, *result)
        for key in self.contracts:
            qs = self.data.quotes.get(key)
            if not qs or not qs.orb_complete or not self.data.is_healthy(key):
                continue
            if key in self.positions.positions and self.positions.positions[key].status == "OPEN":
                self._manage_position(key, qs); continue
            for side in ("LONG", "SHORT"):
                snap = self.scorer.score(key, side, qs)
                if not snap.get("valid"):
                    continue
                strategy = self.router.route(snap, self.session)
                if not strategy:
                    continue
                eq_ok, eq_verified, eq_reason = self._execution_quality(qs)
                features = {
                    "data_healthy": 1.0, "execution_quality_ok": 1.0 if eq_ok else 0.0,
                    "signal_age_seconds": 0.0, "entry_drift_pct": 0.0,
                    "volume_expansion": snap["rvol"], "expected_r": snap["expected_r"],
                    "remaining_edge_pct": snap["remaining_edge"],
                }
                decision, reason, audit_data = self.entry.evaluate(snap, strategy, features)
                log.info("MCX V12.4: %s %s | %s | strategy=%s score=%.1f rvol=%.2f R=%.2f EQ=%s | %s",
                         key, side, decision, strategy.value, snap["score"], snap["rvol"], snap["expected_r"], eq_reason, reason)
                self.audit.log(base.AuditEvent.ENTRY_DECISION, {
                    "key": key, "side": side, "decision": decision, "strategy": strategy.value,
                    "score": snap["score"], "reason": reason, "execution_quality": eq_reason,
                    "execution_quality_verified": eq_verified, "volume_source": snap["volume_source"], **audit_data})
                if decision == "ENTER_NOW":
                    self._execute_entry(key, side, snap, strategy)


# Re-export configuration and common names expected by the orchestrator.
MCXConfig = base.MCXConfig
MCXStrategy = base.MCXStrategy
MCXSessionManager = base.MCXSessionManager
MCXStrategyProfile = base.MCXStrategyProfile
