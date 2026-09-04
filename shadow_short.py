"""Short shadow scorer — LONG vs SHORT expectancy comparison.
Reuses shadow_compare.py's data helpers + long logics via import (no duplication).
Adds short_logic() gated by fno_universe.is_shortable().
ALERT-ONLY: logs the would-be SELL-to-open intent for expectancy measurement.
It NEVER places a short order (standing rule across every NSE module)."""
import sys, json
sys.path.insert(0, '/home/ubuntu/trading-bot')
import pandas as pd
from datetime import date, timedelta, datetime
import logging

from shadow_compare import (get_hist, get_intra, get_nifty, detect_regime,
                            detect_patterns, shadow_logic)
from fno_universe import is_shortable

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("shadow_short")


def short_logic(day_open, prev_close, orb_h, orb_l, co, ch, cl, cc, cv, regime):
    """Faithful SHORT mirror of shadow_logic (same VWAP/scoring/exit structure, inverted)."""
    gap = ((day_open - prev_close) / prev_close) * 100
    if not (60 <= day_open <= 5000): return None, 'Price'
    if not (-10.0 <= gap <= 2.0): return None, 'Gap %.1f%%' % gap
    patterns, pat_score = detect_patterns(co, ch, cl, cc, cv)
    vwap = sum(cc[:6]) / 6 if len(cc) >= 6 else day_open
    ltp_30 = float(cc[5]) if len(cc) >= 6 else day_open
    below_vwap = ltp_30 < vwap
    avg_vol = sum(cv) / len(cv) if cv and sum(cv) > 0 else 1
    vol_surge = (sum(cv[:6]) / 6) / avg_vol if avg_vol > 0 and len(cv) >= 6 else 0
    score = 0
    if below_vwap: score += 30
    if vol_surge > 1.5: score += 25
    if gap < -0.5: score += 20
    orb_pct = ((orb_h - orb_l) / orb_l) * 100 if orb_l > 0 else 0
    if orb_pct < 3.0: score += 15
    if gap < -1.0 and below_vwap: score += 10
    score += pat_score
    min_score = {'TRENDING': 40, 'CHOPPY': 70, 'HIGH_VOL': 55}.get(regime, 50)
    if score < min_score: return None, 'Score %d<%d' % (score, min_score)
    if not below_vwap: return None, 'Above VWAP'
    post_h = ch[3:] if len(ch) > 3 else ch
    post_l = cl[3:] if len(cl) > 3 else cl
    if min(post_l) < orb_l:
        entry = vwap * 0.999
        sl = max(orb_h, vwap * 1.01)
        risk = sl - entry
        if risk > 0:
            r_t = 2.0 if regime == 'TRENDING' else 1.5
            target = entry - risk * r_t
            day_close = float(cc[-1])
            if min(post_l) <= target:
                return {'entry': entry, 'exit': target, 'pnl': entry-target, 'type': 'TARGET', 'score': score, 'patterns': patterns}, None
            elif max(post_h) >= sl:
                return {'entry': entry, 'exit': sl, 'pnl': entry-sl, 'type': 'SL', 'score': score, 'patterns': patterns}, None
            else:
                return {'entry': entry, 'exit': day_close, 'pnl': entry-day_close, 'type': 'EOD', 'score': score, 'patterns': patterns}, None
    return None, 'No BD below ORB'


def _prev_close(hist, sd):
    if not hist or 'close' not in hist:
        return None
    ht = hist.get('timestamp', [])
    hd = [datetime.fromtimestamp(t).strftime('%Y-%m-%d') if isinstance(t, (int, float))
          else str(t)[:10] for t in ht]
    pi = None
    for i in range(len(hd)):
        if hd[i] == sd and i > 0:
            pi = i - 1; break
    if pi is None:
        for i in range(len(hd) - 1, -1, -1):
            if hd[i] < sd:
                pi = i; break
    if pi is None:
        return None
    return float(hist['close'][pi])


