"""100-point swing scoring. Market regime biases but never disables a side."""
def clamp(x,a=0,b=1): return max(a,min(b,float(x)))

def score_swing(*, rs_10d=0, rs_20d=0, rvol=0, above_sma=False, pullback_pct=0,
                clv=0, sector_rs=0, market_rs=0, catalyst=0, trend_strength=0):
    # 100 points: market 10, sector 10, RS 20, trend 15, volume 10,
    # setup 15, pullback 10, catalyst 5, entry location 5.
    market=10*clamp((market_rs+3)/6)
    sector=10*clamp((sector_rs+3)/6)
    rs=12*clamp((rs_10d+5)/10)+8*clamp((rs_20d+8)/16)
    trend=15*(0.7 if above_sma else 0.2)+15*0.3*clamp(trend_strength)
    volume=10*clamp(rvol/2)
    setup=15*clamp((clv+1)/2)
    pb=10*(1-clamp(abs(pullback_pct-5)/10))
    cat=5*clamp(catalyst)
    entry=5*(1 if 1<=pullback_pct<=8 else 0.5)
    return round(min(100,max(0,market+sector+rs+trend+volume+setup+pb+cat+entry)),2)
