"""
WebSocket-First Trading Engine v2.0
Replaces REST-heavy scanning with real-time WebSocket data
REST API calls: <10/day (orders + historical only)
"""

import time
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
import pytz

log = logging.getLogger("ws_engine")
IST = pytz.timezone("Asia/Kolkata")

class ORBTracker:
    """Tracks Opening Range (9:15-9:30) from WebSocket ticks"""

    def __init__(self):
        self.orb_data = dict()  # sid -> {high, low, open, close, volume}
        self.orb_complete = False
        self.orb_start = None
        self.orb_end = None

    def reset(self):
        """Reset for new trading day"""
        self.orb_data = dict()
        self.orb_complete = False
        self.orb_start = datetime.now(IST).replace(hour=9, minute=15, second=0)
        self.orb_end = datetime.now(IST).replace(hour=9, minute=30, second=0)
        log.info("ORB tracker reset for new day")

    def on_tick(self, sid, ltp, volume=0):
        """Process each WebSocket tick during ORB period"""
        now = datetime.now(IST)
        if now < self.orb_start or now > self.orb_end:
            return
        
        sid = str(sid)
        if sid not in self.orb_data:
            self.orb_data[sid] = {
                "high": ltp,
                "low": ltp,
                "open": ltp,
                "close": ltp,
                "volume": volume,
                "tick_count": 1
            }
        else:
            d = self.orb_data[sid]
            if ltp > d["high"]:
                d["high"] = ltp
            if ltp < d["low"]:
                d["low"] = ltp
            d["close"] = ltp
            d["volume"] = max(d["volume"], volume)
            d["tick_count"] += 1

    def mark_complete(self):
        """Called at 9:30 to finalize ORB"""
        self.orb_complete = True
        valid = sum(1 for d in self.orb_data.values() if d["tick_count"] >= 3)
        log.info(f"ORB COMPLETE: {len(self.orb_data)} stocks tracked, {valid} with 3+ ticks")

    def get_orb(self, sid):
        """Get ORB high/low for a stock"""
        return self.orb_data.get(str(sid))

    def get_orb_range_pct(self, sid):
        """Get ORB range as % of price"""
        orb = self.get_orb(sid)
        if not orb or orb["low"] == 0:
            return 0
        return (orb["high"] - orb["low"]) / orb["low"] * 100


class PrevCloseTracker:
    """Gets prev_close from WebSocket QUOTE mode - ZERO REST calls"""

    def __init__(self):
        self.prev_closes = dict()  # sid -> prev_close
        self.day_opens = dict()  # sid -> first tick of day
        self.ready = False

    def on_quote(self, sid, data):
        """Process WebSocket QUOTE data which includes prev_close"""
        sid = str(sid)
        prev_close = data.get("prev_close", 0) or data.get("close", 0)
        if prev_close and prev_close > 0:
            self.prev_closes[sid] = float(prev_close)

    def on_first_tick(self, sid, ltp):
        """Record first tick as day open"""
        sid = str(sid)
        if sid not in self.day_opens:
            self.day_opens[sid] = float(ltp)

    def get_prev_close(self, sid):
        return self.prev_closes.get(str(sid), 0)

    def get_gap_pct(self, sid, current_ltp):
        """Calculate gap % using WebSocket data only"""
        pc = self.get_prev_close(sid)
        if not pc or pc <= 0:
            return 0
        return (current_ltp - pc) / pc * 100

    def count_ready(self):
        return len(self.prev_closes)


