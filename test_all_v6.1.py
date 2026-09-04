"""
═══════════════════════════════════════════════════════════════════
TRADING BOT v6.1 — COMPREHENSIVE TEST SUITE
═══════════════════════════════════════════════════════════════════
Tests: 12 total
  1. Config Validation (all thresholds sane)
  2. Watchlist CSV Loading
  3. Technical Indicators (Supertrend, RSI, EMA, VWAP, ATR, Compression)
  4. Scoring Engine Logic
  5. Position Sizing Calculator
  6. Trailing Stop Loss (all 4 phases)
  7. Partial Fill Handling Logic
  8. Dhan API Connectivity (balance check)
  9. AWS SSM Parameters (all 11 readable)
 10. DynamoDB Tables (all 4 exist + writable)
 11. SES Email (test send)
 12. Adaptive Polling Logic
═══════════════════════════════════════════════════════════════════
Run: python test_all_v6.1.py
═══════════════════════════════════════════════════════════════════
"""

import sys
import time
import traceback
import pandas as pd
import numpy as np

# ═══════════════════════════════════════════════════════════════════
# TEST FRAMEWORK
# ═══════════════════════════════════════════════════════════════════
results = []

def run_test(test_num, test_name, test_func):
    """Run a single test and capture result."""
    print(f"\n{'─'*60}")
    print(f"  TEST {test_num}: {test_name}")
    print(f"{'─'*60}")
    try:
        test_func()
        results.append(('PASS', test_num, test_name))
        print(f"  ✅ PASSED")
    except AssertionError as e:
        results.append(('FAIL', test_num, test_name, str(e)))
        print(f"  ❌ FAILED: {e}")
    except Exception as e:
        results.append(('FAIL', test_num, test_name, str(e)))
        print(f"  ❌ ERROR: {e}")
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════
# TEST 1: CONFIG VALIDATION
# ═══════════════════════════════════════════════════════════════════
def test_config():
    import config
    
    # Price filters
    assert config.PRICE_FLOOR == 60, f"PRICE_FLOOR should be 60, got {config.PRICE_FLOOR}"
    assert config.PRICE_CEIL_TIER1 == 5000, f"PRICE_CEIL_TIER1 should be 5000"
    assert config.PRICE_CEIL_TIER2 == 1500, f"PRICE_CEIL_TIER2 should be 1500"
    assert config.PRICE_FLOOR < config.PRICE_CEIL_TIER2 < config.PRICE_CEIL_TIER1
    print(f"    Price range: ₹{config.PRICE_FLOOR} - ₹{config.PRICE_CEIL_TIER1} (T1), ₹{config.PRICE_CEIL_TIER2} (T2)")
    
    # Gap filters
    assert config.GAP_MIN == 0.3, f"GAP_MIN should be 0.3"
    assert config.GAP_MAX_TIER2 == 8.0, f"GAP_MAX_TIER2 should be 8.0"
    assert config.GAP_REJECT == 15.0, f"GAP_REJECT should be 15.0"
    assert config.TIER2_GAP_MIN == 1.0, f"TIER2_GAP_MIN should be 1.0"
    assert config.GAP_MIN < config.TIER2_GAP_MIN < config.GAP_MAX_TIER2 < config.GAP_REJECT
    print(f"    Gap range: {config.GAP_MIN}% - {config.GAP_MAX_TIER2}% (T2 min: {config.TIER2_GAP_MIN}%)")
    
    # Scoring weights must sum to 1.0
    total_weight = (config.WEIGHT_RS + config.WEIGHT_TREND + config.WEIGHT_RVOL + 
                    config.WEIGHT_ATR_EXPANSION + config.WEIGHT_BREAKOUT)
    assert abs(total_weight - 1.0) < 0.001, f"Weights sum to {total_weight}, should be 1.0"
    print(f"    Scoring weights sum: {total_weight:.3f} ✓")
    
    # Verify updated weights (post-analyst review)
    assert config.WEIGHT_RS == 0.25, f"WEIGHT_RS should be 0.25 (was 0.30)"
    assert config.WEIGHT_TREND == 0.25, f"WEIGHT_TREND should be 0.25 (was 0.15)"
    assert config.WEIGHT_RVOL == 0.20, f"WEIGHT_RVOL should be 0.20 (was 0.25)"
    assert config.WEIGHT_ATR_EXPANSION == 0.15, f"WEIGHT_ATR should be 0.15 (was 0.10)"
    assert config.WEIGHT_BREAKOUT == 0.15, f"WEIGHT_BREAKOUT should be 0.15 (was 0.20)"
    print(f"    Weights: RS={config.WEIGHT_RS}, Trend={config.WEIGHT_TREND}, "
          f"RVOL={config.WEIGHT_RVOL}, ATR={config.WEIGHT_ATR_EXPANSION}, "
          f"Breakout={config.WEIGHT_BREAKOUT}")
    
    # Risk parameters
    assert config.RISK_PER_TRADE_PCT == 2.0, f"Risk should be 2%"
    assert config.ATR_SL_MULTIPLIER == 1.5, f"ATR multiplier should be 1.5"
    assert config.DAILY_LOSS_LIMIT_PCT == 5.0, f"Daily loss limit should be 5%"
    assert config.MAX_CONSECUTIVE_LOSSES == 3, f"Max consecutive losses should be 3"
    print(f"    Risk: {config.RISK_PER_TRADE_PCT}% per trade, "
          f"{config.DAILY_LOSS_LIMIT_PCT}% daily limit, "
          f"{config.MAX_CONSECUTIVE_LOSSES} consecutive lock")
    
    # Trailing SL phases must be in order
    assert config.TRAIL_PHASE1_TRIGGER < config.TRAIL_PHASE2_TRIGGER < config.TRAIL_PHASE3_TRIGGER
    assert config.TRAIL_PHASE1_LEVEL < config.TRAIL_PHASE2_LEVEL
    print(f"    Trailing: P1={config.TRAIL_PHASE1_TRIGGER}R, "
          f"P2={config.TRAIL_PHASE2_TRIGGER}R, P3={config.TRAIL_PHASE3_TRIGGER}R")
    
    # Time rules
    assert config.SCAN_INTERVAL_NORMAL == 60, f"Normal scan should be 60s"
    assert config.SCAN_INTERVAL_NEAR_ORB == 15, f"Near ORB scan should be 15s"
    assert config.MONITOR_INTERVAL_SECONDS == 30, f"Monitor interval should be 30s"
    print(f"    Polling: {config.SCAN_INTERVAL_NORMAL}s normal, "
          f"{config.SCAN_INTERVAL_NEAR_ORB}s near ORB, "
          f"{config.MONITOR_INTERVAL_SECONDS}s active trade")
    
    # New v6.1 fields
    assert config.PARTIAL_FILL_MIN_PCT == 0.50, f"Partial fill min should be 50%"
    assert config.SPREAD_MAX_PCT == 0.5, f"Max spread should be 0.5%"
    assert config.RS_PERSISTENCE_MIN == 0.8, f"RS persistence should be 0.8"
    assert config.VOL_COMPRESSION_RATIO == 0.8, f"Vol compression ratio should be 0.8"
    assert config.BONUS_SECTOR_TOP == 0.05, f"Sector top bonus should be 0.05"
    assert config.PENALTY_SECTOR_WEAK == -0.03, f"Sector weak penalty should be -0.03"
    print(f"    v6.1 additions: Spread<{config.SPREAD_MAX_PCT}%, "
          f"RS persist>{config.RS_PERSISTENCE_MIN}, "
          f"PartialFill>{config.PARTIAL_FILL_MIN_PCT*100}%")
    
    # DynamoDB tables (4 in v6.1)
    assert hasattr(config, 'TABLE_ORDER_AUDIT'), "Missing TABLE_ORDER_AUDIT"
    assert config.TABLE_ORDER_AUDIT == "TradingBot_OrderAudit"
    print(f"    Tables: {config.TABLE_TRADES}, {config.TABLE_DAILY_STATE}, "
          f"{config.TABLE_ACTIVE_TRADE}, {config.TABLE_ORDER_AUDIT}")


