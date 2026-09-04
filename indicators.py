import pandas as pd
import numpy as np

def compute_vwap(df):
    df = df.copy()
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = df['volume'].cumsum()
    cum_tp_vol = (typical_price * df['volume']).cumsum()
    return cum_tp_vol / cum_vol

def compute_rsi(df, period=14):
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_atr(df, period=14):
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def compute_supertrend(df, length=7, multiplier=3.0):
    high = df['high'].values.copy()
    low = df['low'].values.copy()
    close = df['close'].values.copy()
    n = len(df)
    prev_close = np.empty(n)
    prev_close = close
    prev_close[1:] = close[:-1]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).ewm(span=length, adjust=False).mean().values
    hl2 = (high + low) / 2
    upper_band = hl2 + (multiplier * atr)
    lower_band = hl2 - (multiplier * atr)
    direction = np.ones(n)
    supertrend = np.zeros(n)
    final_upper = upper_band.copy()
    final_lower = lower_band.copy()
    for i in range(1, n):
        if final_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            pass
        else:
            final_upper[i] = final_upper[i-1]
        if final_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            pass
        else:
            final_lower[i] = final_lower[i-1]
        if direction[i-1] == -1:
            if close[i] > final_upper[i]:
                direction[i] = 1
            else:
                direction[i] = -1
        else:
            if close[i] < final_lower[i]:
                direction[i] = -1
            else:
                direction[i] = 1
        if direction[i] == 1:
            supertrend[i] = final_lower[i]
        else:
            supertrend[i] = final_upper[i]
    result = pd.DataFrame(index=df.index)
    result['supertrend'] = supertrend
    result['direction'] = direction
    return result

def compute_volatility_compression(df):
    atr_5 = compute_atr(df, period=5)
    atr_20 = compute_atr(df, period=20)
    last_atr5 = atr_5.iloc[-2] if len(atr_5) > 1 else atr_5.iloc[-1]
    last_atr20 = atr_20.iloc[-2] if len(atr_20) > 1 else atr_20.iloc[-1]
    if last_atr20 == 0 or pd.isna(last_atr20):
        return None
    return last_atr5 / last_atr20

def compute_trend_quality(df):
    ema9 = compute_ema(df['close'], 9)
    ema21 = compute_ema(df['close'], 21)
    last_close = df['close'].iloc[-1]
    last_ema9 = ema9.iloc[-1]
    last_ema21 = ema21.iloc[-1]
    score = 0.0
    if last_ema9 > last_ema21:
        score += 0.5
    if last_close > last_ema9 and last_close > last_ema21:
        score += 0.5
    return score

def compute_all(df):
    df = df.copy()
    df['vwap'] = compute_vwap(df)
    df['rsi'] = compute_rsi(df)
    df['ema9'] = compute_ema(df['close'], 9)
    df['ema21'] = compute_ema(df['close'], 21)
    df['atr'] = compute_atr(df)
    st = compute_supertrend(df)
    df['supertrend'] = st['supertrend']
    df['st_direction'] = st['direction']
    return df
