"""
Candidate Intelligence Module — V11.0.0 (2026-08-31)
======================================================
Patch 3 of the V11 deployment plan.  CandidateState persistence and
intelligence layer for the V11 trading bot (NSE intraday ORB on Dhan).

KEY PROBLEM SOLVED
------------------
Aug 31: 21 candidates across scans 3-7 (09:50-10:56 IST) ALL returned
INSUFFICIENT_DATA because each scan cycle started fresh — no memory of
previously seen candidates.  By scan 8 (11:13 IST), 1h 41min of trading
opportunity was lost.  CandidateState persistence across scan cycles fixes
this by accumulating evidence over multiple scans, enabling fast-track
entry on strong breakouts (IDBI/LUMAXTECH pattern), and invalidating
candidates whose setups have decayed.

State Machine
-------------
::

    DETECTED  →  ACCUMULATING  →  READY  →  SCORED
                     ↓                        ↓
                INVALIDATED            WAIT_PULLBACK → (back to READY)
                                           ↓
                                      INVALIDATED
                     ↓ (fast-track)
                   READY
    Terminal states:  EXHAUSTED, INVALIDATED

CandidateState Fields
---------------------
See CandidateState dataclass for full schema.  Key sections:
  - Observation tracking (price/volume history, watermarks)
  - Scoring (V10.1-R base_score, timing_score, move_stage)
  - Decision tracking (V10R/V853/final decisions)

Persistence
-----------
``{base_path}/candidate_states.json`` — overwritten on each ``save()``.
Format: ``{"version": "V11.0", "saved_at": "ISO", "candidates": {…}}``
Loaded on startup (``load()``), cleared on new session (``reset()``).

Fast-Track (Critical)
---------------------
Strong breakouts bypass accumulation to preserve IDBI-type early entries:
  - price > 1.5% beyond breakout_level
  - rvol >= 3.0
  - distance < 2.0 ATR from breakout_level
  - valid_candles >= 5

Public Interface
----------------
::

    mgr = CandidateManager(base_path="/home/ubuntu/trading-bot")
    state = mgr.update_candidate("IDBI", "sid", obs_dict, breakout_level=205.5, side="BUY")
    ready = mgr.get_ready_candidates()
    mgr.mark_scored("IDBI", score=7.5, timing_score=1.2, base_score=6.3,
                     move_stage="EARLY_ENTRY", entry_type="EARLY_ENTRY", position_pct=1.0)
    mgr.mark_decision("IDBI", "ENTER", "ENTER_NOW", "ENTER")
    mgr.save()

Implementation Notes
--------------------
- stdlib only — no external dependencies
- ``threading.Lock()`` for thread safety
- Graceful fallback: persistence failure → log error, never crash the bot
- ISO UTC timestamps everywhere
- Importable standalone: ``python -c "from v11_candidate_intelligence import CandidateManager"``

Depends on: nothing (Patches 1 & 2 are peers, not dependencies).
"""

from __future__ import annotations
import json, os, logging, threading, copy, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any

log = logging.getLogger("v11_candidate_intelligence")
IST = timezone(timedelta(hours=5, minutes=30))

# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

VERSION = "V11.0"

# Stage enum (ordered progression)
STAGE_DETECTED       = "DETECTED"
STAGE_ACCUMULATING   = "ACCUMULATING"
STAGE_READY          = "READY"
STAGE_SCORED         = "SCORED"
STAGE_WAIT_PULLBACK  = "WAIT_PULLBACK"
STAGE_DEVELOPING     = "DEVELOPING"
STAGE_EXHAUSTED      = "EXHAUSTED"
STAGE_INVALIDATED    = "INVALIDATED"

ACTIVE_STAGES = frozenset({
    STAGE_DETECTED, STAGE_ACCUMULATING, STAGE_READY,
    STAGE_SCORED, STAGE_WAIT_PULLBACK, STAGE_DEVELOPING,
})

TERMINAL_STAGES = frozenset({STAGE_EXHAUSTED, STAGE_INVALIDATED})

