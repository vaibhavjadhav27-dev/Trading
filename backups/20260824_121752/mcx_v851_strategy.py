"""MCX V8.5.1 shadow strategy.

Pure strategy layer: no broker calls and no live orders.
The integration layer must supply native MCX OHLCV candles and contract metadata.

Design:
- Native MCX price is the trigger. International/FX data is context only.
- Two primary setups: ORB continuation and VWAP trend pullback.
- A third setup, compression breakout, catches smaller early moves without forcing trades.
- Risk-first sizing; margin is a constraint, never the sizing driver.
- Profit runner uses ATR and R, not a fixed profit target.
"""
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Tuple
import math

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))

def pct(a, b):
    return (float(a)-float(b))/float(b)*100.0 if b else 0.0

@dataclass(frozen=True)
class MCXConfig:
    min_score: float = 72.0
    min_edge_pct: float = 0.20
    min_rvol: float = 1.15
    max_spread_pct: float = 0.08
    risk_per_trade_pct: float = 0.40
    max_portfolio_risk_pct: float = 1.20
    reserve_cash_pct: float = 20.0
    orb_minutes: int = 15
    max_extension_atr: float = 1.8
    stop_atr: float = 1.15
    trail_atr_strong: float = 1.60
    trail_atr_weak: float = 1.00
    max_hold_bars: int = 48

@dataclass(frozen=True)
class MCXContract:
    symbol: str
    security_id: str
    expiry: str
    lot_size: float
    margin_required: float
    tick_size: float
    bid: float
    ask: float

@dataclass(frozen=True)
class MCXSignal:
    symbol: str
    side: str
    score: float
    entry: float
    stop: float
    initial_target: float
    expected_move_pct: float
    risk_per_lot: float
    setup: str
    reasons: tuple

def score_mcx(*, orb_break=0, vwap_alignment=0, momentum=0, rvol=1,
              volatility_quality=0, global_context=0, fx_context=0,
              structure=0, spread_quality=1):
    """100-point score. Native MCX factors dominate; external context is capped."""
    return round(100.0 * (
        0.22*clamp(orb_break) +
        0.16*clamp(vwap_alignment) +
        0.16*clamp(momentum) +
        0.12*clamp((rvol-0.8)/1.7) +
        0.12*clamp(volatility_quality) +
        0.08*clamp(global_context) +
        0.05*clamp(fx_context) +
        0.07*clamp(structure) +
        0.02*clamp(spread_quality)
    ), 2)

def _side_metrics(side, px, vwap, atr, orb_high, orb_low, mom5, mom15,
                  rvol, global_bias, fx_bias, compression):
    if side == "LONG":
        aligned = px > vwap
        break_ok = px > orb_high
        momentum_ok = mom5 > 0 and mom15 > 0
        ext = (px-vwap)/atr if atr else 99
        stop = min(orb_high, vwap) - 0.15*atr
        target = px + 2.2*atr
        ctx = clamp((global_bias+1)/2)
        fx = clamp((fx_bias+1)/2)
    else:
        aligned = px < vwap
        break_ok = px < orb_low
        momentum_ok = mom5 < 0 and mom15 < 0
        ext = (vwap-px)/atr if atr else 99
        stop = max(orb_low, vwap) + 0.15*atr
        target = px - 2.2*atr
        ctx = clamp((-global_bias+1)/2)
        fx = clamp((-fx_bias+1)/2)
    orb_strength = 1.0 if break_ok else 0.0
    vwap_strength = clamp(1.0 - abs(px-vwap)/(2.0*atr)) if atr else 0
    mom_strength = clamp((abs(mom5)+abs(mom15))/2.0)
    vol_quality = clamp((rvol-1.0)/1.5)
    structure = 1.0 if momentum_ok and (break_ok or aligned) else 0.35
    setup = "ORB_CONTINUATION" if break_ok and momentum_ok else (
        "VWAP_TREND_PULLBACK" if aligned and momentum_ok and ext <= 1.2 else
        "COMPRESSION_BREAKOUT" if compression and break_ok else None
    )
    return setup, score_mcx(
        orb_break=orb_strength, vwap_alignment=vwap_strength,
        momentum=mom_strength, rvol=rvol,
        volatility_quality=clamp(1.0-abs(ext)/3.0),
        global_context=ctx, fx_context=fx,
        structure=structure, spread_quality=1.0
    ), stop, target, ext

