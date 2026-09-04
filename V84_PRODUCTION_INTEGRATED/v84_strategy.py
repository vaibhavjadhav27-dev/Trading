"""V8.4 strategy adapter for the proven V8.2 production orchestration.

This module deliberately does NOT place orders. It converts the existing V8.2
candidate feature object into the new five-mode V8.4 opportunity decision.
"""
from __future__ import annotations
from dataclasses import asdict
from v84.scoring import evaluate


def _market_bias(features):
    """Directional NIFTY context in [-1,1], using live VWAP/EMA structure when available."""
    n=features.get("nifty_data") or {}
    px=float(n.get("ltp",0) or 0); vw=float(n.get("vwap",0) or 0)
    e20=float(n.get("ema20",0) or 0); e50=float(n.get("ema50",0) or 0)
    vals=[]
    if px and vw: vals.append(1 if px>vw else -1 if px<vw else 0)
    if e20 and e50: vals.append(1 if e20>e50 else -1 if e20<e50 else 0)
    if vals:return sum(vals)/len(vals)
    gap=float(n.get("gap",features.get("nifty_gap",0)) or 0)
    return max(-1,min(1,gap/1.0))


def _sector_bias(features):
    if features.get("sector_leading"): return 1.0
    if features.get("sector_against"): return -1.0
    return 0.0


def final_decision(features):
    f=dict(features)
    df=f.get("df")
    if df is None or len(df)<20:return {"status":"WATCH","reason":"INSUFFICIENT_DATA"}
    sig=evaluate(
        df,
        rs=float(f.get("rs",0) or 0),
        market_bias=_market_bias(f),
        sector_bias=_sector_bias(f),
        avg_daily_volume=float(f.get("avg_daily_volume",f.get("adv_20d",0)) or 0),
    )
    if sig is None:
        return {"status":"WATCH","reason":"NO_QUALIFYING_V84_SETUP","symbol":f.get("symbol","?")}
    return {
        "status":"ENTER",
        "symbol":str(f.get("symbol","?")),
        "side":sig.side,
        "candidate_score":float(f.get("candidate_score",0) or 0),
        "final_score":float(sig.score),
        "edge":float(sig.edge),
        "setup_type":sig.mode,
        "entry_price":float(sig.entry),
        "stop":float(sig.stop),
        "target":float(sig.target),
        "expected_move_pct":float(sig.expected_move_pct),
        "risk_pct":float(sig.risk_pct),
        "entry_quality":0.0,
        "setup_quality":0.0,
        "deployment_multiple":1.0,
        "reason":sig.reason,
    }
