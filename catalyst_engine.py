#!/usr/bin/env python3
"""
Catalyst Engine v1.0 - News, Earnings, RBI, Sentiment Integration
Components: A) yfinance earnings B) Google News RSS C) Groq AI sentiment D) RBI calendar E) Combined scoring
"""
import json, os, logging, time, requests
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional

log = logging.getLogger("catalyst")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IST_OFFSET = timedelta(hours=5, minutes=30)
CACHE_FILE = "catalyst_cache.json"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

def ist_now():
    return datetime.utcnow() + IST_OFFSET


# ═══════════════════════════════════════════════════════════════
# COMPONENT A: yfinance Earnings Calendar
# ═══════════════════════════════════════════════════════════════

def get_earnings_data(tickers: List[str]) -> Dict:
    """Fetch earnings dates and analyst info from yfinance"""
    results = {}
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed - skipping earnings")
        return results
    for ticker in tickers[:20]:
        try:
            nse_ticker = f"{ticker}.NS"
            stock = yf.Ticker(nse_ticker)
            info = stock.info or {}
            cal = stock.calendar or {}
            earnings_date = None
            if isinstance(cal, dict) and "Earnings Date" in cal:
                ed = cal["Earnings Date"]
                if isinstance(ed, list) and len(ed) > 0:
                    earnings_date = str(ed)
                elif ed:
                    earnings_date = str(ed)
            days_to_earnings = None
            if earnings_date:
                try:
                    ed_date = datetime.strptime(earnings_date[:10], "%Y-%m-%d").date()
                    days_to_earnings = (ed_date - date.today()).days
                except Exception:
                    pass
            results[ticker] = {
                "earnings_date": earnings_date,
                "days_to_earnings": days_to_earnings,
                "recommendation": info.get("recommendationKey", "none"),
                "target_price": info.get("targetMeanPrice", 0),
                "current_price": info.get("currentPrice", 0),
                "analyst_count": info.get("numberOfAnalystOpinions", 0),
                "earnings_bonus": 10 if days_to_earnings and 0 < days_to_earnings <= 7 else 0,
                "analyst_bonus": 5 if info.get("recommendationKey") in ["buy", "strong_buy"] else 0
            }
            time.sleep(0.5)
        except Exception as e:
            results[ticker] = {"error": str(e), "earnings_bonus": 0, "analyst_bonus": 0}
    return results


# ═══════════════════════════════════════════════════════════════
# COMPONENT B: Google News RSS Headlines
# ═══════════════════════════════════════════════════════════════

def get_news_headlines(tickers: List[str], max_per_ticker: int = 3) -> Dict:
    """Fetch recent news headlines from Google News RSS"""
    results = {}
    for ticker in tickers[:20]:
        try:
            query = f"{ticker} NSE stock"
            url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            headlines = []
            if resp.status_code == 200:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(resp.text)
                items = root.findall(".//item")
                for item in items[:max_per_ticker]:
                    title = item.find("title")
                    pub_date = item.find("pubDate")
                    if title is not None:
                        headlines.append({
                            "title": title.text,
                            "date": pub_date.text if pub_date is not None else ""
                        })
            results[ticker] = {"headlines": headlines, "count": len(headlines)}
            time.sleep(0.3)
        except Exception as e:
            results[ticker] = {"headlines": [], "count": 0, "error": str(e)}
    return results


# ═══════════════════════════════════════════════════════════════
# COMPONENT C: Groq AI Sentiment Analysis
# ═══════════════════════════════════════════════════════════════

