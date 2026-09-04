"""
Test Suite for V11 Observability Engine
========================================
Comprehensive coverage for v11_observability.py — Patch 2 of V11 deploy.

Run:
    python -m pytest test_v11_observability.py -v
    python -m unittest test_v11_observability -v

Coverage:
    1.  Candidate ledger writes valid JSONL with all expected fields
    2.  Trade lifecycle ledger writes valid JSONL
    3.  Trade EXIT auto-computes mfe_capture_pct
    4.  Execution ledger truncates broker_response to 500 chars
    5.  Missed opportunity ledger initialises tracking fields
    6.  Missed opportunity update logic (BUY side — subsequent_high)
    7.  Missed opportunity update logic (SELL side — subsequent_low)
    8.  Missed_r and would_have_been_winner computation
    9.  Latency trace start → mark → finish flow
    10. Latency trace computes named deltas (ltp_ms, ohlc_ms, etc.)
    11. Latency trace writes LATENCY_TRACE event to execution ledger
    12. Unknown trace_id handling
    13. Session summary aggregation (counts, funnel, avg latency)
    14. Thread safety — concurrent writes to each ledger
    15. File rotation — day boundary (mock date change)
    16. Graceful failure — read-only directory (no crash)
    17. JSONL integrity — every line is independently parseable
    18. Auto-timestamp injection when caller omits timestamp
    19. Candidate funnel breakdown in session summary
    20. Multiple latency traces + averaging in summary
    21. Missed opportunity no-update when price unchanged
    22. Empty session summary (no writes yet)
    23. Non-serializable data graceful fallback
    24. Ledger read_date for specific dates
    25. Update missed with no matching symbols
    26. Concurrent latency traces
"""

import unittest
import json
import os
import sys
import shutil
import tempfile
import time
import threading
import concurrent.futures
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

# ── Adjust path so we can import the module under test ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from v11_observability import (
    ObservabilityEngine,
    _LedgerWriter,
    _LatencyTrace,
    _utc_now_iso,
    _today_str,
    _truncate,
    _safe_json_line,
    CANDIDATE_STAGES,
    CANDIDATE_ACTIONS,
    TRADE_EVENTS,
    EXECUTION_EVENTS,
    POSITION_STATES,
    MOVE_STAGES,
    LATENCY_STAGES,
)


# ═══════════════════════════════════════════════════════════════════════
# TEST BASE CLASS — shared setup / teardown
# ═══════════════════════════════════════════════════════════════════════

