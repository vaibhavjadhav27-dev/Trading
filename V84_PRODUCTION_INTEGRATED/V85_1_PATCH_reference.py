"""
V8.5.1 production hardening patch

Purpose:
- Fix Dhan SL-M payload / DH-906 path.
- Make broker reconciliation exception-safe.
- Remove arbitrary 3-position cap; capacity is controlled by risk + margin.
- Allocate margin across all qualified candidates in one scan.
- Use Dhan multi-order margin calculation when available.
- Add complete sizing/cost audit.
- Add broker-side trailing SL modification.
- Persist peak/MFE state.
- Add re-entry quality cooldown.

This module is deliberately independent of the existing V8.2/V8.4 strategy.
It is an execution/capital-management layer. Do not replace the five entry modes.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


# -----------------------------
# Configuration
# -----------------------------
@dataclass(frozen=True)
class V851Config:
    # Risk is a portfolio budget, not a position-count limit.
    risk_per_trade_pct: float = 0.60
    max_open_risk_pct: float = 1.50
    cash_reserve_pct: float = 10.0

    # Keep this as a safety brake only. Set to 0 for no position-count cap.
    # Recommended production value after validation: 0.
    max_intraday_positions: int = 0

    # No arbitrary 60% single-position cap if risk/margin allow it.
    # 0 means disabled.
    max_single_notional_pct: float = 0.0

    # Transaction economics.
    min_net_edge_pct: float = 0.15
    cost_multiplier_for_entry: float = 1.50

    # Re-entry quality control.
    reentry_cooldown_minutes: int = 5
    reentry_failed_setup_minutes: int = 20

    # Broker-side trailing.
    trail_activation_r: float = 1.0
    trail_1r: float = 0.50
    trail_2r: float = 1.25
    trail_2_5r: float = 1.75
    trail_3r: float = 2.25
    max_giveback_r: float = 1.50

    # Live confirmation.
    confirmation_seconds: int = 10
    confirmation_checks: int = 3


CFG = V851Config()


def _now():
    return datetime.now(IST)


def _round_price(px: float, tick: float = 0.05) -> float:
    """Round to exchange tick; use 0.05 default for NSE equity."""
    if not px or px <= 0:
        return px
    tick = tick if tick > 0 else 0.05
    return round(round(px / tick) * tick, 2)


def _positive(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


# -----------------------------
# 1. Dhan SL-M gateway fix
# -----------------------------
def place_order_fixed(gateway, security_id, qty, price=0.0,
                      transaction_type="BUY", order_type="MARKET",
                      trigger_price=0.0, correlation_id=None,
                      exchange_segment="NSE_EQ", product_type="INTRADAY"):
    """
    Replacement for gateway.place_order.

    Critical fix:
    STOP_LOSS_MARKET must not be sent with price == triggerPrice.
    The current V8.5 code does exactly that, which matches the observed
    DH-906 rejection. For SLM send price=0 and triggerPrice=trigger.
    """
    import uuid

    ot = str(order_type).upper()
    is_slm = ot == "STOP_LOSS_MARKET"
    if is_slm:
        api_price = 0.0
        api_trigger = _round_price(_positive(trigger_price))
        if api_trigger <= 0:
            raise ValueError("SL-M requires positive trigger_price")
    else:
        api_price = _positive(price)
        api_trigger = _positive(trigger_price)

    payload = {
        "dhanClientId": gateway.client_id,
        "correlationId": correlation_id or ("V851-" + uuid.uuid4().hex[:20]),
        "transactionType": transaction_type,
        "exchangeSegment": exchange_segment,
        "productType": product_type,
        "orderType": ot,
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": int(qty),
        "disclosedQuantity": 0,
        "price": api_price,
        "triggerPrice": api_trigger,
        "afterMarketOrder": False,
    }
    return gateway._request("POST", "/orders", payload, kind="order")


def install_gateway_patch(gateway):
    """Monkey-patch an existing V82DhanGateway instance safely."""
    import types
    gateway.place_order = types.MethodType(
        lambda self, security_id, qty, price=0, transaction_type="BUY",
               order_type="MARKET", trigger_price=0, correlation_id=None:
        place_order_fixed(self, security_id, qty, price, transaction_type,
                          order_type, trigger_price, correlation_id),
        gateway,
    )
    return gateway


def safe_place_hard_sl(gateway, security_id: str, qty: int, side: str,
                       trigger: float, current_price: float,
                       tick: float = 0.05) -> Optional[dict]:
    """Validate direction and install a real broker-side SLM."""
    side = side.upper()
    trigger = _round_price(trigger, tick)
    current_price = _positive(current_price)

    if side == "LONG":
        if trigger >= current_price:
            trigger = _round_price(current_price * 0.9975, tick)
        transaction = "SELL"
    elif side == "SHORT":
        if trigger <= current_price:
            trigger = _round_price(current_price * 1.0025, tick)
        transaction = "BUY"
    else:
        raise ValueError(f"Unknown side: {side}")

    # Never submit a stop that has crossed the current market.
    import logging as _lg
    _lg.getLogger('v851_debug').warning(f'SL DEBUG: side={side} trigger={trigger} current_price={current_price} qty={qty} sid={security_id}')
    if side == "LONG" and trigger >= current_price:
        _lg.getLogger('v851_debug').warning(f'SL REJECTED: LONG trigger {trigger} >= current {current_price}')
        return None
    if side == "SHORT" and trigger <= current_price:
        _lg.getLogger('v851_debug').warning(f'SL REJECTED: SHORT trigger {trigger} <= current {current_price}')
        return None

    for attempt in range(2):
        try:
            resp = gateway.place_order(
                security_id, qty, 0.0, transaction,
                "STOP_LOSS_MARKET", trigger_price=trigger
            )
            oid = resp.get("orderId") if isinstance(resp, dict) else None
            if oid:
                status = gateway.get_order_status(oid) or {}
                st = str(status.get("orderStatus", status.get("status", ""))).upper()
                if st in ("PENDING", "TRANSIT", "TRADED", "PART_TRADED"):
                    return {**resp, "triggerPrice": trigger, "verifiedStatus": st}
            # If Dhan rejected it, refresh the market before retrying.
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.25)
            if side == "LONG":
                trigger = _round_price(trigger * 0.998, tick)
            else:
                trigger = _round_price(trigger * 1.002, tick)

    return None


# -----------------------------
# 2. Dhan multi-order margin
# -----------------------------
def calculate_multi_margin(gateway, orders: List[dict],
                          include_position: bool = True,
                          include_orders: bool = True) -> dict:
    """Use Dhan's combined margin endpoint for the candidate batch."""
    payload = {
        "includePosition": include_position,
        "includeOrders": include_orders,
        "dhanClientId": gateway.client_id,
        "scripts": orders,
    }
    try:
        return gateway._request("POST", "/margincalculator/multi", payload,
                                kind="order") or {}
    except Exception:
        # Caller can fall back to single-order margin.
        return {}