def analyze_sentiment_groq(ticker: str, headlines: List[str]) -> Dict:
    """Use Groq (LLaMA 3) to analyze sentiment of news headlines"""
    if not headlines:
        return {"score": 0, "reasoning": "No headlines available"}
    api_key = GROQ_API_KEY
    if not api_key:
        try:
            from secrets_manager import get_parameter
            api_key = get_parameter("/trading-engine/ai/groq-api-key")
        except Exception:
            return {"score": 0, "reasoning": "No Groq API key configured"}
    if not api_key:
        return {"score": 0, "reasoning": "No Groq API key"}
    headlines_text = chr(10).join([f"- {h}" for h in headlines[:5]])
    prompt = f"""Analyze these news headlines for {ticker} (NSE India stock) and give a sentiment score.

Headlines:
{headlines_text}

Respond ONLY with a JSON object:
{{"score": <integer from -10 to +10>, "reasoning": "<one sentence>"}}

Score guide: -10=extremely bearish, -5=bearish, 0=neutral, +5=bullish, +10=extremely bullish"""

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 100
            },
            timeout=15
        )
        if resp.status_code == 200:
            content = resp.json()["choices"]["message"]["content"]
            try:
                result = json.loads(content)
                return {"score": max(-10, min(10, int(result.get("score", 0)))), "reasoning": result.get("reasoning", "")}
            except json.JSONDecodeError:
                if "bullish" in content.lower():
                    return {"score": 5, "reasoning": content[:100]}
                elif "bearish" in content.lower():
                    return {"score": -5, "reasoning": content[:100]}
                return {"score": 0, "reasoning": content[:100]}
        else:
            return {"score": 0, "reasoning": f"API error: {resp.status_code}"}
    except Exception as e:
        return {"score": 0, "reasoning": f"Error: {str(e)[:50]}"}


def batch_sentiment(tickers_headlines: Dict) -> Dict:
    """Analyze sentiment for multiple tickers"""
    results = {}
    for ticker, data in list(tickers_headlines.items())[:10]:
        headlines = [h["title"] for h in data.get("headlines", []) if h.get("title")]
        results[ticker] = analyze_sentiment_groq(ticker, headlines)
        time.sleep(1)
    return results


# ═══════════════════════════════════════════════════════════════
# COMPONENT D: RBI Policy Calendar
# ═══════════════════════════════════════════════════════════════

RBI_POLICY_DATES_2026 = [
    {"date": "2026-02-07", "event": "Monetary Policy", "impact": "rate_sensitive"},
    {"date": "2026-04-09", "event": "Monetary Policy", "impact": "rate_sensitive"},
    {"date": "2026-06-06", "event": "Monetary Policy", "impact": "rate_sensitive"},
    {"date": "2026-08-08", "event": "Monetary Policy", "impact": "rate_sensitive"},
    {"date": "2026-10-08", "event": "Monetary Policy", "impact": "rate_sensitive"},
    {"date": "2026-12-05", "event": "Monetary Policy", "impact": "rate_sensitive"},
]

RATE_SENSITIVE_SECTORS = ["BANK", "NBFC", "HOUSING", "AUTO", "REALTY", "INFRA"]

def get_rbi_context() -> Dict:
    """Check if RBI policy is upcoming and which sectors benefit"""
    today = date.today()
    upcoming = None
    days_to_policy = None
    for policy in RBI_POLICY_DATES_2026:
        policy_date = datetime.strptime(policy["date"], "%Y-%m-%d").date()
        diff = (policy_date - today).days
        if 0 <= diff <= 14:
            upcoming = policy
            days_to_policy = diff
            break
    return {
        "upcoming_policy": upcoming,
        "days_to_policy": days_to_policy,
        "rate_sensitive_sectors": RATE_SENSITIVE_SECTORS,
        "is_policy_week": days_to_policy is not None and days_to_policy <= 7,
        "rbi_bonus": 5 if days_to_policy and days_to_policy <= 7 else 0
    }


def get_rbi_sector_bonus(ticker: str, sector: str, rbi_context: Dict) -> int:
    """Calculate RBI bonus for a specific stock based on its sector"""
    if not rbi_context.get("is_policy_week"):
        return 0
    sector_upper = (sector or "").upper()
    for rs in RATE_SENSITIVE_SECTORS:
        if rs in sector_upper:
            return 5
    return 0


# ═══════════════════════════════════════════════════════════════
# COMPONENT E: Combined Catalyst Scoring
# ═══════════════════════════════════════════════════════════════

def calculate_catalyst_score(ticker: str, earnings: Dict, sentiment: Dict, rbi_bonus: int) -> Dict:
    """Combine all catalyst signals into a single score"""
    e = earnings.get(ticker, {})
    s = sentiment.get(ticker, {})
    earnings_bonus = e.get("earnings_bonus", 0)
    analyst_bonus = e.get("analyst_bonus", 0)
    sentiment_score = s.get("score", 0)
    total = earnings_bonus + analyst_bonus + sentiment_score + rbi_bonus
    return {
        "ticker": ticker,
        "catalyst_score": total,
        "earnings_bonus": earnings_bonus,
        "analyst_bonus": analyst_bonus,
        "sentiment_score": sentiment_score,
        "rbi_bonus": rbi_bonus,
        "sentiment_reasoning": s.get("reasoning", ""),
        "earnings_date": e.get("earnings_date"),
        "days_to_earnings": e.get("days_to_earnings"),
        "recommendation": e.get("recommendation", "none"),
        "signal": "STRONG_BUY" if total >= 15 else "BUY" if total >= 5 else "NEUTRAL" if total >= 0 else "AVOID"
    }


