#!/usr/bin/env python3
import json, logging, boto3, requests
from datetime import datetime, date, timedelta
from decimal import Decimal
from secrets_manager import get_parameter

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('WeeklyAnalysis')

GEMINI_MODEL = 'gemini-3.6-flash'
GEMINI_URL = 'https://generativelanguage.googleapis.com/v1/models/{model}:generateContent'
NL = chr(10)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def get_week_dates():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(5)]


def get_weekly_trades():
    dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
    table = dynamodb.Table('TradingBot_Trades')
    week_dates = get_week_dates()
    trades = []
    for d in week_dates:
        try:
            resp = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('date').eq(d.isoformat())
            )
            trades.extend(resp.get('Items', []))
        except Exception:
            try:
                resp = table.scan(
                    FilterExpression=boto3.dynamodb.conditions.Attr('date').eq(d.isoformat())
                )
                trades.extend(resp.get('Items', []))
            except Exception as e:
                log.warning(f'Could not fetch trades for {d}: {e}')
    return trades


def get_weekly_daily_states():
    dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
    table = dynamodb.Table('TradingBot_DailyState')
    week_dates = get_week_dates()
    states = []
    for d in week_dates:
        try:
            resp = table.get_item(Key={'date': d.isoformat()})
            if 'Item' in resp:
                states.append(resp['Item'])
        except Exception as e:
            log.warning(f'Could not fetch state for {d}: {e}')
    return states


def read_weekly_bot_logs():
    week_dates = get_week_dates()
    date_strs = [d.strftime('%Y-%m-%d') for d in week_dates]
    daily_data = {d: {'shortlisted': [], 'rejected': [], 'traded': None} for d in date_strs}
    try:
        with open('bot.log', 'r') as f:
            for line in f:
                for ds in date_strs:
                    if ds in line:
                        if 'SHORTLISTED' in line or 'PASSED' in line:
                            daily_data[ds]['shortlisted'].append(line.strip()[:120])
                        elif 'REJECTED' in line or 'FAILED' in line:
                            daily_data[ds]['rejected'].append(line.strip()[:120])
                        elif 'ENTRY' in line or 'BUY' in line:
                            daily_data[ds]['traded'] = line.strip()[:120]
                        break
    except FileNotFoundError:
        pass
    return daily_data


def read_daily_analysis_logs():
    week_dates = get_week_dates()
    date_strs = [d.strftime('%Y-%m-%d') for d in week_dates]
    analyses = []
    try:
        with open('analysis.log', 'r') as f:
            content = f.read()
            for ds in date_strs:
                if ds in content:
                    analyses.append(f'Analysis ran on {ds}')
    except FileNotFoundError:
        pass
    return analyses


def call_gemini(prompt):
    import time as _time
    api_key = get_parameter('/trading-engine/ai/gemini-api-key')
    if not api_key:
        return 'Gemini API key not available.'
    url = GEMINI_URL.format(model=GEMINI_MODEL) + f'?key={api_key}'
    payload = {'contents': [{'parts': [{'text': prompt}]}],
               'generationConfig': {'maxOutputTokens': 4096}}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait = 5 * (2 ** attempt)
                log.info(f'Gemini retry {attempt+1}/{max_retries}, waiting {wait}s...')
                _time.sleep(wait)
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('candidates',[{}])[0].get('content',{}).get('parts',[{}])[0].get('text','')
            elif resp.status_code == 429:
                log.warning(f'Gemini rate limited (429), attempt {attempt+1}')
                if attempt == max_retries - 1:
                    return 'Gemini quota exceeded. Weekly analysis unavailable.'
                continue
            else:
                return f'Gemini error: {resp.status_code} - {resp.text[:200]}'
        except Exception as e:
            if attempt == max_retries - 1:
                return f'Gemini unavailable: {str(e)}'
    return 'Gemini failed after retries.'


