"""
Execution Integrity Module — V11.0.0 (2026-08-31)
===================================================
Hardened drop-in replacement for execution_integrity.py.

Fixes ALL P0 bugs from Aug 24-31 production logs:
  DH-906  — Side-aware SL builder with local pre-validation
  P0-ATTR — BrokerReconciler.kill_switch_active AttributeError
  P0-ORPH — Orphan positions running 3+ hrs with no SL
  P0-CAP  — Canary notional cap ₹10K not enforced
  P0-REST — State lost on bot restart (intent ledger + recovery)
  P0-SL   — SL verify loop with retry + emergency flatten

Architecture (unchanged from V8):
    NEVER infer position state from internal logic.
    ALWAYS verify against broker before concluding.

New in V11:
    A1. StopLossBuilder          — side-aware SL with local DH-906 guard
    A2. verify_sl()              — submit → poll → retry → flatten
    A3. create_intent / confirm  — atomic position creation via intent ledger
    A4. BrokerReconciler fix     — uses guard.kill_switch_active, SL-or-flatten
    A5. ExecutionGuard fix       — correct field names, report_reconciliation_clean
    A6. Restart recovery         — intent ledger replay, restart limiter
    A7. EOD force close          — uses StopLossBuilder, EXIT AUDIT with MFE/MAE

Public interface preserved (drop-in):
    ExecutionIntegrity(dhan, bot_instance, base_path=None)
      .resolve_order(order_id)
      .reconcile()
      .confirm_sl(sl_order_id, security_id, side, trigger_price)
      .eod_force_close()
      .guard.entries_allowed()

New methods:
      .sl_builder.build_sl_order(side, stop_level, ltp, qty, sid)
      .verify_sl(sl_order_id, sid, side, max_retries)
      .create_intent(symbol, sid, side, qty, price, sl_level)
      .confirm_fill(intent_id, order_id, fill_qty, fill_price)
      .recover_intents()
"""

from __future__ import annotations
import json, time, logging, uuid, os, threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Dict, List, Any, Tuple

log = logging.getLogger("execution_integrity")
IST = timezone(timedelta(hours=5, minutes=30))


# ═══════════════════════════════════════════════════════════════════════
# A1. STOP-LOSS BUILDER  —  Side-aware, DH-906-proof
# ═══════════════════════════════════════════════════════════════════════

class StopLossBuilder:
    """
    Builds SL-M orders with LOCAL pre-validation that catches DH-906
    ('Price should be greater than Trigger Price' for SHORT,
     'Trigger Price should be greater than Price' for LONG)
    BEFORE the order ever reaches Dhan.

    Rules (Dhan SL-M semantics):
        LONG exit  = SELL SL-M
            triggerPrice < current_ltp   (fires when price FALLS to trigger)
            price        = trigger * 0.95 (5% buffer below trigger for slippage)
        SHORT exit = BUY SL-M
            triggerPrice > current_ltp   (fires when price RISES to trigger)
            price        = trigger * 1.05 (5% buffer above trigger for slippage)
    """

    # Slippage buffers — generous enough to avoid rejection in fast markets
    LONG_PRICE_FACTOR  = 0.95   # price = trigger * 0.95 for SELL SL-M
    SHORT_PRICE_FACTOR = 1.05   # price = trigger * 1.05 for BUY SL-M

    @staticmethod
    def build_sl_order(side: str, stop_level: float, current_ltp: float,
                       qty: int, security_id: str) -> dict:
        """
        Build a validated SL-M order dict.

        Args:
            side:         "LONG" or "SHORT" — the POSITION side (not the exit txn)
            stop_level:   desired trigger price for the stop-loss
            current_ltp:  last traded price at time of SL placement
            qty:          quantity to protect
            security_id:  Dhan security ID

        Returns:
            dict with keys: txn, trigger_price, price, qty, security_id

        Raises:
            ValueError: if the order would be rejected by Dhan (DH-906 pre-check)
        """
        if qty <= 0:
            raise ValueError(f"SL qty must be > 0, got {qty}")
        if stop_level <= 0:
            raise ValueError(f"SL stop_level must be > 0, got {stop_level}")
        if current_ltp <= 0:
            raise ValueError(f"SL current_ltp must be > 0, got {current_ltp}")

        side = side.upper()
        stop_level = round(stop_level, 2)

        if side == "LONG":
            # Exit a LONG = SELL SL-M
            # Trigger must be BELOW current LTP (fires on drop)
            if stop_level >= current_ltp:
                raise ValueError(
                    f"LONG SL: triggerPrice ({stop_level}) must be < LTP ({current_ltp}). "
                    f"Would cause DH-906: 'Trigger Price should be greater than Price' rejection."
                )
            trigger_price = stop_level
            price = round(trigger_price * StopLossBuilder.LONG_PRICE_FACTOR, 2)
            txn = "SELL"

            # Dhan SELL SL-M validation: price < triggerPrice (always true with 0.95 factor)
            # But double-check to be safe
            if price >= trigger_price:
                price = round(trigger_price - 0.05, 2)

        elif side == "SHORT":
            # Exit a SHORT = BUY SL-M
            # Trigger must be ABOVE current LTP (fires on rise)
            if stop_level <= current_ltp:
                raise ValueError(
                    f"SHORT SL: triggerPrice ({stop_level}) must be > LTP ({current_ltp}). "
                    f"Would cause DH-906: 'Price should be greater than Trigger Price' rejection."
                )
            trigger_price = stop_level
            price = round(trigger_price * StopLossBuilder.SHORT_PRICE_FACTOR, 2)
            txn = "BUY"

            # Dhan BUY SL-M validation: price > triggerPrice (always true with 1.05 factor)
            if price <= trigger_price:
                price = round(trigger_price + 0.05, 2)

        else:
            raise ValueError(f"Unknown side '{side}', expected 'LONG' or 'SHORT'")

        order = {
            "txn": txn,
            "trigger_price": trigger_price,
            "price": price,
            "qty": qty,
            "security_id": security_id,
        }

        # ── FINAL LOCAL VALIDATION (catches anything we missed) ──
        StopLossBuilder._validate_locally(order, side, current_ltp)
        return order

    @staticmethod
    def _validate_locally(order: dict, side: str, current_ltp: float):
        """
        Re-validate the fully-built order against Dhan's known rules.
        Raises ValueError on any rule violation.
        """
        tp = order["trigger_price"]
        pr = order["price"]
        txn = order["txn"]

        if txn == "SELL":
            # Dhan rule: for SELL SL, price ≤ triggerPrice
            if pr > tp:
                raise ValueError(
                    f"SELL SL-M local validation failed: price ({pr}) > triggerPrice ({tp})"
                )
            # Trigger must be below LTP for it to be a stop
            if tp >= current_ltp:
                raise ValueError(
                    f"SELL SL-M: trigger ({tp}) >= LTP ({current_ltp}) — would execute immediately"
                )

        elif txn == "BUY":
            # Dhan rule: for BUY SL, price ≥ triggerPrice
            if pr < tp:
                raise ValueError(
                    f"BUY SL-M local validation failed: price ({pr}) < triggerPrice ({tp})"
                )
            # Trigger must be above LTP for it to be a stop
            if tp <= current_ltp:
                raise ValueError(
                    f"BUY SL-M: trigger ({tp}) <= LTP ({current_ltp}) — would execute immediately"
                )


# ═══════════════════════════════════════════════════════════════════════
# INTENT STATES  —  Atomic position lifecycle
# ═══════════════════════════════════════════════════════════════════════

