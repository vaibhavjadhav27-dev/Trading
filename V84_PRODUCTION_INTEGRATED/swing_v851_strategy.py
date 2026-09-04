"""Swing V8.5.1 shadow strategy.

Designed for 3-15 trading-day opportunities. No live orders.
Primary objective: anticipate 6%+ moves while allowing materially larger runners.

Setups:
1) EARLY_ACCUMULATION - before breakout, asymmetric risk/reward.
2) BREAKOUT - confirmed price/volume expansion.
3) BREAKOUT_RETEST - failed-breakout risk reduced by successful retest.
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any
import math

def clamp(x,lo=0.0,hi=1.0): return max(lo,min(hi,float(x)))

@dataclass(frozen=True)
class SwingConfig:
    min_score: float = 74.0
    min_expected_move_pct: float = 6.0
    risk_per_trade_pct: float = 0.60
    max_portfolio_risk_pct: float = 2.40
    max_positions: int = 0
    max_hold_days: int = 15
    min_hold_days: int = 2
    early_accumulation_max_extension_atr: float = 1.5
    breakout_max_extension_atr: float = 2.2
    trail_activation_pct: float = 6.0
    trail_factor_at_6pct: float = 0.94
    trail_factor_at_10pct: float = 0.90
    time_stop_days: int = 10
    min_rvol: float = 1.10

@dataclass(frozen=True)
class SwingSignal:
    ticker: str
    side: str
    score: float
    entry: float
    stop: float
    expected_move_pct: float
    expected_days: int
    setup: str
    reasons: tuple

def score_swing_v851(*, market=0, sector=0, rs10=0, rs20=0, trend=0,
                     accumulation=0, volume=0, structure=0, catalyst=0,
                     entry_location=0):
    # Market is a context factor, not a hard gate.
    return round(100*(
        .07*clamp((market+1)/2) +
        .10*clamp((sector+1)/2) +
        .15*clamp((rs10+1)/2) +
        .10*clamp((rs20+1)/2) +
        .15*clamp(trend) +
        .13*clamp(accumulation) +
        .10*clamp(volume) +
        .12*clamp(structure) +
        .03*clamp(catalyst) +
        .05*clamp(entry_location)
    ),2)

def evaluate_swing(snapshot: Dict[str,Any], cfg=SwingConfig()) -> Optional[SwingSignal]:
    req=("ticker","price","sma20","sma50","sma200","atr","rs10","rs20","rvol",
         "sector_rs","market_rs","high20","low20","base_tightness","accumulation",
         "breakout","retest","close_strength")
    if any(k not in snapshot for k in req): return None
    px=float(snapshot["price"]); atr=float(snapshot["atr"])
    if px<=0 or atr<=0 or float(snapshot["rvol"])<cfg.min_rvol: return None

    trend_score=(int(px>snapshot["sma20"])+int(snapshot["sma20"]>snapshot["sma50"])+
                 int(snapshot["sma50"]>=snapshot["sma200"]))/3
    rs_score=clamp((float(snapshot["rs10"])+2)/10)
    volume=clamp((float(snapshot["rvol"])-0.8)/1.7)
    structure=clamp(float(snapshot["close_strength"]))
    accumulation=clamp(float(snapshot["accumulation"]))
    location=clamp(1-abs((px-float(snapshot["sma20"]))/atr)/3)

    extension=(px-float(snapshot["sma20"]))/atr
    setups=[]
    if bool(snapshot["breakout"]) and float(snapshot["rvol"])>=1.35 and extension<=cfg.breakout_max_extension_atr:
        setups.append(("BREAKOUT",1.0))
    if bool(snapshot["retest"]) and px>=float(snapshot["sma20"]) and structure>=0.55:
        setups.append(("BREAKOUT_RETEST",0.95))
    if (not snapshot["breakout"] and accumulation>=0.65 and trend_score>=.67 and
        extension<=cfg.early_accumulation_max_extension_atr and float(snapshot["base_tightness"])>=0.60):
        setups.append(("EARLY_ACCUMULATION",0.90))
    if not setups: return None

    setup,_=max(setups,key=lambda x:x[1])
    setup_bonus={"BREAKOUT":1.0,"BREAKOUT_RETEST":0.95,"EARLY_ACCUMULATION":0.90}[setup]
    score=score_swing_v851(
        market=float(snapshot["market_rs"]), sector=float(snapshot["sector_rs"]),
        rs10=float(snapshot["rs10"])/8, rs20=float(snapshot["rs20"])/12,
        trend=trend_score, accumulation=accumulation, volume=volume,
        structure=structure, catalyst=float(snapshot.get("catalyst",0)),
        entry_location=location
    )
    score=min(100,score+5*setup_bonus)
    # Structure stop; fallback to ATR. Keep risk tight enough to seek asymmetric moves.
    structural_stop=min(float(snapshot["low20"]), px-1.15*atr)
    stop=max(structural_stop, px-2.2*atr)
    risk_pct=(px-stop)/px*100
    if risk_pct<=0 or risk_pct>5.5: return None

    # Expected move is based on structure/ATR, capped only for scoring, not for exits.
    expected=max(6.0, min(15.0, 2.8*atr/px*100 + 3.0))
    if setup=="EARLY_ACCUMULATION": expected=max(expected,7.0)
    days=5 if setup=="BREAKOUT_RETEST" else (7 if setup=="EARLY_ACCUMULATION" else 6)

    if score<cfg.min_score or expected<cfg.min_expected_move_pct: return None
    reasons=(setup,f"RS10={snapshot['rs10']:.2f}%",f"RVOL={snapshot['rvol']:.2f}",
             f"trend={trend_score:.2f}",f"accumulation={accumulation:.2f}")
    return SwingSignal(snapshot["ticker"],"LONG",round(score,2),px,round(stop,2),
                       round(expected,2),days,setup,reasons)

def size_swing(signal:SwingSignal, capital:float, existing_risk:float,cfg=SwingConfig()):
    if capital<=0 or signal.entry<=signal.stop: return {"qty":0,"reason":"INVALID"}
    maxrisk=capital*cfg.max_portfolio_risk_pct/100
    remaining=max(0,maxrisk-existing_risk)
    trade_risk=min(capital*cfg.risk_per_trade_pct/100,remaining)
    per_share=signal.entry-signal.stop
    qty=math.floor(trade_risk/per_share)
    return {"qty":max(0,qty),"risk_rupees":round(max(0,qty*per_share),2),
            "risk_pct":round(max(0,qty*per_share)/capital*100,4),
            "remaining_risk_after":round(remaining-max(0,qty*per_share),2),
            "reason":"OK" if qty>0 else "RISK_TOO_SMALL"}

def swing_exit_v851(days_held, entry, ltp, peak, original_stop, momentum_ok=True, structure_ok=True, cfg=SwingConfig()):
    """6% is a milestone, not an automatic exit. Strong winners remain runners."""
    if ltp<=original_stop:
        return "HARD_SL", original_stop
    gain=(ltp-entry)/entry*100 if entry else 0
    peak_gain=(peak-entry)/entry*100 if entry else 0
    if peak_gain>=cfg.trail_activation_pct:
        factor=cfg.trail_factor_at_10pct if peak_gain>=10 else cfg.trail_factor_at_6pct
        trail=max(original_stop, peak*factor)
        if not momentum_ok or not structure_ok:
            trail=max(original_stop, peak*0.90)
        if ltp<=trail:
            return "TRAIL_SL", trail
        return "HOLD_RUNNER", trail
    if days_held>=cfg.time_stop_days and gain<2:
        return "TIME_STOP", original_stop
    return "HOLD", original_stop

def opportunity_switch(current_positions, candidates, capital, min_improvement_pct=3.0):
    """Return current positions that are candidates for replacement on opportunity cost."""
    # candidate expected move is compared with each holding's remaining expected move.
    switches=[]
    for p in current_positions:
        remaining=float(p.get("remaining_expected_pct",0))
        for c in candidates:
            if float(c.expected_move_pct)-remaining >= min_improvement_pct:
                switches.append((p["ticker"],c.ticker,round(c.expected_move_pct-remaining,2)))
                break
    return switches