def run_catalyst_analysis(tickers: List[str], sectors: Dict = None) -> List[Dict]:
    """
    Full catalyst pipeline:
    1. Fetch earnings (yfinance)
    2. Fetch news (Google RSS)
    3. Analyze sentiment (Groq)
    4. Check RBI calendar
    5. Calculate combined score
    """
    log.info(f"Running catalyst analysis for {len(tickers)} stocks...")
    sectors = sectors or {}

    # Step 1: Earnings
    log.info("  [A] Fetching earnings data...")
    earnings = get_earnings_data(tickers)
    log.info(f"  [A] Earnings data for {len(earnings)} stocks")

    # Step 2: News headlines
    log.info("  [B] Fetching news headlines...")
    news = get_news_headlines(tickers)
    log.info(f"  [B] News for {len(news)} stocks")

    # Step 3: Sentiment analysis
    log.info("  [C] Running Groq sentiment analysis...")
    sentiment = batch_sentiment(news)
    log.info(f"  [C] Sentiment for {len(sentiment)} stocks")

    # Step 4: RBI context
    log.info("  [D] Checking RBI policy calendar...")
    rbi_context = get_rbi_context()
    log.info(f"  [D] Policy week: {rbi_context['is_policy_week']}")

    # Step 5: Combined scoring
    log.info("  [E] Calculating combined catalyst scores...")
    results = []
    for ticker in tickers:
        rbi_bonus = get_rbi_sector_bonus(ticker, sectors.get(ticker, ""), rbi_context)
        score = calculate_catalyst_score(ticker, earnings, sentiment, rbi_bonus)
        results.append(score)

    results.sort(key=lambda x: -x["catalyst_score"])
    log.info(f"  [E] Scoring complete: {len(results)} stocks scored") if results else log.info("  No results")

    # Cache results
    cache = {
        "date": ist_now().strftime("%Y-%m-%d"),
        "time": ist_now().strftime("%H:%M:%S"),
        "rbi_context": rbi_context,
        "results": results
    }
    # Remove non-serializable
    cache["rbi_context"]["upcoming_policy"] = str(cache["rbi_context"].get("upcoming_policy"))
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, default=str)
    log.info(f"  Cached to {CACHE_FILE}")

    return results


def get_cached_catalysts() -> List[Dict]:
    """Load cached catalyst scores (avoid re-running during market hours)"""
    if not os.path.exists(CACHE_FILE):
        return []
    with open(CACHE_FILE, "r") as f:
        cache = json.load(f)
    if cache.get("date") != ist_now().strftime("%Y-%m-%d"):
        return []
    return cache.get("results", [])


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    # Load tickers from swing scanner results or watchlist
    tickers = []
    if os.path.exists("paper_trades.json"):
        with open("paper_trades.json", "r") as f:
            pt = json.load(f)
        tickers = [w["ticker"] for w in pt.get("watchlist", [])]
        tickers += [a["ticker"] for a in pt.get("active", [])]
    if not tickers and os.path.exists("watchlist.csv"):
        import pandas as pd
        wl = pd.read_csv("watchlist.csv")
        tickers = list(wl.iloc[:, 0].head(20))
    if not tickers:
        tickers = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    # Run top 10 only for speed
    tickers = tickers[:10]
    print(f"Analyzing {len(tickers)} stocks: {tickers}")
    results = run_catalyst_analysis(tickers)
    print(f"{'=' * 60}")
    print(f"  CATALYST ANALYSIS RESULTS")
    print(f"{'=' * 60}")
    for r in results:
        signal_emoji = {"STRONG_BUY": "🟢🟢", "BUY": "🟢", "NEUTRAL": "⚪", "AVOID": "🔴"}.get(r["signal"], "?")
        print(f"  {signal_emoji} {r['ticker']:<12} Score:{r['catalyst_score']:+3d} | Earn:{r['earnings_bonus']} Analyst:{r['analyst_bonus']} Sent:{r['sentiment_score']:+d} RBI:{r['rbi_bonus']}  {r['sentiment_reasoning'][:40]}")
    print(f"{'=' * 60}")