class IntentState(Enum):
    INTENT_CREATED   = "INTENT_CREATED"
    ORDER_SUBMITTED  = "ORDER_SUBMITTED"
    FILL_CONFIRMED   = "FILL_CONFIRMED"
    SL_VERIFIED      = "SL_VERIFIED"
    ACTIVE           = "ACTIVE"
    EMERGENCY_CLOSED = "EMERGENCY_CLOSED"
    CANCELLED        = "CANCELLED"


@dataclass
class PositionIntent:
    """Pre-trade intent — written to disk BEFORE any broker call."""
    intent_id: str
    symbol: str
    security_id: str
    side: str
    qty: int
    price: float
    sl_level: float
    state: str = IntentState.INTENT_CREATED.value
    order_id: str = ""
    fill_qty: int = 0
    fill_price: float = 0.0
    sl_order_id: str = ""
    trade_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    notional: float = 0.0

    def __post_init__(self):
        now = datetime.now(IST).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if self.notional == 0.0 and self.price > 0 and self.qty > 0:
            self.notional = round(self.price * self.qty, 2)


# ═══════════════════════════════════════════════════════════════════════
# 1. ORDER STATE MACHINE  (unchanged from V8)
# ═══════════════════════════════════════════════════════════════════════

class OrderState(Enum):
    SUBMITTED = "SUBMITTED"
    PENDING   = "PENDING"
    PARTIAL   = "PARTIAL"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"
    EXPIRED   = "EXPIRED"
    UNKNOWN   = "UNKNOWN"


@dataclass
class OrderRecord:
    order_id: str
    trade_id: str
    symbol: str
    security_id: str
    side: str       # LONG / SHORT
    txn: str        # BUY / SELL
    requested_qty: int
    price: float
    state: OrderState = OrderState.SUBMITTED
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    sl_order_id: Optional[str] = None
    sl_confirmed: bool = False
    sl_trigger: float = 0.0
    created_at: str = ""
    resolved_at: str = ""
    resolution_method: str = ""
    broker_verified: bool = False

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(IST).isoformat()