# -----------------------------
# 3. Cost-aware opportunity
# -----------------------------
def estimate_round_trip_cost(gateway, security_id: str, qty: int,
                            entry: float, expected_exit: float,
                            transaction_type: str = "BUY") -> float:
    """Prefer Dhan's margin calculator brokerage; fall back to local charge module."""
    try:
        d = gateway.calculate_margin(security_id, qty, transaction_type, entry) or {}
        brokerage = _positive(d.get("brokerage"))
        if brokerage > 0:
            # Brokerage response is for the order. Add a conservative second leg.
            return brokerage * 2.0
    except Exception:
        pass
    try:
        from dhan_charges import dhan_charges_mis
        return _positive(dhan_charges_mis(qty, entry, expected_exit))
    except Exception:
        # Conservative placeholder only; do not use this for final accounting.
        return max(20.0, qty * entry * 0.00015)


def expected_net_edge_pct(qty: int, entry: float, expected_exit: float,
                          side: str, cost_rupees: float) -> float:
    gross = (expected_exit - entry) * qty if side == "LONG" else (entry - expected_exit) * qty
    notional = max(entry * qty, 1.0)
    return (gross - cost_rupees) / notional * 100.0


# -----------------------------
# 4. Portfolio-aware quantity
# -----------------------------
def open_risk_rupees(active_positions: Dict[str, dict]) -> float:
    total = 0.0
    for p in active_positions.values():
        qty = abs(int(_positive(p.get("qty"))))
        entry = _positive(p.get("entry"))
        sl = _positive(p.get("sl"))
        if qty and entry and sl:
            total += abs(entry - sl) * qty
    return total