def main():
    print('=' * 70)
    print('  SHORT SHADOW SCORER  —  LONG vs SHORT expectancy (ALERT-ONLY)')
    print('=' * 70)
    wl = pd.read_csv('watchlist.csv')
    tcol = 'ticker' if 'ticker' in wl.columns else wl.columns[0]
    scol = 'security_id' if 'security_id' in wl.columns else wl.columns[1]
    stocks = [{'ticker': str(r[tcol]).strip().upper(), 'sid': int(r[scol])}
              for _, r in wl.head(50).iterrows()]
    shortable_n = sum(1 for s in stocks if is_shortable(s['ticker']))
    print('  Watchlist scanned: %d  |  shortable: %d' % (len(stocks), shortable_n))

    nifty = get_nifty(days=15)
    if not nifty or 'close' not in nifty:
        print('  ERROR: No NIFTY data'); return
    ts = nifty.get('timestamp', [])
    dates = sorted(set(datetime.fromtimestamp(t).strftime('%Y-%m-%d')
                       if isinstance(t, (int, float)) else str(t)[:10] for t in ts))
    recent = dates[-5:] if len(dates) >= 5 else dates
    print('  Dates:', recent)

    results = {'long': [], 'short': [], 'regimes': {}, 'skipped_not_shortable': 0}
    for sd in recent:
        di = dates.index(sd) if sd in dates else -1
        if di >= 5:
            rd = {k: nifty[k][:di + 1] for k in ['close', 'open', 'high', 'low']}
            regime, _ = detect_regime(rd)
        else:
            regime = 'NORMAL'
        results['regimes'][sd] = regime
        day_long, day_short = [], []
        for stock in stocks:
            intra = get_intra(stock['sid'], sd)
            if not intra or 'open' not in intra or len(intra['open']) < 6:
                continue
            co = [float(x) for x in intra['open']]
            ch = [float(x) for x in intra['high']]
            cl = [float(x) for x in intra['low']]
            cc = [float(x) for x in intra['close']]
            cv = [int(x) for x in intra.get('volume', [])]
            hist = get_hist(stock['sid'], days=10)
            pc = _prev_close(hist, sd)
            if pc is None:
                continue
            day_open = co[0]
            orb_h = max(ch[0], ch[1], ch[2])
            orb_l = min(cl[0], cl[1], cl[2])

            lr, _ = shadow_logic(day_open, pc, orb_h, orb_l, co, ch, cl, cc, cv, regime)
            if lr:
                lr['ticker'] = stock['ticker']; results['long'].append(lr); day_long.append(lr)

            if not is_shortable(stock['ticker']):
                results['skipped_not_shortable'] += 1
                continue                              # honest gate: never score un-shortable
            srt, _ = short_logic(day_open, pc, orb_h, orb_l, co, ch, cl, cc, cv, regime)
            if srt:
                srt['ticker'] = stock['ticker']; results['short'].append(srt); day_short.append(srt)

        bl = max(day_long, key=lambda x: x['score']) if day_long else None
        bs = max(day_short, key=lambda x: x['score']) if day_short else None
        print('  --- %s [%s] ---' % (sd, regime))
        print('    LONG  best: %s' % (('%s sc:%d pnl:%+.1f' % (bl['ticker'], bl['score'], bl['pnl'])) if bl else '—'))
        print('    SHORT best: %s' % (('%s sc:%d pnl:%+.1f' % (bs['ticker'], bs['score'], bs['pnl'])) if bs else '—'))
        if bl and bs:
            print('    SIDE_COMPARE -> %s (%d vs %d)  [SHADOW, no order]'
                  % ('SHORT' if bs['score'] > bl['score'] else 'LONG', bs['score'], bl['score']))

    L, S = results['long'], results['short']
    lw = sum(1 for t in L if t['pnl'] > 0); sw = sum(1 for t in S if t['pnl'] > 0)
    lp = sum(t['pnl'] for t in L); sp = sum(t['pnl'] for t in S)
    print('=' * 70)
    print('  LONG :  %d trades | %dW | PnL %+.1f' % (len(L), lw, lp))
    print('  SHORT:  %d trades | %dW | PnL %+.1f  (skipped %d not-shortable)'
          % (len(S), sw, sp, results['skipped_not_shortable']))
    print('  NOTE: shadow only — no short orders were or will be placed.')
    with open('short_comparison.json', 'w') as f:
        json.dump({'long': [{'t': t['ticker'], 'pnl': round(t['pnl'], 1), 'type': t['type'], 'score': t.get('score', 0)} for t in L],
                   'short': [{'t': t['ticker'], 'pnl': round(t['pnl'], 1), 'type': t['type'], 'score': t.get('score', 0)} for t in S],
                   'regimes': results['regimes'],
                   'skipped_not_shortable': results['skipped_not_shortable']}, f, indent=2)
    print('  Saved: short_comparison.json')


if __name__ == '__main__':
    main()
