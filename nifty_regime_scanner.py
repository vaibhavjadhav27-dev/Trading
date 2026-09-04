#!/usr/bin/env python3
"""
nifty_regime_scanner.py — Periodic Nifty regime sampler (v2, corrected)
============================================================================
Created 2026-08-08 (Sat). Rewritten 2026-08-08 to call the bot's REAL
regime classifier. Live from Mon 2026-08-10.

WHAT IT DOES
  Every 15 minutes, 09:30 -> 14:00 IST (inclusive), runs the trading bot's
  own check_market_quality() method — the exact live classifier — and
  appends one row per tick to a dated CSV timeline. The CSV is RETAINED
  through market close so the post-ORB rescan (and direction picks) read the
  freshest regime instead of a stale 09:15 open reading.

WHY THIS DESIGN (no reimplementation -> no drift)
  The regime logic lives INSIDE TradingBot.check_market_quality() (def @560):
    - get_historical_daily('13','IDX_I',days=70) -> daily closes
    - ema50/ema20 = indicators.compute_ema(closes, 50/20); last_close
    - nifty_ltp = fetch_ltp_concurrent(['13','25'])
    - nifty_vwap = compute_vwap(5-min intraday '13')
    - gates:  last_close < EMA50 -> return 'NO_TRADE'
              nifty_ltp <= VWAP  -> return 'CONSERVATIVE'
    - else classify by gap_pct (ltp vs prev close) & slope (ema20 vs ema50):
              gap>+0.3 & slope>0 & above50            -> TRENDING_UP
              gap<-0.3 & slope<0 & !above50           -> TRENDING_DOWN
              gap<-0.2 | slope<-0.1                   -> CHOPPY
              else                                    -> NORMAL
  check_market_quality() is a pure READ (no orders), so we call it directly
  and read self.regime + self.nifty_data. This runs the live classifier
  verbatim — if the bot's logic changes, the scanner follows automatically.

RUN MODE
  Standalone, own cron. One invocation = ONE snapshot. Self-guards on IST:
  before 09:30 or after 14:00 IST -> exits without writing.
  Cron (UTC, weekdays):  */15 4-8 * * 1-5   (09:30..14:15 IST; >14:00 guarded)

STORAGE
  candle_archive/regime_timeline_YYYY-MM-DD.csv  (one row per tick)
  Columns:
    ts_ist, ts_utc, mode, nifty_regime,
    nifty_ltp, nifty_vwap, ema20, ema50, prev_close,
    gap_pct, slope, above_vwap, above_ema50, note
"""

import os, sys, csv
from datetime import datetime, timezone, timedelta

BOT_DIR = "/home/ubuntu/trading-bot"
ARCHIVE_DIR = os.path.join(BOT_DIR, "candle_archive")
IST = timezone(timedelta(hours=5, minutes=30))

WIN_START = (9, 30)   # 09:30 IST inclusive
WIN_END   = (14, 0)   # 14:00 IST inclusive

sys.path.insert(0, BOT_DIR)


def _now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def _in_window(now_ist):
    start = now_ist.replace(hour=WIN_START[0], minute=WIN_START[1], second=0, microsecond=0)
    end   = now_ist.replace(hour=WIN_END[0],   minute=WIN_END[1],   second=0, microsecond=0)
    return start <= now_ist <= end


def _make_bot():
    """
    Instantiate TradingBot the way the app does. DhanClient() takes NO args
    (fetches token/client_id internally). We try common constructor shapes.
    """
    import trading_bot as tb
    BotCls = None
    for name in ("TradingBot", "Bot", "ORBBot", "NSEBot"):
        if hasattr(tb, name):
            BotCls = getattr(tb, name)
            break
    if BotCls is None:
        raise RuntimeError("Could not find the bot class in trading_bot.py "
                           "(tried TradingBot/Bot/ORBBot/NSEBot). "
                           "Tell me the class name and I'll wire it.")
    try:
        return BotCls()
    except TypeError:
        pass
    try:
        from dhan_client import DhanClient
        return BotCls(DhanClient())
    except Exception as e:
        raise RuntimeError(f"Could not construct {BotCls.__name__}: {e}")


def sample():
    """Run the bot's real check_market_quality() and read the result."""
    note = []
    bot = _make_bot()

    if not hasattr(bot, "check_market_quality"):
        raise RuntimeError("bot has no check_market_quality() — tell me the "
                           "real method name and I'll wire it.")

    mode = ""
    try:
        ret = bot.check_market_quality()   # pure read; sets self.regime, self.nifty_data
        mode = str(ret) if ret is not None else "FULL"
    except Exception as e:
        note.append(f"check_market_quality err:{e}")

    nd = getattr(bot, "nifty_data", {}) or {}
    regime = getattr(bot, "regime", "") or ""

    nifty_ltp  = nd.get("ltp", "")
    nifty_vwap = nd.get("vwap", "")
    ema20      = nd.get("ema20", "")
    ema50      = nd.get("ema50", "")
    prev_close = nd.get("prev_close", "")

    def _num(x):
        try:
            return float(x)
        except Exception:
            return None

    lt, pc, e20, e50, vw = map(_num, (nifty_ltp, prev_close, ema20, ema50, nifty_vwap))
    gap_pct = round((lt - pc) / pc * 100, 3) if (lt and pc) else ""
    slope   = round((e20 - e50) / e50 * 100, 3) if (e20 and e50) else ""
    above_vwap = (lt > vw) if (lt and vw) else ""
    above_ema50 = (lt > e50) if (lt and e50) else ""

    return {
        "mode": mode,
        "nifty_regime": regime,
        "nifty_ltp": round(lt, 2) if lt else nifty_ltp,
        "nifty_vwap": round(vw, 2) if vw else nifty_vwap,
        "ema20": round(e20, 2) if e20 else ema20,
        "ema50": round(e50, 2) if e50 else ema50,
        "prev_close": round(pc, 2) if pc else prev_close,
        "gap_pct": gap_pct,
        "slope": slope,
        "above_vwap": above_vwap,
        "above_ema50": above_ema50,
        "note": "; ".join(note),
    }


HEADER = ["ts_ist", "ts_utc", "mode", "nifty_regime", "nifty_ltp", "nifty_vwap",
          "ema20", "ema50", "prev_close", "gap_pct", "slope",
          "above_vwap", "above_ema50", "note"]


def write_row(now_ist, snap):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    date_str = now_ist.strftime("%Y-%m-%d")
    path = os.path.join(ARCHIVE_DIR, f"regime_timeline_{date_str}.csv")
    row = {
        "ts_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        "ts_utc": now_ist.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        **{k: snap.get(k, "") for k in HEADER if k not in ("ts_ist", "ts_utc")},
    }
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)
    return path


def main():
    force = "--force" in sys.argv
    now = _now_ist()
    if not force and not _in_window(now):
        print(f"[SKIP] {now.strftime('%H:%M IST')} outside 09:30-14:00 window - no write.")
        return
    snap = sample()
    path = write_row(now, snap)
    print(f"[OK] {now.strftime('%Y-%m-%d %H:%M IST')} -> {path}")
    print(f"     mode={snap['mode']} regime={snap['nifty_regime']!r} "
          f"ltp={snap['nifty_ltp']} vwap={snap['nifty_vwap']} "
          f"gap={snap['gap_pct']}% slope={snap['slope']}% "
          f"aboveVWAP={snap['above_vwap']} aboveEMA50={snap['above_ema50']}")
    if snap["note"]:
        print(f"     NOTE: {snap['note']}")


if __name__ == "__main__":
    main()
