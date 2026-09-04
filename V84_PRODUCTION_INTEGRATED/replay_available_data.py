from pathlib import Path
import pandas as pd, numpy as np, json, sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from v84.scoring import evaluate
root=Path('/mnt/data/integrate/home/ubuntu/trading-bot/candle_archive')
results=[]
for daydir in sorted(root.glob('candles_5min_*')):
    day=daydir.name.replace('candles_5min_','')
    for fp in list(daydir.glob('*.csv'))[::max(1, len(list(daydir.glob('*.csv')))//60)]:
        try:
            d=pd.read_csv(fp)
            d.columns=[str(x).strip().lower() for x in d.columns]
            if not all(c in d for c in ['open','high','low','close','volume']) or len(d)<30: continue
            d=d[['open','high','low','close','volume']].apply(pd.to_numeric,errors='coerce').dropna().reset_index(drop=True)
            for i in range(20,len(d)-6,3):
                hist=d.iloc[:i+1]
                s=evaluate(hist,rs=0,market_bias=0,sector_bias=0,avg_daily_volume=0)
                if s is None: continue
                fut=d.iloc[i+1:i+7]
                outcome='TIMEOUT'; mfe=-999; mae=999
                if s.side=='LONG':
                    mfe=(fut.high.max()/s.entry-1)*100; mae=(fut.low.min()/s.entry-1)*100
                    hit_t=(fut.high>=s.target).to_numpy(); hit_s=(fut.low<=s.stop).to_numpy()
                else:
                    mfe=(s.entry/fut.low.min()-1)*100; mae=(s.entry/fut.high.max()-1)*100
                    hit_t=(fut.low<=s.target).to_numpy(); hit_s=(fut.high>=s.stop).to_numpy()
                ti=np.where(hit_t)[0]; si=np.where(hit_s)[0]
                if len(ti) and len(si): outcome='AMBIGUOUS' if ti[0]==si[0] else ('TARGET' if ti[0]<si[0] else 'STOP')
                elif len(ti): outcome='TARGET'
                elif len(si): outcome='STOP'
                results.append({'day':day,'ticker':fp.stem,'i':i,'side':s.side,'mode':s.mode,'score':s.score,'mfe_pct':mfe,'mae_pct':mae,'outcome':outcome,'risk_pct':s.risk_pct,'expected_pct':s.expected_move_pct})
        except Exception: pass
out=pd.DataFrame(results)
summary={
 'signals':int(len(out)),
 'days':sorted(out.day.unique().tolist()) if len(out) else [],
 'by_mode':{},
 'targets':int((out['outcome']=='TARGET').sum()) if 'outcome' in out else 0,'stops':int((out['outcome']=='STOP').sum()) if 'outcome' in out else 0,'ambiguous':int((out['outcome']=='AMBIGUOUS').sum()) if 'outcome' in out else 0,'timeouts':int((out['outcome']=='TIMEOUT').sum()) if 'outcome' in out else 0
}
for mode,g in out.groupby('mode'):
    summary['by_mode'][mode]={'signals':int(len(g)),'target_rate_pct':round((g.outcome=='TARGET').mean()*100,2),'stop_rate_pct':round((g.outcome=='STOP').mean()*100,2),'avg_mfe_pct':round(g.mfe_pct.mean(),3),'avg_mae_pct':round(g.mae_pct.mean(),3),'avg_score':round(g.score.mean(),2)}
print(json.dumps(summary,indent=2))
Path('/mnt/data/V84_PRODUCTION_INTEGRATED/AVAILABLE_DATA_REPLAY.json').write_text(json.dumps(summary,indent=2))