def build_weekly_prompt(trades, daily_data, daily_states):
    week_dates = get_week_dates()
    today = date.today().strftime('%d %b %Y')
    monday = week_dates.strftime('%d %b')
    friday = week_dates.strftime('%d %b')

    # Summarize trades
    if trades:
        trades_summary = NL.join([
            f'  {t.get("date")}: {t.get("ticker","?")} | Entry: {t.get("entry_price","?")} | '
            f'Exit: {t.get("exit_price","?")} | P&L: {t.get("pnl","?")} | R: {t.get("r_multiple","?")}'
            for t in trades])
    else:
        trades_summary = '  No trades executed this week'

    # Summarize daily shortlists
    daily_summary = ''
    for ds in sorted(daily_data.keys()):
        d = daily_data[ds]
        n_short = len(d['shortlisted'])
        n_reject = len(d['rejected'])
        traded = d['traded'] or 'No trade'
        daily_summary += f'  {ds}: {n_short} shortlisted, {n_reject} rejected, Trade: {traded}{NL}'

    # Win/loss stats
    wins = sum(1 for t in trades if float(t.get('pnl', 0)) > 0)
    losses = sum(1 for t in trades if float(t.get('pnl', 0)) < 0)
    total_pnl = sum(float(t.get('pnl', 0)) for t in trades)
    avg_r = sum(float(t.get('r_multiple', 0)) for t in trades) / max(len(trades), 1)

    return (f'You are an expert Indian stock market analyst and quantitative trading strategist.{NL}'
            f'Analyze this weeks trading data ({monday} to {friday}). Evaluate entry timing, price efficiency, and provide specific parameter recommendations.{NL}'
            f'{NL}=== WEEKLY TRADE RESULTS ==={NL}'
            f'{trades_summary}{NL}'
            f'{NL}Stats: {len(trades)} trades, {wins} wins, {losses} losses, '
            f'Total P&L: Rs.{total_pnl:.0f}, Avg R-multiple: {avg_r:.2f}{NL}'
            f'{NL}=== DAILY SHORTLISTING SUMMARY ==={NL}'
            f'{daily_summary}{NL}'
            f'{NL}=== OUR STRATEGY ==={NL}'
            f'ORB (15-min), Hybrid selection (616 watchlist + Top 20 dynamic), '
            f'Scoring: RS(25%)+Trend(25%)+RVOL(20%)+ATR(15%)+Breakout(15%), '
            f'Trailing SL 4-phase, Max 1 trade/day, 2% risk per trade{NL}'
            f'{NL}=== ANALYZE AND PROVIDE ==={NL}'
            f'{NL}1. WEEKLY PERFORMANCE SUMMARY:{NL}'
            f'   - Win rate, avg profit vs avg loss, expectancy{NL}'
            f'   - Best and worst trade analysis{NL}'
            f'{NL}2. PATTERN RECOGNITION:{NL}'
            f'   - Common traits of winning trades (gap size, score, sector, time){NL}'
            f'   - Common traits of losing trades{NL}'
            f'   - Stocks we shortlisted that became top gainers (hits){NL}'
            f'   - Top market gainers we completely missed (misses){NL}'
            f'{NL}3. FILTER EFFECTIVENESS:{NL}'
            f'   - Are our gap/price/turnover filters catching winners?{NL}'
            f'   - What % of daily top 3 gainers would have passed our filters?{NL}'
            f'   - Any filter too strict or too loose?{NL}'
            f'{NL}4. SCORING ACCURACY:{NL}'
            f'   - Correlation between score and actual P&L{NL}'
            f'   - Should any weight be adjusted?{NL}'
            f'{NL}5. SPECIFIC ACTION POINTS FOR NEXT WEEK:{NL}'
            f'   - List exactly 3 concrete changes (with numbers){NL}'
            f'   - Format: "Change X from Y to Z because this week showed..."{NL}'
            f'   - Each must be measurable and testable{NL}'
            f'{NL}6. CONFIDENCE RATING:{NL}'
            f'   - Rate strategy effectiveness 1-10{NL}'
            f'   - Rate filter quality 1-10{NL}'
            f'   - Rate risk management 1-10{NL}'
            f'{NL}Be data-driven. Reference specific trades and dates. No generic advice.')


