import requests
import json
import time
import logging
import sys
import os
from datetime import datetime, timedelta
from decimal import Decimal

sys.path.insert(0, '/home/ubuntu/trading-bot')
from secrets_manager import get_parameter
import boto3

log = logging.getLogger('market_intel')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ═══ DATA COLLECTORS (No AI needed) ═══

class NSEDataCollector:
    """Fetches NSE market data - gainers, losers, FII/DII, sectors."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
        })
        self.base_url = 'https://www.nseindia.com'
        self._init_cookies()

    def _init_cookies(self):
        """NSE requires cookies from homepage first."""
        try:
            self.session.get(self.base_url, timeout=10)
            time.sleep(1)
        except Exception as e:
            log.warning(f'NSE cookie init failed: {e}')

    def get_top_gainers(self, count=10):
        """Get top gainers from NSE."""
        try:
            url = f'{self.base_url}/api/live-analysis-variations?index=gainers&type=EQUITY'
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                gainers = []
                for item in data.get('data', [])[:count]:
                    gainers.append({
                        'symbol': item.get('symbol', ''),
                        'ltp': item.get('ltp', 0),
                        'change': item.get('change', 0),
                        'pChange': item.get('pChange', 0),
                        'open': item.get('open', 0),
                        'high': item.get('high', 0),
                        'low': item.get('low', 0),
                        'prev_close': item.get('previousClose', 0),
                        'volume': item.get('totalTradedVolume', 0),
                        'value_cr': item.get('totalTradedValue', 0) / 10000000 if item.get('totalTradedValue') else 0,
                    })
                return gainers
            else:
                log.warning(f'NSE gainers HTTP {resp.status_code}')
                return []
        except Exception as e:
            log.error(f'NSE gainers error: {e}')
            return []

    def get_fii_dii_data(self):
        """Get FII/DII activity data."""
        try:
            url = f'{self.base_url}/api/fiidiiTradeReact'
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data
            return {}
        except Exception as e:
            log.error(f'FII/DII error: {e}')
            return {}

    def get_market_status(self):
        """Get NIFTY 50 and market breadth."""
        try:
            url = f'{self.base_url}/api/allIndices'
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                nifty = {}
                for idx in data.get('data', []):
                    if idx.get('index') == 'NIFTY 50':
                        nifty = {
                            'value': idx.get('last', 0),
                            'change': idx.get('percentChange', 0),
                            'advances': idx.get('advances', 0),
                            'declines': idx.get('declines', 0),
                        }
                        break
                return nifty
            return {}
        except Exception as e:
            log.error(f'Market status error: {e}')
            return {}


class MacroDataCollector:
    """Fetches USD/INR, Crude Oil, and other macro data."""

    def get_usd_inr(self):
        try:
            resp = requests.get('https://api.exchangerate-api.com/v4/latest/USD', timeout=10)
            if resp.status_code == 200:
                return resp.json().get('rates', {}).get('INR', 0)
            return 0
        except Exception:
            return 0

    def get_crude_oil(self):
        """Get crude oil price from free API."""
        try:
            resp = requests.get('https://api.api-ninjas.com/v1/commodityprice?name=crude_oil',
                              headers={'X-Api-Key': 'free'}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get('price', 0)
            return 0
        except Exception:
            return 0


# ═══ MULTI-AI PROVIDER (Fallback Chain) ═══

class AIAnalyzer:
    """Multi-provider AI with automatic fallback."""

    def __init__(self):
        self.grok_key = None
        self.gemini_key = None
        self.groq_key = None
        try:
            self.grok_key = get_parameter('/trading-engine/ai/grok-api-key')
        except Exception:
            pass
        try:
            self.gemini_key = get_parameter('/trading-engine/ai/gemini-api-key')
        except Exception:
            pass
        try:
            self.groq_key = get_parameter('/trading-engine/ai/groq-api-key')
        except Exception:
            pass

    def analyze(self, prompt, max_tokens=2000):
        """Advisory analysis only: Grok -> Gemini -> Groq -> template. Never a live order gate."""
        if self.grok_key:
            result = self._try_grok(prompt, max_tokens)
            if result:
                return result, 'grok'
        if self.gemini_key:
            result = self._try_gemini(prompt, max_tokens)
            if result:
                return result, 'gemini'
        if self.groq_key:
            result = self._try_groq(prompt, max_tokens)
            if result:
                return result, 'groq'

        # Last resort: template-based summary
        return self._template_fallback(prompt), 'template'


    def _try_grok(self, prompt, max_tokens):
        try:
            url='https://api.x.ai/v1/chat/completions'
            headers={'Authorization':f'Bearer {self.grok_key}','Content-Type':'application/json'}
            payload={'model':'grok-4.3','messages':[{'role':'user','content':prompt}], 'max_tokens':max_tokens}
            resp=requests.post(url,json=payload,headers=headers,timeout=30)
            if resp.status_code==200:
                data=resp.json(); return data.get('choices',[{}])[0].get('message',{}).get('content')
            log.warning(f'Grok failed: {resp.status_code}')
        except Exception as e:
            log.warning(f'Grok error: {e}')
        return None
    def _try_groq(self, prompt, max_tokens):
        """Call Groq API (Llama 3.1 70B - free tier)."""
        try:
            url = 'https://api.groq.com/openai/v1/chat/completions'
            headers = {
                'Authorization': f'Bearer {self.groq_key}',
                'Content-Type': 'application/json'
            }
            payload = {
                'model': 'openai/gpt-oss-120b',
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
                'temperature': 0.7
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return data['choices'].__getitem__(0)['message']['content']
            else:
                log.warning(f'Groq failed: {resp.status_code}')
                return None
        except Exception as e:
            log.warning(f'Groq error: {e}')
            return None

    def _try_gemini(self, prompt, max_tokens):
        """Call Gemini API."""
        try:
            url = f'https://generativelanguage.googleapis.com/v1/models/gemini-3.6-flash:generateContent?key={self.gemini_key}'
            payload = {
                'contents': [{'parts': [{'text': prompt}]}],
                'generationConfig': {'maxOutputTokens': max_tokens}
            }
            time.sleep(2)  # Rate limit safety
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                candidates = result.get('candidates', [])
                if candidates:
                    parts = candidates.__getitem__(0).get('content', {}).get('parts', [])
                    if parts:
                        return parts.__getitem__(0).get('text', '')
            log.warning(f'Gemini failed: {resp.status_code}')
            return None
        except Exception as e:
            log.warning(f'Gemini error: {e}')
            return None

    def _template_fallback(self, prompt):
        """Generate a basic template response when all AI providers fail."""
        return 'AI analysis unavailable. Please review raw data in the daily learning log.'


# ═══ DAILY LEARNING STORAGE (DynamoDB) ═══

class DailyLearningStore:
    """Stores daily market learnings in DynamoDB for weekly pattern detection."""

    def __init__(self):
        self.dynamo = boto3.resource('dynamodb', region_name='ap-south-1')
        self.table_name = 'TradingBot_DailyLearnings'
        self._ensure_table()

    def _ensure_table(self):
        try:
            self.table = self.dynamo.Table(self.table_name)
            self.table.load()
        except Exception:
            self.dynamo.create_table(
                TableName=self.table_name,
                KeySchema=[
                    {'AttributeName': 'date', 'KeyType': 'HASH'},
                ],
                AttributeDefinitions=[
                    {'AttributeName': 'date', 'AttributeType': 'S'},
                ],
                BillingMode='PAY_PER_REQUEST'
            )
            self.table = self.dynamo.Table(self.table_name)
            self.table.wait_until_exists()
            log.info(f'Created {self.table_name} table')

    def store_learning(self, date_str, data):
        """Store daily learning record."""
        item = {
            'date': date_str,
            'timestamp': datetime.now().isoformat(),
        }
        # Convert floats to Decimal for DynamoDB
        for key, value in data.items():
            if isinstance(value, float):
                item[key] = str(round(value, 4))
            elif isinstance(value, (list, dict)):
                item[key] = json.dumps(value)
            else:
                item[key] = str(value) if value is not None else 'N/A'
        self.table.put_item(Item=item)
        log.info(f'Stored learning for {date_str}')

    def get_weekly_learnings(self, days=5):
        """Retrieve last N days of learnings for pattern detection."""
        learnings = []
        today = datetime.now()
        for i in range(days):
            date_str = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            try:
                resp = self.table.get_item(Key={'date': date_str})
                if 'Item' in resp:
                    learnings.append(resp['Item'])
            except Exception:
                pass
        return learnings


# ═══ MAIN ANALYSIS ENGINE ═══

class MarketIntelligence:
    """Combines data collection, AI analysis, and learning storage."""

    def __init__(self):
        self.nse = NSEDataCollector()
        self.macro = MacroDataCollector()
        self.ai = AIAnalyzer()
        self.store = DailyLearningStore()

    def run_daily_analysis(self):
        """Complete daily post-market analysis."""
        today = datetime.now().strftime('%Y-%m-%d')
        log.info(f'=== Daily Market Intelligence: {today} ===')

        # Step 1: Collect Data
        log.info('Collecting market data...')
        top_gainers = self.nse.get_top_gainers(10)
        if not top_gainers:
            # wired 2026-07-21: NSE returns 200-empty on server IP -> Dhan fallback (mirror post_market_analysis.py:310)
            log.warning('NSE gainers empty -> Dhan fallback (get_top_gainers_from_dhan)')
            try:
                from post_market_analysis import get_top_gainers_from_dhan as _dhan_gainers
                _dg = _dhan_gainers() or []
                top_gainers = [{
                    'symbol': _d.get('ticker', ''),
                    'ltp': _d.get('ltp', 0),
                    'change': round((_d.get('ltp', 0) or 0) - (_d.get('prev_close', 0) or 0), 2),
                    'pChange': _d.get('gain_pct', 0),
                    'open': _d.get('open', 0), 'high': _d.get('high', 0), 'low': _d.get('low', 0),
                    'prev_close': _d.get('prev_close', 0),
                    'volume': _d.get('volume', 0), 'value_cr': _d.get('value_cr', 0),
                } for _d in _dg]
                log.info(f'Dhan fallback: got {len(top_gainers)} gainers')
            except Exception as _fe:
                log.error(f'Dhan gainer fallback failed: {_fe}')
                top_gainers = []
        fii_dii = self.nse.get_fii_dii_data()
        market = self.nse.get_market_status()
        usd_inr = self.macro.get_usd_inr()

        log.info(f'  Top gainers: {len(top_gainers)}')
        log.info(f'  NIFTY: {market.get("value", "N/A")} ({market.get("change", "N/A")}%)')
        log.info(f'  USD/INR: {usd_inr}')

        # Step 2: Load our shortlisting results from today
        our_candidates = []
        our_rejected = []
        try:
            if os.path.exists('/tmp/today_analysis.json'):
                with open('/tmp/today_analysis.json', 'r') as f:
                    our_data = json.load(f)
                    our_candidates = our_data.get('gainers', [])
                    our_rejected = our_data.get('losers', [])
        except Exception:
            pass

        # Step 3: Compare our shortlist with actual market gainers
        our_tickers = set(c.get('ticker', '') for c in our_candidates)
        missed_gainers = []
        for g in top_gainers[:5]:
            if g['symbol'] not in our_tickers:
                # Classify correctly — only flag as MISSED if it passed our criteria
                _open = g.get('open', 0)
                _ltp  = g.get('ltp', 0)
                _gap  = round((_ltp - _open) / _open * 100, 2) if _open else 0
                _price = _ltp
                # Tag with classification before adding
                if _gap > 8.0:
                    g['_classification'] = f'NOT-OUR-SETUP (gap +{_gap:.1f}% > 8% cap)'
                elif _price < 60:
                    g['_classification'] = f'NOT-OUR-SETUP (price Rs.{_price:.0f} < Rs.60)'
                elif _price > 5000:
                    g['_classification'] = f'NOT-OUR-SETUP (price Rs.{_price:.0f} > Rs.5000)'
                else:
                    g['_classification'] = 'MISSED (not shortlisted — check watchlist/RVOL/ADT)'
                missed_gainers.append(g)

        # Step 4: Build AI analysis prompt
        def _intra(g):
            o = g.get('open') or 0
            return ((g.get('ltp',0) - o) / o * 100) if o else 0
        gainers_text = chr(10).join([
            f"  {i+1}. {g['symbol']}: Open Rs.{g.get('open',0)} -> LTP Rs.{g['ltp']} | Intraday {_intra(g):+.2f}% | vs-PrevClose +{g['pChange']:.2f}% | Vol {g['volume']:,.0f} | Value Rs.{g['value_cr']:.1f}Cr"
            for i, g in enumerate(top_gainers[:5])
        ])

        our_text = chr(10).join([
            f"  - {c['ticker']}: +{c.get('change_pct', 0):.2f}%, Gap {c.get('gap_pct', 0):+.2f}%"
            for c in our_candidates[:5]
        ]) if our_candidates else '  No candidates shortlisted today'

        # shortlist from scan-time capture (populates from first live 09:15 scan onward)
        _short_text = "  (no shortlist captured yet - scan-time write fires from tomorrow's 09:15 run)"
        try:
            import glob
            _pf = f"candle_archive/pending_{date.today()}.json"
            if os.path.exists(_pf):
                _pj = json.load(open(_pf))
                _rows = _pj.get("candidates", [])
                if _rows:
                    _short_text = chr(10).join([
                        f"  #{r.get('rank')}: {r.get('symbol')} | gap {r.get('gap')}% | RS {r.get('rs')} | score {r.get('score')} | tier {r.get('tier')} | kept={r.get('kept')}"
                        for r in _rows[:15]])
        except Exception as _e:
            _short_text = f"  (shortlist read failed: {_e})"

        missed_text = chr(10).join([
            f"  - {g['symbol']}: +{g['pChange']:.2f}% (NOT in our shortlist)"
            for g in missed_gainers
        ]) if missed_gainers else (
            '  ⚠️ Gainer feed unavailable - NSE returned empty and Dhan fallback returned nothing '
            '(this is NOT a real "all shortlisted" result)' if not top_gainers
            else '  All top gainers were in our shortlist!')

        fii_text = json.dumps(fii_dii, indent=2)[:500] if fii_dii else 'N/A'

        prompt = f"""You are an expert Indian stock market analyst. Analyze today's ({today}) NSE market:

