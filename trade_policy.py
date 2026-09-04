
"""V8.1 trading policy: two-stage score, two-sided, all market regimes."""
RAW_MAX_SCORE=100.0
CANDIDATE_MIN=50.0
MIN_CONVICTION=60.0
EDGE_MIN=8.0
EDGE_MIN_CHOPPY=6.0

def confidence_pct(score):
    if score is None: return 0.0
    return max(0.0,min(100.0,float(score)))

def margin_tier(confidence):
    c=confidence_pct(confidence)
    if c<60: return 0.0,"NO_TRADE"
    if c<=70: return 1.0,"BALANCE_1X"
    if c<=80: return 2.0,"DHAN_2X"
    return 4.5,"DHAN_4.5X"

def expected_move_ok(entry,target,side,min_move_pct=0.40):
    try:
        if not entry or not target or entry<=0:return False
        move=((target-entry)/entry*100) if side=="LONG" else ((entry-target)/entry*100)
        return move>=float(min_move_pct)
    except Exception:return False

def directional_edge(long_score,short_score,regime="NORMAL",sector_boost_L=0,sector_boost_S=0):
    L=-1 if long_score is None else float(long_score)
    S=-1 if short_score is None else float(short_score)
    if L<0 and S<0:return "NO_TRADE",0,L,S
    r=str(regime or "NORMAL").upper()
    # Market context is a small bias only; stock score remains dominant.
    if r in ("TRENDING_UP","BULLISH"): L+=3; S-=1
    elif r in ("TRENDING_DOWN","BEARISH"): L-=1; S+=3
    L+=float(sector_boost_L or 0); S+=float(sector_boost_S or 0)
    side="LONG" if L>=S else "SHORT"
    return side,abs(L-S),L,S

def pick_side(regime,long_score,short_score,sector_boost_L=0,sector_boost_S=0,entry_quality_L=0,entry_quality_S=0):
    side,edge,L,S=directional_edge(long_score,short_score,regime,sector_boost_L,sector_boost_S)
    if side=="NO_TRADE": return side,"no directional score"
    L+=float(entry_quality_L or 0); S+=float(entry_quality_S or 0)
    side="LONG" if L>=S else "SHORT"; edge=abs(L-S)
    req=EDGE_MIN_CHOPPY if str(regime).upper()=="CHOPPY" else EDGE_MIN
    winner=max(L,S)
    if winner<CANDIDATE_MIN:
        return "NO_TRADE",f"winner={winner:.1f} below candidate floor {CANDIDATE_MIN}"
    if winner<MIN_CONVICTION:
        return "WATCH",f"winner={winner:.1f}; needs live entry confirmation to reach {MIN_CONVICTION}"
    if edge<req:
        return "WATCH",f"directional edge={edge:.1f} < {req:.1f}"
    return side,f"{regime}: {side} wins L={L:.1f} S={S:.1f} edge={edge:.1f}"

def setup_entry_decision(*, side, score, edge, expected_move_pct, entry_quality,
                         setup_quality, regime="NORMAL", confirmed=False,
                         sudden_move=False, momentum_accel=0.0):
    """Final deterministic entry gate. Returns (bool, reason)."""
    score=float(score or 0); edge=float(edge or 0); move=float(expected_move_pct or 0)
    eq=float(entry_quality or 0); sq=float(setup_quality or 0)
    r=str(regime or "NORMAL").upper()
    if score<MIN_CONVICTION:return False,f"score {score:.1f}<60"
    if edge < (EDGE_MIN_CHOPPY if r=="CHOPPY" else EDGE_MIN):
        # Choppy alternative: a very strong named setup can compensate for a
        # smaller directional edge, but never for a score below 60.
        if not (r=="CHOPPY" and sq>=13 and eq>=8): return False,f"edge {edge:.1f} too small"
    if move<0.40:return False,f"remaining move {move:.2f}%<0.40%"
    if eq<6:return False,f"entry quality {eq:.1f}<6"
    if sq<8:return False,f"setup quality {sq:.1f}<8"
    if not confirmed:
        # Sudden-move continuation may use momentum confirmation rather than ORB.
        if not (sudden_move and momentum_accel>=0.35): return False,"entry trigger not confirmed"
    return True,"ENTRY_OK"
