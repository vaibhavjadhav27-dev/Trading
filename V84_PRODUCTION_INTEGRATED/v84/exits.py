from dataclasses import dataclass
@dataclass
class ExitState:
    side:str; entry:float; stop:float; peak:float; weak_count:int=0; best_r:float=0

def update(s,price,r,structure_stop=None,confirmed_failure=False):
    if s.side=='LONG': s.peak=max(s.peak,price)
    else:s.peak=min(s.peak,price)
    s.best_r=max(s.best_r,r)
    if r<0.15:s.weak_count+=1
    else:s.weak_count=0
    if r<=-0.05:return 'HARD_SL',s.stop
    if confirmed_failure and s.best_r>=1.0:return 'PROFIT_REVERSAL',s.stop
    if s.best_r>=2.0:
        if s.side=='LONG': trail=s.peak-(s.peak-s.entry)*0.35
        else: trail=s.peak+(s.entry-s.peak)*0.35
        if structure_stop is not None: trail=max(trail,structure_stop) if s.side=='LONG' else min(trail,structure_stop)
        if (s.side=='LONG' and trail>s.stop) or (s.side=='SHORT' and trail<s.stop):return 'TRAIL_UPDATE',trail
    if s.best_r>=1.0:
        be=s.entry*1.001 if s.side=='LONG' else s.entry*0.999
        if (s.side=='LONG' and be>s.stop) or (s.side=='SHORT' and be<s.stop):return 'TRAIL_UPDATE',be
    return 'HOLD',s.stop
