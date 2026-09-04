"""
Test Suite for V11 Candidate Intelligence
===========================================
Comprehensive coverage for v11_candidate_intelligence.py — Patch 3 of V11 deploy.

Run:
    python -m pytest test_v11_candidate_intelligence.py -v
    python -m unittest test_v11_candidate_intelligence -v

Coverage:
    1.  CandidateState creation and field defaults
    2.  CandidateState to_dict / from_dict round-trip
    3.  Stage transition: DETECTED → ACCUMULATING (auto on 2nd observation)
    4.  Stage transition: ACCUMULATING → READY (normal accumulation)
    5.  Fast-track bypass: strong breakout skips accumulation → READY
    6.  Fast-track requires ALL criteria (partial fails)
    7.  Invalidation: breakout lost (BUY price retreats below breakout)
    8.  Invalidation: breakout lost (SELL price rises above breakout)
    9.  Invalidation: volume disappeared (rvol < 0.5 for 2+ obs)
    10. Invalidation: momentum collapsed (BUY momentum reversed)
    11. Invalidation: stale (8+ minutes in ACCUMULATING, no READY)
    12. READY → SCORED via mark_scored()
    13. SCORED stays SCORED on WATCH decision
    14. SCORED → INVALIDATED on VETO decision
    15. mark_scored with EXHAUSTED move_stage → EXHAUSTED
    16. WAIT_PULLBACK entry via mark_wait_pullback()
    17. WAIT_PULLBACK → READY on pullback recovery
    18. WAIT_PULLBACK → INVALIDATED on breakout lost
    19. WAIT_PULLBACK → INVALIDATED on stale (4+ scans)
    20. mark_exhausted() terminal transition
    21. Persistence: save() → load() round-trip (all fields intact)
    22. Persistence: survives simulated crash (load from file, state intact)
    23. Persistence: corrupt file graceful handling
    24. Persistence: missing file graceful handling
    25. cleanup_stale() removes old candidates
    26. get_ready_candidates() filtering
    27. get_scored_candidates() filtering
    28. get_active_candidates() filtering (excludes terminal)
    29. mark_scored on unknown symbol (no crash)
    30. mark_decision on unknown symbol (no crash)
    31. invalidate on unknown symbol (no crash)
    32. Thread safety: concurrent update_candidate calls
    33. reset() clears everything
    34. get_summary() counts by stage
    35. get_summary() avg_observations
    36. get_summary() fast_tracked count
    37. Duplicate symbol update (same symbol across scans preserves state)
    38. Watermark tracking (high and low across observations)
    39. Terminal state is sticky (INVALIDATED cannot transition out)
    40. EXHAUSTED terminal state is sticky
    41. Decision with ACCUMULATE action → back to ACCUMULATING
    42. Multiple candidates lifecycle (mixed stages)
    43. Observation history preserved across updates
    44. Empty observations edge case
    45. No base_path → persistence disabled (no crash)
    46. Price history and volume history tracking
    47. CandidateState from_dict with extra unknown fields (forward compat)
    48. Fast-track sets fast_tracked flag
    49. get_state returns None for unknown symbol
    50. get_state returns deep copy (mutation safety)
    51. Atomic save (temp file → rename pattern)
    52. Volume declining blocks normal accumulation
    53. SELL side fast-track
    54. SELL side invalidation (momentum collapsed)
    55. Pullback recovery SELL side
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

from v11_candidate_intelligence import (
    CandidateState,
    CandidateManager,
    _TransitionEngine,
    _utc_now_iso,
    _parse_iso,
    _minutes_since,
    VERSION,
    STAGE_DETECTED,
    STAGE_ACCUMULATING,
    STAGE_READY,
    STAGE_SCORED,
    STAGE_WAIT_PULLBACK,
    STAGE_DEVELOPING,
    STAGE_EXHAUSTED,
    STAGE_INVALIDATED,
    ACTIVE_STAGES,
    TERMINAL_STAGES,
    ALL_STAGES,
    FAST_TRACK_PRICE_PCT,
    FAST_TRACK_RVOL_MIN,
    FAST_TRACK_ATR_MAX,
    FAST_TRACK_CANDLES_MIN,
    ACCUMULATION_OBS_MIN,
    ACCUMULATION_VALID_OBS_MIN,
    ACCUMULATION_VALID_CANDLES,
    INVALIDATION_RVOL_THRESHOLD,
    INVALIDATION_STALE_MINUTES,
    INVALIDATION_PULLBACK_SCANS,
)


# ═══════════════════════════════════════════════════════════════════════════
# TEST BASE CLASS — shared setup / teardown
# ═══════════════════════════════════════════════════════════════════════════

class CandidateTestBase(unittest.TestCase):
    """Base class with temp directory setup/teardown."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="v11_ci_test_")
        self.mgr = CandidateManager(base_path=self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_obs(self, ltp=200.0, volume=100000, valid_candles=6,
                  rvol=2.0, momentum_5m=0.5, atr=2.0, time_iso=None):
        """Helper to build an observation dict."""
        return {
            "time": time_iso or _utc_now_iso(),
            "ltp": ltp,
            "volume": volume,
            "valid_candles": valid_candles,
            "rvol": rvol,
            "momentum_5m": momentum_5m,
            "atr": atr,
        }

    def _accumulate_to_ready(self, symbol="TEST", security_id="sid_1",
                              breakout_level=190.0, side="BUY",
                              base_ltp=200.0):
        """Helper: push a candidate through to READY via normal accumulation."""
        for i in range(4):
            obs = self._make_obs(
                ltp=base_ltp + i,
                volume=100000 + i * 5000,
                valid_candles=6,
                rvol=2.0,
                momentum_5m=0.5,
            )
            state = self.mgr.update_candidate(
                symbol, security_id, obs,
                breakout_level=breakout_level, side=side,
            )
        return state


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1-2: CANDIDATESTATE BASICS
# ═══════════════════════════════════════════════════════════════════════════

class TestCandidateStateBasics(CandidateTestBase):
    """Tests 1-2: CandidateState dataclass fundamentals."""

    def test_01_defaults(self):
        """Test 1: CandidateState creation with default field values."""
        state = CandidateState()
        self.assertEqual(state.symbol, "")
        self.assertEqual(state.security_id, "")
        self.assertEqual(state.current_stage, STAGE_DETECTED)
        self.assertIsNone(state.side)
        self.assertEqual(state.breakout_level, 0.0)
        self.assertEqual(state.observations, [])
        self.assertEqual(state.observation_count, 0)
        self.assertEqual(state.price_history, [])
        self.assertEqual(state.volume_history, [])
        self.assertEqual(state.high_watermark, 0.0)
        self.assertEqual(state.low_watermark, float("inf"))
        self.assertIsNone(state.score)
        self.assertIsNone(state.timing_score)
        self.assertIsNone(state.base_score)
        self.assertIsNone(state.move_stage)
        self.assertIsNone(state.entry_type)
        self.assertEqual(state.position_pct, 1.0)
        self.assertIsNone(state.v10r_decision)
        self.assertIsNone(state.v853_decision)
        self.assertIsNone(state.final_action)
        self.assertIsNone(state.veto_reason)
        self.assertFalse(state.invalidated)
        self.assertEqual(state.invalidation_reason, "")
        self.assertFalse(state.fast_tracked)

    def test_02_to_dict_from_dict_roundtrip(self):
        """Test 2: CandidateState to_dict → from_dict preserves all fields."""
        state = CandidateState(
            symbol="IDBI",
            security_id="sid_123",
            first_seen="2026-08-31T04:20:00+00:00",
            last_seen="2026-08-31T04:25:00+00:00",
            side="BUY",
            breakout_level=205.5,
            observations=[{"ltp": 206.0, "rvol": 2.5}],
            observation_count=1,
            price_history=[206.0],
            volume_history=[120000.0],
            high_watermark=206.0,
            low_watermark=204.0,
            current_stage=STAGE_ACCUMULATING,
            score=7.5,
            timing_score=1.2,
            base_score=6.3,
            move_stage="EARLY_ENTRY",
            entry_type="EARLY_ENTRY",
            position_pct=0.3,
            v10r_decision="ENTER",
            v853_decision="ENTER_NOW",
            final_action="ENTER",
            veto_reason=None,
            invalidated=False,
            invalidation_reason="",
            fast_tracked=True,
        )
        d = state.to_dict()
        restored = CandidateState.from_dict(d)

        self.assertEqual(restored.symbol, "IDBI")
        self.assertEqual(restored.side, "BUY")
        self.assertEqual(restored.breakout_level, 205.5)
        self.assertEqual(restored.score, 7.5)
        self.assertEqual(restored.move_stage, "EARLY_ENTRY")
        self.assertTrue(restored.fast_tracked)
        self.assertEqual(restored.low_watermark, 204.0)
        self.assertEqual(len(restored.observations), 1)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3-4: STAGE TRANSITIONS — DETECTED → ACCUMULATING → READY
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalAccumulation(CandidateTestBase):
    """Tests 3-4: Normal stage progression without fast-track."""

    def test_03_detected_to_accumulating(self):
        """Test 3: Second observation moves DETECTED → ACCUMULATING."""
        obs1 = self._make_obs(ltp=205.0, valid_candles=3)
        state = self.mgr.update_candidate("IDBI", "sid", obs1,
                                           breakout_level=200.0, side="BUY")
        # First observation — still DETECTED or ACCUMULATING
        self.assertIn(state.current_stage, (STAGE_DETECTED, STAGE_ACCUMULATING))

        obs2 = self._make_obs(ltp=206.0, valid_candles=4)
        state = self.mgr.update_candidate("IDBI", "sid", obs2,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_ACCUMULATING)
        self.assertEqual(state.observation_count, 2)

    def test_04_accumulating_to_ready(self):
        """Test 4: Normal accumulation (3+ obs, 2+ valid candle obs, volume up) → READY."""
        state = self._accumulate_to_ready()
        self.assertEqual(state.current_stage, STAGE_READY)
        self.assertEqual(state.observation_count, 4)
        self.assertFalse(state.fast_tracked)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5-6: FAST-TRACK BYPASS
# ═══════════════════════════════════════════════════════════════════════════

class TestFastTrack(CandidateTestBase):
    """Tests 5-6: Fast-track bypass to READY."""

    def test_05_fast_track_strong_breakout(self):
        """Test 5: Strong breakout (1.5%+ beyond, rvol 3+, candles 5+) → READY immediately."""
        # First observation — can fast-track on first if all criteria met
        obs = self._make_obs(
            ltp=210.0,      # 210/200 = 5% above breakout — well above 1.5%
            rvol=4.0,        # > 3.0
            valid_candles=7,  # > 5
            atr=3.0,          # distance = 10/3 = 3.33 → wait, need < 2.0 ATR
        )
        # ATR too big — distance 10/3 = 3.33 > 2.0, won't fast-track
        state = self.mgr.update_candidate("IDBI", "sid", obs,
                                           breakout_level=200.0, side="BUY")
        self.assertNotEqual(state.current_stage, STAGE_READY)

        # Now with proper ATR where distance < 2.0 ATR
        self.mgr.reset()
        obs = self._make_obs(
            ltp=204.0,       # 2% above breakout 200 → OK
            rvol=4.0,        # > 3.0
            valid_candles=7, # > 5
            atr=5.0,         # distance = 4/5 = 0.8 ATR < 2.0 → OK
        )
        state = self.mgr.update_candidate("FAST", "sid", obs,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_READY)
        self.assertTrue(state.fast_tracked)

    def test_06_fast_track_partial_criteria_fails(self):
        """Test 6: Fast-track fails if ANY single criterion is missing."""
        # Missing rvol (only 2.0 < 3.0)
        obs = self._make_obs(ltp=204.0, rvol=2.0, valid_candles=7, atr=5.0)
        state = self.mgr.update_candidate("A", "sid", obs,
                                           breakout_level=200.0, side="BUY")
        self.assertNotEqual(state.current_stage, STAGE_READY)

        # Missing valid_candles (only 3 < 5)
        self.mgr.reset()
        obs = self._make_obs(ltp=204.0, rvol=4.0, valid_candles=3, atr=5.0)
        state = self.mgr.update_candidate("B", "sid", obs,
                                           breakout_level=200.0, side="BUY")
        self.assertNotEqual(state.current_stage, STAGE_READY)

        # Missing price beyond (only 0.5% < 1.5%)
        self.mgr.reset()
        obs = self._make_obs(ltp=201.0, rvol=4.0, valid_candles=7, atr=5.0)
        state = self.mgr.update_candidate("C", "sid", obs,
                                           breakout_level=200.0, side="BUY")
        self.assertNotEqual(state.current_stage, STAGE_READY)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7-11: INVALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestInvalidation(CandidateTestBase):
    """Tests 7-11: Various invalidation triggers."""

    def test_07_breakout_lost_buy(self):
        """Test 7: BUY candidate price drops below breakout_level → INVALIDATED."""
        obs1 = self._make_obs(ltp=205.0)
        self.mgr.update_candidate("X", "sid", obs1,
                                   breakout_level=200.0, side="BUY")
        # Price retreats below breakout
        obs2 = self._make_obs(ltp=198.0)
        state = self.mgr.update_candidate("X", "sid", obs2,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "BREAKOUT_LOST")

    def test_08_breakout_lost_sell(self):
        """Test 8: SELL candidate price rises above breakout_level → INVALIDATED."""
        obs1 = self._make_obs(ltp=195.0)
        self.mgr.update_candidate("Y", "sid", obs1,
                                   breakout_level=200.0, side="SELL")
        obs2 = self._make_obs(ltp=202.0)
        state = self.mgr.update_candidate("Y", "sid", obs2,
                                           breakout_level=200.0, side="SELL")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "BREAKOUT_LOST")

    def test_09_volume_disappeared(self):
        """Test 9: rvol < 0.5 for 2+ consecutive observations → INVALIDATED."""
        obs1 = self._make_obs(ltp=205.0, rvol=2.0)
        self.mgr.update_candidate("V", "sid", obs1,
                                   breakout_level=200.0, side="BUY")
        obs2 = self._make_obs(ltp=206.0, rvol=0.3)
        self.mgr.update_candidate("V", "sid", obs2,
                                   breakout_level=200.0, side="BUY")
        obs3 = self._make_obs(ltp=206.5, rvol=0.2)
        state = self.mgr.update_candidate("V", "sid", obs3,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "VOLUME_DISAPPEARED")

    def test_10_momentum_collapsed_buy(self):
        """Test 10: BUY momentum negative for 2+ obs → INVALIDATED."""
        obs1 = self._make_obs(ltp=205.0, momentum_5m=0.5)
        self.mgr.update_candidate("M", "sid", obs1,
                                   breakout_level=200.0, side="BUY")
        obs2 = self._make_obs(ltp=204.0, momentum_5m=-0.3)
        self.mgr.update_candidate("M", "sid", obs2,
                                   breakout_level=200.0, side="BUY")
        obs3 = self._make_obs(ltp=203.0, momentum_5m=-0.5)
        state = self.mgr.update_candidate("M", "sid", obs3,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "MOMENTUM_COLLAPSED")

    def test_11_stale_timeout(self):
        """Test 11: 8+ minutes in ACCUMULATING without READY → INVALIDATED (STALE)."""
        # Create candidate with first_seen 10 minutes ago
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        obs1 = self._make_obs(ltp=205.0, valid_candles=3, time_iso=old_time)
        self.mgr.update_candidate("STALE", "sid", obs1,
                                   breakout_level=200.0, side="BUY")

        # Second observation — should trigger stale check
        obs2 = self._make_obs(ltp=206.0, valid_candles=3)
        state = self.mgr.update_candidate("STALE", "sid", obs2,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "STALE")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 12-15: SCORING AND DECISIONS
