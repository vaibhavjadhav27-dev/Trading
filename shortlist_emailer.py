# shortlist_emailer.py - Formats shortlist email for daily notification
from datetime import datetime

def collect_shortlist_data(candidates, rejected, orb_data, nifty_data, funnel_counts):
    return {
        'candidates': candidates or [],
        'rejected': rejected if isinstance(rejected, list) else [],
        'orb_data': orb_data or {},
        'nifty': nifty_data or {},
        'funnel': funnel_counts or {},
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M')
    }

def build_shortlist_email(data):
    cands = data.get('candidates', [])
    rejected = data.get('rejected', [])
    nifty = data.get('nifty', {})
    funnel = data.get('funnel', {})
    # ── MARKET STATUS BLOCK ─────────────────────────────────────────
    nifty_ltp   = nifty.get('ltp', 'N/A')
    nifty_prev  = nifty.get('prev_close', 0)
    nifty_chg   = round((float(nifty_ltp)-float(nifty_prev))/float(nifty_prev)*100, 2) if nifty_prev else 0
    nifty_chg_s = f'+{nifty_chg:.2f}%' if nifty_chg >= 0 else f'{nifty_chg:.2f}%'
    chg_color   = '#27ae60' if nifty_chg >= 0 else '#e74c3c'
    mode        = nifty.get('mode', 'UNKNOWN')
    regime      = nifty.get('regime', mode)
    banknifty   = nifty.get('banknifty', nifty.get('bank_nifty', 'N/A'))

    # Regime → trade signal
    regime_upper = str(regime).upper()
    if 'CHOPPY' in regime_upper:
        signal = '🔴 NO TRADE — Choppy regime'
        sig_color = '#e74c3c'
    elif 'CONSERVATIVE' in regime_upper:
        signal = '🟡 CAUTIOUS — Half-size entries only'
        sig_color = '#f39c12'
    elif 'NO_TRADE' in regime_upper:
        signal = '🔴 NO TRADE — Below EMA50'
        sig_color = '#e74c3c'
    elif 'TRENDING' in regime_upper:
        signal = '🟢 TRENDING — Full size, up to 2 positions'
        sig_color = '#27ae60'
    else:
        signal = '🟢 NORMAL — Standard 1× sizing'
        sig_color = '#27ae60'

    # Top NIFTY movers from nifty_data if available
    nifty50_gainers = nifty.get('top_gainers', [])
    nifty50_losers  = nifty.get('top_losers', [])

    html = '<h2>Daily Shortlist Report - {timestamp}</h2>'
    html += f'''<div style="background:#f8f9fa;border-left:5px solid {sig_color};
                padding:12px;margin-bottom:16px;font-family:monospace">
      <b style="font-size:16px">📊 MARKET STATUS</b><br>
      <table style="width:100%;margin-top:8px">
        <tr>
          <td><b>NIFTY:</b> {nifty_ltp}
              <span style="color:{chg_color}"> {nifty_chg_s}</span></td>
          <td><b>BankNifty:</b> {banknifty}</td>
          <td><b>Regime:</b> {regime}</td>
        </tr>
      </table>
      <div style="margin-top:8px;padding:6px;background:{sig_color};
                  color:white;border-radius:4px;font-weight:bold">
        {signal}
      </div>'''

    # NIFTY50 top movers (if available from nifty_data)
    if nifty50_gainers or nifty50_losers:
        html += '<table style="width:100%;margin-top:10px"><tr valign="top">'
        html += '<td width="50%"><b>🟢 Top Gainers</b><br>'
        for g in nifty50_gainers[:3]:
            sym = g.get('symbol', g.get('ticker','?'))
            chg = g.get('pChange', g.get('change_pct', 0))
            html += f'&nbsp;&nbsp;{sym}: <span style="color:#27ae60">+{chg:.1f}%</span><br>'
        html += '</td><td width="50%"><b>🔴 Top Losers</b><br>'
        for l in nifty50_losers[:3]:
            sym = l.get('symbol', l.get('ticker','?'))
            chg = l.get('pChange', l.get('change_pct', 0))
            html += f'&nbsp;&nbsp;{sym}: <span style="color:#e74c3c">{chg:.1f}%</span><br>'
        html += '</td></tr></table>'
    html += '</div>'
    html += f'<h3>Candidates: {len(cands)}</h3>'
    if cands:
        html += '<table border="1" cellpadding="4" style="border-collapse:collapse">'
        html += '<tr><th>Ticker</th><th>LTP</th><th>Gap%</th><th>Score</th></tr>'
        for c in cands:
            if isinstance(c, dict):
                t = c.get('ticker', '?')
                l = c.get('ltp', 0)
                g = c.get('gap_pct', 0)
                s = c.get('score', 0)
                html += f'<tr><td>{t}</td><td>{l:.1f}</td><td>{g:+.1f}%</td><td>{s:.2f}</td></tr>'
        html += '</table>'
    else:
        html += '<p>No candidates passed all filters today.</p>'
    html += '<h3>Filter Funnel</h3><ul>'
    for stage, counts in funnel.items():
        if isinstance(counts, dict):
            p = counts.get('passed', 0)
            r = counts.get('rejected', 0)
            html += f'<li>{stage}: passed={p}, rejected={r}</li>'
    html += '</ul>'
    return html