def evaluate_mcx(snapshot: Dict[str, Any], cfg=MCXConfig()) -> Optional[MCXSignal]:
    """Evaluate one native MCX contract from a precomputed market snapshot.

    Required snapshot fields:
      price, vwap, atr, orb_high, orb_low, mom5, mom15, rvol,
      global_bias [-1,1], fx_bias [-1,1], compression(bool),
      spread_pct, lot_size, symbol.
    """
    req=("price","vwap","atr","orb_high","orb_low","mom5","mom15","rvol","spread_pct","symbol","lot_size")
    if any(k not in snapshot for k in req):
        return None
    px=float(snapshot["price"]); atr=float(snapshot["atr"])
    if px <= 0 or atr <= 0 or float(snapshot["spread_pct"]) > cfg.max_spread_pct:
        return None
    if float(snapshot["rvol"]) < cfg.min_rvol:
        return None

    candidates=[]
    for side in ("LONG","SHORT"):
        setup, score, stop, target, ext = _side_metrics(
            side, px, float(snapshot["vwap"]), atr,
            float(snapshot["orb_high"]), float(snapshot["orb_low"]),
            float(snapshot["mom5"]), float(snapshot["mom15"]),
            float(snapshot["rvol"]), float(snapshot.get("global_bias",0)),
            float(snapshot.get("fx_bias",0)), bool(snapshot.get("compression",False))
        )
        if not setup:
            continue
        if ext > cfg.max_extension_atr and setup != "ORB_CONTINUATION":
            continue
        risk=abs(px-stop)*float(snapshot["lot_size"])
        move=abs(target-px)/px*100
        edge=move*0.65  # conservative fraction of initial ATR objective
        reasons=(setup, f"RVOL={snapshot['rvol']:.2f}", f"VWAP={'aligned' if (px>snapshot['vwap'])==(side=='LONG') else 'conflict'}")
        if score >= cfg.min_score and edge >= cfg.min_edge_pct:
            candidates.append(MCXSignal(snapshot["symbol"],side,score,px,stop,target,move,risk,setup,reasons))
    if not candidates:
        return None
    candidates.sort(key=lambda x:x.score, reverse=True)
    return candidates[0]

def size_mcx(signal: MCXSignal, capital: float, existing_risk: float,
             margin_per_lot: float, cfg=MCXConfig()) -> Dict[str, Any]:
    """Risk-first quantity. Never force one lot when risk budget cannot support it."""
    if capital <= 0 or signal.risk_per_lot <= 0:
        return {"qty":0,"reason":"INVALID_CAPITAL_OR_RISK"}
    max_risk=capital*cfg.max_portfolio_risk_pct/100
    remaining=max(0.0, max_risk-existing_risk)
    trade_risk=min(capital*cfg.risk_per_trade_pct/100, remaining)
    risk_qty=math.floor(trade_risk/signal.risk_per_lot)
    margin_qty=math.floor((capital*(1-cfg.reserve_cash_pct/100)-0.0)/margin_per_lot) if margin_per_lot>0 else 0
    qty=max(0, min(risk_qty, margin_qty))
    return {
        "qty":qty,
        "risk_qty":risk_qty,
        "margin_qty":margin_qty,
        "risk_rupees":round(qty*signal.risk_per_lot,2),
        "margin_required":round(qty*margin_per_lot,2),
        "remaining_risk_after":round(remaining-qty*signal.risk_per_lot,2),
        "reason":"OK" if qty>0 else "RISK_OR_MARGIN_TOO_SMALL"
    }

def update_mcx_trail(side, entry, peak, atr, current_sl, momentum_ok=True, setup_failed=False):
    """Broker-side trail target; integration layer must PUT the pending SL."""
    if side=="LONG":
        desired=peak-(1.60 if momentum_ok and not setup_failed else 1.00)*atr
        return max(current_sl, desired)
    desired=peak+(1.60 if momentum_ok and not setup_failed else 1.00)*atr
    return min(current_sl, desired)

def select_contract(contracts, expected_move_by_symbol):
    """Select the best liquid contract by net opportunity, not merely margin efficiency."""
    valid=[]
    for c in contracts:
        spread=(c.ask-c.bid)/c.bid*100 if c.bid else 999
        if c.lot_size<=0 or c.margin_required<=0 or spread>0.08:
            continue
        move=float(expected_move_by_symbol.get(c.symbol,0))
        score=move/max(c.margin_required,1)
        valid.append((score,c))
    valid.sort(key=lambda x:x[0], reverse=True)
    return valid[0][1] if valid else None