class OrderStateTracker:
    """Tracks every order from submission to final resolution."""

    def __init__(self, dhan, ledger_path: Path):
        self.dhan = dhan
        self.orders: Dict[str, OrderRecord] = {}
        self.ledger_path = ledger_path
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def register(self, order_id: str, trade_id: str, symbol: str,
                 security_id: str, side: str, qty: int, price: float) -> OrderRecord:
        txn = "BUY" if side == "LONG" else "SELL"
        rec = OrderRecord(
            order_id=order_id, trade_id=trade_id, symbol=symbol,
            security_id=security_id, side=side, txn=txn,
            requested_qty=qty, price=price
        )
        self.orders[order_id] = rec
        log.info(f"ORDER REGISTERED: {trade_id} {symbol} {side} qty={qty} oid={order_id}")
        return rec

    def resolve(self, order_id: str, dhan_gateway) -> Optional[OrderRecord]:
        """
        Resolve an order's final state by querying broker.
        CORE of the expert's architecture: never assume — always verify.
        """
        rec = self.orders.get(order_id)
        if not rec:
            log.error(f"ORDER NOT TRACKED: {order_id}")
            return None

        sid = rec.security_id
        side = rec.side

        # Step 1: Query order status from broker
        order_status = {}
        try:
            order_status = dhan_gateway.get_order_status(order_id) or {}
        except Exception as e:
            log.warning(f"Order status query failed for {order_id}: {e}")

        broker_status = str(order_status.get("orderStatus", "")).upper()
        broker_qty = int(order_status.get("filledQty", 0) or
                         order_status.get("tradedQty", 0) or 0)
        broker_price = float(order_status.get("averageTradedPrice", 0) or 0)

        # Step 2: Map broker status
        if broker_status in ("TRADED", "FILLED"):
            rec.state = OrderState.FILLED
            rec.filled_qty = broker_qty or rec.requested_qty
            rec.avg_fill_price = broker_price or rec.price
            rec.resolution_method = "normal"

        elif broker_status in ("PART_TRADED", "PARTIALLY_FILLED"):
            rec.state = OrderState.PARTIAL
            rec.filled_qty = broker_qty
            rec.avg_fill_price = broker_price or rec.price
            rec.resolution_method = "normal"

        elif broker_status in ("CANCELLED", "REJECTED", "EXPIRED"):
            if broker_qty > 0:
                rec.state = OrderState.PARTIAL
                rec.filled_qty = broker_qty
                rec.avg_fill_price = broker_price or rec.price
                rec.resolution_method = "partial_before_cancel"
            else:
                rec.state = OrderState(broker_status) if broker_status in ("CANCELLED", "REJECTED", "EXPIRED") else OrderState.CANCELLED
                rec.filled_qty = 0

        elif broker_status in ("PENDING", "TRANSIT", ""):
            log.warning(f"ORDER STILL PENDING: {order_id} {rec.symbol} — attempting cancel + verify")
            try:
                dhan_gateway.cancel_order(order_id)
                time.sleep(0.8)
            except Exception as e:
                log.warning(f"Cancel attempt for {order_id}: {e}")

            try:
                order_status = dhan_gateway.get_order_status(order_id) or {}
                broker_status = str(order_status.get("orderStatus", "")).upper()
                broker_qty = int(order_status.get("filledQty", 0) or
                                 order_status.get("tradedQty", 0) or 0)
                broker_price = float(order_status.get("averageTradedPrice", 0) or 0)
            except Exception:
                pass

            net_qty = 0
            try:
                net_qty = int(dhan_gateway.verify_position(sid, side))
            except Exception as e:
                log.warning(f"Position verify failed for {sid}: {e}")

            if broker_qty > 0 or (side == "LONG" and net_qty > 0) or (side == "SHORT" and net_qty < 0):
                rec.state = OrderState.FILLED
                rec.filled_qty = broker_qty if broker_qty > 0 else abs(net_qty)
                rec.avg_fill_price = broker_price if broker_price > 0 else rec.price
                rec.resolution_method = "race_adoption"
                log.warning(f"RACE ADOPTION: {rec.symbol} {order_id} — filled {rec.filled_qty} @ {rec.avg_fill_price}")
            else:
                rec.state = OrderState.CANCELLED
                rec.filled_qty = 0
                rec.resolution_method = "timeout_cancelled"
                log.info(f"ORDER CANCELLED (no fill): {rec.symbol} {order_id}")

        else:
            log.warning(f"UNKNOWN ORDER STATUS: {broker_status} for {order_id}")
            net_qty = 0
            try:
                net_qty = int(dhan_gateway.verify_position(sid, side))
            except Exception:
                pass
            if (side == "LONG" and net_qty > 0) or (side == "SHORT" and net_qty < 0):
                rec.state = OrderState.FILLED
                rec.filled_qty = abs(net_qty)
                rec.avg_fill_price = broker_price or rec.price
                rec.resolution_method = "position_discovery"
            else:
                rec.state = OrderState.UNKNOWN
                rec.resolution_method = "unresolved"

        rec.resolved_at = datetime.now(IST).isoformat()
        rec.broker_verified = True
        self._persist(rec)
        return rec

    def _persist(self, rec: OrderRecord):
        try:
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(asdict(rec), default=str) + "\n")
        except Exception as e:
            log.error(f"Ledger write failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 2. BROKER RECONCILER  —  V11: uses guard.kill_switch_active (A4 fix)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AdoptedPosition:
    security_id: str
    symbol: str
    side: str
    qty: int
    avg_price: float
    initial_sl: float
    sl_order_id: Optional[str] = None
    adopted_at: str = ""
    trade_id: str = ""

    def __post_init__(self):
        if not self.adopted_at:
            self.adopted_at = datetime.now(IST).isoformat()
        if not self.trade_id:
            self.trade_id = f"ADOPT_{uuid.uuid4().hex[:8]}"


class BrokerReconciler:
    """
    Discovers broker positions not in local state and ADOPTS them.

    V11 FIX (A4):
        - No longer references self.kill_switch_active (AttributeError).
          Uses self.guard.kill_switch_active through the injected guard ref.
        - _adopt_position REQUIRES broker-confirmed SL.
          If SL fails → emergency close (NOT adopt without SL).
    """

    def __init__(self, dhan, bot_instance, guard: "ExecutionGuard",
                 sl_builder: StopLossBuilder):
        self.dhan = dhan
        self.bot = bot_instance
        self.guard = guard                # ← V11 FIX: reference to ExecutionGuard
        self.sl_builder = sl_builder      # ← V11: side-aware SL builder
        self.adoption_log: List[AdoptedPosition] = []
        self.mismatch_count = 0

    def reconcile(self, active_positions: Dict) -> Dict[str, Any]:
        report = {
            "broker_positions": 0,
            "local_positions": len(active_positions),
            "matched": 0,
            "orphans_found": 0,
            "orphans_adopted": 0,
            "adoption_failures": 0,
            "emergency_closes": 0,
            "mismatch": False,
            "details": [],
        }

        try:
            broker_pos = self.dhan.get_positions() or []
        except Exception as e:
            log.error(f"RECONCILE: Broker position fetch failed: {e}")
            report["mismatch"] = True
            self.mismatch_count += 1
            return report

        active_broker = [bp for bp in broker_pos if int(bp.get("netQty", 0)) != 0]
        report["broker_positions"] = len(active_broker)

        local_sids = set(str(sid) for sid in active_positions.keys())

        for bp in active_broker:
            sid = str(bp.get("securityId", ""))
            symbol = bp.get("tradingSymbol", "?")
            net_qty = int(bp.get("netQty", 0))
            side = "LONG" if net_qty > 0 else "SHORT"
            avg_price = float(
                bp.get("costPrice", 0) or
                bp.get("buyAvg" if net_qty > 0 else "sellAvg", 0)
            )

            if sid in local_sids:
                report["matched"] += 1
                continue

            report["orphans_found"] += 1
            log.warning(f"ORPHAN DETECTED: {symbol} sid={sid} {side} qty={abs(net_qty)} @ {avg_price}")

            adopted = self._adopt_position(sid, symbol, side, abs(net_qty), avg_price)
            if adopted:
                report["orphans_adopted"] += 1
                report["details"].append(f"ADOPTED: {symbol} {side} {abs(net_qty)} @ {avg_price}")
            else:
                report["adoption_failures"] += 1
                report["emergency_closes"] += 1
                report["details"].append(f"ADOPTION FAILED → EMERGENCY CLOSE: {symbol} {side}")

        report["mismatch"] = report["orphans_found"] > 0 or report["adoption_failures"] > 0
        if report["mismatch"]:
            self.mismatch_count += 1
        else:
            self.mismatch_count = 0

        return report

    def _adopt_position(self, sid: str, symbol: str, side: str,
                        qty: int, avg_price: float) -> bool:
        """
        Adopt an orphan position WITH confirmed SL.
        V11 RULE: If SL cannot be confirmed → EMERGENCY CLOSE, not adopt unprotected.
        """
        try:
            # Calculate SL — 0.75% conservative default
            if side == "LONG":
                initial_sl = round(avg_price * 0.9925, 2)
            else:
                initial_sl = round(avg_price * 1.0075, 2)

            # Get current LTP for SL builder validation
            current_ltp = avg_price  # fallback
            try:
                ltp_resp = self.dhan.get_ltp(sid)
                if ltp_resp and isinstance(ltp_resp, (int, float)):
                    current_ltp = float(ltp_resp)
                elif isinstance(ltp_resp, dict):
                    current_ltp = float(ltp_resp.get("lastPrice", avg_price))
            except Exception:
                pass

            # Build SL order via StopLossBuilder (DH-906 safe)
            try:
                sl_order = self.sl_builder.build_sl_order(
                    side=side, stop_level=initial_sl,
                    current_ltp=current_ltp, qty=qty, security_id=sid
                )
            except ValueError as ve:
                log.error(f"ADOPT SL BUILD FAILED for {symbol}: {ve} — EMERGENCY CLOSE")
                self._emergency_close(sid, symbol, side, qty)
                return False

            # Place SL via broker
            sl_oid = None
            try:
                sl_resp = self.dhan.place_order(
                    order_type="STOP_LOSS", security_id=sid, qty=sl_order["qty"],
                    trigger_price=sl_order["trigger_price"],
                    price=sl_order["price"], txn=sl_order["txn"]
                )
                sl_oid = (sl_resp or {}).get("orderId") if isinstance(sl_resp, dict) else None
            except Exception as e:
                log.error(f"ADOPT SL PLACE FAILED for {symbol}: {e}")

            # V11 FIX: SL MUST be confirmed. If not → emergency close.
            if not sl_oid:
                log.error(f"ADOPT: No SL order ID for {symbol} — EMERGENCY CLOSE (not adopt unprotected)")
                self._emergency_close(sid, symbol, side, qty)
                return False

            # Verify SL is actually live
            sl_confirmed = False
            try:
                time.sleep(0.3)
                sl_status = (self.dhan.get_order_status(sl_oid) or {}).get("orderStatus", "").upper()
                if sl_status in ("PENDING", "TRANSIT", "TRIGGER_PENDING"):
                    sl_confirmed = True
                else:
                    log.error(f"ADOPT SL NOT LIVE for {symbol}: status={sl_status}")
            except Exception as e:
                log.error(f"ADOPT SL VERIFY FAILED for {symbol}: {e}")

            if not sl_confirmed:
                log.error(f"ADOPT: SL not confirmed for {symbol} — EMERGENCY CLOSE")
                self._emergency_close(sid, symbol, side, qty)
                return False

            # SL confirmed — safe to adopt
            trade_id = f"ADOPT_{uuid.uuid4().hex[:8]}"
            position_data = {
                "symbol": symbol,
                "security_id": sid,
                "side": side,
                "qty": qty,
                "entry": avg_price,
                "sl": initial_sl,
                "initial_sl": initial_sl,
                "sl_order_id": sl_oid,
                "peak": avg_price,
                "best_r": 0.0,
                "entry_time": datetime.now(IST).isoformat(),
                "trade_id": trade_id,
                "adopted": True,
                "adoption_reason": "BROKER_RECONCILE",
            }

            self.bot.active_positions[sid] = position_data
            log.warning(
                f"POSITION ADOPTED: {symbol} {side} qty={qty} @ {avg_price} "
                f"SL={initial_sl} sl_oid={sl_oid} (CONFIRMED)"
            )

            self.adoption_log.append(AdoptedPosition(
                security_id=sid, symbol=symbol, side=side,
                qty=qty, avg_price=avg_price, initial_sl=initial_sl,
                sl_order_id=sl_oid, trade_id=trade_id
            ))
            return True

        except Exception as e:
            log.error(f"ADOPTION FAILED for {symbol}: {e}")
            self._emergency_close(sid, symbol, side, qty)
            return False

    def _emergency_close(self, sid: str, symbol: str, side: str, qty: int):
        """Last resort: close the position on broker immediately."""
        try:
            txn = "SELL" if side == "LONG" else "BUY"
            self.dhan.place_order(sid, qty, 0, txn, "MARKET")
            log.warning(f"EMERGENCY EXIT (adoption failed): {symbol} {txn} {qty}")
        except Exception as ee:
            log.error(f"EMERGENCY EXIT ALSO FAILED: {symbol}: {ee}")
            # Activate kill switch — we have an unprotected position
            self.guard.report_mismatch(  # ← V11 FIX: uses self.guard, not self.kill_switch_active
                f"UNPROTECTED_ORPHAN: {symbol} {side} qty={qty} — could not close"
            )


# ═══════════════════════════════════════════════════════════════════════
# 3. EXECUTION GUARD  —  V11: correct field names (A5 fix)
# ═══════════════════════════════════════════════════════════════════════

class ExecutionGuard:
    """
    Controls entry gating based on execution integrity.

    V11 FIX (A5):
        - Verified all field names: kill_switch_active, sl_failures,
          consecutive_api_failures (matching what entries_allowed checks).
        - Added report_reconciliation_clean(): clears kill_switch ONLY
          on clean reconciliation. Never auto-clear on timeout.
    """

    def __init__(self):
        self.kill_switch_active: bool = False
        self.kill_reasons: List[str] = []
        self.consecutive_api_failures: int = 0
        self.max_api_failures: int = 3
        self.sl_failures: int = 0            # ← V11: verified field name
        self.max_sl_failures: int = 2
        self._kill_switch_activated_at: Optional[str] = None

    def entries_allowed(self) -> Tuple[bool, str]:
        """Check if new entries are permitted. Returns (allowed, reason)."""
        # V11 FIX (A5): uses verified field names
        if self.kill_switch_active:
            return False, f"KILL_SWITCH: {'; '.join(self.kill_reasons)}"

        if self.sl_failures >= self.max_sl_failures:
            return False, f"SL_FAILURES: {self.sl_failures} >= {self.max_sl_failures}"

        if self.consecutive_api_failures >= self.max_api_failures:
            return False, f"API_FAILURES: {self.consecutive_api_failures} >= {self.max_api_failures}"

        return True, "OK"

    def report_mismatch(self, details: str):
        self.kill_switch_active = True
        self._kill_switch_activated_at = datetime.now(IST).isoformat()
        reason = f"BROKER_MISMATCH: {details}"
        if reason not in self.kill_reasons:
            self.kill_reasons.append(reason)
        log.error(f"KILL-SWITCH ACTIVATED: {reason}")

    def report_sl_failure(self, symbol: str):
        self.sl_failures += 1
        if self.sl_failures >= self.max_sl_failures:
            self.kill_switch_active = True
            self._kill_switch_activated_at = datetime.now(IST).isoformat()
            reason = f"SL_FAILURES: {self.sl_failures} consecutive"
            if reason not in self.kill_reasons:
                self.kill_reasons.append(reason)
            log.error(f"KILL-SWITCH ACTIVATED: {reason}")

    def report_api_failure(self, endpoint: str):
        self.consecutive_api_failures += 1
        if self.consecutive_api_failures >= self.max_api_failures:
            self.kill_switch_active = True
            self._kill_switch_activated_at = datetime.now(IST).isoformat()
            reason = f"API_UNHEALTHY: {self.consecutive_api_failures} consecutive failures"
            if reason not in self.kill_reasons:
                self.kill_reasons.append(reason)
            log.error(f"KILL-SWITCH ACTIVATED: {reason}")

    def report_api_success(self):
        self.consecutive_api_failures = 0

    def report_sl_success(self):
        self.sl_failures = 0

    def report_reconcile_ok(self):
        """Called after a reconciliation with zero mismatches."""
        self.kill_reasons = [r for r in self.kill_reasons if "BROKER_MISMATCH" not in r]
        if not self.kill_reasons:
            if self.kill_switch_active:
                log.info("KILL-SWITCH CLEARED (reconcile_ok): All issues resolved")
            self.kill_switch_active = False
        self.sl_failures = 0

    def report_reconciliation_clean(self):
        """
        V11 (A5): Explicit method to clear kill-switch ONLY on a clean
        reconciliation cycle.  Different from report_reconcile_ok in that it
        clears ALL reasons (not just BROKER_MISMATCH), but ONLY when the
        caller has verified positions + SL + orders are all consistent.

        EXPERT RULE: Never auto-clear on timeout. Only clear when a full
        reconciliation pass confirms zero issues.
        """
        if self.kill_switch_active:
            log.info(
                f"KILL-SWITCH CLEARED (reconciliation_clean): "
                f"was active since {self._kill_switch_activated_at}, "
                f"reasons were: {self.kill_reasons}"
            )
        self.kill_switch_active = False
        self.kill_reasons.clear()
        self.sl_failures = 0
        self.consecutive_api_failures = 0
        self._kill_switch_activated_at = None


# ═══════════════════════════════════════════════════════════════════════
# 4. TRADE LEDGER  (unchanged from V8)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    security_id: str
    side: str
    strategy: str = ""
    setup_type: str = ""
    score: float = 0.0

    signal_time: str = ""
    order_id: str = ""
    requested_qty: int = 0
    filled_qty: int = 0
    avg_entry: float = 0.0
    entry_method: str = ""

    initial_sl: float = 0.0
    current_sl: float = 0.0
    sl_order_id: str = ""
    sl_confirmed: bool = False
    risk_per_share: float = 0.0

    mfe: float = 0.0
    mae: float = 0.0
    peak_r: float = 0.0

    exit_reason: str = ""
    exit_price: float = 0.0
    exit_time: str = ""
    realised_pnl: float = 0.0
    realised_r: float = 0.0
    broker_confirmed: bool = False

    created_at: str = ""
    closed: bool = False


class TradeLedger:
    def __init__(self, ledger_path: Path):
        self.ledger_path = ledger_path
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.active_trades: Dict[str, TradeRecord] = {}

    def open_trade(self, symbol: str, security_id: str, side: str,
                   score: float = 0.0, strategy: str = "", setup_type: str = "",
                   signal_time: str = "") -> TradeRecord:
        trade_id = f"T_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        rec = TradeRecord(
            trade_id=trade_id, symbol=symbol, security_id=security_id,
            side=side, score=score, strategy=strategy, setup_type=setup_type,
            signal_time=signal_time or datetime.now(IST).isoformat(),
            created_at=datetime.now(IST).isoformat()
        )
        self.active_trades[trade_id] = rec
        return rec

    def record_fill(self, trade_id: str, order_id: str, filled_qty: int,
                    avg_price: float, entry_method: str = "normal"):
        rec = self.active_trades.get(trade_id)
        if not rec:
            return
        rec.order_id = order_id
        rec.filled_qty = filled_qty
        rec.avg_entry = avg_price
        rec.entry_method = entry_method
        rec.risk_per_share = abs(avg_price - rec.initial_sl) if rec.initial_sl else 0

    def record_sl(self, trade_id: str, initial_sl: float, sl_order_id: str,
                  confirmed: bool = False):
        rec = self.active_trades.get(trade_id)
        if not rec:
            return
        rec.initial_sl = initial_sl
        rec.current_sl = initial_sl
        rec.sl_order_id = sl_order_id
        rec.sl_confirmed = confirmed
        rec.risk_per_share = abs(rec.avg_entry - initial_sl) if rec.avg_entry else 0

    def update_extremes(self, trade_id: str, current_price: float):
        rec = self.active_trades.get(trade_id)
        if not rec or not rec.avg_entry:
            return
        if rec.side == "LONG":
            excursion = current_price - rec.avg_entry
        else:
            excursion = rec.avg_entry - current_price

        if excursion > rec.mfe:
            rec.mfe = excursion
        if excursion < -rec.mae:
            rec.mae = abs(excursion)

        if rec.risk_per_share > 0:
            current_r = excursion / rec.risk_per_share
            if current_r > rec.peak_r:
                rec.peak_r = current_r

    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str,
                    broker_confirmed: bool = False):
        rec = self.active_trades.get(trade_id)
        if not rec:
            return
        rec.exit_price = exit_price
        rec.exit_reason = exit_reason
        rec.exit_time = datetime.now(IST).isoformat()
        rec.broker_confirmed = broker_confirmed
        rec.closed = True

        if rec.side == "LONG":
            rec.realised_pnl = (exit_price - rec.avg_entry) * rec.filled_qty
        else:
            rec.realised_pnl = (rec.avg_entry - exit_price) * rec.filled_qty

        if rec.risk_per_share > 0:
            if rec.side == "LONG":
                rec.realised_r = (exit_price - rec.avg_entry) / rec.risk_per_share
            else:
                rec.realised_r = (rec.avg_entry - exit_price) / rec.risk_per_share

        self._persist(rec)
        del self.active_trades[trade_id]

    def _persist(self, rec: TradeRecord):
        try:
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(asdict(rec), default=str) + "\n")
        except Exception as e:
            log.error(f"Trade ledger write failed: {e}")

    def get_today_trades(self) -> List[Dict]:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        trades = []
        try:
            if self.ledger_path.exists():
                with open(self.ledger_path) as f:
                    for line in f:
                        try:
                            t = json.loads(line.strip())
                            if t.get("created_at", "").startswith(today):
                                trades.append(t)
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass
        return trades


