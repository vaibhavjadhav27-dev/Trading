"""MCX shadow scoring: 100 points, direction-neutral and compatible with equity policy."""
def clamp(x,a=0,b=1): return max(a,min(b,float(x)))
def score_mcx(*,trend=0,relative_strength=0,rvol=0,orb_strength=0,volatility=0,session_context=0):
    # 20 trend, 20 relative strength, 15 RVOL, 20 ORB, 10 volatility, 15 session context.
    return round(100*(0.20*clamp((trend+1)/2)+0.20*clamp((relative_strength+1)/2)+0.15*clamp(rvol/3)+0.20*clamp(orb_strength)+0.10*clamp(volatility)+0.15*clamp((session_context+1)/2)),2)
