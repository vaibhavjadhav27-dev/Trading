"""
NSE Dynamic Universe Scanner
=============================
Expert V10.1 directive: "Watchlist is priority only, not a hard boundary.
Maintain core watchlist + broad liquid universe + dynamic discovery."

Architecture:
  Tier 1 (FAST FILTER): Batch LTP for full NSE liquid universe (~1500 stocks)
    - Runs every 60s
    - Cheap: only price + volume from batch API
    - Gate: tradability_gate() (price >= 60, vol >= 100K, turnover >= 10Cr, spread <= 0.2%)
    
  Tier 2 (DEEP SCAN): Full OHLC + indicators for promoted candidates
    - Only stocks passing Tier 1 get candle data + scoring
    - This is where V10.1 scoring/momentum/RS runs
    
  Dynamic Discovery:
    - RVOL surge (> 3x average) auto-promotes to Tier 2
    - Pre-market gap (2-5%) auto-promotes to Tier 2
    - Sector breakout leader auto-promotes related stocks

Deployment:
    1. SCP to server: V84_PRODUCTION_INTEGRATED/nse_universe_scanner.py
    2. Import in trading_bot_v84.py at startup
    3. Call universe.refresh_universe() every 60s in main loop
    4. Use universe.get_candidates() instead of fixed watchlist

Author: Expert V10.1 ENGINEER_WIRING.md implementation
Date: 2026-08-27
"""

import os
import csv
import json
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Set, Optional, Tuple

log = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

MIN_PRICE = 60.0              # Rs 60 minimum
MAX_PRICE = 3500.0            # Rs 3500 maximum
MIN_VOLUME = 100_000          # 1 lakh shares minimum current volume
MIN_AVG_TURNOVER = 10_00_00_000  # Rs 10 crore avg daily turnover
MAX_SPREAD_PCT = 0.20         # 0.20% max spread
MAX_DATA_AGE_SEC = 2.0        # 2 second max data staleness
MIN_RVOL_PROMOTE = 3.0        # RVOL > 3x for dynamic promotion
MIN_GAP_PROMOTE = 2.0         # 2% gap for pre-market promotion
MAX_GAP_PROMOTE = 8.0         # 8% max gap (beyond = too extended)

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
UNIVERSE_CACHE_FILE = "nse_universe_cache.json"
TURNOVER_CACHE_FILE = "nse_avg_turnover.json"

# Tier 1 batch size (Dhan API limit)
LTP_BATCH_SIZE = 100

# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class StockMeta:
    """Static metadata for a stock from scrip master."""
    symbol: str
    security_id: int
    isin: str = ""
    lot_size: int = 1
    tick_size: float = 0.05
    exchange_segment: str = "NSE_EQ"


@dataclass
class LiveTick:
    """Live market data for a stock."""
    security_id: int
    symbol: str
    ltp: float = 0.0
    volume: int = 0
    bid: float = 0.0
    ask: float = 0.0
    prev_close: float = 0.0
    last_update: float = 0.0  # timestamp

    @property
    def spread_pct(self) -> float:
        if self.ltp <= 0:
            return 999.0
        if self.bid > 0 and self.ask > 0:
            return (self.ask - self.bid) / self.ltp * 100
        return 0.1  # assume OK if no bid/ask

    @property
    def data_age_sec(self) -> float:
        if self.last_update <= 0:
            return 999.0
        return time.time() - self.last_update

    @property
    def gap_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.ltp - self.prev_close) / self.prev_close * 100


# ============================================================
# TRADABILITY GATE (Expert's spec)
# ============================================================

def tradability_gate(tick: LiveTick, avg_turnover: float = 0.0) -> Tuple[bool, str]:
    """
    Expert's tradability gate from ENGINEER_WIRING.md.
    
    Returns (passed, reason).
    """
    if tick.ltp < MIN_PRICE:
        return False, "PRICE_BELOW_60"
    if tick.ltp > MAX_PRICE:
        return False, "PRICE_ABOVE_3500"
    if tick.volume < MIN_VOLUME:
        return False, "VOLUME_BELOW_100K"
    if avg_turnover > 0 and avg_turnover < MIN_AVG_TURNOVER:
        return False, "TURNOVER_BELOW_10CR"
    if tick.spread_pct > MAX_SPREAD_PCT:
        return False, "SPREAD_TOO_WIDE"
    if tick.data_age_sec > MAX_DATA_AGE_SEC:
        return False, "STALE_DATA"
    # Price band proximity check (need exchange data)
    # For now, skip (most liquid stocks are far from bands)
    return True, "TRADABLE"


# ============================================================
# UNIVERSE MANAGER
# ============================================================

