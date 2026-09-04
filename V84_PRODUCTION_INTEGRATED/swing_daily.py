"""Swing engine v8: 100-point scoring, all market regimes, paper-first execution."""
import json, os, logging, requests
from datetime import datetime,date
from secrets_manager import get_parameter
from swing_policy_v8 import score_swing
try:
 from swing_logger import log_event
except ImportError:
 def log_event(a,p): return False
log=logging.getLogger('swing_daily'); logging.basicConfig(level=logging.INFO)
POSITIONS_FILE='swing_positions.json'; JOURNAL_DIR='journal/swing'; HISTORY_FILE='stock_history_30d.json'
VIRTUAL_CAPITAL=50000.0; RISK_PER_TRADE=0.02; SCORE_THRESHOLD=60.0; SWING_LIVE_ENABLED=False
os.makedirs(JOURNAL_DIR,exist_ok=True)

def load_positions():
 try:
  with open(POSITIONS_FILE) as f: d=json.load(f)
  if isinstance(d,dict): d.setdefault('active',[]); d.setdefault('closed',[]); return d
 except Exception: pass
 return {'active':[],'closed':[]}
def save_positions(p):
 with open(POSITIONS_FILE,'w') as f: json.dump(p,f,indent=2)
def load_history():
 with open(HISTORY_FILE) as f: return json.load(f).get('stocks',{})
def sma(x,n): return sum(x[-n:])/n if len(x)>=n else None
def rs(x,n): return (x[-1]-x[-n])/x[-n]*100 if len(x)>=n and x[-n] else 0
def rvol(v,n=20):
 if len(v)<n or not sum(v[-n:]): return 0
 return v[-1]/(sum(v[-n:])/n)
def scan_candidates():
 out=[]
 for ticker,h in load_history().items():
  try:
   if isinstance(h,list):
    c=[float(x['close']) for x in h]; hi=[float(x['high']) for x in h]; lo=[float(x['low']) for x in h]; v=[float(x.get('volume',0)) for x in h]
   else: continue
   if len(c)<20 or not 60<=c[-1]<=5000: continue
   s20=sma(c,20); s50=sma(c,50) if len(c)>=50 else s20
   if not s20: continue
   h20=max(hi[-20:]); l20=min(lo[-20:]); clv=((c[-1]-l20)-(h20-c[-1]))/(h20-l20) if h20>l20 else 0
   pb=(h20-c[-1])/h20*100 if h20 else 0
   rs10=rs(c,10); rs20=rs(c,20); rv=rvol(v)
   score=score_swing(rs_10d=rs10,rs_20d=rs20,rvol=rv,above_sma=c[-1]>=s20,pullback_pct=pb,clv=clv,sector_rs=0,market_rs=0,trend_strength=1 if c[-1]>=s20 else 0)
   sl=min(lo[-3:]) if lo else c[-1]*.95; sl=max(sl,c[-1]*.92); risk=c[-1]-sl
   if risk<=0: continue
   target=c[-1]+risk*2.5
   out.append({'ticker':ticker,'cmp':round(c[-1],2),'score':score,'rs_10d':round(rs10,2),'rs_20d':round(rs20,2),'rvol':round(rv,2),'pullback_pct':round(pb,2),'sma20':round(s20,2),'high_20d':round(h20,2),'sl':round(sl,2),'target':round(target,2),'risk_pct':round(risk/c[-1]*100,2),'rr_ratio':2.5,'status':'BUY' if score>=SCORE_THRESHOLD else 'WATCH'})
  except Exception: continue
 out.sort(key=lambda x:-x['score']); return out

def auto_select_paper_trades(candidates,positions):
 active={p['ticker'] for p in positions.get('active',[])}; new=[]
 for c in candidates:
  if c['ticker'] in active or c['score']<SCORE_THRESHOLD or c['risk_pct']>8: continue
  risk_amt=VIRTUAL_CAPITAL*RISK_PER_TRADE; rps=c['cmp']-c['sl']; qty=int(risk_amt/rps) if rps>0 else 0
  if qty<1: continue
  new.append({'ticker':c['ticker'],'entry_price':c['cmp'],'entry_date':str(date.today()),'qty':qty,'sl':c['sl'],'target':c['target'],'trailing_sl':c['sl'],'score':c['score'],'status':'PAPER_ACTIVE','peak_price':c['cmp'],'notional':round(qty*c['cmp'],2),'risk_pct':c['risk_pct'],'days_held':0})
 return new[:10]
def push_to_sheets(candidates,positions):
 try:
  url=get_parameter('/trading-engine/google/apps-script-url'); payload={'action':'swing_signals','data':candidates[:20],'date':str(date.today())}; log_event('swing_signals',payload); requests.post(url,json=payload,timeout=20)
 except Exception as e: log.warning('Sheets push failed: %s',e)
def run():
 positions=load_positions(); candidates=scan_candidates(); new=auto_select_paper_trades(candidates,positions)
 if new: positions['active'].extend(new); save_positions(positions)
 push_to_sheets(candidates,positions)
 with open(os.path.join(JOURNAL_DIR,str(date.today())+'.json'),'w') as f: json.dump({'date':str(date.today()),'candidates':candidates[:50],'new_entries':new,'active':positions['active']},f,indent=2)
 log.info('SWING V8: %d candidates, %d new paper entries; live=%s',len(candidates),len(new),SWING_LIVE_ENABLED)
 return candidates
if __name__=='__main__': run()
