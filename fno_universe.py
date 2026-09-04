"""F&O membership (shortable universe) from the Dhan scrip master.
Server-safe: reads the static Dhan master CSV, NOT the blocked NSE API.
Shortable = NSE single-stock F&O underliers (EXCH_ID=NSE, INSTRUMENT=FUTSTK).
Cache is date-stamped with the same TTL pattern as the ban cache."""
import json, os, logging
from datetime import date
import pandas as pd

log = logging.getLogger("fno_universe")
MASTER_CSV = "dhan_scrip_master.csv"
CACHE_FILE = "fno_members.json"
_cache = None

def build_fno_members(master_csv=MASTER_CSV, cache_file=CACHE_FILE):
    df = pd.read_csv(master_csv, low_memory=False)
    m = df[(df["EXCH_ID"] == "NSE") & (df["INSTRUMENT"] == "FUTSTK")]
    members = sorted({str(s).strip().upper() for s in m["UNDERLYING_SYMBOL"].dropna()
                      if str(s).strip() and "NSETEST" not in str(s).upper()})
    payload = {"date": str(date.today()), "count": len(members), "members": members}
    _tmp = cache_file + ".tmp"
    with open(_tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(_tmp, cache_file)   # atomic — no truncation risk
    log.info("F&O members rebuilt: %d NSE single-stock underliers" % len(members))
    return set(members)

CACHE_TTL_DAYS = 7

def _load():
    global _cache
    if _cache is not None:
        return _cache
    cached = None
    try:
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        cdate = date.fromisoformat(cached.get("date", "1970-01-01"))
        if cached.get("members") and (date.today() - cdate).days < CACHE_TTL_DAYS:
            _cache = set(cached["members"]); return _cache
    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError):
        pass
    try:
        _cache = build_fno_members()          # stale/missing -> rebuild from master
    except FileNotFoundError:
        if cached and cached.get("members"):   # master gone -> use stale cache, don't crash
            log.warning("scrip master absent; using stale cache (%s, %d names). Re-download to refresh."
                        % (cached.get("date"), len(cached["members"])))
            _cache = set(cached["members"])
        else:
            raise
    return _cache

def is_shortable(ticker):
    """True if ticker is an NSE single-stock F&O underlier (shortable intraday)."""
    if not ticker:
        return False
    return str(ticker).strip().upper() in _load()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    members = build_fno_members()
    print("F&O shortable universe: %d NSE single-stock names" % len(members))
    print("sample:", sorted(members)[:12])
    # verify against watchlist
    wl = pd.read_csv("watchlist.csv")
    tcol = "ticker" if "ticker" in wl.columns else wl.columns[0]
    wl_tickers = [str(t).strip().upper() for t in wl[tcol].dropna()]
    shortable = [t for t in wl_tickers if t in members]
    not_short = [t for t in wl_tickers if t not in members]
    print("WATCHLIST: %d of %d shortable" % (len(shortable), len(wl_tickers)))
    print("  NOT shortable (sample 15):", not_short[:15])