== TOP 5 NSE GAINERS ==
{gainers_text}

== MARKET CONTEXT ==
NIFTY 50: {market.get('value', 'N/A')} ({market.get('change', 'N/A')}%)
Advances: {market.get('advances', 'N/A')}, Declines: {market.get('declines', 'N/A')}
USD/INR: {usd_inr}
FII/DII: {fii_text}

== OUR SHORTLISTED STOCKS ==
{our_text}

== STOCKS WE MISSED (Top gainers NOT in our list) ==
{missed_text}

== OUR ACTUAL FILTER THRESHOLDS (config.py) ==
  Price floor Rs.60; ceiling Rs.5000 (tier1) / Rs.1500 (tier2)
  Gap: 0.3% min to 8% max (reject >15%); tier2 gap min 1.0%
  RVOL threshold >=2.0 (RVOL >2.5 bypasses gap minimum)
  ORB range: 0.8%-3.0% of price
  ADT (avg daily turnover): sweet spot Rs.50-70Cr; min Rs.5Cr

== OUR SHORTLISTED CANDIDATES (scan-time capture) ==
{_short_text}

== ECONOMICS (weigh before recommending we "trade more") ==
  MIS round-trip cost ~Rs.24/trade; we require reward >= 3x cost.
  Goal is EXPECTANCY, not frequency. On weak/CONSERVATIVE tape, NO-TRADE is the CORRECT outcome, not a miss.
  You are ALERT/ANALYSIS only - never recommend autonomous live orders; all signals are for manual decision.

