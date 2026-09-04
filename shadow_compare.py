import sys, time, json, os
sys.path.insert(0, '/home/ubuntu/trading-bot')
import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
import config
from secrets_manager import get_dhan_token, get_dhan_client_id
import requests
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

token = get_dhan_token()
client_id = get_dhan_client_id()
headers = {'Content-Type': 'application/json', 'access-token': token, 'client-id': client_id}

def detect_regime(nifty_data):
    if not nifty_data or 'close' not in nifty_data:
        return 'NORMAL', {}
    closes = [float(c) for c in nifty_data['close']]
    opens = [float(o) for o in nifty_data['open']]
    highs = [float(h) for h in nifty_data['high']]
    lows = [float(l) for l in nifty_data['low']]
    if len(closes) < 5:
        return 'NORMAL', {}
    prev_close = closes[-2]
    gap_pct = ((opens[-1] - prev_close) / prev_close) * 100
    day_range = ((highs[-1] - lows[-1]) / lows[-1]) * 100
    atr_vals = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(-5, 0)]
    atr = sum(atr_vals) / len(atr_vals) if atr_vals else 1
    orb_atr_ratio = day_range / (atr / prev_close * 100) if atr > 0 else 1
    ema_slope = 0
    if len(closes) >= 10:
        ema_slope = ((sum(closes[-5:])/5 - sum(closes[-10:-5])/5) / (sum(closes[-10:-5])/5)) * 100
    metrics = {'gap': round(gap_pct,2), 'range': round(day_range,2), 'ema_slope': round(ema_slope,2)}
    if abs(gap_pct) > 1.0 and orb_atr_ratio > 1.3 and ema_slope > 0.3:
        return 'TRENDING', metrics
    elif abs(gap_pct) < 0.3 and orb_atr_ratio < 0.8:
        return 'CHOPPY', metrics
    elif day_range > 2.0:
        return 'HIGH_VOL', metrics
    return 'NORMAL', metrics

def detect_patterns(co, ch, cl, cc, cv):
    patterns = []
    if len(cc) < 6: return patterns, 0
    avg_vol = sum(cv) / len(cv) if cv and sum(cv) > 0 else 1
    vol_ratio = (sum(cv[:6]) / 6) / avg_vol if avg_vol > 0 else 0
    if vol_ratio > 2.0: patterns.append('VOL_CLIMAX')
    elif vol_ratio > 1.5: patterns.append('VOL_SURGE')
    orb_r = max(ch[:3]) - min(cl[:3])
    if len(ch) > 6:
        post_r = max(ch[3:7]) - min(cl[3:7])
        if post_r > orb_r * 1.5: patterns.append('RANGE_EXPAND')
    green = sum(1 for i in range(min(4, len(cc))) if cc[i] > co[i])
    if green >= 3: patterns.append('OPENING_DRIVE')
    score = len(patterns) * 15
    if 'VOL_CLIMAX' in patterns: score += 10
    if 'OPENING_DRIVE' in patterns: score += 10
    return patterns, min(score, 50)

def production_logic(day_open, prev_close, orb_h, orb_l, post_highs, post_lows, day_close):
    orb_pct = ((orb_h - orb_l) / orb_l) * 100 if orb_l > 0 else 0
    gap = ((day_open - prev_close) / prev_close) * 100
    if not (60 <= day_open <= 5000): return None, 'Price'
    if not (0.3 <= abs(gap) <= 8.0): return None, f'Gap {gap:.1f}%'
    if not (0.8 <= orb_pct <= 5.0): return None, f'ORB {orb_pct:.1f}%'
    if gap < 0: return None, 'Neg gap'
    if max(post_highs) > orb_h * 1.001:
        entry = orb_h * 1.001
        sl = orb_l
        risk = entry - sl
        if risk > 0:
            target = entry + risk * 1.5
            if max(post_highs) >= target:
                return {'entry': entry, 'exit': target, 'pnl': target - entry, 'type': 'TARGET'}, None
            elif min(post_lows) <= sl:
                return {'entry': entry, 'exit': sl, 'pnl': sl - entry, 'type': 'SL'}, None
            else:
                return {'entry': entry, 'exit': day_close, 'pnl': day_close - entry, 'type': 'EOD'}, None
    return None, 'No breakout'