ALL_STAGES = ACTIVE_STAGES | TERMINAL_STAGES

# Fast-track thresholds
FAST_TRACK_PRICE_PCT    = 0.015   # 1.5% beyond breakout_level
FAST_TRACK_RVOL_MIN     = 3.0
FAST_TRACK_ATR_MAX      = 2.0     # distance < 2.0 ATR from breakout
FAST_TRACK_CANDLES_MIN  = 5

# Accumulation → READY thresholds
ACCUMULATION_OBS_MIN          = 3     # minimum observation_count
ACCUMULATION_VALID_OBS_MIN    = 2     # observations with valid_candles >= 5
ACCUMULATION_VALID_CANDLES    = 5

# Invalidation thresholds
INVALIDATION_RVOL_THRESHOLD  = 0.5   # rvol < this for 2+ obs → volume died
INVALIDATION_STALE_MINUTES   = 8     # no progress in 8 minutes → stale
INVALIDATION_PULLBACK_SCANS  = 4     # 4 scans with no recovery → stale pullback

# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _utc_now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO timestamp string to a datetime object."""
    # Handle 'Z' suffix and various ISO formats
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def _minutes_since(iso_ts: str) -> float:
    """Return minutes elapsed since the given ISO timestamp."""
    dt = _parse_iso(iso_ts)
    now = datetime.now(timezone.utc)
    # Ensure both are timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


# ═══════════════════════════════════════════════════════════════════════════
# CANDIDATE STATE DATACLASS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CandidateState:
    """
    Full state for a single candidate stock across scan cycles.
    
    Created on first detection, updated each scan, persisted to JSON.
    Stage machine governs lifecycle from DETECTED → terminal.
    """
    # Identity
    symbol: str = ""
    security_id: str = ""
    first_seen: str = ""           # ISO UTC
    last_seen: str = ""            # ISO UTC
    side: Optional[str] = None     # BUY / SELL (once determined)
    breakout_level: float = 0.0    # ORB high (BUY) or low (SELL)

    # Observation tracking
    observations: List[Dict] = field(default_factory=list)
    observation_count: int = 0
    price_history: List[float] = field(default_factory=list)
    volume_history: List[float] = field(default_factory=list)
    high_watermark: float = 0.0
    low_watermark: float = float("inf")

    # State machine
    current_stage: str = STAGE_DETECTED

    # Scoring (populated once READY)
    score: Optional[float] = None
    timing_score: Optional[float] = None
    base_score: Optional[float] = None
    move_stage: Optional[str] = None    # EARLY_ENTRY / LATE_CONTINUATION / EXHAUSTED / NORMAL
    entry_type: Optional[str] = None    # NORMAL / EARLY_ENTRY
    position_pct: float = 1.0

    # Decision tracking
    v10r_decision: Optional[str] = None    # ENTER / WATCH / VETO
    v853_decision: Optional[str] = None    # ENTER_NOW / DO_NOT_TRADE / WATCH
    final_action: Optional[str] = None     # ENTER / WATCH / VETO / ACCUMULATE
    veto_reason: Optional[str] = None

    # Metadata
    invalidated: bool = False
    invalidation_reason: str = ""
    fast_tracked: bool = False

    def to_dict(self) -> dict:
        """Serialize to dict for JSON persistence."""
        d = asdict(self)
        # Convert inf to a safe JSON value
        if d.get("low_watermark") == float("inf"):
            d["low_watermark"] = 0.0
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CandidateState":
        """Deserialize from dict (loaded from JSON)."""
        # Handle inf restoration — 0.0 with no observations means unset
        state = cls()
        for k, v in d.items():
            if hasattr(state, k):
                setattr(state, k, v)
        # Restore inf if low_watermark is 0 and no observations yet
        if state.low_watermark == 0.0 and state.observation_count == 0:
            state.low_watermark = float("inf")
        return state