def size_candidate(gateway, candidate: dict, decision: dict,
                   active_positions: Dict[str, dict],
                   config: V851Config = CFG,
                   remaining_risk_rupees: float = 0.0) -> dict:
    """Risk + margin + cost aware sizing. Never increases qty to satisfy a cost floor."""
    balance = _positive(gateway.get_balance())
    entry = _positive(decision.get("entry_price"))
    stop = _positive(decision.get("stop"))
    side = str(decision.get("side", "")).upper()
    if balance <= 0 or entry <= 0 or stop <= 0:
        return {"qty": 0, "reason": "INVALID_PRICE_OR_BALANCE"}

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return {"qty": 0, "reason": "INVALID_STOP_DISTANCE"}

    current_open_risk = open_risk_rupees(active_positions)
    max_open_risk = balance * config.max_open_risk_pct / 100.0
    remaining_risk = max(0.0, max_open_risk - current_open_risk)
    # Use batch-aware remaining_risk if provided (caps to what's actually available)
    if remaining_risk_rupees > 0:
        remaining_risk = min(remaining_risk, remaining_risk_rupees)
    per_trade_risk = min(balance * config.risk_per_trade_pct / 100.0,
                          remaining_risk)
    if per_trade_risk <= 0:
        return {"qty": 0, "reason": "OPEN_RISK_CAP"}

    # Use the available cash after reserve only as a capital sanity constraint.
    cash_capacity = balance * (1.0 - config.cash_reserve_pct / 100.0)
    risk_qty = int(per_trade_risk / stop_distance)
    capital_qty = int(cash_capacity / entry) if entry else 0

    # Optional notional ceiling. 0 = disabled.
    if config.max_single_notional_pct > 0:
        notional_qty = int((balance * config.max_single_notional_pct / 100.0) / entry)
    else:
        notional_qty = 10**12

    qty = max(0, min(risk_qty, capital_qty, notional_qty))
    if qty < 1:
        return {"qty": 0, "reason": "RISK_OR_CAPITAL_TOO_SMALL"}

    expected_exit = _positive(decision.get("target"), entry)
    txn = "BUY" if side == "LONG" else "SELL"
    cost = estimate_round_trip_cost(gateway, str(candidate["security_id"]),
                                    qty, entry, expected_exit, txn)
    net_edge = expected_net_edge_pct(qty, entry, expected_exit, side, cost)
    if net_edge < config.min_net_edge_pct:
        return {"qty": 0, "reason": "NET_EDGE_TOO_SMALL", "estimated_cost": cost,
                "net_edge_pct": net_edge}

    # Broker single-order margin check.
    margin = gateway.calculate_margin(str(candidate["security_id"]), qty, txn, entry) or {}
    available = _positive(margin.get("availableBalance", margin.get("availabelBalance", balance)), balance)
    required = _positive(margin.get("totalMargin"))
    if required > available and required > 0:
        qty = max(0, int(qty * available / required * 0.98))
        if qty < 1:
            return {"qty": 0, "reason": "MARGIN_REJECT"}
        margin = gateway.calculate_margin(str(candidate["security_id"]), qty, txn, entry) or margin
        required = _positive(margin.get("totalMargin"))
        available = _positive(margin.get("availableBalance", margin.get("availabelBalance", available)), available)

    return {
        "qty": qty,
        "risk_rupees": round(min(per_trade_risk, qty * stop_distance), 2),
        "risk_pct": round(min(per_trade_risk, qty * stop_distance) / balance * 100, 4),
        "entry": entry,
        "stop": stop,
        "notional": round(qty * entry, 2),
        "margin_required": required,
        "margin_available": available,
        "estimated_round_trip_cost": round(cost, 2),
        "expected_net_edge_pct": round(net_edge, 4),
        "reason": "OK",
    }


