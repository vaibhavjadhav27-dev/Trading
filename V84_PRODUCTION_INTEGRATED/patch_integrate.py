"""Live integration layer for directional entry/monitor/exit.

Safety invariants:
- one active position at a time; there is NO daily trade-count cap
- LONG and SHORT use identical conviction/margin rules
- actual broker fill is required before a trade is recorded
- actual position sign/quantity is reconciled after fill
- server-side SL must be accepted; otherwise the position is immediately exited
- +0.40% is the minimum profit-management trigger, not a guaranteed outcome
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
import config
from trade_policy import confidence_pct, margin_tier, pick_side
from short_live import (
    size_position, get_dynamic_sl, place_hard_sl, profit_lock_floor,
    current_profit_pct, cover_floor_price, confirm_exit,
)

log = logging.getLogger("trading_bot")


def _order_status_name(status):
    if not isinstance(status, dict):
        return ""
    return str(status.get("orderStatus") or status.get("status") or "").upper()


def _filled_from_order(bot, order_id, fallback_qty=0, fallback_price=0.0):
    """Poll order/trade book and return (filled_qty, avg_price, status)."""
    if not order_id:
        return 0, 0.0, "NO_ORDER_ID"
    last = {}
    for _ in range(12):
        try:
            last = bot.dhan.get_order_status(order_id) or {}
            st = _order_status_name(last)
            if st in ("TRADED", "FILLED"):
                qty = int(last.get("filledQty") or last.get("tradedQuantity") or last.get("quantity") or 0)
                px = float(last.get("averageTradedPrice") or last.get("tradedPrice") or 0)
                try:
                    trades = bot.dhan.get_trades_for_order(order_id) or []
                    if isinstance(trades, dict):
                        trades = trades.get("data", trades.get("trades", [])) or []
                    if isinstance(trades, list) and trades:
                        qty = sum(int(t.get("tradedQuantity", 0) or 0) for t in trades)
                        notional = sum(float(t.get("tradedPrice", 0) or 0) * int(t.get("tradedQuantity", 0) or 0) for t in trades)
                        px = notional / qty if qty else px
                except Exception:
                    pass
                return qty or int(fallback_qty), px or float(fallback_price), st
            if st in ("REJECTED", "CANCELLED", "EXPIRED"):
                return 0, 0.0, st
        except Exception as exc:
            log.warning(f"fill poll error: {exc}")
        time.sleep(1)
    return 0, 0.0, _order_status_name(last) or "TIMEOUT"


def _verify_position(bot, security_id, side, expected_qty):
    """Return live absolute quantity if broker position has the correct sign."""
    positions = bot.dhan.get_positions() or []
    if isinstance(positions, dict):
        positions = positions.get("data", positions.get("positions", [])) or []
    for p in positions if isinstance(positions, list) else []:
        if str(p.get("securityId", "")) != str(security_id):
            continue
        net = int(p.get("netQty", 0) or 0)
        ok = (net > 0) if side == "LONG" else (net < 0)
        if ok:
            return min(abs(net), int(expected_qty))
        return 0
    return 0


def _fit_to_margin(bot, security_id, qty, price, side, balance, leverage):
    """Use Dhan's margin calculator; never assume a theoretical 3x/5x is available."""
    txn = "BUY" if side == "LONG" else "SELL"
    info = bot.dhan.calculate_margin(security_id, qty, txn, price)
    if not isinstance(info, dict):
        raise RuntimeError("Dhan margin calculator returned no usable response")
    total = float(info.get("totalMargin", info.get("total_margin", 0)) or 0)
    avail = float(info.get("availableBalance", info.get("available_balance", balance)) or balance)
    if total <= avail * 0.95:
        return qty, total, float(info.get("leverage", leverage) or leverage), info
    # One proportional reduction, then re-check. This avoids repeated API calls.
    if total <= 0:
        raise RuntimeError("Dhan margin calculator returned zero margin")
    reduced = max(1, int(qty * (avail * 0.95 / total)))
    if reduced >= qty:
        raise RuntimeError(f"Insufficient Dhan margin: required={total:.2f}, available={avail:.2f}")
    info2 = bot.dhan.calculate_margin(security_id, reduced, txn, price)
    if not isinstance(info2, dict):
        raise RuntimeError("Dhan margin re-check failed")
    total2 = float(info2.get("totalMargin", 0) or 0)
    if total2 > avail * 0.95:
        raise RuntimeError(f"Insufficient Dhan margin after resize: required={total2:.2f}, available={avail:.2f}")
    return reduced, total2, float(info2.get("leverage", leverage) or leverage), info2


