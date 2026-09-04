"""
═══════════════════════════════════════════════════════════════════════════
test_all.py — FULL SYSTEM VALIDATION
═══════════════════════════════════════════════════════════════════════════
Run this RIGHT NOW to verify every component works before tomorrow.
Works outside market hours. Does NOT place real orders.

Usage:
    python test_all.py

Tests:
    1. Python imports + dependencies
    2. watchlist.csv file + format
    3. AWS Parameter Store → Dhan token fetch
    4. Dhan API → Token validity (balance check)
    5. DynamoDB → Read/Write test
    6. AWS SES → Send test email
    7. Dhan OHLC data → Historical fetch
    8. Technical indicators → pandas-ta computation
    9. Signal evaluation → Mock signal check
    10. Full dry-run (no real orders)
═══════════════════════════════════════════════════════════════════════════
"""

import sys
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️  WARN"

results = []


def test(name, func):
    """Run a test and track result."""
    print(f"\n{'─' * 50}")
    print(f"TEST: {name}")
    print(f"{'─' * 50}")
    try:
        success, message = func()
        status = PASS if success else FAIL
        results.append((name, status, message))
        print(f"  {status} {message}")
        return success
    except Exception as e:
        results.append((name, FAIL, str(e)))
        print(f"  {FAIL} {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Python Imports
# ═══════════════════════════════════════════════════════════════════════════
def test_imports():
    try:
        import pandas as pd
        import indicators
        import numpy as np
        import requests
        import boto3
        from bs4 import BeautifulSoup
        from decimal import Decimal
        from boto3.dynamodb.conditions import Key
        import config
        import secrets_manager
        return True, f"All imports OK (pandas {pd.__version__}, boto3 {boto3.__version__})"
    except ImportError as e:
        return False, f"Missing: {e}. Run: pip install -r requirements.txt"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: Watchlist CSV
# ═══════════════════════════════════════════════════════════════════════════
def test_watchlist():
    import pandas as pd
    import config

    filepath = config.WATCHLIST_FILE
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}. Upload it to {os.getcwd()}/"

    df = pd.read_csv(filepath, dtype=str)
    df.columns = df.columns.str.strip().str.lower()

    if "ticker" not in df.columns or "security_id" not in df.columns:
        return False, f"Missing columns. Found: {df.columns.tolist()}. Need: ticker, security_id"

    df = df.dropna(subset=["ticker", "security_id"])
    count = len(df)

    if count == 0:
        return False, "CSV has 0 valid rows"

    # Show first 5
    print(f"  First 5 stocks:")
    for _, row in df.head(5).iterrows():
        print(f"    {row['ticker']} → {row['security_id']}")

    return True, f"{count} stocks loaded from {filepath}"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: Parameter Store (Dhan Token)
# ═══════════════════════════════════════════════════════════════════════════
def test_parameter_store():
    from secrets_manager import get_dhan_credentials

    creds = get_dhan_credentials()

    if not creds.get("client_id"):
        return False, (
            "client_id not found.\n"
            "  Fix: aws ssm put-parameter --name /tradingbot/dhan/client_id "
            "--value YOUR_ID --type SecureString --region ap-south-1"
        )

    if not creds.get("access_token"):
        return False, (
            "access_token not found.\n"
            "  Fix: aws ssm put-parameter --name /tradingbot/dhan/access_token "
            "--value YOUR_TOKEN --type SecureString --region ap-south-1"
        )

    token_preview = creds["access_token"][:10] + "..." + creds["access_token"][-5:]
    return True, f"client_id: {creds['client_id'][:8]}... | token: {token_preview}"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: Dhan API (Balance Check = Token Validity)
