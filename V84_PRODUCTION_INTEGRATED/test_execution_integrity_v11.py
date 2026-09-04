"""
Test Suite for Execution Integrity V11
========================================
15 emergency + correctness scenarios — all with mock Dhan gateway.
No real API calls.

Run:
    python -m pytest test_execution_integrity_v11.py -v
    python -m unittest test_execution_integrity_v11 -v

Coverage:
    1.  Process crash after entry, before SL → restart detects + protects or flattens
    2.  Restart with broker position, absent local state → adopt with SL
    3.  SL submission rejected (DH-906) → emergency flatten
    4.  Partial fill → correct quantity tracking
    5.  Network failure during SL verify → retry then flatten
    6.  Repeated restart (3rd in 1 hour) → stop restarting
    7.  Kill-switch active → no new entries, keep monitoring existing
    8.  Orphan position with unknown entry price → conservative flatten
    9.  StopLossBuilder LONG: triggerPrice < LTP, price = trigger * 0.95
    10. StopLossBuilder SHORT: triggerPrice > LTP, price = trigger * 1.05
    11. StopLossBuilder validation: trigger on wrong side of LTP → ValueError
    12. entries_allowed uses correct field names
    13. report_reconciliation_clean clears kill_switch
    14. Intent ledger persistence and recovery
    15. Canary caps enforced (₹10K notional limit)
"""

import unittest
import json
import os
import sys
import shutil
import tempfile
import time
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path
from dataclasses import asdict

# ── Adjust path so we can import the module under test ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from execution_integrity_v11 import (
    StopLossBuilder,
    ExecutionIntegrity,
    ExecutionGuard,
    BrokerReconciler,
    OrderState,
    OrderStateTracker,
    IntentState,
    PositionIntent,
    RestartLimiter,
    TradeLedger,
)


# ═══════════════════════════════════════════════════════════════════════
# REUSABLE MOCK GATEWAY
# ═══════════════════════════════════════════════════════════════════════

class MockDhanGateway:
    """
    Configurable mock for the Dhan broker gateway.

    Usage:
        gw = MockDhanGateway()
        gw.set_order_status("oid1", {"orderStatus": "TRADED", "filledQty": 10})
        gw.set_positions([{"securityId": "1234", "netQty": 10, ...}])
        gw.set_ltp("1234", 500.0)

        # For SL placement:
        gw.sl_response = {"orderId": "sl_001"}
        # or to simulate rejection:
        gw.sl_response = None
    """

    def __init__(self):
        self._order_statuses = {}
        self._positions = []
        self._ltps = {}
        self._position_qtys = {}

        self.sl_response = {"orderId": "sl_mock_001"}
        self.sl_placement_calls = []
        self.market_order_calls = []
        self.market_order_response = {"orderId": "mkt_mock_001"}
        self.cancel_calls = []
        self.get_order_status_calls = []

        self.raise_on_get_order_status = False
        self.raise_on_get_positions = False
        self.raise_on_place_sl = False
        self.raise_on_place_order = False
        self.raise_on_get_ltp = False
        self.raise_on_verify_position = False

    def set_order_status(self, order_id, status_dict):
        self._order_statuses[order_id] = status_dict

    def set_positions(self, positions):
        self._positions = positions

    def set_ltp(self, sid, price):
        self._ltps[str(sid)] = price

    def set_position_qty(self, sid, side, qty):
        self._position_qtys[(str(sid), side)] = qty

    def get_order_status(self, order_id):
        self.get_order_status_calls.append(order_id)
        if self.raise_on_get_order_status:
            raise ConnectionError("Network failure (mock)")
        return self._order_statuses.get(order_id, {})

    def get_positions(self):
        if self.raise_on_get_positions:
            raise ConnectionError("Network failure (mock)")
        return self._positions

    def get_ltp(self, sid):
        if self.raise_on_get_ltp:
            raise ConnectionError("LTP fetch failed (mock)")
        price = self._ltps.get(str(sid))
        if price is not None:
            return {"lastPrice": price}
        return {"lastPrice": 0}

    def verify_position(self, sid, side):
        if self.raise_on_verify_position:
            raise ConnectionError("Position verify failed (mock)")
        return self._position_qtys.get((str(sid), side), 0)

    def place_sl_order(self, security_id, qty, trigger_price, price, txn):
        self.sl_placement_calls.append({
            "security_id": security_id, "qty": qty,
            "trigger_price": trigger_price, "price": price, "txn": txn,
        })
        if self.raise_on_place_sl:
            raise ConnectionError("SL placement network error (mock)")
        return self.sl_response

    def place_hard_sl(self, sid, qty, side, trigger_price):
        """Legacy API compat."""
        txn = "SELL" if side == "LONG" else "BUY"
        return self.place_sl_order(sid, qty, trigger_price, trigger_price, txn)

    def place_order(self, sid, qty, price, txn, order_type):
        self.market_order_calls.append({
            "sid": sid, "qty": qty, "price": price, "txn": txn, "order_type": order_type,
        })
        if self.raise_on_place_order:
            raise ConnectionError("Order placement failed (mock)")
        return self.market_order_response

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)