def side_aware_entry(bot, regime, long_score, short_score, long_candidate, short_candidate):
    # No daily trade-count cap, but never overwrite an unclosed broker position.
    if getattr(bot, "active_trade", None):
        log.info("ENTRY HOLD: existing position remains active; no overwrite")
        return None

    side, reason = pick_side(regime, long_score, short_score)
    log.info(f"SIDE SELECTION: {side} — {reason}")
    if side == "NO_TRADE":
        return None
    if side == "SHORT" and not getattr(config, "ENABLE_SHORTS", True):
        return None

    candidate = long_candidate if side == "LONG" else short_candidate
    score = long_score if side == "LONG" else short_score
    if not candidate or score is None:
        return None

    symbol = candidate.get("symbol") or candidate.get("ticker") or "?"
    sid = str(candidate["security_id"])
    try:
        ltp_map = bot.fetch_ltp_concurrent([sid])
        price = float(ltp_map.get(sid, 0) or candidate.get("entry_price", 0))
    except Exception:
        price = float(candidate.get("entry_price", 0) or 0)
    if price <= 0:
        return None

    expected_move = candidate.get("expected_move_pct")
    if expected_move is None and candidate.get("expected_target"):
        target = float(candidate["expected_target"])
        expected_move = abs(target - price) / price * 100.0
    if expected_move is None and candidate.get("expected_r"):
        expected_move = float(candidate["expected_r"]) * 0.40  # conservative fallback; scans should provide target
    if expected_move is None or float(expected_move) < float(getattr(config, "MIN_EXPECTED_MOVE_PCT", 0.40)):
        log.info(f"ENTRY REJECT {symbol}: expected move {expected_move} < {getattr(config, 'MIN_EXPECTED_MOVE_PCT', 0.40)}%")
        return None

    balance = bot.dhan.get_balance()
    if not balance or balance <= 0:
        return None
    conf = confidence_pct(score)
    leverage, tier = margin_tier(conf)
    qty, _, _ = size_position(balance, price, score=score, regime=regime)
    if qty <= 0 or leverage <= 0:
        return None
    qty, required_margin, actual_leverage, margin_info = _fit_to_margin(
        bot, sid, qty, price, side, balance, leverage
    )

    txn = "BUY" if side == "LONG" else "SELL"
    order = bot.dhan.place_order(int(sid), qty, 0, txn, "MARKET") or {}
    order_id = order.get("orderId") if isinstance(order, dict) else None
    if not order_id:
        log.error(f"ENTRY REJECTED by broker: {symbol}: {order}")
        return None

    filled_qty, fill_price, order_status = _filled_from_order(bot, order_id, qty, price)
    if filled_qty <= 0:
        try: bot.dhan.cancel_order(order_id)
        except Exception: pass
        log.error(f"ENTRY NOT FILLED: {symbol} status={order_status}")
        return None
    if filled_qty < qty * float(getattr(config, "PARTIAL_FILL_MIN_PCT", 0.50)):
        try:
            bot.dhan.place_order(int(sid), filled_qty, 0, "SELL" if side == "LONG" else "BUY", "MARKET")
        except Exception as exc:
            log.critical(f"PARTIAL FILL EXIT FAILED {symbol}: {exc}")
        return None

    live_qty = _verify_position(bot, sid, side, filled_qty)
    if live_qty <= 0:
        log.critical(f"FILL/POSITION MISMATCH {symbol}: fill={filled_qty} side={side}; no position recorded")
        return None
    filled_qty = live_qty

    # Dynamic SL when ATR/VWAP are available; otherwise planned SL or 0.75% fallback.
    atr = candidate.get("atr")
    vwap = candidate.get("vwap")
    sl_price, sl_pct = get_dynamic_sl(
        fill_price, side, atr=float(atr) if atr else None,
        vwap=float(vwap) if vwap else None,
        floor_pct=0.40, ceil_pct=2.0
    )
    if candidate.get("planned_sl"):
        planned = float(candidate["planned_sl"])
        # Planned structural SL wins only if it is on the correct side of the fill.
        if (side == "LONG" and planned < fill_price) or (side == "SHORT" and planned > fill_price):
            sl_price = planned
            sl_pct = abs(fill_price - sl_price) / fill_price * 100.0

    try:
        sl_result, sl_trigger = place_hard_sl(bot.dhan, sid, filled_qty, fill_price, side, sl_price=sl_price)
        sl_order_id = sl_result.get("orderId", "") if isinstance(sl_result, dict) else ""
        if not sl_order_id:
            raise RuntimeError(f"SL response missing orderId: {sl_result}")
    except Exception as exc:
        log.critical(f"SL PROTECTION FAILED {symbol}: {exc} — exiting filled position immediately")
        try:
            bot.dhan.place_order(int(sid), filled_qty, 0, "SELL" if side == "LONG" else "BUY", "MARKET")
        except Exception as exit_exc:
            log.critical(f"EMERGENCY EXIT FAILED {symbol}: {exit_exc}")
        return None

    bot.active_trade = {
        "symbol": symbol, "security_id": sid, "entry_price": str(round(fill_price, 2)),
        "qty": str(filled_qty), "side": side, "sl_price": str(round(sl_trigger, 2)),
        "r_value": str(round(abs(fill_price - sl_trigger), 2)),
        "max_price": str(round(fill_price, 2)), "min_price": str(round(fill_price, 2)),
        "trailing_sl": str(round(sl_trigger, 2)), "phase": "INITIAL",
        "entry_time": datetime.now().isoformat(), "tier": tier,
        "leverage": str(round(actual_leverage, 2)), "score": str(score),
        "confidence": str(round(conf, 2)), "regime": regime, "reason": reason,
        "sl_order_id": sl_order_id, "expected_move_pct": str(round(float(expected_move), 3)),
        "margin_required": str(round(required_margin, 2)),
    }
    bot.dynamo.save_active_trade(bot.active_trade)
    log.info(f"ENTRY CONFIRMED: {side} {symbol} fill=₹{fill_price:.2f} qty={filled_qty} conf={conf:.1f}% tier={tier} actual_margin={required_margin:.2f} SL=₹{sl_trigger:.2f}")
    return bot.active_trade