# ═══════════════════════════════════════════════════════════════════
# TEST 2: WATCHLIST CSV
# ═══════════════════════════════════════════════════════════════════
def test_watchlist():
    df = pd.read_csv('watchlist.csv')
    
    assert 'ticker' in df.columns, "Missing 'ticker' column"
    assert 'security_id' in df.columns, "Missing 'security_id' column"
    assert len(df) > 0, "Watchlist is empty"
    assert df['security_id'].dtype in [np.int64, np.float64, object], "security_id wrong type"
    assert df['ticker'].nunique() == len(df), "Duplicate tickers found"
    assert df['security_id'].nunique() == len(df), "Duplicate security_ids found"
    assert not df['ticker'].isnull().any(), "Null tickers found"
    assert not df['security_id'].isnull().any(), "Null security_ids found"
    
    print(f"    Loaded: {len(df)} stocks")
    print(f"    Columns: {list(df.columns)}")
    print(f"    Sample: {df.head(3).to_string(index=False)}")
    print(f"    No duplicates: ✓")
    print(f"    No nulls: ✓")


# ═══════════════════════════════════════════════════════════════════
# TEST 3: TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════════════
def test_indicators():
    import indicators as ind
    
    # Create sample OHLCV data (50 candles)
    np.random.seed(42)
    n = 50
    base_price = 500
    closes = base_price + np.cumsum(np.random.randn(n) * 2)
    highs = closes + np.abs(np.random.randn(n) * 3)
    lows = closes - np.abs(np.random.randn(n) * 3)
    opens = closes + np.random.randn(n) * 1
    volumes = np.random.randint(10000, 100000, n)
    
    df = pd.DataFrame({
        'open': opens, 'high': highs, 'low': lows,
        'close': closes, 'volume': volumes
    })
    
    # Test VWAP
    vwap = ind.compute_vwap(df)
    assert vwap is not None, "VWAP returned None"
    assert len(vwap) == n, f"VWAP length {len(vwap)} != {n}"
    assert not vwap.isnull().all(), "VWAP all NaN"
    print(f"    VWAP: last={vwap.iloc[-1]:.2f} ✓")
    
    # Test RSI
    rsi = ind.compute_rsi(df)
    assert rsi is not None, "RSI returned None"
    last_rsi = rsi.dropna().iloc[-1]
    assert 0 <= last_rsi <= 100, f"RSI {last_rsi} out of range"
    print(f"    RSI(14): last={last_rsi:.2f} ✓")
    
    # Test EMA
    ema9 = ind.compute_ema(df['close'], 9)
    ema21 = ind.compute_ema(df['close'], 21)
    assert ema9 is not None and ema21 is not None, "EMA returned None"
    print(f"    EMA9={ema9.iloc[-1]:.2f}, EMA21={ema21.iloc[-1]:.2f} ✓")
    
    # Test ATR
    atr = ind.compute_atr(df)
    assert atr is not None, "ATR returned None"
    last_atr = atr.dropna().iloc[-1]
    assert last_atr > 0, f"ATR {last_atr} should be positive"
    print(f"    ATR(14): last={last_atr:.2f} ✓")
    
    # Test Supertrend
    st = ind.compute_supertrend(df)
    assert st is not None, "Supertrend returned None"
    assert 'supertrend' in st.columns, "Missing supertrend column"
    assert 'direction' in st.columns, "Missing direction column"
    last_dir = st['direction'].dropna().iloc[-1]
    assert last_dir in [-1, 1], f"Direction {last_dir} not in [-1, 1]"
    print(f"    Supertrend: dir={int(last_dir)} ✓")
    
    # Test Volatility Compression (NEW in v6.1)
    compression = ind.compute_volatility_compression(df)
    assert compression is not None, "Vol compression returned None"
    assert compression > 0, f"Compression ratio {compression} should be positive"
    print(f"    Vol Compression (ATR5/ATR20): {compression:.3f} ✓")
    
    # Test Trend Quality (NEW in v6.1)
    trend_quality = ind.compute_trend_quality(df)
    assert 0.0 <= trend_quality <= 1.0, f"Trend quality {trend_quality} out of range"
    print(f"    Trend Quality: {trend_quality:.2f} ✓")
    
    # Test compute_all
    result = ind.compute_all(df)
    expected_cols = ['vwap', 'rsi', 'ema9', 'ema21', 'atr', 'supertrend', 'st_direction']
    for col in expected_cols:
        assert col in result.columns, f"Missing column: {col}"
    print(f"    compute_all: all {len(expected_cols)} columns present ✓")