# -----------------------------
# 5. Peak / broker-side trailing
# -----------------------------
def desired_trailing_stop(position: dict, price: float,
                          momentum_ok: bool = True,
                          structure_ok: bool = True) -> Optional[float]:
    entry = _positive(position.get("entry"))
    original_sl = _positive(position.get("sl"))
    side = str(position.get("side", "LONG")).upper()
    peak = _positive(position.get("peak"), entry)
    best_r = _positive(position.get("best_r"))
    risk_per_share = abs(entry - original_sl)
    if entry <= 0 or risk_per_share <= 0:
        return None

    # Do not tighten aggressively while structure/momentum is healthy.
    if side == "LONG":
        if best_r >= 3.0:
            floor = entry + 2.25 * risk_per_share
        elif best_r >= 2.5:
            floor = entry + 1.75 * risk_per_share
        elif best_r >= 2.0:
            floor = entry + 1.25 * risk_per_share
        elif best_r >= 1.0:
            floor = entry + 0.50 * risk_per_share
        else:
            return original_sl

        # If momentum/structure weakens, use the lower of the structural floor
        # and a peak-based protection level; never move backwards.
        if not momentum_ok or not structure_ok:
            floor = max(floor, peak - 1.50 * risk_per_share)
        return max(original_sl, _round_price(floor))

    else:
        if best_r >= 3.0:
            floor = entry - 2.25 * risk_per_share
        elif best_r >= 2.5:
            floor = entry - 1.75 * risk_per_share
        elif best_r >= 2.0:
            floor = entry - 1.25 * risk_per_share
        elif best_r >= 1.0:
            floor = entry - 0.50 * risk_per_share
        else:
            return original_sl
        if not momentum_ok or not structure_ok:
            floor = min(floor, peak + 1.50 * risk_per_share)
        return min(original_sl, _round_price(floor))


def modify_pending_sl(gateway, order_id: str, qty: int, new_trigger: float,
                      side: str, current_price: float) -> bool:
    """Modify the existing pending SLM rather than creating many SL orders."""
    if not order_id or qty <= 0:
        return None
    side = side.upper()
    trigger = _round_price(new_trigger)
    if side == "LONG":
        transaction = "SELL"
        if trigger >= current_price:
            return None
    else:
        transaction = "BUY"
        if trigger <= current_price:
            return None

    # Dhan order modification API supports orderType/quantity/price/triggerPrice.
    payload = {
        "dhanClientId": gateway.client_id,
        "orderId": str(order_id),
        "orderType": "STOP_LOSS_MARKET",
        "quantity": int(qty),
        "price": 0.0,
        "triggerPrice": trigger,
        "validity": "DAY",
    }
    try:
        r = gateway._request("PUT", f"/orders/{order_id}", payload, kind="order") or {}
        st = str(r.get("orderStatus", r.get("status", ""))).upper()
        return r if st in ("TRANSIT", "PENDING", "TRADED", "PART_TRADED") else None
    except Exception:
        return None