# ═══════════════════════════════════════════════════════════════════════════
# TRANSITION ENGINE — stage logic
# ═══════════════════════════════════════════════════════════════════════════

class _TransitionEngine:
    """
    Encapsulates all stage transition logic.
    Pure functions — no I/O, no state mutation except via returned actions.
    """

    @staticmethod
    def check_fast_track(state: CandidateState, obs: dict) -> bool:
        """
        Determine if a candidate qualifies for fast-track to READY.
        
        Criteria (ALL must be true):
          1. Price > 1.5% beyond breakout_level
          2. rvol >= 3.0
          3. Distance from breakout < 2.0 ATR
          4. valid_candles >= 5
        """
        if state.breakout_level <= 0:
            return False

        ltp = obs.get("ltp", 0)
        rvol = obs.get("rvol", 0)
        valid_candles = obs.get("valid_candles", 0)
        atr = obs.get("atr", 0)

        if valid_candles < FAST_TRACK_CANDLES_MIN:
            return False
        if rvol < FAST_TRACK_RVOL_MIN:
            return False

        # Price beyond breakout by at least 1.5%
        side = state.side
        if side == "BUY":
            pct_beyond = (ltp - state.breakout_level) / state.breakout_level if state.breakout_level else 0
        elif side == "SELL":
            pct_beyond = (state.breakout_level - ltp) / state.breakout_level if state.breakout_level else 0
        else:
            return False

        if pct_beyond < FAST_TRACK_PRICE_PCT:
            return False

        # Anti-chase: distance from breakout < 2.0 ATR
        if atr > 0:
            distance_atr = abs(ltp - state.breakout_level) / atr
            if distance_atr >= FAST_TRACK_ATR_MAX:
                return False

        return True

    @staticmethod
    def check_invalidation(state: CandidateState, obs: dict) -> Optional[str]:
        """
        Check if the candidate should be invalidated.
        
        Returns invalidation reason string, or None if still valid.
        """
        ltp = obs.get("ltp", 0)
        rvol = obs.get("rvol", 0)
        momentum_5m = obs.get("momentum_5m", 0)
        
        # 1. Breakout level lost (price retreated past ORB)
        if state.breakout_level > 0 and state.side:
            if state.side == "BUY" and ltp < state.breakout_level:
                return "BREAKOUT_LOST"
            if state.side == "SELL" and ltp > state.breakout_level:
                return "BREAKOUT_LOST"

        # 2. Volume disappeared (rvol < 0.5 for 2+ observations)
        if state.observation_count >= 2:
            recent_rvols = [
                o.get("rvol", 1.0) for o in state.observations[-2:]
            ]
            if all(r < INVALIDATION_RVOL_THRESHOLD for r in recent_rvols):
                return "VOLUME_DISAPPEARED"

        # 3. Momentum collapsed (momentum_5m reversed sign for 2+ obs)
        if state.observation_count >= 2 and state.side:
            recent_momentums = [
                o.get("momentum_5m", 0) for o in state.observations[-2:]
            ]
            if state.side == "BUY" and all(m < 0 for m in recent_momentums):
                return "MOMENTUM_COLLAPSED"
            if state.side == "SELL" and all(m > 0 for m in recent_momentums):
                return "MOMENTUM_COLLAPSED"

        # 4. State age > 8 minutes without reaching READY
        if state.current_stage in (STAGE_DETECTED, STAGE_ACCUMULATING):
            age_minutes = _minutes_since(state.first_seen)
            if age_minutes > INVALIDATION_STALE_MINUTES:
                return "STALE"

        return None

    @staticmethod
    def check_accumulation_complete(state: CandidateState) -> bool:
        """
        Check if normal accumulation criteria are met for READY.
        
        Requires:
          - observation_count >= 3
          - At least 2 observations with valid_candles >= 5
          - Volume not declining (last obs volume >= first obs volume)
        """
        if state.observation_count < ACCUMULATION_OBS_MIN:
            return False

        valid_obs_count = sum(
            1 for o in state.observations
            if o.get("valid_candles", 0) >= ACCUMULATION_VALID_CANDLES
        )
        if valid_obs_count < ACCUMULATION_VALID_OBS_MIN:
            return False

        # Volume trending: last >= first (not declining)
        if len(state.volume_history) >= 2:
            if state.volume_history[-1] < state.volume_history[0]:
                return False

        return True

    @staticmethod
    def check_pullback_recovery(state: CandidateState, obs: dict) -> bool:
        """
        In WAIT_PULLBACK: check if price has pulled back then recovered
        above breakout_level.
        """
        if state.breakout_level <= 0 or not state.side:
            return False
        
        ltp = obs.get("ltp", 0)
        
        # Must have dipped then recovered
        if state.side == "BUY":
            # Low watermark should have come close to breakout, and now recovered
            dipped = state.low_watermark <= state.breakout_level * 1.005
            recovered = ltp > state.breakout_level
            return dipped and recovered
        elif state.side == "SELL":
            dipped = state.high_watermark >= state.breakout_level * 0.995
            recovered = ltp < state.breakout_level
            return dipped and recovered

        return False

    @staticmethod
    def check_pullback_stale(state: CandidateState) -> bool:
        """Check if WAIT_PULLBACK has been going too long (4+ scans)."""
        # Count observations since entering WAIT_PULLBACK
        pullback_obs = 0
        for obs in reversed(state.observations):
            pullback_obs += 1
            if pullback_obs >= INVALIDATION_PULLBACK_SCANS:
                return True
        return False


