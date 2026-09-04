import os, sys, json, time, logging
import pandas as pd
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

sys.path.insert(0, '/home/ubuntu/trading-bot')

HISTORY_FILE = '/home/ubuntu/trading-bot/stock_history_30d.json'

def pull_history():
    import yfinance as yf

    # Load watchlist
    wl = pd.read_csv('/home/ubuntu/trading-bot/watchlist.csv')
    tickers = wl['ticker'].tolist()

    log.info(f'Pulling 30-day history for {len(tickers)} stocks...')

    history = {}
    batch_size = 50

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        # Add .NS suffix for NSE stocks
        symbols = [f'{t}.NS' for t in batch]

        try:
            data = yf.download(symbols, period='1mo', interval='1d',
                             group_by='ticker', progress=False, threads=True)

            for j, ticker in enumerate(batch):
                symbol = f'{ticker}.NS'
                try:
                    if len(batch) == 1:
                        df = data
                    else:
                        df = data[symbol] if symbol in data.columns.get_level_values(0) else None

                    if df is not None and not df.empty:
                        records = []
                        for idx, row in df.iterrows():
                            records.append({
                                'date': idx.strftime('%Y-%m-%d'),
                                'open': round(float(row['Open']), 2) if pd.notna(row['Open']) else 0,
                                'high': round(float(row['High']), 2) if pd.notna(row['High']) else 0,
                                'low': round(float(row['Low']), 2) if pd.notna(row['Low']) else 0,
                                'close': round(float(row['Close']), 2) if pd.notna(row['Close']) else 0,
                                'volume': int(row['Volume']) if pd.notna(row['Volume']) else 0
                            })
                        history[ticker] = records
                except Exception as e:
                    pass

            log.info(f'  {min(i+batch_size, len(tickers))}/{len(tickers)} done ({len(history)} stocks with data)')
            time.sleep(1)  # Rate limit courtesy

        except Exception as e:
            log.warning(f'  Batch {i}-{i+batch_size} failed: {e}')
            time.sleep(2)

    # Add NIFTY 50 index for true relative-strength (non-blocking if it fails)
    try:
        ndf = yf.download('^NSEI', period='1mo', interval='1d', progress=False)
        def _cell(row, col):
            v = row[col]
            try: return float(v)
            except Exception:
                try: return float(v.iloc[0])
                except Exception: return 0.0
        nrecs = []
        for idx, row in ndf.iterrows():
            c = _cell(row, 'Close')
            if c > 0:
                nrecs.append({'date': idx.strftime('%Y-%m-%d'),
                    'open': round(_cell(row,'Open'),2), 'high': round(_cell(row,'High'),2),
                    'low': round(_cell(row,'Low'),2), 'close': round(c,2), 'volume': 0})
        if len(nrecs) >= 6:
            history['NIFTY'] = nrecs
            log.info(f'NIFTY index added: {len(nrecs)} sessions (RS true-relative)')
        else:
            log.warning('NIFTY fetch insufficient - RS stays raw-return fallback')
    except Exception as e:
        log.warning(f'NIFTY fetch failed ({e}) - RS raw-return fallback (non-blocking)')

    # Save to file
    with open(HISTORY_FILE, 'w') as f:
        json.dump({'updated': str(date.today()), 'stocks': history}, f)

    log.info(f'Saved {len(history)} stocks to {HISTORY_FILE}')

    # Calculate key metrics
    metrics = {}
    for ticker, records in history.items():
        if len(records) < 5:
            continue
        closes = [r['close'] for r in records if r['close'] > 0]
        volumes = [r['volume'] for r in records if r['volume'] > 0]

        if len(closes) >= 5 and len(volumes) >= 5:
            # 5-day relative strength
            rs_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0
            # Average volume (10-day)
            avg_vol = sum(volumes[-10:]) / len(volumes[-10:]) if len(volumes) >= 10 else sum(volumes) / len(volumes)
            # Latest close
            latest_close = closes[-1]
            # FILTERS_V2 pre-compute (2026-07-14): EMAs, ADV, structural high, PDH.
            # period='1mo' ~= 22 sessions -> ema5/ema20/adv_20d OK. Per-stock ema50
            # NOT computed (insufficient history; not used in filters).
            _highs = [r['high'] for r in records if r['high'] > 0]
            _cs = pd.Series(closes)
            _ema5 = round(float(_cs.ewm(span=5, adjust=False).mean().iloc[-1]), 2)
            _ema20 = round(float(_cs.ewm(span=20, adjust=False).mean().iloc[-1]), 2) if len(closes) >= 20 else _ema5
            _adv20 = int(sum(volumes[-20:]) / len(volumes[-20:])) if len(volumes) >= 20 else int(avg_vol)
            _shigh5 = round(max(_highs[-5:]), 2) if len(_highs) >= 5 else (round(max(_highs), 2) if _highs else 0)
            _pdh = round(_highs[-1], 2) if _highs else 0

            metrics[ticker] = {
                'rs_5d': round(rs_5d, 2),
                'avg_vol_10d': int(avg_vol),
                'latest_close': latest_close,
                'days_available': len(records),
                'ema5': _ema5,
                'ema20': _ema20,
                'adv_20d': _adv20,
                'structural_high_5d': _shigh5,
                'prev_day_high': _pdh
            }

    # Save metrics
    metrics_file = '/home/ubuntu/trading-bot/stock_metrics.json'
    with open(metrics_file, 'w') as f:
        json.dump({'updated': str(date.today()), 'metrics': metrics}, f)

    log.info(f'Metrics calculated for {len(metrics)} stocks')
    return len(history), len(metrics)

if __name__ == '__main__':
    total, metrics = pull_history()
    print(f'Done: {total} stocks history, {metrics} stocks with metrics')