Please analyze:
1. WHAT DROVE THE TOP GAINERS: News, sector rotation, FII buying, results, crude/USD impact? Be specific.
2. WHY WE MISSED EACH: For EVERY missed gainer, name the ONE specific criterion that most likely rejected it (price / gap / RVOL / ORB-range / ADT) and state its value vs our threshold. If you lack a field, say which field is needed - do NOT guess vaguely.
3. SELECTION ACCURACY: Compare OUR shortlisted candidates above vs the actual top gainers. Which did we rank well? Which gainers were absent from our shortlist and why? Was our ranking order justified by outcome?
4. ENTRY/EXIT TIMING (patterns, not price levels): Describe the winning setups in terms of VWAP position, RVOL, volume profile, and ORB-range behaviour - the repeatable signal, not the rupee level.
5. ONE FILTER CHANGE FOR TOMORROW: A single, specific, testable threshold change (e.g. "lower ADT_MIN to Rs.40Cr") with the expected trade-off in false signals. Only if the economics above justify it.

Be specific with numbers and reasoning."""

        # Step 5: Get AI analysis
        log.info('Running AI analysis...')
        analysis, provider = self.ai.analyze(prompt)
        log.info(f'  AI provider used: {provider}')

        # Step 6: Store daily learning
        learning_data = {
            'nifty_value': market.get('value', 0),
            'nifty_change': market.get('change', 0),
            'advances': market.get('advances', 0),
            'declines': market.get('declines', 0),
            'usd_inr': usd_inr,
            'top_gainers': json.dumps(top_gainers[:5]),
            'our_candidates': json.dumps(our_candidates[:5]),
            'missed_gainers': json.dumps(missed_gainers),
            'ai_analysis': analysis[:3000],  # DynamoDB 400KB limit
            'ai_provider': provider,
            'fii_dii': json.dumps(fii_dii)[:1000],
        }
        self.store.store_learning(today, learning_data)

        # Step 7: Send email
        self._send_email(today, top_gainers, our_candidates, missed_gainers, market, analysis, provider)

        return analysis, provider

    def run_weekly_pattern_analysis(self):
        """Analyze patterns from the week's learnings (runs Friday)."""
        log.info('=== Weekly Pattern Analysis ===')

        # Get last 5 days of learnings
        learnings = self.store.get_weekly_learnings(5)
        log.info(f'Retrieved {len(learnings)} days of data')

        if not learnings:
            log.warning('No daily learnings found for this week')
            return 'No data available for weekly analysis'

        # Build weekly summary
        weekly_summary_parts = []
        for day in learnings:
            date = day.get('date', 'unknown')
            gainers = day.get('top_gainers', '[]')
            missed = day.get('missed_gainers', '[]')
            nifty_chg = day.get('nifty_change', '0')
            weekly_summary_parts.append(
                f"Date: {date}, NIFTY: {nifty_chg}%, Top Gainers: {gainers[:200]}, Missed: {missed[:200]}"
            )

        weekly_summary = chr(10).join(weekly_summary_parts)

        prompt = f"""You are an expert trading system optimizer. Analyze this week's daily learnings and identify patterns:

== WEEKLY DATA ==
{weekly_summary}

Analyze:
1. RECURRING PATTERNS: Are certain sectors, cap-sizes, or technical setups repeatedly appearing in top gainers?
2. SYSTEMATIC MISSES: Are we consistently missing the same TYPE of stock? (e.g., low-gap momentum, sector rotators)
3. FILTER EFFECTIVENESS: Based on 5 days of data, which filters are working and which need tuning?
4. WEEKLY REGIME: Was this a trending week, rotational week, or choppy week? How should our strategy adapt?
5. NEXT WEEK OUTLOOK: Based on patterns observed, what to expect and prepare for?
6. ACTION ITEMS: Specific filter/parameter changes with exact values to implement before Monday.

Be specific with numbers. This analysis drives actual trading parameter changes."""

        analysis, provider = self.ai.analyze(prompt)
        log.info(f'Weekly analysis via: {provider}')

        # Store weekly summary
        today = datetime.now().strftime('%Y-%m-%d')
        self.store.store_learning(f'WEEKLY_{today}', {
            'type': 'weekly_analysis',
            'days_analyzed': len(learnings),
            'ai_analysis': analysis[:3000],
            'ai_provider': provider,
        })

        # Send weekly email
        self._send_weekly_email(analysis, provider, len(learnings))

        return analysis, provider

    def _send_email(self, date, gainers, our_stocks, missed, market, analysis, provider):
        """Send daily intelligence email via SES."""
        try:
            ses = boto3.client('ses', region_name='ap-south-1')
            sender = get_parameter('/trading-engine/ses/sender-email')
            recipient = get_parameter('/trading-engine/ses/recipient-email')

            def _intra_pct(g):
                o = g.get('open') or 0
                return ((g.get('ltp', 0) - o) / o * 100) if o else 0.0
            def _grow(g):
                ip = _intra_pct(g)
                ic = 'green' if ip >= 0 else '#c0392b'
                return (f"<tr><td>{g['symbol']}</td>"
                        f"<td>Rs.{g.get('open', 0)}</td>"
                        f"<td>Rs.{g['ltp']}</td>"
                        f"<td style='color:{ic}'>{ip:+.2f}%</td>"
                        f"<td style='color:green'>+{g['pChange']:.2f}%</td>"
                        f"<td>{g['volume']:,.0f}</td></tr>")
            gainers_html = ''.join([_grow(g) for g in gainers[:5]])

            missed_html = ''.join([
                (lambda _g: f'<tr><td>{_g["symbol"]}</td><td>+{_g["pChange"]:.2f}%</td><td>{_g.get("_classification","MISSED")}</td></tr>')(g)
                for g in missed
            ]) if missed else (
                '<tr><td colspan="3">⚠️ Gainer feed unavailable - could not fetch (not a real result)</td></tr>'
                if not gainers
                else '<tr><td colspan="3">All top gainers were in our shortlist!</td></tr>')

            body = f"""<html><body>
<h2>Daily Market Intelligence - {date}</h2>
<h3>Market: NIFTY {market.get('value', 'N/A')} ({market.get('change', 'N/A')}%)</h3>
<h3>Top 5 NSE Gainers</h3>
<table border="1" cellpadding="5"><tr><th>Stock</th><th>Open</th><th>LTP</th><th>Intraday %</th><th>vs Prev-Close</th><th>Volume</th></tr>
{gainers_html}</table>
<p style="font-size:10px;color:#888;">Intraday % = LTP vs today's open; vs Prev-Close = official day change.</p>
<h3>Stocks We Missed</h3>
<table border="1" cellpadding="5"><tr><th>Stock</th><th>Change</th><th>Status</th></tr>
{missed_html}</table>
<h3>AI Analysis (via {provider})</h3>
<pre>{analysis[:2500]}</pre>
</body></html>"""

            ses.send_email(
                Source=sender,
                Destination={'ToAddresses': [recipient]},
                Message={
                    'Subject': {'Data': f'Market Intelligence - {date} | NIFTY {market.get("change", "")}%'},
                    'Body': {'Html': {'Data': body}}
                }
            )
            log.info('Daily intelligence email sent')
        except Exception as e:
            log.error(f'Email error: {e}')

    def _send_weekly_email(self, analysis, provider, days):
        """Send weekly pattern analysis email."""
        try:
            ses = boto3.client('ses', region_name='ap-south-1')
            sender = get_parameter('/trading-engine/ses/sender-email')
            recipient = get_parameter('/trading-engine/ses/recipient-email')

            body = f"""<html><body>
<h2>Weekly Pattern Analysis ({days} days)</h2>
<h3>AI Analysis (via {provider})</h3>
<pre>{analysis[:3000]}</pre>
<p><em>Action items above should be reviewed before Monday trading.</em></p>
</body></html>"""

            ses.send_email(
                Source=sender,
                Destination={'ToAddresses': [recipient]},
                Message={
                    'Subject': {'Data': f'Weekly Trading Pattern Analysis - Action Items Inside'},
                    'Body': {'Html': {'Data': body}}
                }
            )
            log.info('Weekly intelligence email sent')
        except Exception as e:
            log.error(f'Weekly email error: {e}')


# ═══ ENTRY POINTS ═══

def run_daily():
    """Called by cron at 4 PM daily."""
    mi = MarketIntelligence()
    analysis, provider = mi.run_daily_analysis()
    print(f'Daily analysis complete via {provider}')

def run_weekly():
    """Called by cron on Friday evening."""
    mi = MarketIntelligence()
    analysis, provider = mi.run_weekly_pattern_analysis()
    print(f'Weekly analysis complete via {provider}')


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv.__getitem__(1) == 'weekly':
        run_weekly()
    else:
        run_daily()