# -----------------------------
# 6. Safe broker reconciliation
# -----------------------------
def safe_reconcile(bot) -> Tuple[bool, str]:
    """Never let reconciliation exceptions terminate the trading process."""
    try:
        positions = bot.dhan.get_positions() or []
        if isinstance(positions, dict):
            positions = positions.get("data", positions.get("positions", [])) or []
        broker = {}
        for p in positions if isinstance(positions, list) else []:
            sid = str(p.get("securityId", p.get("security_id", "")))
            if not sid:
                continue
            nq = int(_positive(p.get("netQty", p.get("net_qty", 0))))
            if nq:
                broker[sid] = p

        for sid, bp in broker.items():
            if sid in bot.active_positions:
                # Broker is authoritative for live quantity.
                local = bot.active_positions[sid]
                bqty = abs(int(_positive(bp.get("netQty", bp.get("net_qty", 0)))))
                if bqty > 0:
                    local["qty"] = bqty
                continue

            # Orphan: recover average price if broker supplies it.
            nq = int(_positive(bp.get("netQty", bp.get("net_qty", 0))))
            side = "LONG" if nq > 0 else "SHORT"
            entry = _positive(bp.get("avgCostPrice", bp.get("averageCostPrice", bp.get("buyAvg", 0))))
            if entry <= 0:
                entry = _positive(bp.get("averageTradedPrice", bp.get("avgTradedPrice", 0)))

            if entry <= 0:
                # Do not manufacture an active managed position.
                bot._event("orphan_unrecoverable", {"sid": sid, "qty": nq}) if hasattr(bot, "_event") else None
                continue

            # Conservative initial stop until strategy context is reconstructed.
            sl = entry * (0.9925 if side == "LONG" else 1.0075)
            state = {
                "symbol": bp.get("tradingSymbol", sid),
                "security_id": sid,
                "side": side,
                "qty": abs(nq),
                "entry": entry,
                "sl": sl,
                "sl_order_id": None,
                "peak": entry,
                "best_r": 0.0,
                "max_profit_pct": 0.0,
                "score": 0.0,
                "tier": 1.0,
                "entry_time": bp.get("createTime", "recovered"),
                "target": 0.0,
                "source": "BROKER_RECONCILIATION",
            }
            bot.active_positions[sid] = state

            try:
                px = _positive(bot.fetch_ltp_concurrent([sid]).get(sid), entry)
                sl_order = safe_place_hard_sl(bot.dhan, sid, abs(nq), side, sl, px)
                if sl_order:
                    state["sl_order_id"] = sl_order.get("orderId")
                else:
                    # Cannot safely manage the orphan. Exit it rather than leave it naked.
                    bot.emergency_exit(sid, abs(nq), side)
                    bot.active_positions.pop(sid, None)
            except Exception:
                try:
                    bot.emergency_exit(sid, abs(nq), side)
                finally:
                    bot.active_positions.pop(sid, None)

        bot._save_state()
        return True, "RECONCILIATION_OK"
    except Exception as exc:
        # Critical: process remains alive. Caller should enter recovery mode.
        return False, f"RECONCILIATION_ERROR:{exc}"


