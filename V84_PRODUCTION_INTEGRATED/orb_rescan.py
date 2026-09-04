"""
orb_rescan.py — Post-ORB rescan + entry guard + balance persist + candle fallback
Reform 2026-08-04
"""
import json, logging
from datetime import datetime

log = logging.getLogger("trading_bot")
BALANCE_JSON = "/home/ubuntu/trading-bot/last_balance.json"


def persist_balance(balance):
    """R5: Write balance to JSON so restarts don't lose it."""
    try:
        with open(BALANCE_JSON, "w") as f:
            json.dump({"balance": float(balance), "ts": str(datetime.now())}, f)
    except Exception:
        pass


def load_balance():
    """R5: Load last persisted balance. Returns 0.0 if unavailable."""
    try:
        with open(BALANCE_JSON) as f:
            return float(json.load(f).get("balance", 0) or 0)
    except Exception:
        return 0.0


def is_entry_allowed(bot):
    """R2: Return True only after post-ORB rescan has completed."""
    return getattr(bot, "_post_orb_ready", False)


def trigger_post_orb(bot):
    """
    R1: Run select_candidates() at 09:30 IST (04:00 UTC) once per session.
    Sets bot._post_orb_ready = True when done.
    Call this from the main run loop after ORB recording.
    """
    if getattr(bot, "_post_orb_ready", False):
        return True
    utc_now = datetime.utcnow()
    ready = utc_now.hour > 4 or (utc_now.hour == 4 and utc_now.minute >= 0)
    if ready:
        log.info("POST-ORB RESCAN: Re-scoring candidates (10:00 IST, full ATR/VWAP/ST)...")
        try:
            bot.select_candidates()
            bot._post_orb_ready = True
            top_l = [c.get("ticker","?") for c in getattr(bot, "_long_candidates", [])[:5]]
            top_s = [c.get("ticker","?") for c in getattr(bot, "_short_candidates", [])[:5]]
            log.info(f"POST-ORB DONE: {len(bot.candidates)} candidates re-ranked")
            log.info(f"  Top LONG:  {top_l}")
            log.info(f"  Top SHORT: {top_s}")
            return True
        except Exception as e:
            log.warning(f"Post-ORB rescan failed: {e} — entries still blocked")
            return False
    else:
        mins = (4 - utc_now.hour) * 60 + (15 - utc_now.minute if utc_now.minute < 15 else 0) if utc_now.hour < 4 else (30 - utc_now.minute)
        log.info(f"POST-ORB RESCAN pending — ~{max(0,mins)}min to go (09:30 IST / 04:00 UTC)")
        return False


def run_candle_fallback(bot, long_pool, short_pool, check_candles_fn):
    """
    CF: Scan ALL 552 self.watchlist stocks for 3 consecutive green/red 1-min candles.
    Stocks not already in any pool added as CANDLE_MOMENTUM tier. No cap.
    """
    existing = set(str(c.get("security_id", "")) for c in long_pool + short_pool)
    long_added, short_added = [], []
    for stock in getattr(bot, "watchlist", []):
        sid = stock.get("security_id")
        if str(sid) in existing:
            continue
        try:
            if check_candles_fn(sid, "LONG"):
                fc = dict(stock)
                fc.update({"direction":"LONG","tier":"CANDLE_MOMENTUM",
                           "gap_pct":0,"rs":0,"long_score":0,"short_score":0})
                long_pool.append(fc)
                existing.add(str(sid))
                long_added.append(stock.get("ticker","?"))
            elif check_candles_fn(sid, "SHORT"):
                fc = dict(stock)
                fc.update({"direction":"SHORT","tier":"CANDLE_MOMENTUM",
                           "gap_pct":0,"rs":0,"long_score":0,"short_score":0})
                short_pool.append(fc)
                existing.add(str(sid))
                short_added.append(stock.get("ticker","?"))
        except Exception:
            pass
    if long_added:
        log.info(f"CANDLE_MOMENTUM LONG ({len(long_added)}): {long_added}")
    if short_added:
        log.info(f"CANDLE_MOMENTUM SHORT ({len(short_added)}): {short_added}")
    return long_added, short_added


def trigger_periodic_rescan(bot):
    """
    Full-universe rescan every 15 min, 09:45-14:45 IST
    (04:30-08:30 UTC). Fixes stocks whose move starts AFTER the
    09:34/09:45 IST scoring checkpoints (e.g. late-morning momentum names
    that were flat/negative at open).

    Self-throttling: fires at most once per 15-min UTC slot. Does NOT
    touch active_trade or already-tracked ORBs -- only re-runs
    select_candidates() to refresh/expand the candidate pool so the
    existing monitoring loop can pick up newly-qualifying names.
    """
    utc_now = datetime.utcnow()

    # Window: 04:15 UTC (09:45 IST) through 09:15 UTC (14:45 IST).
    # The initial 09:31 scan is the baseline; every 15-min slot after that
    # refreshes the full opportunity universe.
    window_start_min = 4 * 60 + 15
    window_end_min    = 9 * 60 + 15
    now_min = utc_now.hour * 60 + utc_now.minute
    if now_min < window_start_min or now_min > window_end_min:
        return False

    # Throttle to once per 15-min slot (e.g. 04:30, 04:45, 05:00, ...)
    slot = now_min - (now_min % 15)
    last_slot = getattr(bot, "_last_periodic_rescan_slot", None)
    if last_slot == slot:
        return False  # already ran this slot

    bot._last_periodic_rescan_slot = slot
    ist_hh = (slot // 60 + 5) % 24
    ist_mm = (slot % 60 + 30) % 60
    if (slot % 60) + 30 >= 60:
        ist_hh = (ist_hh + 1) % 24

    log.info(f"PERIODIC RESCAN slot {slot} (~{ist_hh:02d}:{ist_mm:02d} IST): "
             f"re-scoring full universe...")
    try:
        _before_long  = set(c.get("security_id") for c in getattr(bot, "_long_candidates", []))
        _before_short = set(c.get("security_id") for c in getattr(bot, "_short_candidates", []))

        bot.select_candidates()

        _after_long  = getattr(bot, "_long_candidates", [])
        _after_short = getattr(bot, "_short_candidates", [])
        _new_long  = [c for c in _after_long  if c.get("security_id") not in _before_long]
        _new_short = [c for c in _after_short if c.get("security_id") not in _before_short]

        if _new_long or _new_short:
            log.info(f"PERIODIC RESCAN: +{len(_new_long)} new LONG "
                      f"{[c.get('ticker','?') for c in _new_long][:5]}, "
                      f"+{len(_new_short)} new SHORT "
                      f"{[c.get('ticker','?') for c in _new_short][:5]}")
        else:
            log.info("PERIODIC RESCAN: no new qualifying candidates this slot")
        return True
    except Exception as e:
        log.warning(f"Periodic rescan failed: {e}")
        return False