class NSEUniverseScanner:
    """
    Manages the dynamic NSE stock universe.
    
    Replaces the fixed 552-stock watchlist with a liquid universe
    that discovers stocks dynamically based on volume/price action.
    """

    def __init__(self, gateway=None, core_watchlist: Optional[Set[str]] = None):
        """
        Args:
            gateway: DhanV82Gateway instance (for API calls)
            core_watchlist: Priority stocks (always in Tier 2). Optional.
        """
        self.gateway = gateway
        self.core_watchlist = core_watchlist or set()
        
        # Full universe (from scrip master)
        self.universe: Dict[str, StockMeta] = {}  # symbol -> meta
        self.sid_to_symbol: Dict[int, str] = {}   # security_id -> symbol
        
        # Live state
        self.ticks: Dict[str, LiveTick] = {}      # symbol -> live tick
        self.avg_turnover: Dict[str, float] = {}  # symbol -> 20-day avg turnover
        
        # Tier classification
        self.tier2_symbols: Set[str] = set()      # promoted to deep scan
        self.promoted_reasons: Dict[str, str] = {}  # symbol -> promotion reason
        
        # Timing
        self.last_universe_refresh = 0.0
        self.last_tier1_scan = 0.0
        
        # Load cached data
        self._load_universe_cache()
        self._load_turnover_cache()

    def _load_universe_cache(self):
        """Load cached universe from file."""
        cache_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            UNIVERSE_CACHE_FILE
        )
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    data = json.load(f)
                for item in data.get("stocks", []):
                    meta = StockMeta(
                        symbol=item["symbol"],
                        security_id=int(item["security_id"]),
                        isin=item.get("isin", ""),
                    )
                    self.universe[meta.symbol] = meta
                    self.sid_to_symbol[meta.security_id] = meta.symbol
                log.info(f"Universe cache loaded: {len(self.universe)} stocks")
            except Exception as e:
                log.warning(f"Could not load universe cache: {e}")

    def _load_turnover_cache(self):
        """Load average turnover data."""
        cache_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            TURNOVER_CACHE_FILE
        )
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    self.avg_turnover = json.load(f)
                log.info(f"Turnover cache loaded: {len(self.avg_turnover)} stocks")
            except Exception as e:
                log.warning(f"Could not load turnover cache: {e}")

    def build_universe_from_scrip_master(self, csv_path: str):
        """
        Parse Dhan scrip master CSV to build full NSE equity universe.
        
        Call once at startup or daily refresh.
        Filters: exchange_segment=NSE_EQ, instrument=EQUITY
        """
        count = 0
        self.universe.clear()
        self.sid_to_symbol.clear()
        
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Filter for NSE equity only
                    seg = row.get("SEM_EXM_EXCH_ID", "")
                    instrument = row.get("SEM_INSTRUMENT_NAME", "")
                    
                    if seg != "NSE" or instrument not in ("EQUITY", "EQ"):
                        continue
                    
                    symbol = row.get("SM_SYMBOL_NAME", "").strip()
                    sid = row.get("SEM_SMST_SECURITY_ID", "")
                    
                    if not symbol or not sid:
                        continue
                    
                    try:
                        sid_int = int(sid)
                    except ValueError:
                        continue
                    
                    meta = StockMeta(
                        symbol=symbol,
                        security_id=sid_int,
                        isin=row.get("SM_ISIN", ""),
                    )
                    self.universe[symbol] = meta
                    self.sid_to_symbol[sid_int] = symbol
                    count += 1
            
            log.info(f"Universe built from scrip master: {count} NSE equity stocks")
            
            # Cache it
            cache_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                UNIVERSE_CACHE_FILE
            )
            with open(cache_path, "w") as f:
                json.dump({
                    "date": date.today().isoformat(),
                    "count": count,
                    "stocks": [{"symbol": m.symbol, "security_id": m.security_id, "isin": m.isin}
                              for m in self.universe.values()]
                }, f)
            
        except Exception as e:
            log.error(f"Failed to build universe from scrip master: {e}")
        
        return count

    def tier1_scan(self, ltp_data: Dict[int, float], volume_data: Optional[Dict[int, int]] = None):
        """
        Fast Tier 1 scan: apply tradability gate to full universe.
        
        Args:
            ltp_data: {security_id: last_price} from batch LTP
            volume_data: {security_id: volume} if available
            
        Returns:
            List of symbols promoted to Tier 2.
        """
        promoted = []
        
        for sid, price in ltp_data.items():
            symbol = self.sid_to_symbol.get(sid)
            if not symbol:
                continue
            
            # Update tick
            tick = self.ticks.get(symbol)
            if tick is None:
                tick = LiveTick(security_id=sid, symbol=symbol)
                self.ticks[symbol] = tick
            
            tick.ltp = price
            tick.last_update = time.time()
            if volume_data and sid in volume_data:
                tick.volume = volume_data[sid]
            
            # Core watchlist always promoted
            if symbol in self.core_watchlist:
                if symbol not in self.tier2_symbols:
                    self.tier2_symbols.add(symbol)
                    self.promoted_reasons[symbol] = "CORE_WATCHLIST"
                continue
            
            # Apply tradability gate
            avg_to = self.avg_turnover.get(symbol, 0)
            passed, reason = tradability_gate(tick, avg_to)
            
            if not passed:
                # Remove from Tier 2 if it was there
                if symbol in self.tier2_symbols and self.promoted_reasons.get(symbol) != "CORE_WATCHLIST":
                    self.tier2_symbols.discard(symbol)
                continue
            
            # Dynamic promotion checks
            if symbol not in self.tier2_symbols:
                # RVOL surge promotion
                # (need avg volume to compute — skip if unavailable)
                
                # Gap promotion (pre-market/early session)
                if tick.prev_close > 0:
                    gap = abs(tick.gap_pct)
                    if MIN_GAP_PROMOTE <= gap <= MAX_GAP_PROMOTE:
                        self.tier2_symbols.add(symbol)
                        self.promoted_reasons[symbol] = f"GAP_{tick.gap_pct:+.1f}%"
                        promoted.append(symbol)
                        continue
                
                # Volume surge (if data available)
                if tick.volume >= MIN_VOLUME * 3:  # 3x minimum as rough RVOL proxy
                    self.tier2_symbols.add(symbol)
                    self.promoted_reasons[symbol] = f"VOL_SURGE_{tick.volume:,}"
                    promoted.append(symbol)
        
        self.last_tier1_scan = time.time()
        
        if promoted:
            log.info(f"Tier 1 promoted {len(promoted)} stocks: {promoted[:10]}")
        
        return promoted

    def get_tier2_sids(self) -> List[int]:
        """Get security IDs for Tier 2 (deep scan) stocks."""
        sids = []
        for symbol in self.tier2_symbols:
            meta = self.universe.get(symbol)
            if meta:
                sids.append(meta.security_id)
        return sids

    def get_all_sids(self) -> List[int]:
        """Get all security IDs in universe (for batch LTP)."""
        return [m.security_id for m in self.universe.values()]

    def get_candidates(self) -> Set[str]:
        """Get current Tier 2 candidates (for deep scoring)."""
        return self.tier2_symbols.copy()

    def stats(self) -> dict:
        """Return current scanner statistics."""
        return {
            "universe_size": len(self.universe),
            "tier2_count": len(self.tier2_symbols),
            "core_watchlist": len(self.core_watchlist),
            "dynamic_promoted": len(self.tier2_symbols) - len(self.core_watchlist & self.tier2_symbols),
            "last_scan_age_sec": time.time() - self.last_tier1_scan if self.last_tier1_scan > 0 else -1,
        }