# ═══════════════════════════════════════════════════════════════════════════
def test_dhan_api():
    from secrets_manager import get_dhan_credentials
    import requests

    creds = get_dhan_credentials()
    if not creds.get("access_token"):
        return False, "Cannot test — token not in Parameter Store"

    headers = {
        "access-token": creds["access_token"],
        "client-id": creds["client_id"],
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                           headers=headers, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            balance = float(data.get("availabelBalance", 0))
            return True, f"Token VALID ✅ | Balance: ₹{balance:,.2f}"
        elif resp.status_code == 401:
            return False, (
                "Token EXPIRED ❌ (401 Unauthorized)\n"
                "  Fix: aws ssm put-parameter --name /tradingbot/dhan/access_token "
                "--value NEW_TOKEN --type SecureString --region ap-south-1 --overwrite"
            )
        else:
            return False, f"API returned {resp.status_code}: {resp.text[:100]}"

    except requests.exceptions.Timeout:
        return False, "Timeout connecting to Dhan API"
    except Exception as e:
        return False, f"Connection error: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5: DynamoDB (Read + Write)
# ═══════════════════════════════════════════════════════════════════════════
def test_dynamodb():
    import boto3
    from decimal import Decimal
    import config

    dynamodb = boto3.resource("dynamodb", region_name=config.AWS_REGION_NAME)

    # Test all 3 tables
    tables = [
        config.DYNAMO_TRADE_HISTORY_TABLE,
        config.DYNAMO_ACTIVE_TRADE_TABLE,
        config.DYNAMO_PRICE_LOG_TABLE,
    ]

    for table_name in tables:
        try:
            table = dynamodb.Table(table_name)
            table.load()
            if table.table_status != "ACTIVE":
                return False, f"{table_name} status: {table.table_status} (expected ACTIVE)"
        except Exception as e:
            return False, (
                f"Table {table_name} not found.\n"
                f"  Fix: python dynamo_setup.py"
            )

    # Write test to ActiveTrade
    table = dynamodb.Table(config.DYNAMO_ACTIVE_TRADE_TABLE)
    table.put_item(Item={
        "position_id": "TEST",
        "status": "TEST",
        "test_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    })

    # Read it back
    resp = table.get_item(Key={"position_id": "TEST"})
    item = resp.get("Item")
    if not item:
        return False, "Write succeeded but read failed"

    # Cleanup
    table.delete_item(Key={"position_id": "TEST"})

    return True, f"All 3 tables ACTIVE | Read/Write OK"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 6: AWS SES (Send Test Email)
# ═══════════════════════════════════════════════════════════════════════════
def test_ses():
    import boto3
    import config

    ses = boto3.client("ses", region_name=config.AWS_REGION_NAME)

    try:
        ses.send_email(
            Source=config.SES_EMAIL,
            Destination={"ToAddresses": [config.SES_EMAIL]},
            Message={
                "Subject": {"Data": "🧪 Trading Bot Test Email", "Charset": "UTF-8"},
                "Body": {"Text": {"Data": (
                    f"This is a test email from your trading bot.\n\n"
                    f"If you receive this, SES is working correctly.\n\n"
                    f"Test time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
                    f"All systems operational. Bot will run tomorrow."
                ), "Charset": "UTF-8"}},
            },
        )
        return True, f"Email sent to {config.SES_EMAIL} — check inbox!"
    except Exception as e:
        error = str(e)
        if "not verified" in error.lower():
            return False, (
                f"Email not verified in SES.\n"
                f"  Fix: aws ses verify-email-identity "
                f"--email-address {config.SES_EMAIL} --region {config.AWS_REGION_NAME}"
            )
        return False, f"SES error: {error}"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7: Dhan Historical OHLC Data
# ═══════════════════════════════════════════════════════════════════════════
def test_ohlc_data():
    import requests
    import pandas as pd
    from datetime import timedelta
    from secrets_manager import get_dhan_credentials
    import config

    creds = get_dhan_credentials()
    if not creds.get("access_token"):
        return False, "Skipped — no token"

    # Get first stock from watchlist
    df_wl = pd.read_csv(config.WATCHLIST_FILE, dtype=str)
    df_wl.columns = df_wl.columns.str.strip().str.lower()
    first_ticker = df_wl.iloc[0]["ticker"]
    first_id = df_wl.iloc[0]["security_id"]

    headers = {
        "access-token": creds["access_token"],
        "client-id": creds["client_id"],
        "Content-Type": "application/json",
    }

    # Fetch last 5 days historical
    to_date = datetime.now(IST).strftime("%Y-%m-%d")
    from_date = (datetime.now(IST) - timedelta(days=5)).strftime("%Y-%m-%d")

    payload = {
        "securityId": first_id,
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "interval": "5",
        "fromDate": from_date,
        "toDate": to_date,
    }

    resp = requests.post("https://api.dhan.co/v2/charts/historical",
                        headers=headers, json=payload, timeout=10)

    if resp.status_code != 200:
        return False, f"OHLC API returned {resp.status_code}: {resp.text[:100]}"

    data = resp.json()
    if "open" not in data:
        return False, f"No OHLC data returned for {first_ticker} ({first_id})"

    candles = len(data["open"])
    last_close = data["close"][-1] if data["close"] else 0

    return True, f"{first_ticker} (ID:{first_id}) | {candles} candles | Last close: ₹{last_close}"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 8: Technical Indicators (pandas-ta)
# ═══════════════════════════════════════════════════════════════════════════
def test_indicators():
    import pandas as pd
    import indicators
    import numpy as np

    # Create fake OHLCV data (50 candles)
    np.random.seed(42)
    base = 1000
    closes = base + np.cumsum(np.random.randn(50) * 2)
    df = pd.DataFrame({
        "Open": closes - np.random.rand(50),
        "High": closes + np.random.rand(50) * 3,
        "Low": closes - np.random.rand(50) * 3,
        "Close": closes,
        "Volume": np.random.randint(100000, 500000, 50),
    })

    # Compute indicators
    df = indicators.compute_all(df)

    cols = df.columns.tolist()
    has_vwap = any("VWAP" in c for c in cols)
    has_rsi = any("RSI" in c for c in cols)
    has_st = any("SUPERTd" in c for c in cols)
    has_ema9 = any("EMA_9" in c for c in cols)
    has_ema21 = any("EMA_21" in c for c in cols)
    has_atr = any("ATR" in c.upper() for c in cols)

    all_ok = all([has_vwap, has_rsi, has_st, has_ema9, has_ema21, has_atr])

    if not all_ok:
        missing = []
        if not has_vwap: missing.append("VWAP")
        if not has_rsi: missing.append("RSI")
        if not has_st: missing.append("Supertrend")
        if not has_ema9: missing.append("EMA9")
        if not has_ema21: missing.append("EMA21")
        if not has_atr: missing.append("ATR")
        return False, f"Missing indicators: {missing}"

    rsi_val = df[[c for c in cols if "RSI" in c][0]].iloc[-1]
    return True, f"All indicators computed | RSI={rsi_val:.1f} | pandas-ta working"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 9: Signal Evaluation Logic
# ═══════════════════════════════════════════════════════════════════════════
def test_signal_logic():
    import config

    # Simulate a valid signal
    price = 2500.0
    vwap = 2480.0
    rsi = 62.0
    st_dir = 1
    ema9 = 2495.0
    ema21 = 2485.0
    atr = 30.0
    vol = 500000
    avg_vol = 350000

    c1 = price > vwap
    c2 = config.RSI_MIN <= rsi <= config.RSI_MAX
    c3 = st_dir == 1
    c4 = price > ema9 > ema21
    c5 = vol > (avg_vol * config.VOLUME_THRESHOLD)

    all_met = all([c1, c2, c3, c4, c5])

    sl = round(price - (config.ATR_SL_MULTIPLIER * atr), 1)
    risk = price - sl
    target = round(price + (config.MIN_RISK_REWARD * risk), 1)

    print(f"  Mock signal:")
    print(f"    Price=₹{price} VWAP=₹{vwap} RSI={rsi} ST=BULL EMA9>EMA21 Vol=1.43x")
    print(f"    SL=₹{sl} Target=₹{target} R:R=1:{config.MIN_RISK_REWARD}")
    print(f"    Conditions: {[c1,c2,c3,c4,c5]}")

    if all_met:
        return True, f"Signal VALID | Entry ₹{price} → SL ₹{sl} → Target ₹{target}"
    else:
        return False, "Mock signal failed (unexpected)"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 10: Full Dry Run (No Real Orders)
# ═══════════════════════════════════════════════════════════════════════════
def test_dry_run():
    """Verify TradingEngine initializes without errors."""
    import config
    from secrets_manager import get_dhan_credentials

    # Check all components can instantiate
    components = []

    # Watchlist
    from trading_bot import WatchlistLoader
    wl = WatchlistLoader.load()
    if wl:
        components.append(f"Watchlist: {len(wl)} stocks")
    else:
        return False, "WatchlistLoader failed"

    # DynamoManager
    from trading_bot import DynamoManager
    db = DynamoManager()
    components.append("DynamoDB: connected")

    # Notifier
    from trading_bot import Notifier
    notifier = Notifier()
    components.append("Notifier: ready")

    # DhanClient
    from trading_bot import DhanClient
    try:
        dhan = DhanClient()
        components.append("DhanClient: token loaded")
    except RuntimeError as e:
        return False, f"DhanClient failed: {e}"

    # RiskManager
    from trading_bot import RiskManager
    risk = RiskManager(100000)
    qty = risk.size_position(2500, 2455)
    components.append(f"RiskManager: {qty} shares @ ₹2500 (SL ₹2455)")

    # MarketGuard
    from trading_bot import MarketGuard
    is_trading = MarketGuard.is_trading_day()
    components.append(f"MarketGuard: trading_day={is_trading}")

    return True, " | ".join(components)


# ═══════════════════════════════════════════════════════════════════════════
# RUN ALL TESTS
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("═" * 60)
    print("  TRADING BOT — FULL SYSTEM VALIDATION")
    print(f"  Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"  Working Dir: {os.getcwd()}")
    print("═" * 60)

    test("1. Python Imports & Dependencies", test_imports)
    test("2. Watchlist CSV File", test_watchlist)
    test("3. Parameter Store (Dhan Token)", test_parameter_store)
    test("4. Dhan API (Token Validity)", test_dhan_api)
    test("5. DynamoDB (Tables + Read/Write)", test_dynamodb)
    test("6. AWS SES (Test Email)", test_ses)
    test("7. Dhan OHLC Historical Data", test_ohlc_data)
    test("8. Technical Indicators (pandas-ta)", test_indicators)
    test("9. Signal Evaluation Logic", test_signal_logic)
    test("10. Full Dry Run (All Components)", test_dry_run)

    # ─── Final Report ───
    print("\n" + "═" * 60)
    print("  RESULTS SUMMARY")
    print("═" * 60)

    passed = 0
    failed = 0
    for name, status, msg in results:
        icon = "✅" if "PASS" in status else "❌"
        print(f"  {icon} {name}")
        if "PASS" in status:
            passed += 1
        else:
            failed += 1
            print(f"     → {msg}")

    print(f"\n  {'═' * 40}")
    print(f"  PASSED: {passed}/10 | FAILED: {failed}/10")
    print(f"  {'═' * 40}")

    if failed == 0:
        print("\n  🎉 ALL SYSTEMS GO! Bot is ready for tomorrow.")
        print(f"  Tomorrow ({(datetime.now(IST) + __import__('datetime').timedelta(days=1)).strftime('%A %Y-%m-%d')}):")
        print("  Bot will auto-start via cron at 08:50 IST")
        print("  Or run manually: python trading_bot.py")
    else:
        print(f"\n  ⚠️  Fix the {failed} failed test(s) above before tomorrow.")
        print("  Re-run: python test_all.py")

    print("═" * 60)