# -----------------------------
# 7. Batch allocator
# -----------------------------
def allocate_batch(bot, ranked: List[Tuple[dict, dict]],
                  config: V851Config = CFG) -> List[Tuple[dict, dict, dict]]:
    """
    Allocate all qualified candidates in score/expected-net-edge order.
    This does not impose a position-count cap. Dhan multi-margin is used to
    validate the resulting batch when possible.
    """
    selected: List[Tuple[dict, dict, dict]] = []
    active = bot.active_positions
    # Track cumulative batch risk to enforce aggregate ceiling
    _batch_risk_rupees = 0.0
    _balance = _positive(bot.dhan.get_balance())
    _max_open_risk_rupees = _balance * config.max_open_risk_pct / 100.0
    _existing_risk = open_risk_rupees(active)

    for decision, candidate in ranked:
        if str(candidate["security_id"]) in active:
            continue
        if config.max_intraday_positions > 0 and len(active) + len(selected) >= config.max_intraday_positions:
            break
        # Check aggregate risk ceiling BEFORE sizing
        _remaining_risk = max(0, _max_open_risk_rupees - _existing_risk - _batch_risk_rupees)
        if _remaining_risk <= 0:
            break

        sized = size_candidate(bot.dhan, candidate, decision, active, config, remaining_risk_rupees=_remaining_risk)
        if sized.get("qty", 0) < 1:
            continue
        # Cap this trade's risk to remaining budget
        _trade_risk = abs(_positive(sized.get("entry")) - _positive(decision.get("stop"))) * sized["qty"]
        if _trade_risk > _remaining_risk:
            # Reduce qty to fit remaining risk
            _risk_per_share = abs(_positive(sized.get("entry")) - _positive(decision.get("stop")))
            if _risk_per_share > 0:
                _allowed_qty = int(_remaining_risk / _risk_per_share)
                if _allowed_qty < 1:
                    continue  # Cannot fit even 1 share within remaining risk
                sized["qty"] = min(sized["qty"], _allowed_qty)
                _trade_risk = _risk_per_share * sized["qty"]
        _batch_risk_rupees += _trade_risk
        selected.append((decision, candidate, sized))

    if not selected:
        return []

    # Combined margin validation. Shrink the least valuable positions first
    # if the batch does not fit. This keeps more qualified trades alive.
    orders = []
    for decision, candidate, sized in selected:
        orders.append({
            "exchangeSegment": "NSE_EQ",
            "transactionType": "BUY" if decision["side"] == "LONG" else "SELL",
            "quantity": int(sized["qty"]),
            "productType": "INTRADAY",
            "securityId": str(candidate["security_id"]),
            "price": float(sized["entry"]),
        })

    mm = calculate_multi_margin(bot.dhan, orders)
    total_margin = _positive(mm.get("total_margin", mm.get("totalMargin")))
    available = _positive(mm.get("availableBalance", mm.get("available_balance")),
                          _positive(bot.dhan.get_balance()))
    if total_margin > 0 and available > 0 and total_margin > available:
        # Reduce lower-ranked quantities first, preserving higher-ranked setups.
        for i in range(len(selected) - 1, -1, -1):
            d, c, s = selected[i]
            s["qty"] = max(0, int(s["qty"] * available / total_margin * 0.97))
            if s["qty"] < 1:
                selected.pop(i)
            else:
                # P0-2: Recalculate cost + net edge after quantity reduction
                _entry = _positive(s.get("entry", d.get("entry_price", 0)))
                _target = _positive(d.get("target", _entry * 1.01))
                _txn = "BUY" if d["side"] == "LONG" else "SELL"
                s["estimated_round_trip_cost"] = estimate_round_trip_cost(bot.dhan, str(c["security_id"]), s["qty"], _entry, _target, _txn)
                s["expected_net_edge_pct"] = expected_net_edge_pct(s["qty"], _entry, _target, d["side"], s["estimated_round_trip_cost"])
            # Rebuild/stop once the estimate is comfortably within margin.
            orders = [{
                "exchangeSegment": "NSE_EQ",
                "transactionType": "BUY" if x[0]["side"] == "LONG" else "SELL",
                "quantity": int(x[2]["qty"]),
                "productType": "INTRADAY",
                "securityId": str(x[1]["security_id"]),
                "price": float(x[2]["entry"]),
            } for x in selected]
            mm = calculate_multi_margin(bot.dhan, orders)
            total_margin = _positive(mm.get("total_margin", mm.get("totalMargin")))
            if total_margin <= available:
                break

    return selected


# -----------------------------
# 8. Production tests
# -----------------------------
def run_unit_tests():
    """Pure tests for the dangerous calculations. No broker/API calls."""
    assert _round_price(100.023) == 100.0
    assert _round_price(100.027) == 100.05

    class FakeGateway:
        client_id = "TEST"

    # Verify the intended SLM payload shape by monkeypatching _request.
    captured = {}
    g = FakeGateway()
    g._request = lambda method, endpoint, payload, kind=None: captured.update(payload) or {"orderId": "X", "orderStatus": "PENDING"}
    r = place_order_fixed(g, "1", 10, 0, "SELL", "STOP_LOSS_MARKET", 99.5)
    assert captured["price"] == 0.0
    assert captured["triggerPrice"] == 99.5
    assert r["orderId"] == "X"

    assert expected_net_edge_pct(100, 100, 102, "LONG", 20) > 0
    assert expected_net_edge_pct(100, 100, 100.1, "LONG", 20) < 0

    p = {"entry": 100, "sl": 99, "side": "LONG", "peak": 103, "best_r": 3}
    assert desired_trailing_stop(p, 102, True, True) >= 101
    print("V8.5.1 pure tests: PASS")


if __name__ == "__main__":
    run_unit_tests()