class ObservabilityTestBase(unittest.TestCase):
    """Base class with temp directory setup/teardown."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="v11_obs_test_")
        self.engine = ObservabilityEngine(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _read_jsonl(self, filepath: str) -> list:
        """Read a JSONL file and return list of parsed dicts."""
        records = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records


# ═══════════════════════════════════════════════════════════════════════
# TEST 1-4: CANDIDATE LEDGER
# ═══════════════════════════════════════════════════════════════════════

class TestCandidateLedger(ObservabilityTestBase):
    """Tests for candidate ledger writes."""

    def test_01_write_valid_jsonl(self):
        """Candidate write produces valid single-line JSON."""
        self.engine.log_candidate({
            "scan_cycle": 1,
            "symbol": "RELIANCE",
            "security_id": "1234",
            "side": "BUY",
            "stage": "DETECTED",
            "final_action": "WATCH",
            "ltp": 2500.0,
            "score": None,
        })
        fp = self.engine._candidate_writer.filepath_for_today()
        self.assertTrue(fp.exists())
        records = self._read_jsonl(str(fp))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["symbol"], "RELIANCE")
        self.assertEqual(rec["side"], "BUY")
        self.assertEqual(rec["stage"], "DETECTED")
        self.assertIsNone(rec["score"])

    def test_02_all_fields_present(self):
        """Every canonical candidate field is present (even if None)."""
        self.engine.log_candidate({"symbol": "INFY"})
        records = self.engine._candidate_writer.read_today()
        self.assertEqual(len(records), 1)
        rec = records[0]
        for field in ObservabilityEngine._CANDIDATE_FIELDS:
            self.assertIn(field, rec, f"Missing field: {field}")

    def test_03_auto_timestamp(self):
        """Timestamp is auto-injected when not provided."""
        self.engine.log_candidate({"symbol": "TCS"})
        rec = self.engine._candidate_writer.read_today()[0]
        self.assertIsNotNone(rec["timestamp"])
        # Should be a valid ISO timestamp
        ts = rec["timestamp"]
        self.assertIn("T", ts)

    def test_04_explicit_timestamp_preserved(self):
        """Caller-provided timestamp is preserved, not overwritten."""
        ts = "2026-08-31T09:00:00+00:00"
        self.engine.log_candidate({"symbol": "HDFC", "timestamp": ts})
        rec = self.engine._candidate_writer.read_today()[0]
        self.assertEqual(rec["timestamp"], ts)

    def test_05_multiple_candidates_same_cycle(self):
        """Multiple candidates in one scan cycle all get recorded."""
        for sym in ("RELIANCE", "INFY", "TCS", "HDFC", "ICICI"):
            self.engine.log_candidate({
                "scan_cycle": 5, "symbol": sym, "side": "BUY",
                "stage": "DETECTED", "final_action": "WATCH",
            })
        records = self.engine._candidate_writer.read_today()
        self.assertEqual(len(records), 5)
        symbols = [r["symbol"] for r in records]
        self.assertIn("TCS", symbols)

    def test_06_candidate_funnel_in_summary(self):
        """Session summary correctly computes candidate funnel."""
        actions = ["ENTER", "WATCH", "WATCH", "VETO", "VETO", "VETO",
                    "ACCUMULATE"]
        for i, action in enumerate(actions):
            self.engine.log_candidate({
                "scan_cycle": 1, "symbol": f"SYM_{i}",
                "final_action": action,
            })
        summary = self.engine.get_session_summary()
        funnel = summary["candidate_funnel"]
        self.assertEqual(funnel["ENTER"], 1)
        self.assertEqual(funnel["WATCH"], 2)
        self.assertEqual(funnel["VETO"], 3)
        self.assertEqual(funnel["ACCUMULATE"], 1)


# ═══════════════════════════════════════════════════════════════════════
# TEST 5-8: TRADE LIFECYCLE LEDGER
# ═══════════════════════════════════════════════════════════════════════

class TestTradeLifecycleLedger(ObservabilityTestBase):
    """Tests for trade lifecycle ledger writes."""

    def test_07_write_valid_jsonl(self):
        """Trade event write produces valid JSONL."""
        self.engine.log_trade_event({
            "trade_id": "T_RELIANCE_001",
            "event": "INTENT_CREATED",
            "symbol": "RELIANCE",
            "security_id": "1234",
            "side": "BUY",
            "qty": 10,
        })
        records = self.engine._trade_writer.read_today()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "INTENT_CREATED")

    def test_08_all_trade_fields_present(self):
        """Every canonical trade field is present."""
        self.engine.log_trade_event({"trade_id": "T_001", "event": "EXIT"})
        rec = self.engine._trade_writer.read_today()[0]
        for field in ObservabilityEngine._TRADE_FIELDS:
            self.assertIn(field, rec, f"Missing field: {field}")

    def test_09_mfe_capture_auto_computed(self):
        """EXIT event auto-computes mfe_capture_pct from peak_r and pnl_r."""
        self.engine.log_trade_event({
            "trade_id": "T_001", "event": "EXIT",
            "peak_r": 2.0, "pnl_r": 1.5,
        })
        rec = self.engine._trade_writer.read_today()[0]
        self.assertAlmostEqual(rec["mfe_capture_pct"], 75.0)

    def test_10_mfe_capture_not_overwritten(self):
        """Caller-provided mfe_capture_pct is not overwritten."""
        self.engine.log_trade_event({
            "trade_id": "T_001", "event": "EXIT",
            "peak_r": 2.0, "pnl_r": 1.5,
            "mfe_capture_pct": 80.0,
        })
        rec = self.engine._trade_writer.read_today()[0]
        self.assertAlmostEqual(rec["mfe_capture_pct"], 80.0)

    def test_11_mfe_not_computed_for_non_exit(self):
        """mfe_capture_pct is NOT auto-computed for non-EXIT events."""
        self.engine.log_trade_event({
            "trade_id": "T_001", "event": "MONITOR_TICK",
            "peak_r": 2.0, "pnl_r": 1.5,
        })
        rec = self.engine._trade_writer.read_today()[0]
        self.assertIsNone(rec["mfe_capture_pct"])

    def test_12_full_lifecycle_sequence(self):
        """A complete trade lifecycle produces ordered records."""
        events = [
            "INTENT_CREATED", "ORDER_SUBMITTED", "FILL_CONFIRMED",
            "SL_VERIFIED", "POSITION_ACTIVE", "MONITOR_TICK",
            "TIGHTEN", "EXIT",
        ]
        for evt in events:
            self.engine.log_trade_event({
                "trade_id": "T_LIFE_001", "event": evt,
                "symbol": "RELIANCE",
            })
        records = self.engine._trade_writer.read_today()
        self.assertEqual(len(records), len(events))
        recorded_events = [r["event"] for r in records]
        self.assertEqual(recorded_events, events)


# ═══════════════════════════════════════════════════════════════════════
# TEST 9-12: EXECUTION LEDGER
# ═══════════════════════════════════════════════════════════════════════

class TestExecutionLedger(ObservabilityTestBase):
    """Tests for execution ledger writes."""

    def test_13_write_valid_jsonl(self):
        """Execution write produces valid JSONL."""
        self.engine.log_execution({
            "event": "ORDER_SUBMIT",
            "symbol": "INFY",
            "order_id": "O123",
            "success": True,
            "latency_ms": 45.2,
        })
        records = self.engine._execution_writer.read_today()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "ORDER_SUBMIT")
        self.assertTrue(records[0]["success"])

    def test_14_broker_response_truncated(self):
        """broker_response is truncated to 500 chars max."""
        long_resp = "A" * 1000
        self.engine.log_execution({
            "event": "ORDER_FILL",
            "broker_response": long_resp,
        })
        rec = self.engine._execution_writer.read_today()[0]
        self.assertEqual(len(rec["broker_response"]), 500)

    def test_15_broker_response_short_preserved(self):
        """Short broker_response is NOT truncated."""
        short_resp = '{"status": "ok"}'
        self.engine.log_execution({
            "event": "ORDER_FILL",
            "broker_response": short_resp,
        })
        rec = self.engine._execution_writer.read_today()[0]
        self.assertEqual(rec["broker_response"], short_resp)

    def test_16_all_execution_fields_present(self):
        """Every canonical execution field is present."""
        self.engine.log_execution({"event": "SL_SUBMIT"})
        rec = self.engine._execution_writer.read_today()[0]
        for field in ObservabilityEngine._EXECUTION_FIELDS:
            self.assertIn(field, rec, f"Missing field: {field}")


# ═══════════════════════════════════════════════════════════════════════
# TEST 13-18: MISSED OPPORTUNITY LEDGER
# ═══════════════════════════════════════════════════════════════════════

class TestMissedOpportunityLedger(ObservabilityTestBase):
    """Tests for missed opportunity ledger and update logic."""

    def test_17_write_initialises_tracking_fields(self):
        """log_missed initialises subsequent_high/low from ltp_at_veto."""
        self.engine.log_missed({
            "symbol": "TCS", "security_id": "5678",
            "side": "BUY", "veto_reason": "EXHAUSTED",
            "ltp_at_veto": 3500.0,
        })
        rec = self.engine._missed_writer.read_today()[0]
        self.assertEqual(rec["subsequent_high"], 3500.0)
        self.assertEqual(rec["subsequent_low"], 3500.0)
        self.assertEqual(rec["missed_r"], 0.0)
        self.assertFalse(rec["would_have_been_winner"])

    def test_18_update_buy_subsequent_high(self):
        """update_missed_opportunities tracks subsequent_high for BUY."""
        self.engine.log_missed({
            "symbol": "TCS", "side": "BUY",
            "ltp_at_veto": 3500.0,
        })
        self.engine.update_missed_opportunities({"TCS": 3600.0})
        # Read the last record (update appends a new line)
        records = self.engine._missed_writer.read_today()
        last = records[-1]
        self.assertEqual(last["subsequent_high"], 3600.0)

    def test_19_update_sell_subsequent_low(self):
        """update_missed_opportunities tracks subsequent_low for SELL."""
        self.engine.log_missed({
            "symbol": "HDFC", "side": "SELL",
            "ltp_at_veto": 1600.0,
        })
        self.engine.update_missed_opportunities({"HDFC": 1550.0})
        records = self.engine._missed_writer.read_today()
        last = records[-1]
        self.assertEqual(last["subsequent_low"], 1550.0)

    def test_20_missed_r_and_winner_flag(self):
        """missed_r is computed correctly and would_have_been_winner set."""
        self.engine.log_missed({
            "symbol": "WIPRO", "side": "BUY",
            "ltp_at_veto": 400.0,
        })
        # Price moved up by 4.0 → 1% move on a 400 stock
        # SL distance = 400 * 0.0075 = 3.0
        # missed_r = (404 - 400) / 3.0 = 1.333
        self.engine.update_missed_opportunities({"WIPRO": 404.0})
        records = self.engine._missed_writer.read_today()
        last = records[-1]
        self.assertGreater(last["missed_r"], 1.0)
        self.assertTrue(last["would_have_been_winner"])

    def test_21_no_update_when_price_unchanged(self):
        """No update when current price is at or below previous high (BUY)."""
        self.engine.log_missed({
            "symbol": "ITC", "side": "BUY",
            "ltp_at_veto": 200.0,
        })
        count = self.engine.update_missed_opportunities({"ITC": 200.0})
        self.assertEqual(count, 0)

    def test_22_no_update_unmatched_symbols(self):
        """No update when current_prices has no matching symbols."""
        self.engine.log_missed({
            "symbol": "SAIL", "side": "BUY",
            "ltp_at_veto": 100.0,
        })
        count = self.engine.update_missed_opportunities({"TATA": 500.0})
        self.assertEqual(count, 0)

    def test_23_multiple_missed_updates(self):
        """Multiple missed candidates updated correctly in one call."""
        self.engine.log_missed({
            "symbol": "A", "side": "BUY", "ltp_at_veto": 100.0,
        })
        self.engine.log_missed({
            "symbol": "B", "side": "SELL", "ltp_at_veto": 200.0,
        })
        count = self.engine.update_missed_opportunities({
            "A": 110.0,  # moved up
            "B": 190.0,  # moved down
        })
        self.assertEqual(count, 2)


# ═══════════════════════════════════════════════════════════════════════
# TEST 19-24: LATENCY TRACING
# ═══════════════════════════════════════════════════════════════════════

class TestLatencyTracing(ObservabilityTestBase):
    """Tests for latency trace lifecycle."""

    def test_24_start_returns_trace_id(self):
        """start_latency_trace returns a valid trace_id."""
        tid = self.engine.start_latency_trace(scan_cycle=1)
        self.assertIsInstance(tid, str)
        self.assertTrue(tid.startswith("LT_1_"))

    def test_25_mark_records_stages(self):
        """mark_latency records timestamps for each stage."""
        tid = self.engine.start_latency_trace(1)
        for stage in ("scan_start", "scan_ltp_done", "scan_ohlc_done"):
            result = self.engine.mark_latency(tid, stage)
            self.assertTrue(result)
            time.sleep(0.005)

    def test_26_finish_computes_deltas(self):
        """finish_latency_trace produces deltas with named fields."""
        tid = self.engine.start_latency_trace(1)
        self.engine.mark_latency(tid, "scan_start")
        time.sleep(0.01)
        self.engine.mark_latency(tid, "scan_ltp_done")
        time.sleep(0.01)
        self.engine.mark_latency(tid, "scan_ohlc_done")
        time.sleep(0.01)
        self.engine.mark_latency(tid, "scan_scoring_done")
        time.sleep(0.01)
        self.engine.mark_latency(tid, "decision_done")

        summary = self.engine.finish_latency_trace(tid)
        self.assertIsNotNone(summary)
        deltas = summary["deltas"]
        self.assertIn("ltp_ms", deltas)
        self.assertIn("ohlc_ms", deltas)
        self.assertIn("scoring_ms", deltas)
        self.assertIn("decision_ms", deltas)
        self.assertIn("total_scan_ms", deltas)
        # All should be positive
        for key in ("ltp_ms", "ohlc_ms", "scoring_ms", "decision_ms"):
            self.assertGreater(deltas[key], 0, f"{key} should be > 0")

    def test_27_finish_writes_to_execution_ledger(self):
        """finish_latency_trace writes a LATENCY_TRACE event."""
        tid = self.engine.start_latency_trace(1)
        self.engine.mark_latency(tid, "scan_start")
        time.sleep(0.005)
        self.engine.mark_latency(tid, "decision_done")
        self.engine.finish_latency_trace(tid)

        records = self.engine._execution_writer.read_today()
        latency_events = [r for r in records if r["event"] == "LATENCY_TRACE"]
        self.assertEqual(len(latency_events), 1)
        self.assertEqual(latency_events[0]["scan_cycle"], 1)

    def test_28_unknown_trace_id_mark(self):
        """mark_latency returns False for unknown trace_id."""
        result = self.engine.mark_latency("NONEXISTENT", "scan_start")
        self.assertFalse(result)

    def test_29_unknown_trace_id_finish(self):
        """finish_latency_trace returns None for unknown trace_id."""
        result = self.engine.finish_latency_trace("NONEXISTENT")
        self.assertIsNone(result)

    def test_30_double_finish_returns_none(self):
        """Finishing an already-finished trace returns None."""
        tid = self.engine.start_latency_trace(1)
        self.engine.mark_latency(tid, "scan_start")
        result1 = self.engine.finish_latency_trace(tid)
        self.assertIsNotNone(result1)
        result2 = self.engine.finish_latency_trace(tid)
        self.assertIsNone(result2)

    def test_31_concurrent_latency_traces(self):
        """Multiple traces can run concurrently without interference."""
        traces = []
        for i in range(5):
            tid = self.engine.start_latency_trace(scan_cycle=i)
            self.engine.mark_latency(tid, "scan_start")
            traces.append(tid)

        time.sleep(0.01)

        for tid in traces:
            self.engine.mark_latency(tid, "decision_done")

        summaries = []
        for tid in traces:
            s = self.engine.finish_latency_trace(tid)
            self.assertIsNotNone(s)
            summaries.append(s)

        # Each should have its own scan_cycle
        cycles = [s["scan_cycle"] for s in summaries]
        self.assertEqual(sorted(cycles), [0, 1, 2, 3, 4])


# ═══════════════════════════════════════════════════════════════════════
# TEST 25-28: SESSION SUMMARY
# ═══════════════════════════════════════════════════════════════════════

class TestSessionSummary(ObservabilityTestBase):
    """Tests for session summary aggregation."""

    def test_32_empty_session_summary(self):
        """Summary works with zero writes."""
        summary = self.engine.get_session_summary()
        self.assertEqual(summary["candidate_count"], 0)
        self.assertEqual(summary["trade_count"], 0)
        self.assertEqual(summary["execution_count"], 0)
        self.assertEqual(summary["missed_count"], 0)
        self.assertEqual(summary["avg_latency_ms"], 0.0)

    def test_33_populated_summary(self):
        """Summary correctly counts all ledger entries."""
        # Write some data
        for i in range(5):
            self.engine.log_candidate({"symbol": f"C{i}", "final_action": "WATCH"})
        for i in range(3):
            self.engine.log_trade_event({"trade_id": f"T{i}", "event": "FILL_CONFIRMED"})
        for i in range(7):
            self.engine.log_execution({"event": "ORDER_SUBMIT", "order_id": f"O{i}"})
        for i in range(2):
            self.engine.log_missed({"symbol": f"M{i}", "side": "BUY", "ltp_at_veto": 100.0})

        summary = self.engine.get_session_summary()
        self.assertEqual(summary["candidate_count"], 5)
        self.assertEqual(summary["trade_count"], 3)
        self.assertGreaterEqual(summary["execution_count"], 7)
        self.assertEqual(summary["missed_count"], 2)

    def test_34_avg_latency_computed(self):
        """Average latency across multiple traces is correct."""
        for i in range(3):
            tid = self.engine.start_latency_trace(i)
            self.engine.mark_latency(tid, "scan_start")
            time.sleep(0.01)
            self.engine.mark_latency(tid, "decision_done")
            self.engine.finish_latency_trace(tid)

        summary = self.engine.get_session_summary()
        self.assertEqual(summary["latency_traces"], 3)
        self.assertGreater(summary["avg_latency_ms"], 0)

    def test_35_missed_winners_counted(self):
        """Summary counts missed opportunities that would have won."""
        self.engine.log_missed({
            "symbol": "WIN", "side": "BUY", "ltp_at_veto": 100.0,
        })
        self.engine.update_missed_opportunities({"WIN": 110.0})
        self.engine.log_missed({
            "symbol": "LOSE", "side": "BUY", "ltp_at_veto": 100.0,
        })
        # LOSE doesn't move — no update

        summary = self.engine.get_session_summary()
        self.assertGreaterEqual(summary["missed_would_have_won"], 1)

    def test_36_stage_breakdown_in_summary(self):
        """Summary includes candidate stage breakdown."""
        for stage in ("DETECTED", "DETECTED", "SCORED", "EXHAUSTED"):
            self.engine.log_candidate({"symbol": "X", "stage": stage})
        summary = self.engine.get_session_summary()
        stages = summary["candidate_stages"]
        self.assertEqual(stages["DETECTED"], 2)
        self.assertEqual(stages["SCORED"], 1)
        self.assertEqual(stages["EXHAUSTED"], 1)


# ═══════════════════════════════════════════════════════════════════════
# TEST 29-30: THREAD SAFETY
# ═══════════════════════════════════════════════════════════════════════

class TestThreadSafety(ObservabilityTestBase):
    """Tests for concurrent write safety."""

    def test_37_concurrent_candidate_writes(self):
        """100 concurrent candidate writes produce 100 valid records."""
        def write_one(i):
            return self.engine.log_candidate({
                "scan_cycle": i,
                "symbol": f"SYM_{i}",
                "security_id": str(i),
                "side": "BUY",
                "stage": "DETECTED",
                "final_action": "WATCH",
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(write_one, range(100)))

        self.assertTrue(all(results))
        records = self.engine._candidate_writer.read_today()
        self.assertEqual(len(records), 100)

    def test_38_concurrent_mixed_ledger_writes(self):
        """Concurrent writes to ALL 4 ledgers don't corrupt each other."""
        def write_candidates(n):
            for i in range(n):
                self.engine.log_candidate({"symbol": f"C{i}"})

        def write_trades(n):
            for i in range(n):
                self.engine.log_trade_event({"trade_id": f"T{i}", "event": "FILL_CONFIRMED"})

        def write_executions(n):
            for i in range(n):
                self.engine.log_execution({"event": "ORDER_SUBMIT"})

        def write_missed(n):
            for i in range(n):
                self.engine.log_missed({"symbol": f"M{i}", "side": "BUY", "ltp_at_veto": 100.0})

        threads = [
            threading.Thread(target=write_candidates, args=(50,)),
            threading.Thread(target=write_trades, args=(50,)),
            threading.Thread(target=write_executions, args=(50,)),
            threading.Thread(target=write_missed, args=(50,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(self.engine._candidate_writer.read_today()), 50)
        self.assertEqual(len(self.engine._trade_writer.read_today()), 50)
        self.assertEqual(len(self.engine._execution_writer.read_today()), 50)
        self.assertEqual(len(self.engine._missed_writer.read_today()), 50)

    def test_39_concurrent_latency_mark(self):
        """Multiple threads marking the same trace doesn't crash."""
        tid = self.engine.start_latency_trace(1)
        stages = list(LATENCY_STAGES)

        def mark_stage(stage):
            self.engine.mark_latency(tid, stage)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(mark_stage, stages))

        summary = self.engine.finish_latency_trace(tid)
        self.assertIsNotNone(summary)
        self.assertGreaterEqual(len(summary["marks"]), len(stages))


# ═══════════════════════════════════════════════════════════════════════
# TEST 31-32: FILE ROTATION (DAY BOUNDARY)
# ═══════════════════════════════════════════════════════════════════════

class TestFileRotation(ObservabilityTestBase):
    """Tests for day-based file rotation."""

    def test_40_different_days_different_files(self):
        """Writes on different dates go to different files."""
        # Write with today's date
        self.engine.log_candidate({"symbol": "TODAY"})

        # Simulate tomorrow by patching _today_str
        tomorrow = "2026-09-01"
        with patch("v11_observability._today_str", return_value=tomorrow):
            self.engine.log_candidate({"symbol": "TOMORROW"})

        # Check files
        today_file = self.engine.ledger_dir / f"candidates_{_today_str()}.jsonl"
        tomorrow_file = self.engine.ledger_dir / f"candidates_{tomorrow}.jsonl"
        self.assertTrue(today_file.exists())
        self.assertTrue(tomorrow_file.exists())

        # Each should have exactly 1 record
        today_records = self._read_jsonl(str(today_file))
        tomorrow_records = self._read_jsonl(str(tomorrow_file))
        self.assertEqual(len(today_records), 1)
        self.assertEqual(len(tomorrow_records), 1)
        self.assertEqual(today_records[0]["symbol"], "TODAY")
        self.assertEqual(tomorrow_records[0]["symbol"], "TOMORROW")

    def test_41_read_date_specific(self):
        """read_date returns records for a specific date."""
        target_date = "2026-07-15"
        with patch("v11_observability._today_str", return_value=target_date):
            self.engine.log_candidate({"symbol": "JULY"})

        records = self.engine._candidate_writer.read_date(target_date)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["symbol"], "JULY")

    def test_42_read_nonexistent_date_returns_empty(self):
        """read_date for a date with no file returns empty list."""
        records = self.engine._candidate_writer.read_date("1999-01-01")
        self.assertEqual(records, [])