def shadow_logic(day_open, prev_close, orb_h, orb_l, co, ch, cl, cc, cv, regime):
    gap = ((day_open - prev_close) / prev_close) * 100
    if not (60 <= day_open <= 5000): return None, 'Price'
    if not (-2.0 <= gap <= 10.0): return None, f'Gap {gap:.1f}%'
    patterns, pat_score = detect_patterns(co, ch, cl, cc, cv)
    vwap = sum(cc[:6]) / 6 if len(cc) >= 6 else day_open
    ltp_30 = float(cc[5]) if len(cc) >= 6 else day_open
    above_vwap = ltp_30 > vwap
    avg_vol = sum(cv) / len(cv) if cv and sum(cv) > 0 else 1
    vol_surge = (sum(cv[:6]) / 6) / avg_vol if avg_vol > 0 and len(cv) >= 6 else 0
    score = 0
    if above_vwap: score += 30
    if vol_surge > 1.5: score += 25
    if gap > 0.5: score += 20
    orb_pct = ((orb_h - orb_l) / orb_l) * 100 if orb_l > 0 else 0
    if orb_pct < 3.0: score += 15
    if gap > 1.0 and above_vwap: score += 10
    score += pat_score
    min_score = {'TRENDING': 40, 'CHOPPY': 70, 'HIGH_VOL': 55}.get(regime, 50)
    if score < min_score: return None, f'Score {score}<{min_score}'
    if not above_vwap: return None, 'Below VWAP'
    post_h = ch[3:] if len(ch) > 3 else ch
    post_l = cl[3:] if len(cl) > 3 else cl
    if max(post_h) > orb_h:
        entry = vwap * 1.001
        sl = min(orb_l, vwap * 0.99)
        risk = entry - sl
        if risk > 0:
            r_t = 2.0 if regime == 'TRENDING' else 1.5
            target = entry + risk * r_t
            day_close = float(cc[-1])
            if max(post_h) >= target:
                return {'entry': entry, 'exit': target, 'pnl': target-entry, 'type': 'TARGET', 'score': score, 'patterns': patterns}, None
            elif min(post_l) <= sl:
                return {'entry': entry, 'exit': sl, 'pnl': sl-entry, 'type': 'SL', 'score': score, 'patterns': patterns}, None
            else:
                return {'entry': entry, 'exit': day_close, 'pnl': day_close-entry, 'type': 'EOD', 'score': score, 'patterns': patterns}, None
    return None, 'No BO above ORB'

def get_hist(sid, days=15):
    time.sleep(0.5)
    end = date.today().strftime('%Y-%m-%d')
    start = (date.today() - timedelta(days=days)).strftime('%Y-%m-%d')
    payload = {'securityId': str(sid), 'exchangeSegment': 'NSE_EQ', 'instrument': 'EQUITY',
               'fromDate': start, 'toDate': end, 'expiryCode': 0}
    try:
        r = requests.post('https://api.dhan.co/v2/charts/historical', headers=headers, json=payload, timeout=10)
        if r.status_code == 200: return r.json()
    except: pass
    return None

def get_intra(sid, dt):
    time.sleep(0.5)
    payload = {'securityId': str(sid), 'exchangeSegment': 'NSE_EQ', 'instrument': 'EQUITY',
               'interval': '5', 'fromDate': dt, 'toDate': dt}
    try:
        r = requests.post('https://api.dhan.co/v2/charts/intraday', headers=headers, json=payload, timeout=10)
        if r.status_code == 200: return r.json()
    except: pass
    return None

def get_nifty(days=15):
    time.sleep(0.5)
    end = date.today().strftime('%Y-%m-%d')
    start = (date.today() - timedelta(days=days)).strftime('%Y-%m-%d')
    payload = {'securityId': '13', 'exchangeSegment': 'IDX_I', 'instrument': 'INDEX',
               'fromDate': start, 'toDate': end, 'expiryCode': 0}
    try:
        r = requests.post('https://api.dhan.co/v2/charts/historical', headers=headers, json=payload, timeout=10)
        if r.status_code == 200: return r.json()
    except: pass
    return None

