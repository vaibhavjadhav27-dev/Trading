from dataclasses import dataclass
from .indicators import atr,momentum,opening_range,rvol,vwap,pct
from .config import MCX
@dataclass(frozen=True)
class McxSignal:
    side:str; score:float; entry:float; stop:float; target:float; expected_move_pct:float; reason:str

def evaluate(df,us_bias=0,fx_bias=0,adv=0):
    if df is None or len(df)<20:return None
    px=float(df.close.iloc[-1]); a=atr(df,14); vw=vwap(df); rv=rvol(df,adv); r1,r3,r6,acc=momentum(df); orb=opening_range(df,MCX.native_orb_bars)
    if a<=0 or not orb:return None
    out=[]
    for side in ('LONG','SHORT'):
        dacc=acc if side=='LONG' else -acc; dv=(px-vw) if side=='LONG' else (vw-px); cross=us_bias if side=='LONG' else -us_bias; fx=fx_bias if side=='LONG' else -fx_bias
        br=(px>orb['high'] and r1>0) if side=='LONG' else (px<orb['low'] and r1<0)
        score=25*max(0,min(1,dacc/1.5))+20*max(0,min(1,(rv-.8)/1.7))+20*max(0,min(1,(dv/px*100+.02)/.35))+20*float(br)+10*max(0,min(1,(cross+1)/2))+5*max(0,min(1,(fx+1)/2))
        stop=px-a*.9 if side=='LONG' else px+a*.9; target=px+a*2 if side=='LONG' else px-a*2; move=pct(target,px) if side=='LONG' else pct(px,target)
        if abs(px-vw)>MCX.max_extension_atr*a and not br:score-=10
        out.append(McxSignal(side,max(0,min(100,score)),px,stop,target,move,'native-MCX+cross-market'))
    out.sort(key=lambda x:x.score,reverse=True); best=out[0]; other=out[1]
    if best.score<MCX.min_score or best.score-other.score<10 or best.expected_move_pct<MCX.min_expected_move_pct:return None
    return best