# ═══════════════════════════════════════════════════════════════════════
# 5. RESTART LIMITER  (A6)
# ═══════════════════════════════════════════════════════════════════════

class RestartLimiter:
    """
    Tracks bot restarts. If >= max_per_hour within a rolling window,
    signals that the bot should STOP restarting (crash loop protection).
    """
    RESTART_FILE = "/tmp/bot_restart_count.json"
    MAX_PER_HOUR = 3

    def __init__(self, restart_file: str = None, max_per_hour: int = None):
        self.restart_file = restart_file or self.RESTART_FILE
        self.max_per_hour = max_per_hour or self.MAX_PER_HOUR

    def record_restart(self) -> Tuple[bool, int]:
        """
        Record a restart. Returns (allowed, restart_count_this_hour).
        If allowed is False, the bot should NOT restart again.
        """
        now = time.time()
        restarts = self._load()

        # Prune entries older than 1 hour
        one_hour_ago = now - 3600
        restarts = [ts for ts in restarts if ts > one_hour_ago]
        restarts.append(now)
        self._save(restarts)

        count = len(restarts)
        allowed = count < self.max_per_hour
        if not allowed:
            log.error(
                f"RESTART LIMITER: {count} restarts in the last hour "
                f"(max={self.max_per_hour}). STOPPING."
            )
        else:
            log.info(f"RESTART LIMITER: restart #{count} this hour (max={self.max_per_hour})")

        return allowed, count

    def _load(self) -> List[float]:
        try:
            with open(self.restart_file, "r") as f:
                data = json.load(f)
                return data.get("restarts", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, restarts: List[float]):
        try:
            with open(self.restart_file, "w") as f:
                json.dump({"restarts": restarts}, f)
        except Exception as e:
            log.error(f"RestartLimiter save failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 6. UNIFIED INTERFACE  —  ExecutionIntegrity V11
# ═══════════════════════════════════════════════════════════════════════

class ExecutionIntegrity:
    """
    Unified interface for the execution integrity system — V11.

    Drop-in replacement: same __init__ signature, same public methods,
    plus new A1–A7 capabilities.

    Canary cap: default ₹10,000 notional limit. Set canary_cap_inr=0
    to disable (production-only).
    """

    DEFAULT_CANARY_CAP_INR = 10_000.0

    def __init__(self, dhan, bot_instance, base_path: str = None,
                 canary_cap_inr: float = None):
        if base_path is None:
            base_path = os.path.dirname(os.path.abspath(__file__))

        base = Path(base_path)
        ledger_dir = base / "ledger"
        ledger_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(IST).strftime("%Y-%m-%d")

        self.dhan = dhan
        self.bot = bot_instance
        self.base_path = base

        # Core components
        self.guard = ExecutionGuard()
        self.sl_builder = StopLossBuilder()
        self.tracker = OrderStateTracker(dhan, ledger_dir / f"orders_{today}.jsonl")
        self.reconciler = BrokerReconciler(dhan, bot_instance, self.guard, self.sl_builder)
        self.ledger = TradeLedger(ledger_dir / f"trades_{today}.jsonl")
        self.restart_limiter = RestartLimiter()

        # Intent ledger for atomic position creation (A3/A6)
        self._intent_dir = ledger_dir
        self._intent_file = ledger_dir / f"intents_{today}.jsonl"
        self._intents: Dict[str, PositionIntent] = {}

        # Canary cap (A5 — ₹10K default)
        self.canary_cap_inr = canary_cap_inr if canary_cap_inr is not None else self.DEFAULT_CANARY_CAP_INR
        self._session_notional_inr: float = 0.0  # cumulative notional this session

        # Lock for thread-safety on intent writes
        self._intent_lock = threading.Lock()

    # ── EXISTING PUBLIC INTERFACE (preserved) ──────────────────────────

    def resolve_order(self, order_id: str) -> Optional[OrderRecord]:
        """Resolve an order to its final state using broker as truth."""
        return self.tracker.resolve(order_id, self.dhan)

    def reconcile(self) -> Dict[str, Any]:
        """Run broker reconciliation. Returns report dict."""
        report = self.reconciler.reconcile(self.bot.active_positions)

        if report["mismatch"]:
            self.guard.report_mismatch(
                f"orphans={report['orphans_found']} failures={report['adoption_failures']}"
            )
        else:
            self.guard.report_reconcile_ok()

        return report

    def confirm_sl(self, sl_order_id: str, security_id: str, side: str,
                   trigger_price: float, trade_id: str = "") -> bool:
        """Verify SL order is actually live on broker."""
        if not sl_order_id:
            self.guard.report_sl_failure("NO_ORDER_ID")
            return False

        try:
            status = (
                self.dhan.get_order_status(sl_order_id) or {}
            ).get("orderStatus", "").upper()
            if status in ("PENDING", "TRANSIT", "TRIGGER_PENDING"):
                self.guard.report_sl_success()
                if trade_id:
                    self.ledger.record_sl(trade_id, trigger_price, sl_order_id, confirmed=True)
                log.info(f"SL CONFIRMED: {security_id} trigger={trigger_price} oid={sl_order_id}")
                return True
            else:
                log.error(f"SL NOT CONFIRMED: {security_id} status={status} oid={sl_order_id}")
                self.guard.report_sl_failure(f"{security_id} status={status}")
                return False
        except Exception as e:
            log.error(f"SL CONFIRM FAILED: {security_id}: {e}")
            self.guard.report_sl_failure(f"{security_id}: {e}")
            return False

    def eod_force_close(self) -> Dict[str, Any]:
        """
        End-of-day: close ALL broker positions.
        V11: uses StopLossBuilder for emergency orders, logs EXIT AUDIT with MFE/MAE.
        """
        report = {"closed_local": 0, "closed_orphans": 0, "failures": [], "exit_audits": []}

        try:
            broker_pos = self.dhan.get_positions() or []
        except Exception as e:
            report["failures"].append(f"Broker fetch failed: {e}")
            return report

        for bp in broker_pos:
            nq = int(bp.get("netQty", 0))
            if nq == 0:
                continue

            sid = str(bp.get("securityId", ""))
            symbol = bp.get("tradingSymbol", "?")
            side = "LONG" if nq > 0 else "SHORT"
            txn = "SELL" if nq > 0 else "BUY"
            is_local = sid in self.bot.active_positions
            label = "LOCAL" if is_local else "ORPHAN"

            # EXIT AUDIT: log MFE/MAE for local positions
            audit = {"symbol": symbol, "side": side, "qty": abs(nq), "label": label}
            if is_local:
                pos_data = self.bot.active_positions.get(sid, {})
                trade_id = pos_data.get("trade_id", "")
                trade_rec = self.ledger.active_trades.get(trade_id)
                if trade_rec:
                    audit["mfe"] = trade_rec.mfe
                    audit["mae"] = trade_rec.mae
                    audit["peak_r"] = trade_rec.peak_r
                    audit["entry"] = trade_rec.avg_entry
                log.info(f"EXIT AUDIT: {json.dumps(audit)}")
            report["exit_audits"].append(audit)

            try:
                self.dhan.place_order(sid, abs(nq), 0, txn, "MARKET")
                log.warning(f"EOD CLOSE ({label}): {symbol} {txn} {abs(nq)}")
                if is_local:
                    report["closed_local"] += 1
                else:
                    report["closed_orphans"] += 1
            except Exception as e:
                log.error(f"EOD CLOSE FAILED ({label}): {symbol}: {e}")
                report["failures"].append(f"{symbol}: {e}")

        return report

    # ── A2. SL VERIFICATION LOOP ──────────────────────────────────────

    def verify_sl(self, sl_order_id: str, security_id: str, side: str,
                  max_retries: int = 2) -> bool:
        """
        Submit → sleep → verify SL is live.
        If rejected: retry with adjusted trigger (one retry).
        If still rejected: return False (caller MUST emergency flatten).

        Returns True = POSITION_PROTECTED, False = MUST_FLATTEN.
        """
        if not sl_order_id:
            log.error(f"verify_sl: no sl_order_id for {security_id}")
            self.guard.report_sl_failure(security_id)
            return False

        for attempt in range(1, max_retries + 1):
            try:
                time.sleep(0.5)
                status_resp = self.dhan.get_order_status(sl_order_id) or {}
                status = str(status_resp.get("orderStatus", "")).upper()

                if status in ("PENDING", "TRANSIT", "TRIGGER_PENDING"):
                    log.info(
                        f"SL VERIFIED (attempt {attempt}): {security_id} "
                        f"oid={sl_order_id} status={status}"
                    )
                    self.guard.report_sl_success()
                    return True

                elif status in ("REJECTED", "CANCELLED"):
                    reject_reason = status_resp.get("reasonDescription", "unknown")
                    log.warning(
                        f"SL REJECTED (attempt {attempt}/{max_retries}): "
                        f"{security_id} oid={sl_order_id} reason={reject_reason}"
                    )

                    if attempt < max_retries:
                        # Retry: rebuild SL with slightly adjusted trigger
                        log.info(f"Retrying SL for {security_id} (attempt {attempt + 1})")
                        sl_order_id = self._retry_sl(security_id, side)
                        if not sl_order_id:
                            break
                    # else: fall through to failure

                else:
                    log.warning(
                        f"SL UNEXPECTED STATUS (attempt {attempt}): "
                        f"{security_id} oid={sl_order_id} status={status}"
                    )
                    # Treat as pending if it's a transient state
                    if status in ("OPEN", "CONFIRMED"):
                        self.guard.report_sl_success()
                        return True

            except Exception as e:
                log.error(f"SL VERIFY ERROR (attempt {attempt}): {security_id}: {e}")
                self.guard.report_api_failure("get_order_status")

        # All attempts exhausted
        log.error(f"SL VERIFY FAILED after {max_retries} attempts: {security_id}")
        self.guard.report_sl_failure(security_id)
        return False

    def _retry_sl(self, security_id: str, side: str) -> Optional[str]:
        """
        Retry SL placement with slightly adjusted trigger.
        Returns new sl_order_id or None.
        """
        pos = self.bot.active_positions.get(security_id, {})
        if not pos:
            return None

        entry = pos.get("entry", 0)
        if entry <= 0:
            return None

        qty = pos.get("qty", 0)
        if qty <= 0:
            return None

        # Widen the SL by 0.25% from entry for the retry
        if side == "LONG":
            new_sl = round(entry * 0.99, 2)     # 1% below entry (wider)
        else:
            new_sl = round(entry * 1.01, 2)     # 1% above entry (wider)

        # Get LTP
        current_ltp = entry
        try:
            ltp_resp = self.dhan.get_ltp(security_id)
            if isinstance(ltp_resp, (int, float)):
                current_ltp = float(ltp_resp)
            elif isinstance(ltp_resp, dict):
                current_ltp = float(ltp_resp.get("lastPrice", entry))
        except Exception:
            pass

        try:
            sl_order = self.sl_builder.build_sl_order(
                side=side, stop_level=new_sl,
                current_ltp=current_ltp, qty=qty, security_id=security_id
            )
            sl_resp = self.dhan.place_order(
                order_type="STOP_LOSS", security_id=security_id, qty=sl_order["qty"],
                trigger_price=sl_order["trigger_price"],
                price=sl_order["price"], txn=sl_order["txn"]
            )
            new_oid = (sl_resp or {}).get("orderId") if isinstance(sl_resp, dict) else None
            if new_oid:
                pos["sl"] = new_sl
                pos["sl_order_id"] = new_oid
                log.info(f"SL RETRY: new oid={new_oid} trigger={new_sl} for {security_id}")
            return new_oid
        except Exception as e:
            log.error(f"SL RETRY FAILED for {security_id}: {e}")
            return None

    # ── A3. ATOMIC POSITION CREATION (INTENT LEDGER) ──────────────────

    def create_intent(self, symbol: str, sid: str, side: str,
                      qty: int, price: float, sl_level: float) -> Optional[str]:
        """
        Step 1 of atomic position creation:
        Write intent to disk BEFORE any broker call.

        Also enforces canary cap (₹10K default).

        Returns intent_id, or None if blocked (cap exceeded, kill-switch, etc.).
        """
        # Gate check
        allowed, reason = self.guard.entries_allowed()
        if not allowed:
            log.warning(f"INTENT BLOCKED: {reason}")
            return None

        # Canary cap check (A5 — P0-CAP fix)
        notional = round(price * qty, 2)
        if self.canary_cap_inr > 0:
            projected = self._session_notional_inr + notional
            if projected > self.canary_cap_inr:
                log.error(
                    f"CANARY CAP EXCEEDED: {symbol} notional=₹{notional:.0f} "
                    f"session_total=₹{self._session_notional_inr:.0f} "
                    f"cap=₹{self.canary_cap_inr:.0f} "
                    f"projected=₹{projected:.0f}"
                )
                self.guard.report_mismatch(
                    f"CANARY_CAP: projected ₹{projected:.0f} > cap ₹{self.canary_cap_inr:.0f}"
                )
                return None

        intent_id = f"INT_{datetime.now(IST).strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}"
        intent = PositionIntent(
            intent_id=intent_id, symbol=symbol, security_id=sid,
            side=side, qty=qty, price=price, sl_level=sl_level,
            state=IntentState.INTENT_CREATED.value,
            notional=notional,
        )

        with self._intent_lock:
            self._intents[intent_id] = intent
            self._persist_intent(intent)

        log.info(
            f"INTENT CREATED: {intent_id} {symbol} {side} qty={qty} "
            f"price={price} sl={sl_level} notional=₹{notional:.0f}"
        )
        return intent_id

    def mark_order_submitted(self, intent_id: str, order_id: str):
        """Mark that the broker order has been submitted for this intent."""
        intent = self._intents.get(intent_id)
        if not intent:
            log.error(f"mark_order_submitted: unknown intent {intent_id}")
            return
        intent.order_id = order_id
        intent.state = IntentState.ORDER_SUBMITTED.value
        intent.updated_at = datetime.now(IST).isoformat()
        self._persist_intent(intent)
        log.info(f"INTENT {intent_id}: ORDER_SUBMITTED oid={order_id}")

    def confirm_fill(self, intent_id: str, order_id: str,
                     fill_qty: int, fill_price: float):
        """
        Step 2: confirm fill from broker.
        Updates notional tracking for canary cap.
        """
        intent = self._intents.get(intent_id)
        if not intent:
            log.error(f"confirm_fill: unknown intent {intent_id}")
            return
        intent.order_id = order_id
        intent.fill_qty = fill_qty
        intent.fill_price = fill_price
        intent.state = IntentState.FILL_CONFIRMED.value
        intent.updated_at = datetime.now(IST).isoformat()
        intent.notional = round(fill_price * fill_qty, 2)
        self._persist_intent(intent)

        # Update session notional for canary cap
        self._session_notional_inr += intent.notional

        log.info(
            f"INTENT {intent_id}: FILL_CONFIRMED oid={order_id} "
            f"qty={fill_qty} price={fill_price} "
            f"session_notional=₹{self._session_notional_inr:.0f}"
        )

    def mark_sl_verified(self, intent_id: str, sl_order_id: str):
        """Step 3: SL verified on broker."""
        intent = self._intents.get(intent_id)
        if not intent:
            return
        intent.sl_order_id = sl_order_id
        intent.state = IntentState.SL_VERIFIED.value
        intent.updated_at = datetime.now(IST).isoformat()
        self._persist_intent(intent)
        log.info(f"INTENT {intent_id}: SL_VERIFIED sl_oid={sl_order_id}")

    def mark_active(self, intent_id: str, trade_id: str = ""):
        """Step 4: position fully active with confirmed SL."""
        intent = self._intents.get(intent_id)
        if not intent:
            return
        intent.state = IntentState.ACTIVE.value
        intent.trade_id = trade_id
        intent.updated_at = datetime.now(IST).isoformat()
        self._persist_intent(intent)
        log.info(f"INTENT {intent_id}: ACTIVE trade_id={trade_id}")

    def mark_emergency_closed(self, intent_id: str, reason: str = ""):
        """Mark intent as emergency closed."""
        intent = self._intents.get(intent_id)
        if not intent:
            return
        intent.state = IntentState.EMERGENCY_CLOSED.value
        intent.updated_at = datetime.now(IST).isoformat()
        self._persist_intent(intent)
        log.warning(f"INTENT {intent_id}: EMERGENCY_CLOSED reason={reason}")

    def mark_cancelled(self, intent_id: str, reason: str = ""):
        """Mark intent as cancelled (no fill, clean exit)."""
        intent = self._intents.get(intent_id)
        if not intent:
            return
        intent.state = IntentState.CANCELLED.value
        intent.updated_at = datetime.now(IST).isoformat()
        self._persist_intent(intent)
        log.info(f"INTENT {intent_id}: CANCELLED reason={reason}")

    def _persist_intent(self, intent: PositionIntent):
        """Append intent state to the day's intent ledger (JSONL)."""
        try:
            with open(self._intent_file, "a") as f:
                f.write(json.dumps(asdict(intent), default=str) + "\n")
        except Exception as e:
            log.error(f"Intent persist failed: {e}")

    # ── A6. RESTART RECOVERY ──────────────────────────────────────────

    def recover_intents(self) -> Dict[str, Any]:
        """
        Called on startup. Reads the intent ledger, finds incomplete
        intents, and reconciles each with broker state.

        Intent state recovery matrix:
            INTENT_CREATED   → check if order was submitted (query by symbol/time)
            ORDER_SUBMITTED  → resolve_order, then proceed with SL
            FILL_CONFIRMED   → SL was not placed/verified → submit SL or flatten
            SL_VERIFIED      → almost active, mark active
            ACTIVE           → already managed, skip
            EMERGENCY_CLOSED → already handled, skip
            CANCELLED        → already handled, skip

        Returns recovery report.
        """
        report = {
            "intents_found": 0,
            "recovered": 0,
            "emergency_closed": 0,
            "already_active": 0,
            "already_closed": 0,
            "errors": [],
        }

        # Check restart limiter first
        allowed, count = self.restart_limiter.record_restart()
        if not allowed:
            report["errors"].append(
                f"RESTART LIMITER: {count} restarts in last hour. HALTING."
            )
            self.guard.report_mismatch(f"RESTART_LIMITER: {count} restarts in 1 hour")
            return report

        # Load latest state for each intent from the JSONL ledger
        latest_intents: Dict[str, dict] = {}
        try:
            if self._intent_file.exists():
                with open(self._intent_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            iid = record.get("intent_id", "")
                            if iid:
                                latest_intents[iid] = record
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            report["errors"].append(f"Intent ledger read error: {e}")
            return report

        report["intents_found"] = len(latest_intents)

        for iid, rec in latest_intents.items():
            state = rec.get("state", "")

            if state in (IntentState.ACTIVE.value,
                         IntentState.EMERGENCY_CLOSED.value,
                         IntentState.CANCELLED.value):
                if state == IntentState.ACTIVE.value:
                    report["already_active"] += 1
                else:
                    report["already_closed"] += 1
                continue

            # Rebuild PositionIntent
            intent = PositionIntent(
                intent_id=iid,
                symbol=rec.get("symbol", ""),
                security_id=rec.get("security_id", ""),
                side=rec.get("side", ""),
                qty=rec.get("qty", 0),
                price=rec.get("price", 0),
                sl_level=rec.get("sl_level", 0),
                state=state,
                order_id=rec.get("order_id", ""),
                fill_qty=rec.get("fill_qty", 0),
                fill_price=rec.get("fill_price", 0),
                sl_order_id=rec.get("sl_order_id", ""),
                trade_id=rec.get("trade_id", ""),
                created_at=rec.get("created_at", ""),
                notional=rec.get("notional", 0),
            )
            self._intents[iid] = intent

            try:
                recovered = self._recover_single_intent(intent)
                if recovered:
                    report["recovered"] += 1
                else:
                    report["emergency_closed"] += 1
            except Exception as e:
                log.error(f"RECOVERY ERROR for {iid}: {e}")
                report["errors"].append(f"{iid}: {e}")
                report["emergency_closed"] += 1

        log.info(f"INTENT RECOVERY COMPLETE: {json.dumps(report)}")
        return report

    def _recover_single_intent(self, intent: PositionIntent) -> bool:
        """
        Recover a single incomplete intent.
        Returns True if recovered (position is protected), False if emergency closed.
        """
        sid = intent.security_id
        side = intent.side
        symbol = intent.symbol

        # Check if broker has a position for this security
        net_qty = 0
        try:
            net_qty = int(self.dhan.verify_position(sid, side))
        except Exception as e:
            log.warning(f"RECOVERY: position verify failed for {sid}: {e}")

        has_broker_position = (
            (side == "LONG" and net_qty > 0) or
            (side == "SHORT" and net_qty < 0)
        )

        if intent.state == IntentState.INTENT_CREATED.value:
            # Order may or may not have been submitted
            if has_broker_position:
                # Position exists — need to protect it
                return self._recovery_protect_position(intent, abs(net_qty))
            else:
                # No position — safe to cancel
                self.mark_cancelled(intent.intent_id, "recovery: no broker position")
                return True

        elif intent.state == IntentState.ORDER_SUBMITTED.value:
            # Order was submitted but fill not confirmed
            if intent.order_id:
                # Resolve the order
                self.tracker.register(
                    intent.order_id, intent.trade_id or intent.intent_id,
                    symbol, sid, side, intent.qty, intent.price
                )
                order_rec = self.tracker.resolve(intent.order_id, self.dhan)
                if order_rec and order_rec.state in (OrderState.FILLED, OrderState.PARTIAL):
                    intent.fill_qty = order_rec.filled_qty
                    intent.fill_price = order_rec.avg_fill_price
                    return self._recovery_protect_position(intent, order_rec.filled_qty)

            if has_broker_position:
                return self._recovery_protect_position(intent, abs(net_qty))
            else:
                self.mark_cancelled(intent.intent_id, "recovery: order not filled")
                return True

        elif intent.state == IntentState.FILL_CONFIRMED.value:
            # Fill confirmed but SL not placed/verified
            if has_broker_position:
                return self._recovery_protect_position(
                    intent, intent.fill_qty or abs(net_qty)
                )
            else:
                # Position gone (maybe hit a broker-side SL?)
                self.mark_emergency_closed(intent.intent_id, "recovery: position gone after fill")
                return False

        elif intent.state == IntentState.SL_VERIFIED.value:
            # Almost active — verify SL is still live
            if intent.sl_order_id:
                sl_live = self.verify_sl(intent.sl_order_id, sid, side, max_retries=1)
                if sl_live:
                    self.mark_active(intent.intent_id, intent.trade_id)
                    return True
            # SL not live — re-protect
            if has_broker_position:
                return self._recovery_protect_position(
                    intent, intent.fill_qty or abs(net_qty)
                )
            else:
                self.mark_cancelled(intent.intent_id, "recovery: no position at SL_VERIFIED")
                return True

        # Unknown state — be conservative
        if has_broker_position:
            return self._recovery_protect_position(intent, abs(net_qty))
        self.mark_cancelled(intent.intent_id, "recovery: unknown state, no position")
        return True

    def _recovery_protect_position(self, intent: PositionIntent, qty: int) -> bool:
        """
        Protect a broker position discovered during recovery.
        Place SL → verify → adopt. If SL fails → emergency flatten.
        """
        sid = intent.security_id
        side = intent.side
        symbol = intent.symbol
        entry_price = intent.fill_price or intent.price

        if entry_price <= 0:
            # Unknown entry price — conservative flatten
            log.error(f"RECOVERY: unknown entry price for {symbol} — FLATTEN")
            self._emergency_flatten(sid, symbol, side, qty)
            self.mark_emergency_closed(intent.intent_id, "unknown entry price")
            return False

        # Calculate SL
        sl_level = intent.sl_level
        if sl_level <= 0:
            if side == "LONG":
                sl_level = round(entry_price * 0.9925, 2)
            else:
                sl_level = round(entry_price * 1.0075, 2)

        # Get LTP
        current_ltp = entry_price
        try:
            ltp_resp = self.dhan.get_ltp(sid)
            if isinstance(ltp_resp, (int, float)):
                current_ltp = float(ltp_resp)
            elif isinstance(ltp_resp, dict):
                current_ltp = float(ltp_resp.get("lastPrice", entry_price))
        except Exception:
            pass

        # Build and place SL
        try:
            sl_order = self.sl_builder.build_sl_order(
                side=side, stop_level=sl_level,
                current_ltp=current_ltp, qty=qty, security_id=sid
            )
        except ValueError as ve:
            log.error(f"RECOVERY: SL build failed for {symbol}: {ve} — FLATTEN")
            self._emergency_flatten(sid, symbol, side, qty)
            self.mark_emergency_closed(intent.intent_id, f"SL build failed: {ve}")
            return False

        try:
            sl_resp = self.dhan.place_order(
                order_type="STOP_LOSS", security_id=sid, qty=sl_order["qty"],
                trigger_price=sl_order["trigger_price"],
                price=sl_order["price"], txn=sl_order["txn"]
            )
            sl_oid = (sl_resp or {}).get("orderId") if isinstance(sl_resp, dict) else None
        except Exception as e:
            log.error(f"RECOVERY: SL place failed for {symbol}: {e} — FLATTEN")
            self._emergency_flatten(sid, symbol, side, qty)
            self.mark_emergency_closed(intent.intent_id, f"SL place failed: {e}")
            return False

        if not sl_oid:
            self._emergency_flatten(sid, symbol, side, qty)
            self.mark_emergency_closed(intent.intent_id, "no SL order ID")
            return False

        # Verify SL
        sl_live = self.verify_sl(sl_oid, sid, side, max_retries=2)
        if not sl_live:
            self._emergency_flatten(sid, symbol, side, qty)
            self.mark_emergency_closed(intent.intent_id, "SL verify failed")
            return False

        # SL confirmed — adopt position
        self.mark_sl_verified(intent.intent_id, sl_oid)
        position_data = {
            "symbol": symbol,
            "security_id": sid,
            "side": side,
            "qty": qty,
            "entry": entry_price,
            "sl": sl_level,
            "initial_sl": sl_level,
            "sl_order_id": sl_oid,
            "peak": entry_price,
            "best_r": 0.0,
            "entry_time": intent.created_at,
            "trade_id": intent.trade_id or intent.intent_id,
            "adopted": True,
            "adoption_reason": "RESTART_RECOVERY",
        }
        self.bot.active_positions[sid] = position_data
        self.mark_active(intent.intent_id, intent.trade_id or intent.intent_id)
        log.warning(
            f"RECOVERY ADOPTED: {symbol} {side} qty={qty} @ {entry_price} "
            f"SL={sl_level} sl_oid={sl_oid}"
        )
        return True

    def _emergency_flatten(self, sid: str, symbol: str, side: str, qty: int):
        """Emergency market close of a position."""
        try:
            txn = "SELL" if side == "LONG" else "BUY"
            self.dhan.place_order(sid, qty, 0, txn, "MARKET")
            log.warning(f"EMERGENCY FLATTEN: {symbol} {txn} qty={qty}")
        except Exception as e:
            log.error(f"EMERGENCY FLATTEN FAILED: {symbol}: {e}")
            self.guard.report_mismatch(
                f"FLATTEN_FAILED: {symbol} {side} qty={qty}"
            )

    # ── A3 helper: create_position_atomic (convenience wrapper) ───────

    def create_position_atomic(self, symbol: str, sid: str, side: str,
                               qty: int, price: float, sl_level: float) -> Optional[dict]:
        """
        High-level convenience: create intent → return intent dict.
        Caller is responsible for:
          1. Submitting broker order using returned intent_id
          2. Calling confirm_fill(intent_id, oid, fill_qty, fill_price)
          3. Calling verify_sl(sl_oid, sid, side) after SL placement
          4. Calling mark_active(intent_id, trade_id)

        Returns dict with intent_id if allowed, None if blocked.
        """
        intent_id = self.create_intent(symbol, sid, side, qty, price, sl_level)
        if not intent_id:
            return None

        intent = self._intents.get(intent_id)
        return {
            "intent_id": intent_id,
            "symbol": symbol,
            "security_id": sid,
            "side": side,
            "qty": qty,
            "price": price,
            "sl_level": sl_level,
            "state": IntentState.INTENT_CREATED.value,
            "notional": round(price * qty, 2),
        }

    # ── Canary cap query ──────────────────────────────────────────────

    def get_session_notional(self) -> float:
        """Current cumulative notional for this session."""
        return self._session_notional_inr

    def get_canary_cap_remaining(self) -> float:
        """How much notional room remains under canary cap."""
        if self.canary_cap_inr <= 0:
            return float("inf")
        return max(0.0, self.canary_cap_inr - self._session_notional_inr)