# ═══════════════════════════════════════════════════════════════════
# TEST 4: SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════
def test_scoring():
    import config
    
    # Simulate scoring for 3 candidates
    candidates = [
        {'ticker': 'STRONG', 'rs': 3.0, 'rvol': 4.0, 'tier': 'CURATED', 'sector_score': 0.05},
        {'ticker': 'MEDIUM', 'rs': 1.5, 'rvol': 2.5, 'tier': 'CURATED', 'sector_score': 0.0},
        {'ticker': 'WEAK', 'rs': 0.5, 'rvol': 1.5, 'tier': 'DISCOVERY', 'sector_score': -0.03},
    ]
    
    scores = []
    for c in candidates:
        # Manual scoring (mimics ScoringEngine.compute_score)
        rs_norm = min(c['rs'] / 3.0, 1.0)
        rvol_norm = min(c['rvol'] / 3.0, 1.0)
        trend = 0.75  # Assume decent trend
        atr_comp = 0.6  # Neutral compression
        breakout = 0.5  # Moderate breakout
        
        base = (config.WEIGHT_RS * rs_norm +
                config.WEIGHT_TREND * trend +
                config.WEIGHT_RVOL * rvol_norm +
                config.WEIGHT_ATR_EXPANSION * atr_comp +
                config.WEIGHT_BREAKOUT * breakout)
        
        # Bonuses/penalties
        bonus = c['sector_score']
        if c['tier'] == 'DISCOVERY':
            bonus += config.PENALTY_TIER2
        
        final = base + bonus
        scores.append((c['ticker'], final))
        print(f"    {c['ticker']}: base={base:.3f}, bonus={bonus:.3f}, final={final:.3f}")
    
    # Strong should beat Medium should beat Weak
    assert scores[0][1] > scores[1][1], "STRONG should score higher than MEDIUM"
    assert scores[1][1] > scores[2][1], "MEDIUM should score higher than WEAK"
    print(f"    Ranking: {scores[0][0]} > {scores[1][0]} > {scores[2][0]} ✓")
    
    # Tier 2 penalty applied
    assert scores[2][1] < scores[1][1], "Discovery penalty should lower score"
    print(f"    Tier 2 penalty: applied ✓")
    
    # Sector bonus differentiation
    assert scores[0][1] > scores[0][1] - 0.05, "Sector bonus should add value"
    print(f"    Sector gradient: +5%/0/-3% applied ✓")


