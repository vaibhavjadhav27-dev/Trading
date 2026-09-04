#!/usr/bin/env python3
"""Pure strategy dry-run: no broker/network calls and no orders."""
import pandas as pd
from v84.scoring import evaluate

def make_trend(side=1,n=90):
    p=100.0; rows=[]
    for i in range(n):
        step=0.05 if i<70 else 0.25
        p += side*step
        rows.append({'open':p-side*.03,'high':p+.12,'low':p-.12,'close':p,'volume':10000+(i%7)*1800})
    return pd.DataFrame(rows)

def main():
    for side in (1,-1):
        d=make_trend(side)
        s=evaluate(d,rs=3*side,market_bias=.9*side,sector_bias=.9*side,avg_daily_volume=800000)
        if s is None: raise SystemExit('STRATEGY_DRY_RUN_FAILED')
        print(side,s.mode,s.side,round(s.score,2),round(s.expected_move_pct,3),round(s.risk_pct,3))
    print('V84_STRATEGY_DRY_RUN_PASS')

if __name__=='__main__':main()