# ═══════════════════════════════════════════════════════════════════════════
# CANDIDATE MANAGER — main class
# ═══════════════════════════════════════════════════════════════════════════

class CandidateManager:
    """
    Thread-safe manager for all candidate states across scan cycles.
    
    Maintains an in-memory dict of CandidateState objects, supports
    persistence to/from JSON, and applies stage transitions on each
    update_candidate() call.
    """

    def __init__(self, base_path: str = None):
        """
        Initialize with path for persistence file.
        
        Args:
            base_path: Directory for candidate_states.json.
                       If None, persistence is disabled (in-memory only).
        """
        self._lock = threading.Lock()
        self._candidates: Dict[str, CandidateState] = {}
        self._transition = _TransitionEngine()
        self._base_path = base_path
        self._persistence_file = None
        
        if base_path:
            self._persistence_file = os.path.join(base_path, "candidate_states.json")
            os.makedirs(base_path, exist_ok=True)

    # ─── Core update ──────────────────────────────────────────────────

    def update_candidate(self, symbol: str, security_id: str,
                         observation: dict, breakout_level: float = 0,
                         side: str = None) -> CandidateState:
        """
        Update or create a candidate. Called once per symbol per scan cycle.
        
        Args:
            symbol:         Stock symbol (e.g. "IDBI")
            security_id:    Dhan security ID
            observation:    Dict with keys: time, ltp, volume, valid_candles,
                           rvol, momentum_5m, atr
            breakout_level: ORB high (BUY) or low (SELL) that was crossed
            side:          "BUY" or "SELL" (once determined)
        
        Returns:
            Updated CandidateState
        """
        with self._lock:
            now_iso = observation.get("time", _utc_now_iso())
            ltp = observation.get("ltp", 0.0)
            volume = observation.get("volume", 0.0)

            if symbol in self._candidates:
                state = self._candidates[symbol]
            else:
                # Create new candidate
                state = CandidateState(
                    symbol=symbol,
                    security_id=security_id,
                    first_seen=now_iso,
                    current_stage=STAGE_DETECTED,
                )
                self._candidates[symbol] = state

            # Update identity fields if provided
            if security_id:
                state.security_id = security_id
            if side:
                state.side = side
            if breakout_level > 0:
                state.breakout_level = breakout_level

            # Update last_seen
            state.last_seen = now_iso

            # Record observation
            state.observations.append(observation)
            state.observation_count = len(state.observations)
            state.price_history.append(ltp)
            state.volume_history.append(volume)

            # Update watermarks
            if ltp > 0:
                if ltp > state.high_watermark:
                    state.high_watermark = ltp
                if ltp < state.low_watermark:
                    state.low_watermark = ltp

            # ── Stage transitions ──
            if state.current_stage in TERMINAL_STAGES:
                # Terminal — no further transitions
                return state

            if state.current_stage == STAGE_WAIT_PULLBACK:
                self._process_wait_pullback(state, observation)
                return state

            if state.current_stage in (STAGE_DETECTED, STAGE_ACCUMULATING):
                self._process_accumulating(state, observation)
                return state

            # READY, SCORED, DEVELOPING — just record the observation
            return state

    def _process_accumulating(self, state: CandidateState, obs: dict):
        """
        Process DETECTED / ACCUMULATING stages.
        
        Checks (in order):
          1. Invalidation (breakout lost, volume died, momentum collapsed, stale)
          2. Fast-track bypass → READY
          3. Normal accumulation complete → READY
          4. Stay in ACCUMULATING
        """
        # Move from DETECTED to ACCUMULATING on second observation
        if state.current_stage == STAGE_DETECTED and state.observation_count >= 2:
            state.current_stage = STAGE_ACCUMULATING

        # Check invalidation
        reason = self._transition.check_invalidation(state, obs)
        if reason:
            state.current_stage = STAGE_INVALIDATED
            state.invalidated = True
            state.invalidation_reason = reason
            log.info(f"[CANDIDATE] {state.symbol} INVALIDATED: {reason}")
            return

        # Check fast-track
        if self._transition.check_fast_track(state, obs):
            state.current_stage = STAGE_READY
            state.fast_tracked = True
            log.info(f"[CANDIDATE] {state.symbol} FAST-TRACKED to READY "
                     f"(rvol={obs.get('rvol', 0):.1f}, "
                     f"candles={obs.get('valid_candles', 0)})")
            return

        # Check normal accumulation
        if self._transition.check_accumulation_complete(state):
            state.current_stage = STAGE_READY
            log.info(f"[CANDIDATE] {state.symbol} accumulated → READY "
                     f"(obs={state.observation_count})")
            return

        # Stay in current stage
        if state.current_stage == STAGE_DETECTED:
            state.current_stage = STAGE_ACCUMULATING

    def _process_wait_pullback(self, state: CandidateState, obs: dict):
        """
        Process WAIT_PULLBACK stage.
        
        Checks:
          1. Pullback recovery (price bounced) → READY for re-score
          2. Breakout lost entirely → INVALIDATED
          3. Stale pullback (4+ scans) → INVALIDATED
        """
        # Check if breakout is completely lost
        ltp = obs.get("ltp", 0)
        if state.breakout_level > 0 and state.side:
            if state.side == "BUY" and ltp < state.breakout_level:
                state.current_stage = STAGE_INVALIDATED
                state.invalidated = True
                state.invalidation_reason = "PULLBACK_BREAKOUT_LOST"
                return
            if state.side == "SELL" and ltp > state.breakout_level:
                state.current_stage = STAGE_INVALIDATED
                state.invalidated = True
                state.invalidation_reason = "PULLBACK_BREAKOUT_LOST"
                return

        # Check recovery
        if self._transition.check_pullback_recovery(state, obs):
            state.current_stage = STAGE_READY
            state.score = None  # Reset score for re-evaluation
            log.info(f"[CANDIDATE] {state.symbol} pullback recovered → READY")
            return

        # Check stale pullback
        if self._transition.check_pullback_stale(state):
            state.current_stage = STAGE_INVALIDATED
            state.invalidated = True
            state.invalidation_reason = "STALE_PULLBACK"
            return

    # ─── Query methods ────────────────────────────────────────────────

    def get_ready_candidates(self) -> List[CandidateState]:
        """Return all candidates in READY stage (need scoring)."""
        with self._lock:
            return [
                copy.deepcopy(s) for s in self._candidates.values()
                if s.current_stage == STAGE_READY
            ]

    def get_scored_candidates(self) -> List[CandidateState]:
        """Return all candidates in SCORED stage (have actionable scores)."""
        with self._lock:
            return [
                copy.deepcopy(s) for s in self._candidates.values()
                if s.current_stage == STAGE_SCORED
            ]

    def get_active_candidates(self) -> List[CandidateState]:
        """Return all non-terminal candidates."""
        with self._lock:
            return [
                copy.deepcopy(s) for s in self._candidates.values()
                if s.current_stage in ACTIVE_STAGES
            ]

    def get_state(self, symbol: str) -> Optional[CandidateState]:
        """Get current state for a symbol, or None."""
        with self._lock:
            state = self._candidates.get(symbol)
            return copy.deepcopy(state) if state else None

    # ─── Scoring ──────────────────────────────────────────────────────

    def mark_scored(self, symbol: str, score: float, timing_score: float,
                    base_score: float, move_stage: str, entry_type: str,
                    position_pct: float):
        """
        Update candidate with computed scores. Transitions READY → SCORED.
        
        Args:
            symbol:       Stock symbol
            score:        Combined score (base_score + timing_score)
            timing_score: Timing component
            base_score:   V10.1-R base component
            move_stage:   EARLY_ENTRY / LATE_CONTINUATION / EXHAUSTED / NORMAL
            entry_type:   NORMAL / EARLY_ENTRY
            position_pct: 1.0 or 0.3
        """
        with self._lock:
            state = self._candidates.get(symbol)
            if not state:
                log.warning(f"[CANDIDATE] mark_scored: unknown symbol {symbol}")
                return
            
            state.score = score
            state.timing_score = timing_score
            state.base_score = base_score
            state.move_stage = move_stage
            state.entry_type = entry_type
            state.position_pct = position_pct

            if move_stage == "EXHAUSTED":
                state.current_stage = STAGE_EXHAUSTED
                state.veto_reason = "EXHAUSTED"
                log.info(f"[CANDIDATE] {symbol} → EXHAUSTED (move_stage)")
            elif state.current_stage == STAGE_READY:
                state.current_stage = STAGE_SCORED
                log.info(f"[CANDIDATE] {symbol} → SCORED "
                         f"(score={score:.2f}, stage={move_stage})")

    # ─── Decision recording ──────────────────────────────────────────

    def mark_decision(self, symbol: str, v10r_decision: str,
                      v853_decision: str, final_action: str,
                      veto_reason: str = None):
        """
        Record the final decision for a candidate.
        
        Args:
            symbol:        Stock symbol
            v10r_decision: ENTER / WATCH / VETO
            v853_decision: ENTER_NOW / DO_NOT_TRADE / WATCH
            final_action:  ENTER / WATCH / VETO / ACCUMULATE
            veto_reason:   If vetoed, the reason string
        """
        with self._lock:
            state = self._candidates.get(symbol)
            if not state:
                log.warning(f"[CANDIDATE] mark_decision: unknown symbol {symbol}")
                return

            state.v10r_decision = v10r_decision
            state.v853_decision = v853_decision
            state.final_action = final_action

            if veto_reason:
                state.veto_reason = veto_reason

            if final_action == "VETO":
                state.current_stage = STAGE_INVALIDATED
                state.invalidated = True
                state.invalidation_reason = veto_reason or "VETOED"
            elif final_action == "WATCH" and state.current_stage == STAGE_SCORED:
                # Stay SCORED — re-evaluate next scan
                pass
            elif final_action == "ACCUMULATE":
                state.current_stage = STAGE_ACCUMULATING

    # ─── Terminal transitions ─────────────────────────────────────────

    def invalidate(self, symbol: str, reason: str):
        """Move candidate to INVALIDATED stage."""
        with self._lock:
            state = self._candidates.get(symbol)
            if not state:
                log.warning(f"[CANDIDATE] invalidate: unknown symbol {symbol}")
                return
            state.current_stage = STAGE_INVALIDATED
            state.invalidated = True
            state.invalidation_reason = reason
            log.info(f"[CANDIDATE] {symbol} INVALIDATED: {reason}")

    def mark_exhausted(self, symbol: str):
        """Move candidate to EXHAUSTED stage."""
        with self._lock:
            state = self._candidates.get(symbol)
            if not state:
                log.warning(f"[CANDIDATE] mark_exhausted: unknown symbol {symbol}")
                return
            state.current_stage = STAGE_EXHAUSTED
            state.veto_reason = "EXHAUSTED"
            log.info(f"[CANDIDATE] {symbol} → EXHAUSTED")

    # ─── Wait pullback entry ─────────────────────────────────────────

    def mark_wait_pullback(self, symbol: str):
        """Move candidate to WAIT_PULLBACK stage (score below gate but setup valid)."""
        with self._lock:
            state = self._candidates.get(symbol)
            if not state:
                log.warning(f"[CANDIDATE] mark_wait_pullback: unknown symbol {symbol}")
                return
            state.current_stage = STAGE_WAIT_PULLBACK
            log.info(f"[CANDIDATE] {symbol} → WAIT_PULLBACK")

    # ─── Persistence ──────────────────────────────────────────────────

    def save(self):
        """
        Persist all candidate states to JSON file (survives restart).
        
        File: {base_path}/candidate_states.json
        Format: {"version": "V11.0", "saved_at": "ISO", "candidates": {…}}
        
        Graceful: on failure, logs error but does not crash.
        """
        if not self._persistence_file:
            return

        with self._lock:
            payload = {
                "version": VERSION,
                "saved_at": _utc_now_iso(),
                "candidates": {
                    sym: state.to_dict()
                    for sym, state in self._candidates.items()
                },
            }

        try:
            # Atomic write: write to temp, then rename
            tmp_path = self._persistence_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            # os.replace is atomic on POSIX, near-atomic on Windows
            os.replace(tmp_path, self._persistence_file)
            log.debug(f"[CANDIDATE] Saved {len(payload['candidates'])} candidates")
        except Exception as e:
            log.error(f"[CANDIDATE] Save failed: {e}")

    def load(self):
        """
        Load candidate states from JSON file (called on startup).
        
        Allows resume after crash. Graceful: missing/corrupt file → empty state.
        """
        if not self._persistence_file:
            return
        if not os.path.exists(self._persistence_file):
            return

        try:
            with open(self._persistence_file, "r", encoding="utf-8") as f:
                payload = json.load(f)

            candidates_raw = payload.get("candidates", {})
            with self._lock:
                self._candidates.clear()
                for sym, d in candidates_raw.items():
                    self._candidates[sym] = CandidateState.from_dict(d)

            log.info(f"[CANDIDATE] Loaded {len(self._candidates)} candidates "
                     f"from {self._persistence_file}")
        except Exception as e:
            log.error(f"[CANDIDATE] Load failed: {e}")

    # ─── Summary ──────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """
        Return summary: counts by stage, avg observations, fast_track count.
        
        Returns:
            dict with keys: total, by_stage, avg_observations,
                           fast_tracked, active, terminal
        """
        with self._lock:
            by_stage: Dict[str, int] = {}
            total_obs = 0
            fast_tracked = 0

            for state in self._candidates.values():
                stage = state.current_stage
                by_stage[stage] = by_stage.get(stage, 0) + 1
                total_obs += state.observation_count
                if state.fast_tracked:
                    fast_tracked += 1

            total = len(self._candidates)
            active = sum(
                1 for s in self._candidates.values()
                if s.current_stage in ACTIVE_STAGES
            )
            terminal = sum(
                1 for s in self._candidates.values()
                if s.current_stage in TERMINAL_STAGES
            )
            avg_obs = total_obs / total if total > 0 else 0.0

            return {
                "total": total,
                "by_stage": by_stage,
                "avg_observations": round(avg_obs, 2),
                "fast_tracked": fast_tracked,
                "active": active,
                "terminal": terminal,
            }

    # ─── Cleanup ──────────────────────────────────────────────────────

    def cleanup_stale(self, max_age_minutes: int = 30):
        """
        Remove candidates not seen in max_age_minutes.
        
        Preserves candidates in terminal states (for audit trail).
        Only removes ACTIVE candidates that have gone stale.
        """
        with self._lock:
            to_remove = []
            for sym, state in self._candidates.items():
                if state.current_stage in TERMINAL_STAGES:
                    # Keep terminal states for audit
                    age = _minutes_since(state.last_seen)
                    if age > max_age_minutes:
                        to_remove.append(sym)
                elif state.current_stage in ACTIVE_STAGES:
                    age = _minutes_since(state.last_seen)
                    if age > max_age_minutes:
                        to_remove.append(sym)

            for sym in to_remove:
                del self._candidates[sym]

            if to_remove:
                log.info(f"[CANDIDATE] Cleaned up {len(to_remove)} stale: {to_remove}")

    def reset(self):
        """Clear all candidates (called at session start / new trading day)."""
        with self._lock:
            count = len(self._candidates)
            self._candidates.clear()
            log.info(f"[CANDIDATE] Reset — cleared {count} candidates")

    # ─── Internals ────────────────────────────────────────────────────

    @property
    def candidate_count(self) -> int:
        """Return total number of tracked candidates."""
        with self._lock:
            return len(self._candidates)

    def _get_raw(self, symbol: str) -> Optional[CandidateState]:
        """Get raw reference (no copy, no lock) — for internal use only."""
        return self._candidates.get(symbol)


# ═══════════════════════════════════════════════════════════════════════════
# MODULE SELF-TEST — run with: python v11_candidate_intelligence.py
# ═══════════════════════════════════════════════════════════════════════════

def _self_test():
    """Quick smoke test — detailed tests in test_v11_candidate_intelligence.py."""
    import tempfile, shutil

    tmp = tempfile.mkdtemp(prefix="ci_selftest_")
    try:
        mgr = CandidateManager(base_path=tmp)

        # Create a candidate
        obs1 = {
            "time": _utc_now_iso(), "ltp": 205.0, "volume": 100000,
            "valid_candles": 3, "rvol": 1.5, "momentum_5m": 0.5, "atr": 2.0,
        }
        state = mgr.update_candidate("IDBI", "sid_123", obs1,
                                      breakout_level=200.0, side="BUY")
        assert state.symbol == "IDBI"
        assert state.current_stage in (STAGE_DETECTED, STAGE_ACCUMULATING)

        # Accumulate
        for i in range(3):
            obs = {
                "time": _utc_now_iso(), "ltp": 206.0 + i, "volume": 110000 + i * 1000,
                "valid_candles": 6, "rvol": 2.0, "momentum_5m": 0.6, "atr": 2.0,
            }
            state = mgr.update_candidate("IDBI", "sid_123", obs,
                                          breakout_level=200.0, side="BUY")

        # Should be READY after enough accumulation
        assert state.current_stage == STAGE_READY, f"Expected READY, got {state.current_stage}"

        # Score it
        mgr.mark_scored("IDBI", score=7.5, timing_score=1.2, base_score=6.3,
                        move_stage="EARLY_ENTRY", entry_type="EARLY_ENTRY",
                        position_pct=1.0)
        state = mgr.get_state("IDBI")
        assert state.current_stage == STAGE_SCORED

        # Persist and reload
        mgr.save()
        mgr2 = CandidateManager(base_path=tmp)
        mgr2.load()
        state2 = mgr2.get_state("IDBI")
        assert state2 is not None
        assert state2.score == 7.5
        assert state2.current_stage == STAGE_SCORED

        # Summary
        summary = mgr.get_summary()
        assert summary["total"] == 1

        print(f"[SELF-TEST] v11_candidate_intelligence: ALL CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    _self_test()