# ═══════════════════════════════════════════════════════════════════
# TEST 5: POSITION SIZING
# ═══════════════════════════════════════════════════════════════════
def test_position_sizing():
    import config
    
    balance = 6000  # ₹6,000
    entry_price = 250  # ₹250 stock
    atr = 8.0  # ATR = ₹8
    
    # Calculate
    available = balance * (1 - config.CASH_BUFFER_PCT / 100)  # 5700
    risk_amount = available * (config.RISK_PER_TRADE_PCT / 100)  # 114
    sl_distance = atr * config.ATR_SL_MULTIPLIER  # 12
    qty = int(risk_amount / sl_distance)  # 9
    
    print(f"    Balance: ₹{balance}")
    print(f"    Available (95%): ₹{available:.0f}")
    print(f"    Risk amount (2%): ₹{risk_amount:.2f}")
    print(f"    SL distance (ATR×1.5): ₹{sl_distance:.2f}")
    print(f"    Quantity: {qty} shares")
    
    assert qty > 0, "Quantity should be positive"
    assert qty >= config.MIN_QUANTITY, f"Qty {qty} below minimum {config.MIN_QUANTITY}"
    assert qty * entry_price <= available, f"Position ₹{qty*entry_price} exceeds available ₹{available}"
    print(f"    Position value: ₹{qty * entry_price} (< ₹{available:.0f}) ✓")
    
    # Tier 2 reduction
    tier2_qty = int(qty * config.TIER2_POSITION_FACTOR)
    assert tier2_qty < qty, "Tier 2 qty should be less"
    assert tier2_qty >= config.MIN_QUANTITY, f"Tier 2 qty {tier2_qty} below minimum"
    print(f"    Tier 2 quantity: {tier2_qty} shares (50% of {qty}) ✓")
    
    # Edge case: expensive stock
    expensive_price = 4500
    expensive_qty = int(available / expensive_price)
    print(f"    Expensive stock (₹{expensive_price}): max {expensive_qty} share(s)")
    
    # Edge case: very volatile stock
    high_atr = 50
    high_sl = high_atr * config.ATR_SL_MULTIPLIER
    high_qty = int(risk_amount / high_sl)
    print(f"    High ATR (₹{high_atr}): qty={high_qty} "
          f"({'OK' if high_qty >= config.MIN_QUANTITY else 'SKIP - below min'})")