class BreakoutDetector:
    """Detects breakouts in real-time from WebSocket tick stream"""

    def __init__(self, orb_tracker, confirmation_seconds=30):
        self.orb = orb_tracker
        self.confirmation_seconds = confirmation_seconds
        self.breakout_candidates = dict()  # sid -> {time, price, confirmed}
        self.confirmed_breakouts = dict()  # sid -> {time, price, orb_high}
        self.rejected = dict()  # sid -> reason

    def on_tick(self, sid, ltp, volume=0):
        """Check every tick for breakout"""
        if not self.orb.orb_complete:
            return None
        
        sid = str(sid)
        orb = self.orb.get_orb(sid)
        if not orb:
            return None
        
        orb_high = orb["high"]
        
        # Already confirmed or rejected
        if sid in self.confirmed_breakouts or sid in self.rejected:
            return self.confirmed_breakouts.get(sid)
        
        # Check for breakout (LTP > ORB high)
        if ltp > orb_high:
            if sid not in self.breakout_candidates:
                # First break above ORB high - start confirmation timer
                self.breakout_candidates[sid] = {
                    "time": time.time(),
                    "price": ltp,
                    "orb_high": orb_high,
                    "ticks_above": 1
                }
                log.debug(f"Breakout candidate: {sid} @ {ltp} > ORB {orb_high}")
            else:
                # Already a candidate - check confirmation time
                candidate = self.breakout_candidates[sid]
                candidate["ticks_above"] += 1
                elapsed = time.time() - candidate["time"]
                if elapsed >= self.confirmation_seconds:
                    # CONFIRMED BREAKOUT!
                    self.confirmed_breakouts[sid] = {
                        "time": datetime.now(IST),
                        "price": ltp,
                        "orb_high": orb_high,
                        "ticks_above": candidate["ticks_above"],
                        "confirmation_secs": round(elapsed, 1)
                    }
                    log.info(f"BREAKOUT CONFIRMED: {sid} @ {ltp} ({candidate[chr(116)+chr(105)+chr(99)+chr(107)+chr(115)+chr(95)+chr(97)+chr(98)+chr(111)+chr(118)+chr(101)]} ticks in {elapsed:.1f}s)")
                    return self.confirmed_breakouts[sid]
        else:
            # Price fell back below ORB high - reject
            if sid in self.breakout_candidates:
                self.rejected[sid] = "Price fell back below ORB high"
                del self.breakout_candidates[sid]
                log.debug(f"Breakout rejected: {sid} fell back below ORB high")
        
        return None

    def get_confirmed(self):
        """Get all confirmed breakouts"""
        return dict(self.confirmed_breakouts)

    def reset(self):
        self.breakout_candidates = dict()
        self.confirmed_breakouts = dict()
        self.rejected = dict()