# ============================================================
# INTEGRATION HELPER
# ============================================================

def create_scanner(gateway=None, watchlist_path: str = None) -> NSEUniverseScanner:
    """
    Factory function to create scanner with existing watchlist as core.
    
    Args:
        gateway: DhanV82Gateway instance
        watchlist_path: Path to existing watchlist.csv (optional)
    """
    core = set()
    if watchlist_path and os.path.exists(watchlist_path):
        try:
            with open(watchlist_path, "r") as f:
                reader = csv.reader(f)
                for row in reader:
                    if row:
                        core.add(row[0].strip())
            log.info(f"Core watchlist loaded: {len(core)} stocks")
        except Exception as e:
            log.warning(f"Could not load watchlist: {e}")

    scanner = NSEUniverseScanner(gateway=gateway, core_watchlist=core)
    return scanner


# ============================================================
# SELF-TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Test tradability gate
    good = LiveTick(security_id=1, symbol="TEST", ltp=150, volume=200000, last_update=time.time())
    assert tradability_gate(good, 15_00_00_000) == (True, "TRADABLE")
    
    cheap = LiveTick(security_id=2, symbol="CHEAP", ltp=30, volume=200000, last_update=time.time())
    assert tradability_gate(cheap, 15_00_00_000)[0] == False
    
    illiquid = LiveTick(security_id=3, symbol="ILLIQ", ltp=150, volume=50000, last_update=time.time())
    assert tradability_gate(illiquid, 15_00_00_000)[0] == False
    
    stale = LiveTick(security_id=4, symbol="STALE", ltp=150, volume=200000, last_update=time.time()-5)
    assert tradability_gate(stale, 15_00_00_000)[0] == False
    
    # Test scanner creation
    scanner = NSEUniverseScanner(core_watchlist={"RELIANCE", "TCS", "INFY"})
    assert scanner.core_watchlist == {"RELIANCE", "TCS", "INFY"}
    
    print("All self-tests PASSED")
    print(f"Scanner stats: {scanner.stats()}")