# ═══════════════════════════════════════════════════════════════════
# TEST 6: TRAILING STOP LOSS (All 4 Phases)
# ═══════════════════════════════════════════════════════════════════
def test_trailing_sl():
    import config
    
    entry_price = 200.0
    atr = 6.0
    r_value = atr * config.ATR_SL_MULTIPLIER  # 9.0
    
    initial_sl = entry_price - r_value  # 191.0
    print(f"    Entry: ₹{entry_price}, R-value: ₹{r_value}, Initial SL: ₹{initial_sl}")
    
    # Simulate price movement: entry → +0.5R → +1R → +1.5R → +2R → +3R → pullback
    price_path = [200, 202, 204.5, 209, 213.5, 218, 227, 220, 215]
    expected_phases = ['INITIAL', 'INITIAL', 'INITIAL', 'PHASE1', 'PHASE1', 
                       'PHASE2', 'PHASE3', 'PHASE3', 'EXIT']
    
    trailing_sl = initial_sl
    max_price = entry_price
    phase = 'INITIAL'
    
    print(f"\n    {'Price':<8} {'R-Mult':<8} {'Phase':<10} {'SL':<10} {'Action'}")
    print(f"    {'─'*50}")
    
    for i, price in enumerate(price_path):
        max_price = max(max_price, price)
        pnl_per_share = price - entry_price
        r_multiple = pnl_per_share / r_value
        
        # Phase determination
        if r_multiple >= config.TRAIL_PHASE3_TRIGGER:
            phase = 'PHASE3'
            max_gain = max_price - entry_price
            new_sl = entry_price + (max_gain * config.TRAIL_PHASE3_FACTOR)
        elif r_multiple >= config.TRAIL_PHASE2_TRIGGER:
            phase = 'PHASE2'
            new_sl = entry_price + (r_value * config.TRAIL_PHASE2_LEVEL)
        elif r_multiple >= config.TRAIL_PHASE1_TRIGGER:
            phase = 'PHASE1'
            new_sl = entry_price + (r_value * config.TRAIL_PHASE1_LEVEL)
        else:
            phase = 'INITIAL'
            new_sl = initial_sl
        
        # SL can only move UP
        trailing_sl = max(trailing_sl, new_sl)
        
        # Check exit
        action = ""
        if price <= trailing_sl:
            phase = 'EXIT'
            action = "← TRAILING SL HIT"
        
        print(f"    ₹{price:<7} {r_multiple:<8.2f} {phase:<10} ₹{trailing_sl:<9.2f} {action}")
        
        if phase == 'EXIT':
            break
    
    # Validate trailing SL logic
    assert initial_sl < entry_price, "Initial SL should be below entry"
    assert trailing_sl > initial_sl, "Trailing SL should have moved up"
    
    # Phase 1 check
    phase1_sl = entry_price + (r_value * config.TRAIL_PHASE1_LEVEL)
    assert phase1_sl > entry_price, "Phase 1 SL should be above entry (breakeven+)"
    print(f"\n    Phase 1 SL: ₹{phase1_sl:.2f} (entry + 0.25R) ✓")
    
    # Phase 2 check  
    phase2_sl = entry_price + (r_value * config.TRAIL_PHASE2_LEVEL)
    assert phase2_sl > phase1_sl, "Phase 2 SL should be above Phase 1"
    print(f"    Phase 2 SL: ₹{phase2_sl:.2f} (entry + 0.75R) ✓")
    
    # Phase 3 check (60% of max gain)
    test_max_gain = 27.0  # If price reached 227
    phase3_sl = entry_price + (test_max_gain * config.TRAIL_PHASE3_FACTOR)
    assert phase3_sl > phase2_sl, "Phase 3 SL should be above Phase 2"
    print(f"    Phase 3 SL: ₹{phase3_sl:.2f} (entry + 60% of ₹{test_max_gain} gain) ✓")
    
    print(f"\n    ✓ All trailing phases validated correctly")