def send_weekly_email(analysis, trades):
    ses = boto3.client('ses', region_name='ap-south-1')
    sender = get_parameter('/trading-engine/ses/sender-email')
    recipient = get_parameter('/trading-engine/ses/recipient-email')
    week_dates = get_week_dates()
    monday = week_dates.strftime('%d %b')
    friday = week_dates.strftime('%d %b %Y')

    # Trade summary table
    wins = sum(1 for t in trades if float(t.get('pnl', 0)) > 0)
    losses = sum(1 for t in trades if float(t.get('pnl', 0)) < 0)
    total_pnl = sum(float(t.get('pnl', 0)) for t in trades)

    trades_rows = ''.join([
        f'<tr><td>{t.get("date","")}</td><td>{t.get("ticker","")}</td>'
        f'<td>{t.get("entry_price","")}</td><td>{t.get("exit_price","")}</td>'
        f'<td style="color:{"green" if float(t.get("pnl",0))>0 else "red"}">'
        f'Rs.{float(t.get("pnl",0)):.0f}</td><td>{t.get("r_multiple","")}</td></tr>'
        for t in trades
    ]) if trades else '<tr><td colspan="6">No trades this week</td></tr>'

    pnl_color = 'green' if total_pnl >= 0 else 'red'

    html = (f'<html><body style="font-family:Arial;max-width:900px;margin:0 auto;">'
            f'<h2>Weekly Trading Bot Analysis - {monday} to {friday}</h2>'
            f'<h3>Performance Summary</h3>'
            f'<table border="0" cellpadding="10" style="border-collapse:collapse;">'
            f'<tr><td><b>Total Trades:</b> {len(trades)}</td>'
            f'<td><b>Wins:</b> {wins}</td>'
            f'<td><b>Losses:</b> {losses}</td>'
            f'<td><b>Total P&L:</b> <span style="color:{pnl_color}">Rs.{total_pnl:.0f}</span></td></tr></table>'
            f'<h3>Trade Details</h3>'
            f'<table border="1" cellpadding="8" style="border-collapse:collapse;width:100%;">'
            f'<tr style="background:#f0f0f0"><th>Date</th><th>Stock</th><th>Entry</th>'
            f'<th>Exit</th><th>P&L</th><th>R-Multiple</th></tr>'
            f'{trades_rows}</table>'
            f'<h3>Gemini Weekly Analysis</h3>'
            f'<div style="background:#f8f9fa;padding:20px;border-radius:8px;white-space:pre-wrap;font-size:14px;">'
            f'{analysis}</div>'
            f'<hr><p style="color:#666;font-size:12px;">Trading Bot v6.1 | Weekly Report | Gemini 2.0 Flash</p>'
            f'</body></html>')

    ses.send_email(Source=sender, Destination={'ToAddresses': [recipient]},
                   Message={'Subject': {'Data': f'Weekly Bot Analysis - {monday} to {friday} | P&L: Rs.{total_pnl:.0f}'},
                            'Body': {'Html': {'Data': html}}})
    log.info('Weekly email sent')


def run_weekly_analysis():
    log.info('=== Weekly Trading Analysis (Friday) ===')

    # Gather data
    log.info('Fetching weekly trades...')
    trades = get_weekly_trades()
    log.info(f'Trades this week: {len(trades)}')

    log.info('Reading daily shortlist data...')
    daily_data = read_weekly_bot_logs()

    log.info('Fetching daily states...')
    daily_states = get_weekly_daily_states()

    # Build prompt and call Gemini
    log.info('Calling Gemini for weekly analysis...')
    prompt = build_weekly_prompt(trades, daily_data, daily_states)
    analysis = call_gemini(prompt)
    log.info(f'Gemini response: {len(analysis)} chars')

    # Send email
    log.info('Sending weekly report...')
    send_weekly_email(analysis, trades)

    log.info('=== Weekly Analysis Complete ===')


if __name__ == '__main__':
    run_weekly_analysis()