# ═══════════════════════════════════════════════════════════════════════
# TEST 33-35: GRACEFUL FAILURE
# ═══════════════════════════════════════════════════════════════════════

class TestGracefulFailure(ObservabilityTestBase):
    """Tests for crash-proof behaviour."""

    def test_43_readonly_dir_doesnt_crash(self):
        """Writing to a read-only directory returns False, doesn't raise."""
        ro_dir = Path(self.tmp_dir) / "readonly"
        ro_dir.mkdir()
        writer = _LedgerWriter(ro_dir, "test")
        try:
            os.chmod(str(ro_dir), 0o444)
            result = writer.write({"test": True})
            # On some platforms (Windows, root) this may still succeed
            # The key assertion: no exception was raised
            self.assertIsInstance(result, bool)
        finally:
            os.chmod(str(ro_dir), 0o755)

    def test_44_nonserializable_data_handled(self):
        """Non-serializable values get string-coerced, not crash."""
        result = self.engine.log_candidate({
            "symbol": "TEST",
            "score": object(),  # Not JSON-serializable
        })
        self.assertTrue(result)  # Should still write successfully
        records = self.engine._candidate_writer.read_today()
        self.assertEqual(len(records), 1)

    def test_45_log_candidate_with_exception_returns_false(self):
        """If internal processing fails, log_candidate returns False."""
        # Monkey-patch _CANDIDATE_FIELDS to cause an unexpected error
        original = ObservabilityEngine._CANDIDATE_FIELDS
        try:
            ObservabilityEngine._CANDIDATE_FIELDS = None  # Will cause iteration error
            result = self.engine.log_candidate({"symbol": "CRASH"})
            self.assertFalse(result)
        finally:
            ObservabilityEngine._CANDIDATE_FIELDS = original