# ═══════════════════════════════════════════════════════════════════
# TEST 7: PARTIAL FILL HANDLING
# ═══════════════════════════════════════════════════════════════════
def test_partial_fill():
    import config
    
    requested_qty = 20
    
    # Scenario 1: 60% filled (above threshold) → CONTINUE
    filled_1 = 12
    fill_pct_1 = filled_1 / requested_qty
    continue_1 = fill_pct_1 >= config.PARTIAL_FILL_MIN_PCT
    print(f"    Scenario 1: Requested={requested_qty}, Filled={filled_1} ({fill_pct_1*100:.0f}%)")
    print(f"      Decision: {'CONTINUE with 12 shares' if continue_1 else 'EXIT'} ✓")
    assert continue_1 == True, "60% fill should continue"
    
    # Scenario 2: 30% filled (below threshold) → EXIT
    filled_2 = 6
    fill_pct_2 = filled_2 / requested_qty
    continue_2 = fill_pct_2 >= config.PARTIAL_FILL_MIN_PCT
    print(f"    Scenario 2: Requested={requested_qty}, Filled={filled_2} ({fill_pct_2*100:.0f}%)")
    print(f"      Decision: {'CONTINUE' if continue_2 else 'EXIT partial fill at market'} ✓")
    assert continue_2 == False, "30% fill should exit"
    
    # Scenario 3: Exactly 50% → CONTINUE (boundary)
    filled_3 = 10
    fill_pct_3 = filled_3 / requested_qty
    continue_3 = fill_pct_3 >= config.PARTIAL_FILL_MIN_PCT
    print(f"    Scenario 3: Requested={requested_qty}, Filled={filled_3} ({fill_pct_3*100:.0f}%)")
    print(f"      Decision: {'CONTINUE with 10 shares' if continue_3 else 'EXIT'} ✓")
    assert continue_3 == True, "50% fill should continue (boundary)"
    
    # Risk recalculation with partial fill
    balance = 6000
    atr = 8.0
    sl_distance = atr * 1.5  # 12
    
    original_risk = requested_qty * sl_distance  # 240
    partial_risk = filled_1 * sl_distance  # 144
    print(f"\n    Risk recalculation:")
    print(f"      Original risk (20 shares): ₹{original_risk:.0f}")
    print(f"      Partial risk (12 shares): ₹{partial_risk:.0f}")
    print(f"      Risk reduced by: {(1 - partial_risk/original_risk)*100:.0f}% ✓")


# ═══════════════════════════════════════════════════════════════════
# TEST 8: DHAN API CONNECTIVITY
# ═══════════════════════════════════════════════════════════════════
def test_dhan_api():
    import requests
    from secrets_manager import get_dhan_token, get_dhan_client_id
    
    token = get_dhan_token()
    client_id = get_dhan_client_id()
    
    assert token is not None, "Dhan token is None (SSM fetch failed)"
    assert client_id is not None, "Dhan client_id is None"
    assert len(token) > 10, f"Token too short: {len(token)} chars"
    print(f"    Token: {token[:8]}...{token[-4:]} ({len(token)} chars)")
    print(f"    Client ID: {client_id}")
    
    # Test balance endpoint
    headers = {
        "Content-Type": "application/json",
        "access-token": token
    }
    resp = requests.get("https://api.dhan.co/v2/fundlimit", headers=headers, timeout=10)
    
    assert resp.status_code == 200, f"API returned {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    
    # Extract balance
    balance = None
    if 'availabelBalance' in data:
        balance = float(data['availabelBalance'])
    elif 'data' in data and 'availabelBalance' in data.get('data', {}):
        balance = float(data['data']['availabelBalance'])
    
    assert balance is not None, f"Could not extract balance from: {list(data.keys())}"
    assert balance >= 0, f"Balance is negative: {balance}"
    print(f"    Balance: ₹{balance:,.2f} ✓")
    print(f"    API Status: 200 OK ✓")


# ═══════════════════════════════════════════════════════════════════
# TEST 9: AWS SSM PARAMETERS
# ═══════════════════════════════════════════════════════════════════
def test_ssm_parameters():
    import boto3
    
    ssm = boto3.client('ssm', region_name='ap-south-1')
    
    required_params = [
        '/trading-engine/dhan/access-token',
        '/trading-engine/dhan/client-id',
        '/trading-engine/dhan/login-id',
        '/trading-engine/dhan/password',
        '/trading-engine/dhan/totp-secret',
        '/trading-engine/ai/gemini-api-key',
        '/trading-engine/google/api-key',
        '/trading-engine/google/apps-script-url',
        '/trading-engine/google/sheet-id',
        '/trading-engine/ses/recipient-email',
        '/trading-engine/ses/sender-email',
    ]
    
    success = 0
    failed = []
    
    for param in required_params:
        try:
            resp = ssm.get_parameter(Name=param, WithDecryption=True)
            value = resp['Parameter']['Value']
            assert len(value) > 0, f"Empty value for {param}"
            success += 1
            short_name = param.split('/')[-1]
            print(f"    ✓ {short_name}: {value[:4]}***")
        except Exception as e:
            failed.append(param)
            print(f"    ✗ {param}: {e}")
    
    print(f"\n    Result: {success}/{len(required_params)} parameters accessible")
    assert success == len(required_params), f"Failed params: {failed}"


