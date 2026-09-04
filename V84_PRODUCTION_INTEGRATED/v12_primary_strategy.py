"""Single-authority V12 live strategy adapter.
Discovery -> independent raw scoring -> V12 hardened decision. Fail closed.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict
import math
from v12_independent_scoring import V12IndependentScorer
from v82_strategy import final_decision as _v82_final_decision
from V11_V12_HARDENED_COMBINED_PATCH import Candidate, Market, Side, StrategyConfig, evaluate_candidate, Decision

class V12PrimaryStrategy:
    VERSION = "V12_PRIMARY_2026_09_03"
    def __init__(self, config=None):
        self.scorer = V12IndependentScorer()
        self.config = config or StrategyConfig()

    @staticmethod
    def _finite(v, default=0.0):
        try:
            v=float(v); return v if math.isfinite(v) else default
        except Exception: return default

    def evaluate(self, c: Dict[str,Any], f: Dict[str,Any], market_regime: str="NORMAL") -> Dict[str,Any]:
        try:
            df=f.get("df")
            if df is None or len(df) < 5:
                return self._wait("INSUFFICIENT_BARS", c)
            prev_close=self._finite(c.get("prev_close", f.get("prev_close",0)))
            nifty=f.get("nifty_df") or f.get("nifty_data")
            orb={"high": f.get("orb_high",c.get("orb_high")), "low": f.get("orb_low",c.get("orb_low"))}
            raw=self.scorer.compute_all(c.get("symbol","?"), df, nifty, c.get("sector_data") or {}, prev_close, orb)
            # V12 DISCOVERY AUTHORITY: Early Discovery is authoritative for discovery_score.
            # Independent scorer provides setup_score + entry_score only.
            _ed = c.get("_discovery_score", 0)
            if _ed and _ed > 0:
                raw["discovery_score"] = float(_ed)
            # V12 SETUP+ENTRY AUTHORITY: V8.2 rich components are authoritative.
            # Independent scorer setup is quantized (only 3 values: 46/47/61).
            # V8.2 setup_quality (0-15) and entry_quality (0-10) have genuine variance.
            try:
                _v82d = _v82_final_decision(f)
                _v82_sq = float(_v82d.get("setup_quality", 0) or 0)
                _v82_eq = float(_v82d.get("entry_quality", 0) or 0)
                if _v82_sq > 0:
                    raw["setup_score"] = round(_v82_sq / 15 * 100, 1)
                if _v82_eq > 0:
                    raw["entry_score"] = round(_v82_eq / 10 * 100, 1)
            except Exception as _v82_exc:
                raw["_v82_authority_fallback"] = True
                raw["_v82_authority_error"] = type(_v82_exc).__name__
            # Fresh quote is authoritative for price/drift.
            raw["entry_drift_pct"] = self._finite(f.get("entry_drift_pct",0))
            raw["signal_age_seconds"] = self._finite(f.get("signal_age_seconds",0))
            raw["execution_quality_ok"] = bool(f.get("execution_quality_ok", True))
            # Composite phase: do not label a strong trend mature solely from range consumption.
            raw["phase"] = self._phase_guard(raw, f)
            raw["breakout_accepted"] = raw.get("breakout_acceptance",0) >= 60
            raw["setup_type"] = self._setup_type(raw, f)
            side = self._side(c, raw, f)
            candidate=Candidate(symbol=c.get("symbol","?"), market=Market.NSE, side=side,
                timestamp=datetime.now().isoformat(), price=raw["price"], discovery_score=raw["discovery_score"],
                setup_score=raw["setup_score"], entry_score=raw["entry_score"], regime=str(market_regime),
                setup=raw["setup_type"], features=raw)
            result=evaluate_candidate(candidate, self.config)
            # Absolute anti-chase: >2 ATR never market-chased; wait for new setup.
            if raw.get("extension_atr",0)>2.0:
                return self._wait("EXTENSION_GT_2ATR", c, raw, side)
            # Mature candidates require a new base/reclaim; score alone cannot enter.
            if raw.get("phase")=="MATURE" and raw.get("extension_atr",0)>1.5 and not (raw.get("pullback_held") or raw.get("reclaim_confirmed")):
                return self._wait("MATURE_WAIT_NEW_BASE", c, raw, side)
            status="ENTER" if result.decision==Decision.ENTER_NOW else "WATCH"
            score=round((raw["discovery_score"]+raw["setup_score"]+raw["entry_score"])/3,1)
            return {"status":status,"reason":result.reason,"side":side.value,"final_score":score,
                    "edge":raw.get("remaining_edge_pct",0),"expected_move_pct":raw.get("remaining_edge_pct",0),
                    "entry_price":raw["price"],"setup_type":raw["setup_type"],"strategy":self.VERSION,
                    "position_pct":1.0,"v12":raw,"audit":result.audit}
        except Exception as e:
            return self._wait("V12_ERROR:"+type(e).__name__, c)

    def _wait(self, reason,c,raw=None,side=None):
        return {"status":"WATCH","reason":reason,"symbol":c.get("symbol","?"),"side":(side.value if side else c.get("side","LONG")),"strategy":self.VERSION,"final_score":0.0,"edge":0.0,"expected_move_pct":0.0,"v12":raw or {}}

    def _side(self,c,raw,f):
        # Existing system direction is only a direction input, never a legacy score dependency.
        s=str(c.get("side",f.get("side","LONG"))).upper()
        return Side.SHORT if s in ("SHORT","SELL") else Side.LONG

    def _setup_type(self, raw,f):
        if raw.get("orb_breakout"): return "ORB_CONTINUATION"
        if raw.get("vwap_reclaim"): return "VWAP_RECLAIM"
        if raw.get("pullback_held"): return "PULLBACK_CONTINUATION"
        return "MOMENTUM_CONTINUATION"

    def _phase_guard(self, raw,f):
        # Mature only when extension/range is combined with deceleration, not one threshold.
        ext=self._finite(raw.get("extension_atr"))
        mom5=abs(self._finite(f.get("momentum_5m")))
        mom15=abs(self._finite(f.get("momentum_15m")))
        decel=mom5 < mom15*0.6 if mom15>0 else False
        if ext>1.5 and decel and raw.get("bars",0)>=12: return "MATURE"
        if raw.get("bars",0)<=8: return "EARLY"
        return "DEVELOPING"