def side_aware_monitor(bot, ltp):
    if not getattr(bot, "active_trade", None):
        return
    trade = bot.active_trade
    entry = float(trade["entry_price"])
    side = trade.get("side", "LONG")
    ltp = float(ltp)
    current = current_profit_pct(entry, ltp, side)
    if side == "LONG":
        peak = max(float(trade.get("max_price", entry)), ltp)
        trade["max_price"] = str(round(peak, 2))
    else:
        peak = min(float(trade.get("min_price", entry)), ltp)
        trade["min_price"] = str(round(peak, 2))
    peak_profit = current_profit_pct(entry, peak, side)
    floor = profit_lock_floor(peak_profit)
    if floor > 0:
        floor_price = cover_floor_price(entry, floor, side)
        breached = ltp <= floor_price if side == "LONG" else ltp >= floor_price
        if breached:
            # Use context if available; if unavailable, do not give back a material protected profit.
            vwap = float(trade.get("last_vwap", 0) or 0)
            vol = float(trade.get("last_vol", 0) or 0)
            vol_ma = float(trade.get("last_vol_ma", vol or 1) or 1)
            sector = float(trade.get("last_sector_chg", 0) or 0)
            closed = bool(trade.get("last_candle_closed", True))
            if vwap > 0:
                ok, why = confirm_exit(side, ltp, floor_price, vwap, vol, vol_ma, sector, closed)
            else:
                ok, why = True, "protected floor breached without fresh context"
            if ok:
                side_aware_exit(bot, ltp, f"PROFIT_LOCK floor={floor:.2f}% peak={peak_profit:.2f}% current={current:.2f}% ({why})")
                return
    if peak_profit >= float(getattr(config, "PROFIT_PROTECT_TRIGGER_PCT", 0.40)):
        trade["phase"] = "PROFIT_PROTECTED"
    bot.dynamo.save_active_trade(trade)


def side_aware_exit(bot, exit_price, reason):
    trade = getattr(bot, "active_trade", None)
    if not trade:
        return False
    symbol = trade["symbol"]; sid = str(trade["security_id"]); side = trade.get("side", "LONG")
    qty = int(trade["qty"]); entry = float(trade["entry_price"]); exit_price = float(exit_price)
    positions = bot.dhan.get_positions() or []
    if isinstance(positions, dict): positions = positions.get("data", positions.get("positions", [])) or []
    live = 0
    for p in positions if isinstance(positions, list) else []:
        if str(p.get("securityId", "")) == sid:
            net = int(p.get("netQty", 0) or 0)
            if (side == "LONG" and net > 0) or (side == "SHORT" and net < 0): live = abs(net)
            break
    if live <= 0:
        log.warning(f"EXIT skipped: broker has no {side} position for {symbol}")
        bot.active_trade = None
        bot.dynamo.clear_active_trade()
        return False
    qty = min(qty, live)
    sl_order = trade.get("sl_order_id", "")
    if sl_order:
        try: bot.dhan.cancel_order(sl_order)
        except Exception as exc: log.warning(f"SL cancel failed {symbol}: {exc}")
    txn = "SELL" if side == "LONG" else "BUY"
    try:
        res = bot.dhan.place_order(int(sid), qty, 0, txn, "MARKET") or {}
        if isinstance(res, dict) and res.get("orderId"):
            fq, fp, st = _filled_from_order(bot, res["orderId"], qty, exit_price)
            if fq <= 0:
                raise RuntimeError(f"exit order not filled: {st}")
            exit_price = fp or exit_price
    except Exception as exc:
        log.critical(f"EXIT ORDER FAILED {side} {symbol}: {exc}")
        return False
    pnl = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
    log.info(f"EXIT CONFIRMED: {side} {symbol} @ ₹{exit_price:.2f} qty={qty} PnL=₹{pnl:.2f} reason={reason}")
    bot.active_trade = None
    bot.dynamo.clear_active_trade()
    return True