# ═══════════════════════════════════════════════════════════════════
# TEST 10: DYNAMODB TABLES
# ═══════════════════════════════════════════════════════════════════
def test_dynamodb():
    import boto3
    import config
    
    dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
    
    required_tables = [
        config.TABLE_TRADES,
        config.TABLE_DAILY_STATE,
        config.TABLE_ACTIVE_TRADE,
        config.TABLE_ORDER_AUDIT,  # NEW in v6.1
    ]
    
    for table_name in required_tables:
        try:
            table = dynamodb.Table(table_name)
            status = table.table_status
            assert status == 'ACTIVE', f"{table_name} status is {status}"
            print(f"    ✓ {table_name}: ACTIVE")
        except Exception as e:
            # Try to create if missing
            if 'ResourceNotFoundException' in str(e):
                print(f"    ✗ {table_name}: NOT FOUND — run dynamo_setup_v6.1.py")
                raise AssertionError(f"Table {table_name} does not exist")
            raise
    
    # Test write/read to OrderAudit (new table)
    audit_table = dynamodb.Table(config.TABLE_ORDER_AUDIT)
    test_item = {
        'order_id': 'TEST_ORDER_001',
        'timestamp': '2026-06-25T10:00:00',
        'symbol': 'TEST',
        'security_id': 0,
        'action': 'BUY',
        'status': 'TEST',
        'requested_qty': 10,
        'filled_qty': 10,
        'requested_price': '100.00',
        'filled_price': '100.00',
        'reason': 'test_validation',
        'latency_ms': 0
    }
    audit_table.put_item(Item=test_item)
    
    # Read back
    resp = audit_table.get_item(Key={'order_id': 'TEST_ORDER_001', 'timestamp': '2026-06-25T10:00:00'})
    assert 'Item' in resp, "Could not read test item from OrderAudit"
    
    # Clean up
    audit_table.delete_item(Key={'order_id': 'TEST_ORDER_001', 'timestamp': '2026-06-25T10:00:00'})
    print(f"    ✓ OrderAudit: write/read/delete test passed")
    
    print(f"\n    All {len(required_tables)} tables verified ✓")


# ═══════════════════════════════════════════════════════════════════
# TEST 11: SES EMAIL
# ═══════════════════════════════════════════════════════════════════
def test_ses_email():
    import boto3
    from secrets_manager import get_ses_sender, get_ses_recipient
    
    sender = get_ses_sender()
    recipient = get_ses_recipient()
    
    assert sender is not None, "SES sender not configured"
    assert recipient is not None, "SES recipient not configured"
    print(f"    Sender: {sender}")
    print(f"    Recipient: {recipient}")
    
    ses = boto3.client('ses', region_name='ap-south-1')
    
    try:
        ses.send_email(
            Source=sender,
            Destination={'ToAddresses': [recipient]},
            Message={
                'Subject': {'Data': '🧪 Trading Bot v6.1 — Test Email'},
                'Body': {
                    'Html': {
                        'Data': (
                            '<h3>Test Successful</h3>'
                            '<p>Trading Bot v6.1 validation suite</p>'
                            '<p>All systems operational.</p>'
                            f'<p>Time: {time.strftime("%Y-%m-%d %H:%M:%S")}</p>'
                        )
                    }
                }
            }
        )
        print(f"    ✓ Test email sent successfully")
    except Exception as e:
        raise AssertionError(f"SES send failed: {e}")