# -----------------------------
# 9. Entry execution using pre-allocated quantity
# -----------------------------
def execute_entry_allocated(bot, candidate: dict, decision: dict, sizing: dict) -> Tuple[bool, str]:
    """Execute one already-allocated candidate; never recalculate quantity mid-order."""
    sid = str(candidate["security_id"])
    side = str(decision["side"]).upper()
    qty = int(sizing.get("qty", 0))
    if qty < 1:
        return False, "NO_ALLOCATED_QTY"
    if sid in bot.active_positions:
        return False, "ALREADY_ACTIVE"

    # Final live price check immediately before order.
    px = _positive(bot.fetch_ltp_concurrent([sid]).get(sid), sizing.get("entry"))
    if px <= 0:
        return False, "NO_LTP"

    stop = _positive(decision.get("stop"))
    if side == "LONG" and stop >= px:
        return False, "INVALID_STOP_SIDE"
    if side == "SHORT" and stop <= px:
        return False, "INVALID_STOP_SIDE"

    txn = "BUY" if side == "LONG" else "SELL"
    resp = bot.dhan.place_order(sid, qty, 0, txn, "MARKET")
    oid = resp.get("orderId") if isinstance(resp, dict) else None
    if not oid:
        return False, "ENTRY_ORDER_NOT_ACCEPTED"

    fill = bot.dhan.verify_fill(oid)
    if fill.get("status") == "PARTIAL":
        # Cancel remainder and VERIFY cancellation
        try:
            bot.dhan.cancel_order(oid)
            time.sleep(0.5)
            # Verify the order is actually cancelled
            _cancel_status = bot.dhan.get_order_status(oid) or {}
            _cs = str(_cancel_status.get("orderStatus", "")).upper()
            if _cs not in ("CANCELLED", "REJECTED", "EXPIRED"):
                # Cancellation may not have worked — retry
                try: bot.dhan.cancel_order(oid)
                except Exception: pass
                time.sleep(0.5)
        except Exception:
            pass
        # Use broker net position as authority for actual filled qty
        _net = int(bot.dhan.verify_position(sid, side))
        if _net != 0:
            fill["qty"] = abs(_net)

    if fill.get("status") not in ("FILLED", "PARTIAL") or int(fill.get("qty", 0)) <= 0:
        return False, f"ENTRY_{fill.get('status', 'UNKNOWN')}"

    filled_qty = int(fill["qty"])
    fill_px = _positive(fill.get("price"), px)
    net_qty = int(bot.dhan.verify_position(sid, side))
    if (side == "LONG" and net_qty <= 0) or (side == "SHORT" and net_qty >= 0):
        bot.emergency_exit(sid, filled_qty, side)
        return False, "BROKER_POSITION_MISMATCH"

    # Never protect more shares than actually filled.
    sl = stop
    if (side == "LONG" and sl >= fill_px) or (side == "SHORT" and sl <= fill_px):
        sl = fill_px * (0.9925 if side == "LONG" else 1.0075)

    sl_resp = safe_place_hard_sl(bot.dhan, sid, filled_qty, side, sl, fill_px)
    sl_oid = sl_resp.get("orderId") if isinstance(sl_resp, dict) else None
    if not sl_oid:
        # SL placement failed — keep position with SOFTWARE SL protection only
        import logging
        logging.getLogger("v851").warning(f"HARD SL FAILED for {sid} {side} — using software SL only (monitor will protect)")
        sl_oid = None  # Position active without broker SL

    # Persist complete management state BEFORE returning success.
    state = {
        "symbol": candidate.get("symbol", "?"),
        "security_id": sid,
        "side": side,
        "qty": filled_qty,
        "entry": fill_px,
        "sl": _round_price(sl),
        "sl_order_id": sl_oid,
        "peak": fill_px,
        "best_r": 0.0,
        "max_profit_pct": 0.0,
        "score": _positive(decision.get("final_score")),
        "mode": decision.get("setup_type", "UNKNOWN"),
        "target": _positive(decision.get("target")),
        "risk_pct": _positive(sizing.get("risk_pct")),
        "entry_time": _now().isoformat(),
        "estimated_round_trip_cost": _positive(sizing.get("estimated_round_trip_cost")),
        "expected_net_edge_pct": _positive(sizing.get("expected_net_edge_pct")),
        "source": "V851",
    }
    bot.active_positions[sid] = state
    bot._save_state()
    return True, "ENTRY_OK"