class WebSocketEngine:
    """
    Main WebSocket-First Engine
    Orchestrates: PrevClose -> ORB Tracking -> Breakout Detection
    REST calls: ZERO during market hours (only for orders)
    """

    def __init__(self, ws_feed, watchlist):
        self.ws_feed = ws_feed
        self.watchlist = watchlist
        self.prev_close_tracker = PrevCloseTracker()
        self.orb_tracker = ORBTracker()
        self.breakout_detector = BreakoutDetector(self.orb_tracker)
        self.live_prices = dict()  # sid -> latest LTP
        self.live_volumes = dict()  # sid -> latest volume
        self.vwap_data = dict()  # sid -> {cum_vol, cum_pv}
        self._running = False
        self._phase = "INIT"  # INIT -> PRE_OPEN -> ORB -> SCANNING -> TRADING

    def start(self):
        """Start the WebSocket engine"""
        self._running = True
        self.orb_tracker.reset()
        self.breakout_detector.reset()
        self._phase = "PRE_OPEN"
        log.info("WebSocket Engine started - PRE_OPEN phase")

    def stop(self):
        self._running = False
        self._phase = "STOPPED"
        log.info("WebSocket Engine stopped")

    def on_tick(self, sid, ltp, volume=0, prev_close=0):
        """Main tick handler - routes to appropriate component"""
        if not self._running:
            return
        
        sid = str(sid)
        self.live_prices[sid] = ltp
        self.live_volumes[sid] = volume
        
        # Track prev_close from quote data
        if prev_close and prev_close > 0:
            self.prev_close_tracker.on_quote(sid, {"prev_close": prev_close})
        
        # Record first tick as day open
        self.prev_close_tracker.on_first_tick(sid, ltp)
        
        # Update VWAP
        self._update_vwap(sid, ltp, volume)
        
        # Route based on phase
        now = datetime.now(IST)
        
        if now.hour == 9 and now.minute < 15:
            self._phase = "PRE_OPEN"
        elif now.hour == 9 and 15 <= now.minute < 30:
            self._phase = "ORB"
            self.orb_tracker.on_tick(sid, ltp, volume)
        elif now.hour == 9 and now.minute == 30 and not self.orb_tracker.orb_complete:
            self.orb_tracker.mark_complete()
            self._phase = "SCANNING"
        elif self.orb_tracker.orb_complete:
            self._phase = "SCANNING"
            # Check for breakout on every tick
            self.breakout_detector.on_tick(sid, ltp, volume)

    def _update_vwap(self, sid, ltp, volume):
        """Incremental VWAP calculation"""
        if volume <= 0:
            return
        if sid not in self.vwap_data:
            self.vwap_data[sid] = {"cum_vol": 0, "cum_pv": 0}
        d = self.vwap_data[sid]
        d["cum_vol"] += volume
        d["cum_pv"] += ltp * volume

    def get_vwap(self, sid):
        """Get current VWAP for a stock"""
        d = self.vwap_data.get(str(sid))
        if not d or d["cum_vol"] == 0:
            return 0
        return d["cum_pv"] / d["cum_vol"]

    def get_live_price(self, sid):
        return self.live_prices.get(str(sid), 0)

    def get_gap_pct(self, sid):
        """Get current gap % for a stock"""
        ltp = self.get_live_price(sid)
        if not ltp:
            return 0
        return self.prev_close_tracker.get_gap_pct(sid, ltp)

    def get_candidates(self, config):
        """
        Get ranked candidates - ZERO REST calls!
        All data comes from WebSocket tick stream.
        """
        if not self.orb_tracker.orb_complete:
            return list()
        
        candidates = list()
        
        for stock in self.watchlist:
            sid = str(stock.get("security_id", ""))
            ticker = stock.get("ticker", "?")
            ltp = self.get_live_price(sid)
            if not ltp:
                continue
            
            # Price filter
            if ltp < config.PRICE_FLOOR or ltp > config.PRICE_CEIL_TIER1:
                continue
            
            # Direction filter (positive gap only)
            gap_pct = self.get_gap_pct(sid)
            if gap_pct < config.GAP_MIN:
                continue
            if gap_pct > config.GAP_MAX_TIER1:
                continue
            
            # ORB range filter
            orb_range = self.orb_tracker.get_orb_range_pct(sid)
            if orb_range <= 0 or orb_range > config.ORB_MAX_PCT:
                continue
            
            # ORB minimum range (must be > 1% of price for meaningful breakout)
            if orb_range < 0.5:
                continue
            
            # VWAP check - price should be above VWAP
            vwap = self.get_vwap(sid)
            above_vwap = ltp > vwap if vwap > 0 else True
            
            # Score calculation
            score = 0.5
            if 1.0 <= gap_pct <= 4.0:
                score += 0.15  # Optimal gap range
            if 1.0 <= orb_range <= 3.0:
                score += 0.1  # Healthy ORB
            if above_vwap:
                score += 0.1  # Above VWAP
            if gap_pct > 4.0:
                score -= 0.1  # Overextended gap penalty
            
            candidates.append({
                "ticker": ticker,
                "sid": sid,
                "ltp": ltp,
                "gap_pct": round(gap_pct, 2),
                "orb_range": round(orb_range, 2),
                "orb_high": self.orb_tracker.get_orb(sid).get("high", 0) if self.orb_tracker.get_orb(sid) else 0,
                "orb_low": self.orb_tracker.get_orb(sid).get("low", 0) if self.orb_tracker.get_orb(sid) else 0,
                "vwap": round(vwap, 2),
                "above_vwap": above_vwap,
                "score": round(score, 2)
            })
        
        # Sort by score descending
        candidates.sort(key=lambda x: -x.get("score", 0))
        log.info(f"WebSocket candidates: {len(candidates)} passed filters")
        return candidates

    def get_status(self):
        """Get current engine status"""
        return {
            "phase": self._phase,
            "prev_closes": self.prev_close_tracker.count_ready(),
            "live_prices": len(self.live_prices),
            "orb_tracked": len(self.orb_tracker.orb_data),
            "orb_complete": self.orb_tracker.orb_complete,
            "breakouts_confirmed": len(self.breakout_detector.confirmed_breakouts),
            "breakouts_pending": len(self.breakout_detector.breakout_candidates),
            "breakouts_rejected": len(self.breakout_detector.rejected)
        }