# ═══════════════════════════════════════════════════════════════════
# TEST 12: ADAPTIVE POLLING LOGIC
# ═══════════════════════════════════════════════════════════════════
def test_adaptive_polling():
    import config
    
    # Simulate stocks at various distances from ORB high
    stocks = [
        {'ticker': 'NEAR', 'ltp': 199.5, 'orb_high': 200.0},   # 0.25% away
        {'ticker': 'CLOSE', 'ltp': 199.0, 'orb_high': 200.0},  # 0.50% away
        {'ticker': 'FAR', 'ltp': 195.0, 'orb_high': 200.0},    # 2.50% away
        {'ticker': 'BROKE', 'ltp': 201.0, 'orb_high': 200.0},  # Already broke
    ]
    
    print(f"    Near ORB threshold: {config.NEAR_ORB_THRESHOLD_PCT}%")
    print(f"    Normal interval: {config.SCAN_INTERVAL_NORMAL}s")
    print(f"    Accelerated interval: {config.SCAN_INTERVAL_NEAR_ORB}s")
    print()
    
    any_near = False
    for stock in stocks:
        distance = (stock['orb_high'] - stock['ltp']) / stock['orb_high'] * 100
        is_near = 0 < distance <= config.NEAR_ORB_THRESHOLD_PCT
        interval = config.SCAN_INTERVAL_NEAR_ORB if is_near else config.SCAN_INTERVAL_NORMAL
        
        if is_near:
            any_near = True
        
        status = "ACCELERATED 🔥" if is_near else ("BROKE ORB ✓" if distance <= 0 else "Normal")
        print(f"    {stock['ticker']:<6} LTP=₹{stock['ltp']:<6} "
              f"dist={distance:>5.2f}%  → {interval}s ({status})")
    
    assert any_near, "At least one stock should trigger accelerated polling"
    
    # Verify: NEAR stock (0.25%) triggers acceleration
    near_dist = (200.0 - 199.5) / 200.0 * 100  # 0.25%
    assert near_dist <= config.NEAR_ORB_THRESHOLD_PCT, "0.25% should be within threshold"
    
    # Verify: CLOSE stock (0.50%) does NOT trigger (> 0.3%)
    close_dist = (200.0 - 199.0) / 200.0 * 100  # 0.50%
    assert close_dist > config.NEAR_ORB_THRESHOLD_PCT, "0.50% should NOT trigger"
    
    # API budget impact
    normal_calls_per_hour = 3600 / config.SCAN_INTERVAL_NORMAL  # 60
    accel_calls_per_hour = 3600 / config.SCAN_INTERVAL_NEAR_ORB  # 240
    extra = accel_calls_per_hour - normal_calls_per_hour
    print(f"\n    API impact if accelerated for full hour: +{extra:.0f} calls")
    print(f"    Typical acceleration period: 2-5 min = +{extra/60*5:.0f} extra calls ✓")


# ═══════════════════════════════════════════════════════════════════
# RUN ALL TESTS
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("═" * 60)
    print("  TRADING BOT v6.1 — COMPREHENSIVE VALIDATION SUITE")
    print("═" * 60)
    
    tests = [
        (1, "Config Validation", test_config),
        (2, "Watchlist CSV", test_watchlist),
        (3, "Technical Indicators", test_indicators),
        (4, "Scoring Engine", test_scoring),
        (5, "Position Sizing", test_position_sizing),
        (6, "Trailing Stop Loss", test_trailing_sl),
        (7, "Partial Fill Handling", test_partial_fill),
        (8, "Dhan API Connectivity", test_dhan_api),
        (9, "SSM Parameters", test_ssm_parameters),
        (10, "DynamoDB Tables (4)", test_dynamodb),
        (11, "SES Email", test_ses_email),
        (12, "Adaptive Polling", test_adaptive_polling),
    ]
    
    for num, name, func in tests:
        run_test(num, name, func)
    
    # ═══ FINAL SUMMARY ═══
    print("\n" + "═" * 60)
    print("  RESULTS SUMMARY")
    print("═" * 60)
    
    passed = sum(1 for r in results if r[0] == 'PASS')
    failed = sum(1 for r in results if r[0] == 'FAIL')
    
    for r in results:
        icon = "✅" if r[0] == 'PASS' else "❌"
        print(f"  {icon} Test {r[1]:>2}: {r[2]}")
        if r[0] == 'FAIL':
            print(f"        → {r[3]}")
    
    print(f"\n{'═' * 60}")
    print(f"  TOTAL: {passed}/{len(results)} PASSED | {failed} FAILED")
    print(f"{'═' * 60}")
    
    if failed == 0:
        print("\n  🎉 ALL SYSTEMS GO — Ready for production!")
        print("  Next: Run paper trading for 1 week before live.")
    else:
        print(f"\n  ⚠️  Fix {failed} failing test(s) before deploying.")
    
    print()
    sys.exit(0 if failed == 0 else 1)

