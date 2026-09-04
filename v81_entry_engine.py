
"""V8.1 candidate + entry decision engine.

This module is broker-agnostic and is the canonical logic used by simulation,
replay and live integration. It deliberately separates:
1) candidate discovery (>=50),
2) live entry validation (>=60),
3) Dhan deployment tier (1x/2x/4.5x).

It supports ORB, momentum continuation, pullback/reclaim, failed breakout and
failed breakdown. A prior sudden move is never rejected solely because of size.
"""
from dataclasses import dataclass, asdict
from typing import Optional

from dual_scorer import score_candidate_dual, CANDIDATE_MIN, ENTRY_MIN
from trade_policy import margin_tier, setup_entry_decision, directional_edge

@dataclass
class Decision:
    symbol:str
    side:str
    candidate_score:float
    final_score:float
    edge:float
    setup_type:str
    entry_price:float
    target:float
    expected_move_pct:float
    entry_quality:float
    setup_quality:float
    deployment_multiple:float
    status:str
    reason:str

def directional_move(entry,target,side):
    if not entry or entry<=0:return 0.0
    return ((target-entry)/entry*100) if side=="LONG" else ((entry-target)/entry*100)

def target_from_context(entry, side, atr_pct=0.0, resistance=None, support=None,
                        momentum_accel=0.0, minimum=0.40):
    """Choose a directional target; never uses abs() to turn a wrong-side target positive."""
    e=float(entry); minimum=float(minimum)
    vol=max(0.0,float(atr_pct or 0.0))*1.25
    impulse=max(0.0,float(momentum_accel or 0.0))*0.6
    desired=max(minimum,vol,impulse)
    if side=="LONG":
        cap=((float(resistance)-e)/e*100) if resistance and float(resistance)>e else None
        move=desired if cap is None else min(desired,max(0.0,cap))
        return e*(1+move/100),move
    cap=((e-float(support))/e*100) if support and float(support)<e else None
    move=desired if cap is None else min(desired,max(0.0,cap))
    return e*(1-move/100),move

def classify_setup(side, *, orb_confirmed=False, momentum_accel=0.0,
                   pullback_reclaim=False, failed_breakout=False, failed_breakdown=False,
                   sudden_move=False):
    if side=="LONG":
        if pullback_reclaim:return "L3_PULLBACK_RECLAIM"
        if failed_breakdown:return "L5_FAILED_BREAKDOWN_REVERSAL"
        if orb_confirmed:return "L1_ORB_BREAKOUT"
        if sudden_move and momentum_accel>=0.35:return "L2_MOMENTUM_CONTINUATION"
    else:
        if pullback_reclaim:return "S3_PULLBACK_REJECTION"
        if failed_breakout:return "S4_FAILED_BREAKOUT_REVERSAL"
        if orb_confirmed:return "S1_ORB_BREAKDOWN"
        if sudden_move and momentum_accel<=-0.35:return "S2_MOMENTUM_CONTINUATION"
    return "WATCH"

def evaluate_candidate(features, *, final_stage=False):
    """Evaluate one stock without touching broker state.

    Required: symbol, ltp. Optional fields are safely defaulted.
    """
    f=features; symbol=str(f.get("symbol") or f.get("ticker") or "?")
    ltp=float(f.get("ltp") or f.get("entry_price") or 0)
    rs=float(f.get("rs") or 0); rvol=float(f.get("rvol") or 0)
    mg=float(f.get("nifty_gap") or 0); srs=float(f.get("sector_rs") or 0)
    regime=str(f.get("regime") or "NORMAL").upper()
    lead=bool(f.get("sector_leading")); lag=bool(f.get("sector_against"))
    df=f.get("df")
    ml=float(f.get("momentum_5m") or 0); m15=float(f.get("momentum_15m") or 0); m30=float(f.get("momentum_30m") or 0)
    accel=ml*2+m15+m30*0.5
    # Candidate stage: setup/entry/opportunity are deliberately zero.
    Lc,Sc,_=score_candidate_dual(gap_pct=f.get("gap_pct",0),rs=rs,rvol=rvol,df=df,ltp=ltp,
        nifty_gap=mg,sector_leading=lead,sector_against=lag,sector_rs=srs,
        momentum_5m=ml,momentum_15m=m15,momentum_30m=m30,return_breakdown=True)
    if not final_stage:
        side,edge,LB,SB=directional_edge(Lc,Sc,regime,
                                         4 if lead else 0,4 if lag else 0)
        if max(Lc,Sc)<CANDIDATE_MIN:
            return {"status":"REJECT","symbol":symbol,"candidate_long":Lc,"candidate_short":Sc,
                    "reason":"candidate_score_below_50"}
        return {"status":"WATCH","symbol":symbol,"candidate_long":Lc,"candidate_short":Sc,
                "preferred_side":side,"edge":edge,"reason":"candidate_watch"}
    # Final-stage evidence.
    side_hint=str(f.get("side") or "").upper()
    side=side_hint if side_hint in ("LONG","SHORT") else ("LONG" if Lc>=Sc else "SHORT")
    orb=bool(f.get("orb_confirmed")); pull=bool(f.get("pullback_reclaim"))
    fb=bool(f.get("failed_breakout")); fbd=bool(f.get("failed_breakdown"))
    sudden=bool(f.get("sudden_move"))
    setup=classify_setup(side,orb_confirmed=orb,momentum_accel=accel,pullback_reclaim=pull,
                         failed_breakout=fb,failed_breakdown=fbd,sudden_move=sudden)
    sq=float(f.get("setup_quality") or (15 if setup!="WATCH" else 0))
    eq=float(f.get("entry_quality") or 0)
    target,move=target_from_context(ltp,side,atr_pct=f.get("atr_pct",0),
                                     resistance=f.get("resistance"),support=f.get("support"),
                                     momentum_accel=accel)
    # If caller provides a directional target, honor it only if it is on the correct side.
    supplied=f.get("target")
    if supplied:
        supplied=float(supplied)
        sm=directional_move(ltp,supplied,side)
        if sm>0: target,move=supplied,sm
    L,S,bd=score_candidate_dual(gap_pct=f.get("gap_pct",0),rs=rs,rvol=rvol,df=df,ltp=ltp,
        nifty_gap=mg,sector_leading=lead,sector_against=lag,sector_rs=srs,
        momentum_5m=ml,momentum_15m=m15,momentum_30m=m30,
        setup_quality_L=sq if side=="LONG" else 0,setup_quality_S=sq if side=="SHORT" else 0,
        entry_quality_L=eq if side=="LONG" else 0,entry_quality_S=eq if side=="SHORT" else 0,
        expected_move_pct=move,breakout_confirmed=(orb and side=="LONG"),
        breakdown_confirmed=(orb and side=="SHORT"),sudden_move=sudden,return_breakdown=True)
    final=max(L,S); other=min(L,S); edge=final-other
    ok,reason=setup_entry_decision(side=side,score=final,edge=edge,expected_move_pct=move,
                                   entry_quality=eq,setup_quality=sq,regime=regime,
                                   confirmed=bool(f.get("confirmed")),sudden_move=sudden,
                                   momentum_accel=accel)
    multiple,tier=margin_tier(final)
    status="ENTER" if ok else "WATCH"
    return Decision(symbol,side,max(Lc,Sc),final,edge,setup,ltp,target,move,eq,sq,
                    multiple,status,reason).__dict__