# ═══════════════════════════════════════════════════════════════════════
# TEST 36-38: JSONL INTEGRITY
# ═══════════════════════════════════════════════════════════════════════

class TestJsonlIntegrity(ObservabilityTestBase):
    """Tests for JSONL format correctness."""

    def test_46_every_line_independently_parseable(self):
        """Each line in a JSONL file is a complete, valid JSON object."""
        for i in range(20):
            self.engine.log_candidate({
                "symbol": f"SYM_{i}",
                "scan_cycle": i,
                "score": i * 1.5 if i % 2 == 0 else None,
            })

        fp = self.engine._candidate_writer.filepath_for_today()
        with open(str(fp), "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    self.assertIsInstance(obj, dict)
                except json.JSONDecodeError:
                    self.fail(f"Line {line_no} is not valid JSON: {line[:80]}")

    def test_47_no_array_wrapping(self):
        """JSONL files do NOT start with [ or end with ] (not JSON array)."""
        self.engine.log_candidate({"symbol": "A"})
        self.engine.log_candidate({"symbol": "B"})

        fp = self.engine._candidate_writer.filepath_for_today()
        with open(str(fp), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertFalse(content.strip().startswith("["))
        self.assertFalse(content.strip().endswith("]"))

    def test_48_newline_terminated(self):
        """Each write produces exactly one newline-terminated line."""
        self.engine.log_candidate({"symbol": "SINGLE"})

        fp = self.engine._candidate_writer.filepath_for_today()
        with open(str(fp), "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("\n"))
        # Ensure no embedded newlines in the JSON itself
        self.assertEqual(lines[0].count("\n"), 1)


# ═══════════════════════════════════════════════════════════════════════
# TEST 39-42: HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

class TestHelperFunctions(unittest.TestCase):
    """Tests for standalone helper functions."""

    def test_49_utc_now_iso_format(self):
        """_utc_now_iso returns a valid ISO timestamp with timezone."""
        ts = _utc_now_iso()
        self.assertIn("T", ts)
        self.assertIn("+00:00", ts)

    def test_50_today_str_format(self):
        """_today_str returns YYYY-MM-DD format."""
        d = _today_str()
        self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")

    def test_51_truncate(self):
        """_truncate limits string length."""
        self.assertEqual(len(_truncate("x" * 1000, 500)), 500)
        self.assertEqual(_truncate("short", 500), "short")
        self.assertEqual(_truncate(12345, 10), "12345")

    def test_52_safe_json_line(self):
        """_safe_json_line handles normal and problematic data."""
        # Normal
        line = _safe_json_line({"a": 1, "b": "test"})
        self.assertIsInstance(json.loads(line), dict)

        # With datetime (non-serializable by default)
        line2 = _safe_json_line({"ts": datetime.now(timezone.utc)})
        obj = json.loads(line2)
        self.assertIn("ts", obj)


# ═══════════════════════════════════════════════════════════════════════
# TEST 43-44: LEDGER WRITER INTERNALS
# ═══════════════════════════════════════════════════════════════════════

class TestLedgerWriterInternals(ObservabilityTestBase):
    """Tests for _LedgerWriter edge cases."""

    def test_53_write_count_tracks(self):
        """write_count increments with each successful write."""
        writer = self.engine._candidate_writer
        self.assertEqual(writer.write_count, 0)
        self.engine.log_candidate({"symbol": "A"})
        self.assertEqual(writer.write_count, 1)
        self.engine.log_candidate({"symbol": "B"})
        self.assertEqual(writer.write_count, 2)

    def test_54_filepath_for_today(self):
        """filepath_for_today returns correct path structure."""
        fp = self.engine._candidate_writer.filepath_for_today()
        self.assertIn("candidates_", str(fp))
        self.assertTrue(str(fp).endswith(".jsonl"))
        self.assertIn(_today_str(), str(fp))


# ═══════════════════════════════════════════════════════════════════════
# TEST 45-46: CONSTANT DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):
    """Tests for enum/constant definitions."""

    def test_55_candidate_stages_complete(self):
        """All 7 candidate stages defined."""
        expected = {"DETECTED", "ACCUMULATING", "READY", "SCORED",
                    "WAIT_PULLBACK", "EXHAUSTED", "INVALIDATED"}
        self.assertEqual(CANDIDATE_STAGES, expected)

    def test_56_trade_events_complete(self):
        """All 10 trade events defined."""
        expected = {
            "INTENT_CREATED", "ORDER_SUBMITTED", "FILL_CONFIRMED",
            "SL_VERIFIED", "POSITION_ACTIVE", "MONITOR_TICK",
            "TIGHTEN", "EXIT", "EMERGENCY_CLOSE", "EOD_CLOSE",
        }
        self.assertEqual(TRADE_EVENTS, expected)

    def test_57_execution_events_complete(self):
        """All 12 execution events defined."""
        expected = {
            "ORDER_SUBMIT", "ORDER_FILL", "ORDER_CANCEL", "ORDER_REJECT",
            "SL_SUBMIT", "SL_VERIFY", "SL_REJECT", "SL_MODIFY",
            "RECONCILE", "ADOPT", "EMERGENCY_CLOSE", "EOD_CLOSE",
        }
        self.assertEqual(EXECUTION_EVENTS, expected)

    def test_58_position_states_complete(self):
        """All 4 position states defined (from V10.1-R)."""
        self.assertEqual(POSITION_STATES, {"INITIAL", "PROVEN", "PROFIT", "RUNNER"})

    def test_59_move_stages_complete(self):
        """All 4 move stages defined."""
        self.assertEqual(MOVE_STAGES, {"EARLY_ENTRY", "LATE_CONTINUATION", "EXHAUSTED", "NORMAL"})


# ═══════════════════════════════════════════════════════════════════════
# TEST 47-48: EDGE CASES
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases(ObservabilityTestBase):
    """Edge cases and boundary conditions."""

    def test_60_empty_data_dict(self):
        """Logging an empty dict doesn't crash (all fields become None)."""
        self.assertTrue(self.engine.log_candidate({}))
        self.assertTrue(self.engine.log_trade_event({}))
        self.assertTrue(self.engine.log_execution({}))
        self.assertTrue(self.engine.log_missed({}))

    def test_61_extra_fields_ignored(self):
        """Extra fields not in the schema are silently dropped."""
        self.engine.log_candidate({
            "symbol": "TEST",
            "unknown_field": "should_not_appear",
            "another_extra": 42,
        })
        rec = self.engine._candidate_writer.read_today()[0]
        self.assertNotIn("unknown_field", rec)
        self.assertNotIn("another_extra", rec)

    def test_62_mfe_capture_zero_peak_r(self):
        """mfe_capture_pct handles zero peak_r without crash."""
        self.engine.log_trade_event({
            "trade_id": "T_ZERO", "event": "EXIT",
            "peak_r": 0.0, "pnl_r": 0.0,
        })
        rec = self.engine._trade_writer.read_today()[0]
        # Should be None because peak_r is 0 (falsy)
        self.assertIsNone(rec["mfe_capture_pct"])

    def test_63_unicode_in_reason(self):
        """Unicode characters in reason field handled correctly."""
        self.engine.log_candidate({
            "symbol": "TATA",
            "reason": "Price ₹2500 > breakout ₹2450 — strong momentum",
        })
        rec = self.engine._candidate_writer.read_today()[0]
        self.assertIn("₹", rec["reason"])

    def test_64_ledger_dir_created_on_init(self):
        """ObservabilityEngine creates the ledger directory if missing."""
        new_path = os.path.join(self.tmp_dir, "deep", "nested", "path")
        engine = ObservabilityEngine(new_path)
        self.assertTrue(engine.ledger_dir.exists())

    def test_65_missed_sell_winner(self):
        """SELL-side missed opportunity correctly flags winner."""
        self.engine.log_missed({
            "symbol": "SHORT_WIN", "side": "SELL",
            "ltp_at_veto": 500.0,
        })
        # Price drops 5 (1%): SL distance = 500 * 0.0075 = 3.75
        # missed_r = (500 - 495) / 3.75 = 1.333
        self.engine.update_missed_opportunities({"SHORT_WIN": 495.0})
        records = self.engine._missed_writer.read_today()
        last = records[-1]
        self.assertTrue(last["would_have_been_winner"])
        self.assertGreater(last["missed_r"], 1.0)


# ═══════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