# ═══════════════════════════════════════════════════════════════════════════

class TestScoringAndDecisions(CandidateTestBase):
    """Tests 12-15: mark_scored and mark_decision flows."""

    def test_12_ready_to_scored(self):
        """Test 12: READY → SCORED via mark_scored()."""
        self._accumulate_to_ready("SCORE_ME")
        self.mgr.mark_scored("SCORE_ME", score=7.5, timing_score=1.2,
                             base_score=6.3, move_stage="EARLY_ENTRY",
                             entry_type="EARLY_ENTRY", position_pct=1.0)
        state = self.mgr.get_state("SCORE_ME")
        self.assertEqual(state.current_stage, STAGE_SCORED)
        self.assertEqual(state.score, 7.5)
        self.assertEqual(state.timing_score, 1.2)
        self.assertEqual(state.base_score, 6.3)
        self.assertEqual(state.move_stage, "EARLY_ENTRY")
        self.assertEqual(state.entry_type, "EARLY_ENTRY")
        self.assertEqual(state.position_pct, 1.0)

    def test_13_scored_stays_on_watch(self):
        """Test 13: SCORED stays SCORED when decision is WATCH."""
        self._accumulate_to_ready("WATCH_ME")
        self.mgr.mark_scored("WATCH_ME", score=5.0, timing_score=0.5,
                             base_score=4.5, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        self.mgr.mark_decision("WATCH_ME", v10r_decision="WATCH",
                               v853_decision="WATCH", final_action="WATCH")
        state = self.mgr.get_state("WATCH_ME")
        self.assertEqual(state.current_stage, STAGE_SCORED)
        self.assertEqual(state.final_action, "WATCH")

    def test_14_scored_to_invalidated_on_veto(self):
        """Test 14: SCORED → INVALIDATED on VETO decision."""
        self._accumulate_to_ready("VETO_ME")
        self.mgr.mark_scored("VETO_ME", score=6.0, timing_score=1.0,
                             base_score=5.0, move_stage="LATE_CONTINUATION",
                             entry_type="NORMAL", position_pct=0.3)
        self.mgr.mark_decision("VETO_ME", v10r_decision="VETO",
                               v853_decision="DO_NOT_TRADE",
                               final_action="VETO",
                               veto_reason="SECTOR_WEAK")
        state = self.mgr.get_state("VETO_ME")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertTrue(state.invalidated)
        self.assertEqual(state.veto_reason, "SECTOR_WEAK")

    def test_15_exhausted_move_stage(self):
        """Test 15: mark_scored with EXHAUSTED move_stage → EXHAUSTED."""
        self._accumulate_to_ready("EXHAUST_ME")
        self.mgr.mark_scored("EXHAUST_ME", score=4.0, timing_score=0.5,
                             base_score=3.5, move_stage="EXHAUSTED",
                             entry_type="NORMAL", position_pct=0.3)
        state = self.mgr.get_state("EXHAUST_ME")
        self.assertEqual(state.current_stage, STAGE_EXHAUSTED)
        self.assertEqual(state.veto_reason, "EXHAUSTED")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 16-19: WAIT_PULLBACK
# ═══════════════════════════════════════════════════════════════════════════

class TestWaitPullback(CandidateTestBase):
    """Tests 16-19: WAIT_PULLBACK stage behavior."""

    def test_16_enter_wait_pullback(self):
        """Test 16: mark_wait_pullback() transitions to WAIT_PULLBACK."""
        self._accumulate_to_ready("PB")
        self.mgr.mark_wait_pullback("PB")
        state = self.mgr.get_state("PB")
        self.assertEqual(state.current_stage, STAGE_WAIT_PULLBACK)

    def test_17_pullback_recovery_buy(self):
        """Test 17: WAIT_PULLBACK → READY on BUY pullback recovery."""
        self._accumulate_to_ready("PBR", breakout_level=200.0, side="BUY",
                                   base_ltp=205.0)
        self.mgr.mark_scored("PBR", score=5.0, timing_score=0.5,
                             base_score=4.5, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        self.mgr.mark_wait_pullback("PBR")

        # Dip close to breakout (low_watermark goes down)
        obs_dip = self._make_obs(ltp=200.5)
        self.mgr.update_candidate("PBR", "sid_1", obs_dip,
                                   breakout_level=200.0, side="BUY")

        # Recover above breakout
        obs_recovery = self._make_obs(ltp=202.0)
        state = self.mgr.update_candidate("PBR", "sid_1", obs_recovery,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_READY)
        # Score should be reset for re-evaluation
        self.assertIsNone(state.score)

    def test_18_pullback_breakout_lost(self):
        """Test 18: WAIT_PULLBACK → INVALIDATED when price breaks below breakout."""
        self._accumulate_to_ready("PBL", breakout_level=200.0, side="BUY",
                                   base_ltp=205.0)
        self.mgr.mark_wait_pullback("PBL")

        obs_lost = self._make_obs(ltp=198.0)
        state = self.mgr.update_candidate("PBL", "sid_1", obs_lost,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "PULLBACK_BREAKOUT_LOST")

    def test_19_pullback_stale(self):
        """Test 19: WAIT_PULLBACK → INVALIDATED after 4+ scans without recovery."""
        self._accumulate_to_ready("PBS", breakout_level=200.0, side="BUY",
                                   base_ltp=205.0)
        self.mgr.mark_wait_pullback("PBS")

        # 4 scans above breakout but no pullback dip+recovery
        for i in range(4):
            obs = self._make_obs(ltp=201.0 + i * 0.1)
            state = self.mgr.update_candidate("PBS", "sid_1", obs,
                                               breakout_level=200.0, side="BUY")

        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "STALE_PULLBACK")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 20: EXHAUSTED TERMINAL
# ═══════════════════════════════════════════════════════════════════════════

class TestExhausted(CandidateTestBase):
    """Test 20: mark_exhausted terminal transition."""

    def test_20_mark_exhausted(self):
        """Test 20: mark_exhausted() moves to EXHAUSTED terminal state."""
        self._accumulate_to_ready("EXH")
        self.mgr.mark_exhausted("EXH")
        state = self.mgr.get_state("EXH")
        self.assertEqual(state.current_stage, STAGE_EXHAUSTED)
        self.assertEqual(state.veto_reason, "EXHAUSTED")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 21-24: PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════

class TestPersistence(CandidateTestBase):
    """Tests 21-24: save/load/crash/corrupt file handling."""

    def test_21_save_load_roundtrip(self):
        """Test 21: save() → load() round-trip preserves all fields."""
        self._accumulate_to_ready("PERSIST")
        self.mgr.mark_scored("PERSIST", score=8.0, timing_score=1.5,
                             base_score=6.5, move_stage="EARLY_ENTRY",
                             entry_type="EARLY_ENTRY", position_pct=1.0)
        self.mgr.mark_decision("PERSIST", "ENTER", "ENTER_NOW", "ENTER")
        self.mgr.save()

        # Load into fresh manager
        mgr2 = CandidateManager(base_path=self.tmp_dir)
        mgr2.load()
        state = mgr2.get_state("PERSIST")

        self.assertIsNotNone(state)
        self.assertEqual(state.symbol, "PERSIST")
        self.assertEqual(state.current_stage, STAGE_SCORED)
        self.assertEqual(state.score, 8.0)
        self.assertEqual(state.timing_score, 1.5)
        self.assertEqual(state.base_score, 6.5)
        self.assertEqual(state.move_stage, "EARLY_ENTRY")
        self.assertEqual(state.final_action, "ENTER")
        self.assertEqual(state.observation_count, 4)

    def test_22_crash_recovery(self):
        """Test 22: Simulated crash — load from file, state intact."""
        self._accumulate_to_ready("CRASH")
        self.mgr.mark_scored("CRASH", score=6.0, timing_score=0.8,
                             base_score=5.2, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        self.mgr.save()

        # Simulate crash by creating a completely new manager
        mgr_recovered = CandidateManager(base_path=self.tmp_dir)
        mgr_recovered.load()

        state = mgr_recovered.get_state("CRASH")
        self.assertIsNotNone(state)
        self.assertEqual(state.score, 6.0)
        self.assertEqual(state.current_stage, STAGE_SCORED)

        # Verify can continue operating
        obs_new = self._make_obs(ltp=210.0)
        state2 = mgr_recovered.update_candidate("CRASH", "sid_1", obs_new,
                                                  breakout_level=190.0, side="BUY")
        self.assertEqual(state2.observation_count, 5)

    def test_23_corrupt_file_graceful(self):
        """Test 23: Corrupt persistence file → graceful handling (no crash)."""
        corrupt_path = os.path.join(self.tmp_dir, "candidate_states.json")
        with open(corrupt_path, "w") as f:
            f.write("{corrupt json!!! not valid")

        mgr2 = CandidateManager(base_path=self.tmp_dir)
        # Should not raise — just log error
        mgr2.load()
        self.assertEqual(mgr2.candidate_count, 0)

    def test_24_missing_file_graceful(self):
        """Test 24: Missing persistence file → empty state (no crash)."""
        empty_dir = tempfile.mkdtemp(prefix="v11_ci_empty_")
        try:
            mgr2 = CandidateManager(base_path=empty_dir)
            mgr2.load()  # Should not crash
            self.assertEqual(mgr2.candidate_count, 0)
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 25: CLEANUP STALE
# ═══════════════════════════════════════════════════════════════════════════

class TestCleanup(CandidateTestBase):
    """Test 25: cleanup_stale removes old candidates."""

    def test_25_cleanup_stale(self):
        """Test 25: cleanup_stale removes candidates not seen recently."""
        # Create candidate with old last_seen
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        obs = self._make_obs(ltp=205.0, time_iso=old_time)
        self.mgr.update_candidate("OLD", "sid", obs,
                                   breakout_level=200.0, side="BUY")

        # Create recent candidate
        obs_new = self._make_obs(ltp=210.0)
        self.mgr.update_candidate("NEW", "sid2", obs_new,
                                   breakout_level=200.0, side="BUY")

        self.assertEqual(self.mgr.candidate_count, 2)
        self.mgr.cleanup_stale(max_age_minutes=30)
        self.assertEqual(self.mgr.candidate_count, 1)
        self.assertIsNone(self.mgr.get_state("OLD"))
        self.assertIsNotNone(self.mgr.get_state("NEW"))


# ═══════════════════════════════════════════════════════════════════════════
# TEST 26-28: QUERY METHODS
# ═══════════════════════════════════════════════════════════════════════════

class TestQueryMethods(CandidateTestBase):
    """Tests 26-28: get_ready/scored/active candidates filtering."""

    def _setup_mixed_candidates(self):
        """Create candidates in various stages."""
        # READY
        self._accumulate_to_ready("READY_1", breakout_level=190.0)
        # SCORED
        self._accumulate_to_ready("SCORED_1", security_id="sid_2",
                                   breakout_level=190.0)
        self.mgr.mark_scored("SCORED_1", score=7.0, timing_score=1.0,
                             base_score=6.0, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        # INVALIDATED
        obs = self._make_obs(ltp=205.0)
        self.mgr.update_candidate("INVALID_1", "sid_3", obs,
                                   breakout_level=200.0, side="BUY")
        self.mgr.invalidate("INVALID_1", "TEST_REASON")
        # ACCUMULATING
        obs = self._make_obs(ltp=205.0)
        self.mgr.update_candidate("ACCUM_1", "sid_4", obs,
                                   breakout_level=200.0, side="BUY")

    def test_26_get_ready_candidates(self):
        """Test 26: get_ready_candidates returns only READY stage."""
        self._setup_mixed_candidates()
        ready = self.mgr.get_ready_candidates()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].symbol, "READY_1")

    def test_27_get_scored_candidates(self):
        """Test 27: get_scored_candidates returns only SCORED stage."""
        self._setup_mixed_candidates()
        scored = self.mgr.get_scored_candidates()
        self.assertEqual(len(scored), 1)
        self.assertEqual(scored[0].symbol, "SCORED_1")

    def test_28_get_active_candidates(self):
        """Test 28: get_active_candidates excludes terminal stages."""
        self._setup_mixed_candidates()
        active = self.mgr.get_active_candidates()
        active_syms = {s.symbol for s in active}
        self.assertIn("READY_1", active_syms)
        self.assertIn("SCORED_1", active_syms)
        self.assertIn("ACCUM_1", active_syms)
        self.assertNotIn("INVALID_1", active_syms)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 29-31: UNKNOWN SYMBOL HANDLING
# ═══════════════════════════════════════════════════════════════════════════

class TestUnknownSymbol(CandidateTestBase):
    """Tests 29-31: Operations on unknown symbols don't crash."""

    def test_29_mark_scored_unknown(self):
        """Test 29: mark_scored on unknown symbol — no crash."""
        # Should not raise
        self.mgr.mark_scored("GHOST", score=5.0, timing_score=1.0,
                             base_score=4.0, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)

    def test_30_mark_decision_unknown(self):
        """Test 30: mark_decision on unknown symbol — no crash."""
        self.mgr.mark_decision("GHOST", "ENTER", "ENTER_NOW", "ENTER")

    def test_31_invalidate_unknown(self):
        """Test 31: invalidate on unknown symbol — no crash."""
        self.mgr.invalidate("GHOST", "TEST")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 32: THREAD SAFETY
# ═══════════════════════════════════════════════════════════════════════════

class TestThreadSafety(CandidateTestBase):
    """Test 32: Concurrent update_candidate calls."""

    def test_32_concurrent_updates(self):
        """Test 32: Thread safety — concurrent updates don't corrupt state."""
        errors = []

        def update_worker(symbol_id):
            try:
                for i in range(20):
                    obs = self._make_obs(ltp=200.0 + i, volume=100000 + i * 1000)
                    self.mgr.update_candidate(
                        f"SYM_{symbol_id}", f"sid_{symbol_id}", obs,
                        breakout_level=190.0, side="BUY",
                    )
            except Exception as e:
                errors.append(str(e))

        threads = []
        for tid in range(10):
            t = threading.Thread(target=update_worker, args=(tid,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(self.mgr.candidate_count, 10)

        # Verify each symbol has 20 observations
        for tid in range(10):
            state = self.mgr.get_state(f"SYM_{tid}")
            self.assertIsNotNone(state)
            self.assertEqual(state.observation_count, 20)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 33: RESET
# ═══════════════════════════════════════════════════════════════════════════

class TestReset(CandidateTestBase):
    """Test 33: reset() clears everything."""

    def test_33_reset_clears_all(self):
        """Test 33: reset() clears all candidates."""
        for i in range(5):
            obs = self._make_obs(ltp=200.0 + i)
            self.mgr.update_candidate(f"SYM_{i}", f"sid_{i}", obs,
                                       breakout_level=190.0, side="BUY")
        self.assertEqual(self.mgr.candidate_count, 5)

        self.mgr.reset()
        self.assertEqual(self.mgr.candidate_count, 0)
        self.assertEqual(len(self.mgr.get_active_candidates()), 0)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 34-36: SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

class TestSummary(CandidateTestBase):
    """Tests 34-36: get_summary() output."""

    def test_34_summary_by_stage(self):
        """Test 34: get_summary() counts by stage."""
        self._accumulate_to_ready("R1")
        self._accumulate_to_ready("R2")
        obs = self._make_obs(ltp=205.0)
        self.mgr.update_candidate("A1", "sid", obs,
                                   breakout_level=200.0, side="BUY")
        self.mgr.invalidate("A1", "TEST")

        summary = self.mgr.get_summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_stage"].get(STAGE_READY, 0), 2)
        self.assertEqual(summary["by_stage"].get(STAGE_INVALIDATED, 0), 1)
        self.assertEqual(summary["active"], 2)
        self.assertEqual(summary["terminal"], 1)

    def test_35_summary_avg_observations(self):
        """Test 35: get_summary() avg_observations field."""
        # 4 obs each for 2 candidates = 8 total / 2 = 4.0
        self._accumulate_to_ready("O1")
        self._accumulate_to_ready("O2")
        summary = self.mgr.get_summary()
        self.assertEqual(summary["avg_observations"], 4.0)

    def test_36_summary_fast_tracked(self):
        """Test 36: get_summary() counts fast_tracked candidates."""
        # Fast-track one
        obs = self._make_obs(ltp=204.0, rvol=4.0, valid_candles=7, atr=5.0)
        self.mgr.update_candidate("FT", "sid", obs,
                                   breakout_level=200.0, side="BUY")
        # Normal one
        self._accumulate_to_ready("NORM")

        summary = self.mgr.get_summary()
        self.assertEqual(summary["fast_tracked"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 37-40: STATE INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════

class TestStateIntegrity(CandidateTestBase):
    """Tests 37-40: State correctness and terminal stickiness."""

    def test_37_duplicate_symbol_preserves(self):
        """Test 37: Same symbol across scans preserves accumulated state."""
        obs1 = self._make_obs(ltp=205.0, volume=100000)
        self.mgr.update_candidate("DUP", "sid", obs1,
                                   breakout_level=200.0, side="BUY")
        obs2 = self._make_obs(ltp=207.0, volume=120000)
        state = self.mgr.update_candidate("DUP", "sid", obs2,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.observation_count, 2)
        self.assertEqual(len(state.price_history), 2)
        self.assertAlmostEqual(state.price_history[0], 205.0)
        self.assertAlmostEqual(state.price_history[1], 207.0)

    def test_38_watermark_tracking(self):
        """Test 38: High and low watermarks tracked across observations."""
        prices = [205.0, 210.0, 203.0, 215.0, 208.0]
        for p in prices:
            obs = self._make_obs(ltp=p)
            self.mgr.update_candidate("WM", "sid", obs,
                                       breakout_level=200.0, side="BUY")
        state = self.mgr.get_state("WM")
        self.assertEqual(state.high_watermark, 215.0)
        self.assertEqual(state.low_watermark, 203.0)

    def test_39_terminal_invalidated_sticky(self):
        """Test 39: INVALIDATED cannot transition out via update_candidate."""
        obs1 = self._make_obs(ltp=205.0)
        self.mgr.update_candidate("TERM", "sid", obs1,
                                   breakout_level=200.0, side="BUY")
        self.mgr.invalidate("TERM", "TEST_INVALIDATION")

        # Further updates should not change stage
        obs2 = self._make_obs(ltp=220.0, rvol=5.0, valid_candles=10)
        state = self.mgr.update_candidate("TERM", "sid", obs2,
                                           breakout_level=200.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        # But observation still recorded
        self.assertEqual(state.observation_count, 2)

    def test_40_terminal_exhausted_sticky(self):
        """Test 40: EXHAUSTED cannot transition out via update_candidate."""
        self._accumulate_to_ready("EXH2")
        self.mgr.mark_exhausted("EXH2")

        obs = self._make_obs(ltp=220.0, rvol=5.0)
        state = self.mgr.update_candidate("EXH2", "sid_1", obs,
                                           breakout_level=190.0, side="BUY")
        self.assertEqual(state.current_stage, STAGE_EXHAUSTED)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 41-43: DECISION AND OBSERVATION FLOWS
# ═══════════════════════════════════════════════════════════════════════════

class TestDecisionFlows(CandidateTestBase):
    """Tests 41-43: ACCUMULATE action, observation preservation."""

    def test_41_accumulate_decision(self):
        """Test 41: Decision with ACCUMULATE action → back to ACCUMULATING."""
        self._accumulate_to_ready("ACC_D")
        self.mgr.mark_scored("ACC_D", score=4.0, timing_score=0.3,
                             base_score=3.7, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        self.mgr.mark_decision("ACC_D", v10r_decision="WATCH",
                               v853_decision="WATCH",
                               final_action="ACCUMULATE")
        state = self.mgr.get_state("ACC_D")
        self.assertEqual(state.current_stage, STAGE_ACCUMULATING)

    def test_42_multiple_candidates_mixed(self):
        """Test 42: Multiple candidates in different stages simultaneously."""
        self._accumulate_to_ready("R")
        obs = self._make_obs(ltp=205.0)
        self.mgr.update_candidate("A", "sid", obs,
                                   breakout_level=200.0, side="BUY")
        self._accumulate_to_ready("S", security_id="sid3")
        self.mgr.mark_scored("S", score=7.0, timing_score=1.0,
                             base_score=6.0, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        self.mgr.invalidate("A", "MANUAL")

        summary = self.mgr.get_summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["active"], 2)  # R (READY) + S (SCORED)
        self.assertEqual(summary["terminal"], 1)  # A (INVALIDATED)

    def test_43_observation_history_preserved(self):
        """Test 43: Full observation history preserved across all updates."""
        for i in range(6):
            obs = self._make_obs(ltp=200.0 + i, volume=100000 + i * 1000,
                                 rvol=1.5 + i * 0.1)
            self.mgr.update_candidate("HIST", "sid", obs,
                                       breakout_level=190.0, side="BUY")

        state = self.mgr.get_state("HIST")
        self.assertEqual(len(state.observations), 6)
        self.assertEqual(len(state.price_history), 6)
        self.assertEqual(len(state.volume_history), 6)
        # Verify ordering
        for i in range(6):
            self.assertAlmostEqual(state.price_history[i], 200.0 + i)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 44-46: EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases(CandidateTestBase):
    """Tests 44-46: Edge case handling."""

    def test_44_empty_observations(self):
        """Test 44: Empty/minimal observation dict doesn't crash."""
        obs = {"time": _utc_now_iso()}  # Minimal — missing most fields
        state = self.mgr.update_candidate("EMPTY", "sid", obs,
                                           breakout_level=0, side=None)
        self.assertIsNotNone(state)
        self.assertEqual(state.observation_count, 1)

    def test_45_no_base_path(self):
        """Test 45: No base_path → persistence disabled (no crash)."""
        mgr = CandidateManager(base_path=None)
        obs = self._make_obs(ltp=200.0)
        state = mgr.update_candidate("NOPERSIST", "sid", obs,
                                      breakout_level=190.0, side="BUY")
        self.assertIsNotNone(state)
        # save/load should not crash
        mgr.save()
        mgr.load()
        self.assertEqual(mgr.candidate_count, 1)

    def test_46_price_volume_history(self):
        """Test 46: Price and volume history tracked correctly."""
        obs1 = self._make_obs(ltp=200.0, volume=50000)
        obs2 = self._make_obs(ltp=205.0, volume=75000)
        obs3 = self._make_obs(ltp=210.0, volume=100000)
        self.mgr.update_candidate("PV", "sid", obs1,
                                   breakout_level=190.0, side="BUY")
        self.mgr.update_candidate("PV", "sid", obs2,
                                   breakout_level=190.0, side="BUY")
        self.mgr.update_candidate("PV", "sid", obs3,
                                   breakout_level=190.0, side="BUY")
        state = self.mgr.get_state("PV")
        self.assertEqual(state.price_history, [200.0, 205.0, 210.0])
        self.assertEqual(state.volume_history, [50000, 75000, 100000])


# ═══════════════════════════════════════════════════════════════════════════
# TEST 47-48: FORWARD COMPAT AND FLAGS
# ═══════════════════════════════════════════════════════════════════════════

class TestForwardCompat(CandidateTestBase):
    """Tests 47-48: Forward compatibility and flags."""

    def test_47_from_dict_extra_fields(self):
        """Test 47: from_dict with extra unknown fields (forward compat)."""
        d = {
            "symbol": "COMPAT",
            "security_id": "sid",
            "current_stage": STAGE_READY,
            "future_field": "should_be_ignored",
            "another_new_thing": 42,
            "observations": [],
            "observation_count": 0,
        }
        state = CandidateState.from_dict(d)
        self.assertEqual(state.symbol, "COMPAT")
        self.assertEqual(state.current_stage, STAGE_READY)
        # Should not crash, just ignore unknown fields

    def test_48_fast_tracked_flag(self):
        """Test 48: fast_tracked flag is set correctly on fast-track."""
        obs = self._make_obs(ltp=204.0, rvol=4.0, valid_candles=7, atr=5.0)
        state = self.mgr.update_candidate("FT_FLAG", "sid", obs,
                                           breakout_level=200.0, side="BUY")
        self.assertTrue(state.fast_tracked)

        # Non-fast-track candidate
        obs2 = self._make_obs(ltp=201.0, rvol=1.0, valid_candles=3)
        state2 = self.mgr.update_candidate("NO_FT", "sid", obs2,
                                            breakout_level=200.0, side="BUY")
        self.assertFalse(state2.fast_tracked)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 49-50: GET_STATE SAFETY
# ═══════════════════════════════════════════════════════════════════════════

class TestGetStateSafety(CandidateTestBase):
    """Tests 49-50: get_state safety."""

    def test_49_get_state_unknown(self):
        """Test 49: get_state returns None for unknown symbol."""
        self.assertIsNone(self.mgr.get_state("NONEXISTENT"))

    def test_50_get_state_deep_copy(self):
        """Test 50: get_state returns deep copy — mutations don't affect internal state."""
        obs = self._make_obs(ltp=200.0)
        self.mgr.update_candidate("COPY", "sid", obs,
                                   breakout_level=190.0, side="BUY")
        state = self.mgr.get_state("COPY")
        # Mutate the returned copy
        state.score = 999.0
        state.observations.append({"fake": True})

        # Internal state should be unchanged
        internal = self.mgr.get_state("COPY")
        self.assertIsNone(internal.score)
        self.assertEqual(len(internal.observations), 1)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 51: ATOMIC SAVE
# ═══════════════════════════════════════════════════════════════════════════

class TestAtomicSave(CandidateTestBase):
    """Test 51: Atomic save pattern."""

    def test_51_atomic_save_no_temp_file_residue(self):
        """Test 51: After save(), no .tmp file remains."""
        obs = self._make_obs(ltp=200.0)
        self.mgr.update_candidate("ATOMIC", "sid", obs,
                                   breakout_level=190.0, side="BUY")
        self.mgr.save()

        # Check that main file exists and no .tmp remains
        main_file = os.path.join(self.tmp_dir, "candidate_states.json")
        tmp_file = main_file + ".tmp"
        self.assertTrue(os.path.exists(main_file))
        self.assertFalse(os.path.exists(tmp_file))

        # Verify JSON is valid
        with open(main_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data["version"], VERSION)
        self.assertIn("ATOMIC", data["candidates"])


# ═══════════════════════════════════════════════════════════════════════════
# TEST 52: VOLUME DECLINING BLOCKS ACCUMULATION
# ═══════════════════════════════════════════════════════════════════════════

class TestVolumeDecline(CandidateTestBase):
    """Test 52: Declining volume blocks normal accumulation to READY."""

    def test_52_volume_decline_blocks_ready(self):
        """Test 52: Volume declining (last < first) blocks READY transition."""
        # 4 observations with declining volume
        volumes = [200000, 180000, 150000, 100000]
        for i, vol in enumerate(volumes):
            obs = self._make_obs(ltp=205.0 + i, volume=vol,
                                 valid_candles=6, rvol=2.0)
            state = self.mgr.update_candidate("DECLINE", "sid", obs,
                                               breakout_level=200.0, side="BUY")

        # Should NOT be READY despite 4 obs with valid candles — volume declined
        self.assertNotEqual(state.current_stage, STAGE_READY)


# ═══════════════════════════════════════════════════════════════════════════
# TEST 53-55: SELL SIDE
# ═══════════════════════════════════════════════════════════════════════════

class TestSellSide(CandidateTestBase):
    """Tests 53-55: SELL side fast-track, invalidation, pullback."""

    def test_53_sell_fast_track(self):
        """Test 53: SELL side fast-track bypass."""
        # breakout_level = 200, ltp = 196 (2% below → OK for SELL)
        obs = self._make_obs(ltp=196.0, rvol=4.0, valid_candles=7, atr=5.0)
        state = self.mgr.update_candidate("SELL_FT", "sid", obs,
                                           breakout_level=200.0, side="SELL")
        self.assertEqual(state.current_stage, STAGE_READY)
        self.assertTrue(state.fast_tracked)

    def test_54_sell_momentum_collapsed(self):
        """Test 54: SELL side momentum collapsed (positive momentum = bad for SELL)."""
        obs1 = self._make_obs(ltp=195.0, momentum_5m=-0.5)
        self.mgr.update_candidate("SELL_MC", "sid", obs1,
                                   breakout_level=200.0, side="SELL")
        obs2 = self._make_obs(ltp=196.0, momentum_5m=0.3)
        self.mgr.update_candidate("SELL_MC", "sid", obs2,
                                   breakout_level=200.0, side="SELL")
        obs3 = self._make_obs(ltp=197.0, momentum_5m=0.5)
        state = self.mgr.update_candidate("SELL_MC", "sid", obs3,
                                           breakout_level=200.0, side="SELL")
        self.assertEqual(state.current_stage, STAGE_INVALIDATED)
        self.assertEqual(state.invalidation_reason, "MOMENTUM_COLLAPSED")

    def test_55_sell_pullback_recovery(self):
        """Test 55: SELL side WAIT_PULLBACK → READY on recovery."""
        # Accumulate SELL to READY
        for i in range(4):
            obs = self._make_obs(
                ltp=195.0 - i,
                volume=100000 + i * 5000,
                valid_candles=6,
                momentum_5m=-0.5,
            )
            self.mgr.update_candidate("SELL_PB", "sid_1", obs,
                                       breakout_level=200.0, side="SELL")

        self.mgr.mark_scored("SELL_PB", score=5.0, timing_score=0.5,
                             base_score=4.5, move_stage="NORMAL",
                             entry_type="NORMAL", position_pct=1.0)
        self.mgr.mark_wait_pullback("SELL_PB")

        # Pullback: price rises near breakout
        obs_dip = self._make_obs(ltp=199.5, momentum_5m=-0.1)
        self.mgr.update_candidate("SELL_PB", "sid_1", obs_dip,
                                   breakout_level=200.0, side="SELL")

        # Recovery: price drops back below breakout
        obs_recover = self._make_obs(ltp=198.0, momentum_5m=-0.5)
        state = self.mgr.update_candidate("SELL_PB", "sid_1", obs_recover,
                                           breakout_level=200.0, side="SELL")
        self.assertEqual(state.current_stage, STAGE_READY)


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
