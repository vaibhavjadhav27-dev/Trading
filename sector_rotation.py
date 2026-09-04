import os, json, logging
from datetime import date
log = logging.getLogger(__name__)

# NSE Sector Index Security IDs (Dhan IDX_I segment)
# WARNING: VERIFY every id against Dhan instrument master.
# AUTO was wrongly sharing "25" with BANK - replace AUTO with the real id.
SECTOR_INDICES = {
    "NIFTY_IT":          "29",
    "NIFTY_PHARMA":      "32",
    "NIFTY_FMCG":        "28",
    "NIFTY_AUTO":        "14",
    "NIFTY_BANK":        "25",
    "NIFTY_METAL":       "31",
    "NIFTY_ENERGY":      "42",
    "NIFTY_REALTY":      "34",
    "NIFTY_PSU_BANK":    "33",
    "NIFTY_MEDIA":       "30",
    "NIFTY_INFRA":       "43",
    "NIFTY_HEALTHCARE":  "447",
    "NIFTY_FIN_SERVICE": "27",
}

SECTOR_MAP = {}

def load_sector_map(watchlist_path="watchlist.csv"):
    """Load sector column from watchlist CSV if available."""
    global SECTOR_MAP
    try:
        import pandas as pd
        df = pd.read_csv(watchlist_path)
        if "sector" in df.columns:
            SECTOR_MAP = {str(t): str(s) for t, s in zip(df["ticker"], df["sector"])
                          if isinstance(s, str) and s.strip()}
            log.info(f"Loaded sector map: {len(SECTOR_MAP)} stocks")
        else:
            log.warning("No sector column in watchlist - sector rotation disabled")
    except Exception as e:
        log.warning(f"Could not load sector map: {e}")

def get_sector_prev_closes():
    """Load sector index prev closes from cache."""
    try:
        cache_path = "sector_prev_close.json"
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Sector cache load failed: {e}")
    return {}

def find_leading_sectors(sector_ltps, sector_prev_closes, nifty_return):
    """Genuinely bullish sectors on a red day. RS ranks; absolute positivity gates."""
    leading = []
    for sector, ltp in sector_ltps.items():
        prev = sector_prev_closes.get(sector, 0)
        if prev <= 0 or ltp <= 0:
            continue
        sector_gap = (ltp - prev) / prev * 100
        sector_rs = sector_gap - nifty_return
        if sector_gap > 0.3 and sector_rs > 0.5:
            leading.append((sector, sector_gap, sector_rs))
    leading.sort(key=lambda x: -x[2])
    return leading[:2]

def is_sector_eligible(ticker, leading_sectors, stock_gap, sector_ltps, sector_prev_closes):
    """Eligible only if sector is leading AND stock is up AND it leads its own sector."""
    if not SECTOR_MAP:
        log.warning("SECTOR_MAP empty -> FAIL-OPEN: bypassing sector sieve, candidate passes to normal bearish eligibility")
        return True, "no_sector_map_failopen"
    stock_sector = SECTOR_MAP.get(ticker, "")
    if not stock_sector:
        return False, "no_sector_assigned"
    leading_names = [s[0] for s in leading_sectors]
    if stock_sector not in leading_names:
        return False, f"sector_{stock_sector}_not_leading"
    if stock_gap <= 0.3:
        return False, "stock_not_positive"
    sector_prev = sector_prev_closes.get(stock_sector, 0)
    sector_ltp  = sector_ltps.get(stock_sector, 0)
    if sector_prev > 0 and sector_ltp > 0:
        sector_return = (sector_ltp - sector_prev) / sector_prev * 100
        if stock_gap <= sector_return:
            return False, "stock_not_sector_leader"
    return True, "sector_rotation_eligible"

def get_bearish_day_sizing_multiplier():
    """On bearish rotation days, use half position size."""
    return 0.5