def main():
    print('=' * 70)
    print('  AUTONOMOUS SHADOW COMPARISON ENGINE')
    print('=' * 70)
    wl = pd.read_csv('watchlist.csv')
    stocks = []
    for idx in range(min(50, len(wl))):
        row = wl.iloc[idx]
        stocks.append({'ticker': str(row.iloc), 'sid': int(row.iloc)})
    print(f'  Watchlist: {len(stocks)} stocks')
    nifty = get_nifty(days=15)
    if not nifty or 'close' not in nifty:
        print('  ERROR: No NIFTY data')
        return
    timestamps = nifty.get('timestamp', [])
    dates = []
    for t in timestamps:
        if isinstance(t, (int, float)):
            dates.append(datetime.fromtimestamp(t).strftime('%Y-%m-%d'))
        else:
            dates.append(str(t)[:10])
    dates = sorted(set(dates))
    recent = dates[-5:] if len(dates) >= 5 else dates
    print(f'  Dates: {recent}')
    results = {'prod': [], 'shadow': [], 'regimes': {}}
    for sd in recent:
        print(f'  --- {sd} ---')
        di = dates.index(sd) if sd in dates else -1
        if di >= 5:
            rd = {k: nifty[k][:di+1] for k in ['close','open','high','low']}
            regime, metrics = detect_regime(rd)
        else:
            regime, metrics = 'NORMAL', {}
        print(f'  Regime: {regime}')
        results['regimes'][sd] = regime
        for stock in stocks:
            intra = get_intra(stock['sid'], sd)
            if not intra or 'open' not in intra or len(intra['open']) < 6: continue
            co = [float(x) for x in intra['open']]
            ch = [float(x) for x in intra['high']]
            cl = [float(x) for x in intra['low']]
            cc = [float(x) for x in intra['close']]
            cv = [int(x) for x in intra.get('volume', [])]
            hist = get_hist(stock['sid'], days=10)
            if not hist or 'close' not in hist or len(hist['close']) < 2: continue
            ht = hist.get('timestamp', [])
            hd = [datetime.fromtimestamp(t).strftime('%Y-%m-%d') if isinstance(t,(int,float)) else str(t)[:10] for t in ht]
            pi = None
            for i in range(len(hd)):
                if hd[i] == sd and i > 0:
                    pi = i - 1
                    break
            if pi is None:
                for i in range(len(hd)-1, -1, -1):
                    if hd[i] < sd:
                        pi = i
                        break
            if pi is None: continue
            prev_close = float(hist['close'][pi])
            orb_h = max(ch, ch, ch)
            orb_l = min(cl, cl, cl)
            post_h = ch[3:] if len(ch) > 3 else ch
            post_l = cl[3:] if len(cl) > 3 else cl
            pr, _ = production_logic(co, prev_close, orb_h, orb_l, post_h, post_l, cc[-1])
            if pr:
                pr['ticker'] = stock['ticker']
                results['prod'].append(pr)
                w = 'W' if pr['pnl'] > 0 else 'L'
                print(f"    P: {stock['ticker']:10s} | {pr['type']:6s} | PnL:{pr['pnl']:+.1f} [{w}]")
            sr, _ = shadow_logic(co, prev_close, orb_h, orb_l, co, ch, cl, cc, cv, regime)
            if sr:
                sr['ticker'] = stock['ticker']
                results['shadow'].append(sr)
                w = 'W' if sr['pnl'] > 0 else 'L'
                pats = ','.join(sr.get('patterns',[]))[:15]
                print(f"    S: {stock['ticker']:10s} | {sr['type']:6s} | PnL:{sr['pnl']:+.1f} [{w}] Sc:{sr['score']} {pats}")
    # Summary
    p, s = results['prod'], results['shadow']
    pw = len([t for t in p if t['pnl'] > 0])
    sw = len([t for t in s if t['pnl'] > 0])
    pp = sum(t['pnl'] for t in p)
    sp = sum(t['pnl'] for t in s)
    print('=' * 70)
    print(f'  PROD:   {len(p)} trades | {pw}W | PnL: Rs.{pp:+.1f}')
    print(f'  SHADOW: {len(s)} trades | {sw}W | PnL: Rs.{sp:+.1f}')
    winner = 'SHADOW' if sp > pp else 'PRODUCTION'
    print(f'  WINNER: {winner}')
    with open('weekly_comparison.json', 'w') as f:
        json.dump({'prod': [{'t':t['ticker'],'pnl':round(t['pnl'],1),'type':t['type']} for t in p],
                   'shadow': [{'t':t['ticker'],'pnl':round(t['pnl'],1),'type':t['type'],'score':t.get('score',0)} for t in s],
                   'winner': winner, 'regimes': results['regimes']}, f, indent=2)
    print('  Saved: weekly_comparison.json')

if __name__ == '__main__':
    main()