class MockBot:
    """Minimal bot stub with active_positions dict."""
    def __init__(self):
        self.active_positions = {}


# ═══════════════════════════════════════════════════════════════════════
# HELPER: create a fresh ExecutionIntegrity in a temp dir
# ═══════════════════════════════════════════════════════════════════════

class V11TestBase(unittest.TestCase):
    """Base class that sets up temp dirs and EI instance."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ei_v11_test_")
        self.gw = MockDhanGateway()
        self.bot = MockBot()
        self.ei = ExecutionIntegrity(
            dhan=self.gw,
            bot_instance=self.bot,
            base_path=self.tmpdir,
            canary_cap_inr=10_000.0,
        )
        # Override restart limiter to use writable temp dir
        self.restart_file = os.path.join(self.tmpdir, "restart_count.json")
        self.ei.restart_limiter = RestartLimiter(restart_file=self.restart_file)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
# TEST 1: Process crash after entry, before SL
# ═══════════════════════════════════════════════════════════════════════

class Test01_CrashAfterEntryBeforeSL(V11TestBase):
    """
    Scenario: Bot placed entry order, got fill, crashed before SL.
    On restart, intent ledger shows FILL_CONFIRMED.
    Broker still has the position.
    Recovery should place SL or flatten.
    """

    def test_recovery_places_sl_or_flattens(self):
        intent = {
            "intent_id": "INT_test01", "symbol": "RELIANCE",
            "security_id": "1234", "side": "LONG", "qty": 10,
            "price": 2500.0, "sl_level": 2480.0,
            "state": IntentState.FILL_CONFIRMED.value,
            "order_id": "entry_oid_001", "fill_qty": 10,
            "fill_price": 2500.0, "sl_order_id": "", "trade_id": "",
            "created_at": "", "updated_at": "", "notional": 25000.0,
        }
        with open(self.ei._intent_file, "w") as f:
            f.write(json.dumps(intent) + "\n")

        self.gw.set_position_qty("1234", "LONG", 10)
        self.gw.set_ltp("1234", 2505.0)
        self.gw.sl_response = {"orderId": "sl_recovery_001"}
        self.gw.set_order_status("sl_recovery_001", {"orderStatus": "PENDING"})

        report = self.ei.recover_intents()
        self.assertEqual(report["recovered"], 1, f"Expected 1 recovery, got {report}")
        self.assertIn("1234", self.bot.active_positions)
        pos = self.bot.active_positions["1234"]
        self.assertIsNotNone(pos.get("sl_order_id"))


# ═══════════════════════════════════════════════════════════════════════
# TEST 2: Restart with broker position, absent local state
# ═══════════════════════════════════════════════════════════════════════

class Test02_BrokerPositionAbsentLocal(V11TestBase):
    """
    Scenario: Bot restarts. Broker has a position. Local state is empty.
    Reconcile should adopt with confirmed SL.
    """

    def test_reconcile_adopts_with_sl(self):
        self.gw.set_positions([{
            "securityId": "5678", "tradingSymbol": "INFY",
            "netQty": 20, "costPrice": 1800.0,
        }])
        self.gw.set_ltp("5678", 1805.0)
        self.gw.sl_response = {"orderId": "sl_adopt_001"}
        self.gw.set_order_status("sl_adopt_001", {"orderStatus": "PENDING"})

        report = self.ei.reconcile()
        self.assertEqual(report["orphans_found"], 1)
        self.assertEqual(report["orphans_adopted"], 1)
        self.assertIn("5678", self.bot.active_positions)
        pos = self.bot.active_positions["5678"]
        self.assertTrue(pos["adopted"])
        self.assertEqual(pos["sl_order_id"], "sl_adopt_001")


# ═══════════════════════════════════════════════════════════════════════
# TEST 3: SL submission rejected (DH-906) → emergency flatten
# ═══════════════════════════════════════════════════════════════════════

class Test03_SLRejectedDH906(V11TestBase):
    """
    Scenario: SL order is submitted but Dhan rejects it (DH-906).
    verify_sl should return False, and caller flattens.
    """

    def test_sl_rejected_returns_false(self):
        self.gw.set_order_status("sl_bad_001", {
            "orderStatus": "REJECTED",
            "reasonDescription": "Price should be greater than Trigger Price"
        })
        result = self.ei.verify_sl("sl_bad_001", "9999", "SHORT", max_retries=1)
        self.assertFalse(result)
        self.assertGreater(self.ei.guard.sl_failures, 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 4: Partial fill → correct quantity tracking
# ═══════════════════════════════════════════════════════════════════════

class Test04_PartialFill(V11TestBase):
    """
    Scenario: Order was for 50 shares, only 30 filled before cancel.
    """

    def test_partial_fill_qty_correct(self):
        self.ei.tracker.register("oid_partial", "T_test04", "SBIN", "3333", "LONG", 50, 600.0)
        self.gw.set_order_status("oid_partial", {
            "orderStatus": "CANCELLED", "filledQty": 30, "averageTradedPrice": 601.5,
        })
        rec = self.ei.resolve_order("oid_partial")
        self.assertEqual(rec.state, OrderState.PARTIAL)
        self.assertEqual(rec.filled_qty, 30)
        self.assertAlmostEqual(rec.avg_fill_price, 601.5)
        self.assertEqual(rec.resolution_method, "partial_before_cancel")


# ═══════════════════════════════════════════════════════════════════════
# TEST 5: Network failure during SL verify → retry then flatten
# ═══════════════════════════════════════════════════════════════════════

class Test05_NetworkFailureSLVerify(V11TestBase):
    """
    Scenario: Network fails every time we try to verify SL status.
    """

    def test_network_failure_returns_false(self):
        self.gw.raise_on_get_order_status = True
        result = self.ei.verify_sl("sl_net_001", "4444", "LONG", max_retries=2)
        self.assertFalse(result)
        self.assertGreater(self.ei.guard.consecutive_api_failures, 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 6: Repeated restart (3rd in 1 hour) → stop restarting
# ═══════════════════════════════════════════════════════════════════════

class Test06_RestartLimiter(V11TestBase):
    """
    Scenario: Bot has restarted 3+ times in the last hour.
    """

    def test_third_restart_blocked(self):
        limiter = RestartLimiter(restart_file=self.restart_file, max_per_hour=3)
        ok1, c1 = limiter.record_restart()
        self.assertTrue(ok1); self.assertEqual(c1, 1)
        ok2, c2 = limiter.record_restart()
        self.assertTrue(ok2); self.assertEqual(c2, 2)
        ok3, c3 = limiter.record_restart()
        self.assertFalse(ok3, "3rd restart should be blocked (max_per_hour=3)")
        self.assertEqual(c3, 3)

    def test_restart_limiter_blocks_recovery(self):
        now = time.time()
        with open(self.restart_file, "w") as f:
            json.dump({"restarts": [now - 100, now - 50, now - 10]}, f)

        self.ei.restart_limiter = RestartLimiter(
            restart_file=self.restart_file, max_per_hour=3
        )
        report = self.ei.recover_intents()
        self.assertTrue(len(report["errors"]) > 0)
        self.assertTrue(self.ei.guard.kill_switch_active)


# ═══════════════════════════════════════════════════════════════════════
# TEST 7: Kill-switch active → no new entries
# ═══════════════════════════════════════════════════════════════════════

class Test07_KillSwitchBlocksEntries(V11TestBase):

    def test_kill_switch_blocks_intent(self):
        self.ei.guard.kill_switch_active = True
        self.ei.guard.kill_reasons = ["TEST_KILL"]
        result = self.ei.create_intent("HDFC", "7777", "LONG", 5, 2800.0, 2780.0)
        self.assertIsNone(result)

    def test_kill_switch_entries_allowed_tuple(self):
        self.ei.guard.kill_switch_active = True
        self.ei.guard.kill_reasons = ["MANUAL_KILL"]
        allowed, reason = self.ei.guard.entries_allowed()
        self.assertFalse(allowed)
        self.assertIn("KILL_SWITCH", reason)


# ═══════════════════════════════════════════════════════════════════════
# TEST 8: Orphan position with unknown entry price → flatten
# ═══════════════════════════════════════════════════════════════════════

class Test08_OrphanUnknownPrice(V11TestBase):

    def test_unknown_price_flattens(self):
        intent = {
            "intent_id": "INT_test08", "symbol": "TATAMOTORS",
            "security_id": "8888", "side": "LONG", "qty": 15,
            "price": 0.0, "sl_level": 0.0,
            "state": IntentState.ORDER_SUBMITTED.value,
            "order_id": "oid_unknown", "fill_qty": 0, "fill_price": 0.0,
            "sl_order_id": "", "trade_id": "",
            "created_at": "", "updated_at": "", "notional": 0,
        }
        with open(self.ei._intent_file, "w") as f:
            f.write(json.dumps(intent) + "\n")

        self.gw.set_position_qty("8888", "LONG", 15)
        self.gw.set_order_status("oid_unknown", {
            "orderStatus": "TRADED", "filledQty": 15, "averageTradedPrice": 0,
        })

        report = self.ei.recover_intents()
        self.assertGreater(report["emergency_closed"], 0)
        self.assertTrue(len(self.gw.market_order_calls) > 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 9: StopLossBuilder LONG correctness
# ═══════════════════════════════════════════════════════════════════════

class Test09_SLBuilderLong(unittest.TestCase):
    def test_long_sl_values(self):
        order = StopLossBuilder.build_sl_order(
            side="LONG", stop_level=990.0, current_ltp=1000.0,
            qty=10, security_id="SID_LONG"
        )
        self.assertEqual(order["txn"], "SELL")
        self.assertEqual(order["trigger_price"], 990.0)
        self.assertAlmostEqual(order["price"], round(990.0 * 0.95, 2))
        self.assertLess(order["price"], order["trigger_price"])
        self.assertEqual(order["qty"], 10)
        self.assertEqual(order["security_id"], "SID_LONG")

    def test_long_sl_trigger_below_ltp(self):
        order = StopLossBuilder.build_sl_order(
            side="LONG", stop_level=495.0, current_ltp=500.0, qty=5, security_id="X"
        )
        self.assertLess(order["trigger_price"], 500.0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 10: StopLossBuilder SHORT correctness
# ═══════════════════════════════════════════════════════════════════════

class Test10_SLBuilderShort(unittest.TestCase):
    def test_short_sl_values(self):
        order = StopLossBuilder.build_sl_order(
            side="SHORT", stop_level=1010.0, current_ltp=1000.0,
            qty=10, security_id="SID_SHORT"
        )
        self.assertEqual(order["txn"], "BUY")
        self.assertEqual(order["trigger_price"], 1010.0)
        self.assertAlmostEqual(order["price"], round(1010.0 * 1.05, 2))
        self.assertGreater(order["price"], order["trigger_price"])
        self.assertEqual(order["qty"], 10)

    def test_short_sl_trigger_above_ltp(self):
        order = StopLossBuilder.build_sl_order(
            side="SHORT", stop_level=505.0, current_ltp=500.0, qty=5, security_id="X"
        )
        self.assertGreater(order["trigger_price"], 500.0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 11: StopLossBuilder validation — wrong side → ValueError
# ═══════════════════════════════════════════════════════════════════════

class Test11_SLBuilderValidation(unittest.TestCase):
    def test_long_trigger_above_ltp_raises(self):
        with self.assertRaises(ValueError) as ctx:
            StopLossBuilder.build_sl_order("LONG", 1005.0, 1000.0, 10, "X")
        self.assertIn("DH-906", str(ctx.exception))

    def test_short_trigger_below_ltp_raises(self):
        with self.assertRaises(ValueError) as ctx:
            StopLossBuilder.build_sl_order("SHORT", 995.0, 1000.0, 10, "X")
        self.assertIn("DH-906", str(ctx.exception))

    def test_long_trigger_equal_ltp_raises(self):
        with self.assertRaises(ValueError):
            StopLossBuilder.build_sl_order("LONG", 1000.0, 1000.0, 10, "X")

    def test_short_trigger_equal_ltp_raises(self):
        with self.assertRaises(ValueError):
            StopLossBuilder.build_sl_order("SHORT", 1000.0, 1000.0, 10, "X")

    def test_invalid_side_raises(self):
        with self.assertRaises(ValueError):
            StopLossBuilder.build_sl_order("NEUTRAL", 990.0, 1000.0, 10, "X")

    def test_zero_qty_raises(self):
        with self.assertRaises(ValueError):
            StopLossBuilder.build_sl_order("LONG", 990.0, 1000.0, 0, "X")

    def test_negative_stop_raises(self):
        with self.assertRaises(ValueError):
            StopLossBuilder.build_sl_order("LONG", -10.0, 1000.0, 10, "X")


# ═══════════════════════════════════════════════════════════════════════
# TEST 12: entries_allowed uses correct field names
# ═══════════════════════════════════════════════════════════════════════

class Test12_EntriesAllowedFieldNames(unittest.TestCase):
    def test_clean_state_allows(self):
        guard = ExecutionGuard()
        allowed, reason = guard.entries_allowed()
        self.assertTrue(allowed)
        self.assertEqual(reason, "OK")

    def test_kill_switch_blocks(self):
        guard = ExecutionGuard()
        guard.kill_switch_active = True
        guard.kill_reasons = ["TEST"]
        allowed, reason = guard.entries_allowed()
        self.assertFalse(allowed)
        self.assertIn("KILL_SWITCH", reason)

    def test_sl_failures_block(self):
        guard = ExecutionGuard()
        guard.sl_failures = 2
        allowed, reason = guard.entries_allowed()
        self.assertFalse(allowed)
        self.assertIn("SL_FAILURES", reason)

    def test_api_failures_block(self):
        guard = ExecutionGuard()
        guard.consecutive_api_failures = 3
        allowed, reason = guard.entries_allowed()
        self.assertFalse(allowed)
        self.assertIn("API_FAILURES", reason)

    def test_sl_failures_below_threshold_allows(self):
        guard = ExecutionGuard()
        guard.sl_failures = 1
        allowed, _ = guard.entries_allowed()
        self.assertTrue(allowed)

    def test_report_sl_failure_increments_correct_field(self):
        guard = ExecutionGuard()
        guard.report_sl_failure("TESTSTOCK")
        self.assertEqual(guard.sl_failures, 1)
        guard.report_sl_failure("TESTSTOCK")
        self.assertEqual(guard.sl_failures, 2)
        self.assertTrue(guard.kill_switch_active)

    def test_report_api_success_resets_counter(self):
        guard = ExecutionGuard()
        guard.consecutive_api_failures = 2
        guard.report_api_success()
        self.assertEqual(guard.consecutive_api_failures, 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 13: report_reconciliation_clean clears kill_switch
# ═══════════════════════════════════════════════════════════════════════

class Test13_ReconciliationClean(unittest.TestCase):
    def test_clears_kill_switch(self):
        guard = ExecutionGuard()
        guard.kill_switch_active = True
        guard.kill_reasons = ["BROKER_MISMATCH: test", "SL_FAILURES: 2"]
        guard.sl_failures = 2
        guard.consecutive_api_failures = 1

        guard.report_reconciliation_clean()

        self.assertFalse(guard.kill_switch_active)
        self.assertEqual(len(guard.kill_reasons), 0)
        self.assertEqual(guard.sl_failures, 0)
        self.assertEqual(guard.consecutive_api_failures, 0)

    def test_report_reconcile_ok_only_clears_mismatch(self):
        guard = ExecutionGuard()
        guard.kill_switch_active = True
        guard.kill_reasons = ["BROKER_MISMATCH: orphan", "API_UNHEALTHY: 3 consecutive failures"]

        guard.report_reconcile_ok()
        self.assertTrue(guard.kill_switch_active)
        self.assertIn("API_UNHEALTHY: 3 consecutive failures", guard.kill_reasons)

    def test_reconciliation_clean_vs_reconcile_ok(self):
        guard = ExecutionGuard()
        guard.kill_switch_active = True
        guard.kill_reasons = ["API_UNHEALTHY: 3 consecutive failures"]
        guard.consecutive_api_failures = 5

        guard.report_reconciliation_clean()
        self.assertFalse(guard.kill_switch_active)
        self.assertEqual(guard.consecutive_api_failures, 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 14: Intent ledger persistence and recovery
# ═══════════════════════════════════════════════════════════════════════

class Test14_IntentLedgerPersistence(V11TestBase):
    def test_intent_written_to_disk(self):
        # Use price within ₹10K canary cap: 5 × ₹1500 = ₹7500
        intent_id = self.ei.create_intent(
            "TCS", "2222", "LONG", 5, 1500.0, 1470.0
        )
        self.assertIsNotNone(intent_id)

        with open(self.ei._intent_file, "r") as f:
            lines = f.readlines()

        self.assertTrue(len(lines) >= 1)
        record = json.loads(lines[-1])
        self.assertEqual(record["intent_id"], intent_id)
        self.assertEqual(record["state"], IntentState.INTENT_CREATED.value)

    def test_intent_state_transitions_persisted(self):
        # 2 × ₹1500 = ₹3000 — within cap
        intent_id = self.ei.create_intent(
            "TCS", "2222", "LONG", 2, 1500.0, 1470.0
        )
        self.ei.mark_order_submitted(intent_id, "oid_tcs_001")
        self.ei.confirm_fill(intent_id, "oid_tcs_001", 2, 1502.0)
        self.ei.mark_sl_verified(intent_id, "sl_tcs_001")
        self.ei.mark_active(intent_id, "T_tcs_001")

        with open(self.ei._intent_file, "r") as f:
            lines = f.readlines()

        states = [json.loads(l)["state"] for l in lines]
        self.assertIn(IntentState.INTENT_CREATED.value, states)
        self.assertIn(IntentState.ORDER_SUBMITTED.value, states)
        self.assertIn(IntentState.FILL_CONFIRMED.value, states)
        self.assertIn(IntentState.SL_VERIFIED.value, states)
        self.assertIn(IntentState.ACTIVE.value, states)

    def test_recovery_reads_latest_state(self):
        for state in [IntentState.INTENT_CREATED.value, IntentState.ACTIVE.value]:
            entry = {
                "intent_id": "INT_multi", "symbol": "HCLTECH",
                "security_id": "3333", "side": "SHORT", "qty": 8,
                "price": 1200.0, "sl_level": 1210.0, "state": state,
                "order_id": "", "fill_qty": 0, "fill_price": 0,
                "sl_order_id": "", "trade_id": "", "created_at": "",
                "updated_at": "", "notional": 0,
            }
            with open(self.ei._intent_file, "a") as f:
                f.write(json.dumps(entry) + "\n")

        report = self.ei.recover_intents()
        self.assertEqual(report["already_active"], 1)
        self.assertEqual(report["recovered"], 0)


# ═══════════════════════════════════════════════════════════════════════
# TEST 15: Canary caps enforced (₹10K notional limit)
# ═══════════════════════════════════════════════════════════════════════

class Test15_CanaryCapEnforced(V11TestBase):
    def test_single_order_exceeds_cap(self):
        """₹81K order should be rejected with ₹10K cap."""
        result = self.ei.create_intent("NIFTY", "NIFTY50", "LONG", 50, 1620.0, 1600.0)
        self.assertIsNone(result)
        self.assertTrue(self.ei.guard.kill_switch_active)

    def test_cumulative_exceeds_cap(self):
        r1 = self.ei.create_intent("A", "1", "LONG", 5, 1000.0, 990.0)
        self.assertIsNotNone(r1)
        self.ei.confirm_fill(r1, "o1", 5, 1000.0)
        r2 = self.ei.create_intent("B", "2", "SHORT", 6, 1000.0, 1010.0)
        self.assertIsNone(r2)

    def test_within_cap_allowed(self):
        result = self.ei.create_intent("SBIN", "SBI1", "LONG", 3, 3000.0, 2970.0)
        self.assertIsNotNone(result)

    def test_cap_tracking_accurate(self):
        r1 = self.ei.create_intent("X", "X1", "LONG", 2, 2000.0, 1980.0)
        self.assertIsNotNone(r1)
        self.ei.confirm_fill(r1, "o1", 2, 2000.0)
        self.assertAlmostEqual(self.ei.get_session_notional(), 4000.0)
        self.assertAlmostEqual(self.ei.get_canary_cap_remaining(), 6000.0)

    def test_disabled_cap(self):
        ei_nocap = ExecutionIntegrity(
            dhan=self.gw, bot_instance=self.bot,
            base_path=self.tmpdir, canary_cap_inr=0,
        )
        result = ei_nocap.create_intent("BIG", "B1", "LONG", 100, 5000.0, 4950.0)
        self.assertIsNotNone(result)


# ═══════════════════════════════════════════════════════════════════════
# ADDITIONAL: BrokerReconciler no longer has AttributeError (A4)
# ═══════════════════════════════════════════════════════════════════════

class TestA4_NoAttributeError(V11TestBase):
    def test_adopt_position_no_attribute_error(self):
        self.gw.set_positions([{
            "securityId": "9001", "tradingSymbol": "WIPRO",
            "netQty": -5, "costPrice": 450.0,
        }])
        self.gw.set_ltp("9001", 448.0)
        self.gw.sl_response = None

        try:
            report = self.ei.reconcile()
        except AttributeError as ae:
            self.fail(f"BrokerReconciler raised AttributeError: {ae}")

        self.assertTrue(len(self.gw.market_order_calls) > 0)

    def test_reconciler_has_guard_reference(self):
        self.assertTrue(hasattr(self.ei.reconciler, 'guard'))
        self.assertIs(self.ei.reconciler.guard, self.ei.guard)


# ═══════════════════════════════════════════════════════════════════════
# ADDITIONAL: EOD force close with EXIT AUDIT (A7)
# ═══════════════════════════════════════════════════════════════════════

class TestA7_EODForceClose(V11TestBase):
    def test_eod_closes_all_positions(self):
        self.gw.set_positions([
            {"securityId": "111", "tradingSymbol": "AAA", "netQty": 10},
            {"securityId": "222", "tradingSymbol": "BBB", "netQty": -5},
        ])
        self.bot.active_positions["111"] = {"trade_id": "T1"}
        report = self.ei.eod_force_close()
        self.assertEqual(report["closed_local"], 1)
        self.assertEqual(report["closed_orphans"], 1)
        self.assertEqual(len(report["exit_audits"]), 2)

    def test_eod_handles_broker_failure(self):
        self.gw.set_positions([
            {"securityId": "333", "tradingSymbol": "CCC", "netQty": 10},
        ])
        self.gw.raise_on_place_order = True
        report = self.ei.eod_force_close()
        self.assertTrue(len(report["failures"]) > 0)


# ═══════════════════════════════════════════════════════════════════════
# ADDITIONAL: verify_sl retry logic (A2)
# ═══════════════════════════════════════════════════════════════════════

class TestA2_VerifySLRetry(V11TestBase):
    def test_verify_sl_success_on_first_try(self):
        self.gw.set_order_status("sl_ok", {"orderStatus": "PENDING"})
        result = self.ei.verify_sl("sl_ok", "1111", "LONG")
        self.assertTrue(result)

    def test_verify_sl_trigger_pending_accepted(self):
        self.gw.set_order_status("sl_tp", {"orderStatus": "TRIGGER_PENDING"})
        result = self.ei.verify_sl("sl_tp", "2222", "SHORT")
        self.assertTrue(result)

    def test_verify_sl_empty_id_fails(self):
        result = self.ei.verify_sl("", "3333", "LONG")
        self.assertFalse(result)


# ═══════════════════════════════════════════════════════════════════════
# ADDITIONAL: backward compatibility
# ═══════════════════════════════════════════════════════════════════════

class TestBackwardCompat(V11TestBase):
    def test_confirm_sl_interface(self):
        self.gw.set_order_status("sl_compat", {"orderStatus": "PENDING"})
        result = self.ei.confirm_sl("sl_compat", "1234", "LONG", 990.0)
        self.assertTrue(result)

    def test_resolve_order_interface(self):
        self.ei.tracker.register("oid_compat", "T1", "SYM", "SID", "LONG", 10, 100.0)
        self.gw.set_order_status("oid_compat", {"orderStatus": "TRADED", "filledQty": 10})
        rec = self.ei.resolve_order("oid_compat")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, OrderState.FILLED)

    def test_guard_entries_allowed_interface(self):
        allowed, reason = self.ei.guard.entries_allowed()
        self.assertIsInstance(allowed, bool)
        self.assertIsInstance(reason, str)

    def test_reconcile_interface(self):
        self.gw.set_positions([])
        report = self.ei.reconcile()
        self.assertIn("broker_positions", report)
        self.assertIn("local_positions", report)
        self.assertIn("orphans_found", report)
        self.assertIn("mismatch", report)


# ═══════════════════════════════════════════════════════════════════════
# ADDITIONAL: create_position_atomic convenience
# ═══════════════════════════════════════════════════════════════════════

class TestAtomicPosition(V11TestBase):
    def test_create_position_atomic_returns_dict(self):
        result = self.ei.create_position_atomic(
            "AXISBANK", "AXIS1", "SHORT", 3, 1100.0, 1110.0
        )
        self.assertIsNotNone(result)
        self.assertIn("intent_id", result)
        self.assertEqual(result["side"], "SHORT")
        self.assertAlmostEqual(result["notional"], 3300.0)

    def test_create_position_atomic_blocked_by_cap(self):
        result = self.ei.create_position_atomic(
            "COSTLY", "C1", "LONG", 100, 500.0, 490.0
        )
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
