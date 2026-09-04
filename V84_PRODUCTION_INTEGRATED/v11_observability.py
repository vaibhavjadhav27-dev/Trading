"""
Observability Engine — V11.2.0 (2026-08-31)
=============================================
Patch 2 of the V11 deployment plan.  Standalone observability foundation
for the V11 trading bot (NSE intraday ORB on Dhan broker).

Provides 4 mandatory JSONL ledgers + latency tracing + session summary.
All ledgers are append-only, one-JSON-dict-per-line, file-per-day under
``{base_path}/ledger/``.

Ledgers
-------
1. **Candidate Ledger** — ``candidates_YYYY-MM-DD.jsonl``
   Every stock in ANY scan cycle.  Fields: symbol, stage, score,
   v10r/v853/v11 decisions, move_stage, entry_type, position_pct,
   market-data snapshot, reason, final_action.

2. **Trade Lifecycle Ledger** — ``trades_YYYY-MM-DD.jsonl``
   One entry per state transition of a trade.  Fields: trade_id,
   event, fill/SL data, R-multiples, MFE/MAE, pnl.

3. **Execution Ledger** — ``execution_YYYY-MM-DD.jsonl``
   Every broker interaction.  Fields: order_id, order_type, latencies,
   broker_response (truncated 500 chars), success, kill_switch_active.

4. **Missed Opportunity Ledger** — ``missed_YYYY-MM-DD.jsonl``
   Candidates that were VETOED/WATCHED but moved favourably.  Updated
   periodically with subsequent_high/low and missed_r.

Latency Tracing
---------------
Per-scan-cycle pipeline profiling:
  scan_start → scan_ltp_done → scan_ohlc_done → scan_scoring_done
  → decision_done → order_submitted → fill_confirmed

Session Summary
---------------
``get_session_summary()`` → counts per ledger, avg latency, candidate
funnel stats.

Public Interface
----------------
::

    engine = ObservabilityEngine(base_path="/home/ubuntu/trading-bot")
    engine.log_candidate({...})
    engine.log_trade_event({...})
    engine.log_execution({...})
    engine.log_missed({...})
    tid = engine.start_latency_trace(scan_cycle=1)
    engine.mark_latency(tid, "scan_start")
    engine.mark_latency(tid, "scan_ltp_done")
    engine.finish_latency_trace(tid)
    engine.update_missed_opportunities({"SYMBOL": 520.0, ...})
    summary = engine.get_session_summary()

Implementation Notes
--------------------
- stdlib only — no external dependencies
- ``threading.Lock()`` per ledger for concurrent writes
- Graceful fallback: write failure → log error, never crash the bot
- ISO UTC timestamps everywhere
- Importable standalone: ``python -c "from v11_observability import ObservabilityEngine"``

Depends on: nothing (Patch 1 execution_integrity_v11.py is a peer, not a dep).
"""

from __future__ import annotations
import json, os, logging, threading, time, uuid
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Optional, Dict, List, Any

log = logging.getLogger("v11_observability")
IST = timezone(timedelta(hours=5, minutes=30))

# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS — field enums for validation
# ═══════════════════════════════════════════════════════════════════════

CANDIDATE_STAGES = frozenset({
    "DETECTED", "ACCUMULATING", "READY", "SCORED",
    "WAIT_PULLBACK", "EXHAUSTED", "INVALIDATED",
})

CANDIDATE_ACTIONS = frozenset({
    "ENTER", "WATCH", "VETO", "ACCUMULATE",
})

TRADE_EVENTS = frozenset({
    "INTENT_CREATED", "ORDER_SUBMITTED", "FILL_CONFIRMED",
    "SL_VERIFIED", "POSITION_ACTIVE", "MONITOR_TICK",
    "TIGHTEN", "EXIT", "EMERGENCY_CLOSE", "EOD_CLOSE",
})

EXECUTION_EVENTS = frozenset({
    "ORDER_SUBMIT", "ORDER_FILL", "ORDER_CANCEL", "ORDER_REJECT",
    "SL_SUBMIT", "SL_VERIFY", "SL_REJECT", "SL_MODIFY",
    "RECONCILE", "ADOPT", "EMERGENCY_CLOSE", "EOD_CLOSE",
})

POSITION_STATES = frozenset({
    "INITIAL", "PROVEN", "PROFIT", "RUNNER",
})

MOVE_STAGES = frozenset({
    "EARLY_ENTRY", "LATE_CONTINUATION", "EXHAUSTED", "NORMAL",
})

LATENCY_STAGES = (
    "scan_start", "scan_ltp_done", "scan_ohlc_done",
    "scan_scoring_done", "decision_done",
    "order_submitted", "fill_confirmed",
)


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _utc_now_iso() -> str:
    """ISO 8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    """YYYY-MM-DD for today (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _truncate(s: str, maxlen: int = 500) -> str:
    """Truncate a string to maxlen chars for ledger storage."""
    if not isinstance(s, str):
        s = str(s)
    return s[:maxlen] if len(s) > maxlen else s


def _safe_json_line(data: dict) -> str:
    """Serialize dict to a single JSON line, with fallback for non-serializable values."""
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        # Nuclear fallback: stringify every value
        safe = {k: str(v) for k, v in data.items()}
        return json.dumps(safe, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# LEDGER WRITER — thread-safe, append-only, day-rotated JSONL
# ═══════════════════════════════════════════════════════════════════════

class _LedgerWriter:
    """
    Thread-safe, append-only JSONL writer with day-based rotation.

    Files are named ``{prefix}_YYYY-MM-DD.jsonl`` and stored in the
    ledger directory.  Each ``write()`` call opens the file in append
    mode, writes one JSON line, and closes — simple, crash-safe, and
    compatible with ``tail -f``.

    A ``threading.Lock`` serialises writes so concurrent scan + execution
    threads cannot interleave partial lines.
    """

    def __init__(self, ledger_dir: Path, prefix: str):
        self._dir = ledger_dir
        self._prefix = prefix
        self._lock = threading.Lock()
        self._write_count = 0

    # ── Core write ────────────────────────────────────────────────

    def write(self, data: dict) -> bool:
        """
        Append one JSON line to today's ledger file.

        Returns True on success, False on failure (with error logged).
        Never raises — the bot must not crash due to observability.
        """
        today = _today_str()
        filename = f"{self._prefix}_{today}.jsonl"
        filepath = self._dir / filename
        line = _safe_json_line(data) + "\n"

        with self._lock:
            try:
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(line)
                self._write_count += 1
                return True
            except OSError as e:
                log.error(
                    f"LEDGER WRITE FAIL [{self._prefix}]: {e} — "
                    f"data keys: {list(data.keys())}"
                )
                return False

    # ── Read-back for analysis ────────────────────────────────────

    def read_today(self) -> List[dict]:
        """Read all records from today's ledger file. Returns [] on error."""
        return self.read_date(_today_str())

    def read_date(self, date_str: str) -> List[dict]:
        """Read all records for a given date string (YYYY-MM-DD)."""
        filename = f"{self._prefix}_{date_str}.jsonl"
        filepath = self._dir / filename
        records = []
        if not filepath.exists():
            return records
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.warning(
                            f"LEDGER PARSE FAIL [{self._prefix}] "
                            f"line {line_no}: {line[:80]}"
                        )
        except OSError as e:
            log.error(f"LEDGER READ FAIL [{self._prefix}]: {e}")
        return records

    @property
    def write_count(self) -> int:
        return self._write_count

    def filepath_for_today(self) -> Path:
        return self._dir / f"{self._prefix}_{_today_str()}.jsonl"


# ═══════════════════════════════════════════════════════════════════════
# LATENCY TRACER — per-scan-cycle pipeline profiling
# ═══════════════════════════════════════════════════════════════════════

class _LatencyTrace:
    """In-flight latency trace for one scan cycle."""

    __slots__ = ("trace_id", "scan_cycle", "marks", "started_at", "finished")

    def __init__(self, trace_id: str, scan_cycle: int):
        self.trace_id = trace_id
        self.scan_cycle = scan_cycle
        self.marks: Dict[str, float] = {}  # stage → epoch seconds
        self.started_at = time.monotonic()
        self.finished = False

    def mark(self, stage: str) -> None:
        self.marks[stage] = time.time()

    def compute_deltas(self) -> Dict[str, Any]:
        """Compute inter-stage deltas in milliseconds."""
        deltas: Dict[str, Any] = {}
        ordered = sorted(self.marks.items(), key=lambda kv: kv[1])
        for i in range(1, len(ordered)):
            prev_stage, prev_ts = ordered[i - 1]
            curr_stage, curr_ts = ordered[i]
            key = f"{prev_stage}_to_{curr_stage}_ms"
            deltas[key] = round((curr_ts - prev_ts) * 1000, 2)

        # Summary fields
        if len(ordered) >= 2:
            deltas["total_ms"] = round(
                (ordered[-1][1] - ordered[0][1]) * 1000, 2
            )

        # Named convenience deltas for the spec
        for name, (start, end) in {
            "ltp_ms": ("scan_start", "scan_ltp_done"),
            "ohlc_ms": ("scan_ltp_done", "scan_ohlc_done"),
            "scoring_ms": ("scan_ohlc_done", "scan_scoring_done"),
            "decision_ms": ("scan_scoring_done", "decision_done"),
            "total_scan_ms": ("scan_start", "decision_done"),
        }.items():
            if start in self.marks and end in self.marks:
                deltas[name] = round(
                    (self.marks[end] - self.marks[start]) * 1000, 2
                )

        return deltas


# ═══════════════════════════════════════════════════════════════════════
# OBSERVABILITY ENGINE — main class
# ═══════════════════════════════════════════════════════════════════════

class ObservabilityEngine:
    """
    Central observability hub for V11.

    Instantiate once at bot startup.  All methods are thread-safe and
    crash-proof (errors are logged, never raised to the caller).

    Parameters
    ----------
    base_path : str
        Root directory for the bot (e.g. ``/home/ubuntu/trading-bot``).
        Ledger files go under ``{base_path}/ledger/``.
    """

    def __init__(self, base_path: str):
        self.base_path = Path(base_path)
        self.ledger_dir = self.base_path / "ledger"

        # Create ledger directory (graceful if exists)
        try:
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error(f"OBSERVABILITY: Cannot create ledger dir {self.ledger_dir}: {e}")

        # Initialise the 4 ledger writers
        self._candidate_writer = _LedgerWriter(self.ledger_dir, "candidates")
        self._trade_writer = _LedgerWriter(self.ledger_dir, "trades")
        self._execution_writer = _LedgerWriter(self.ledger_dir, "execution")
        self._missed_writer = _LedgerWriter(self.ledger_dir, "missed")

        # Latency traces — {trace_id: _LatencyTrace}
        self._traces: Dict[str, _LatencyTrace] = {}
        self._traces_lock = threading.Lock()

        # In-memory missed-candidates cache for update_missed_opportunities
        self._missed_cache: Dict[str, dict] = {}  # keyed by "symbol_side"
        self._missed_lock = threading.Lock()

        # Session-level counters
        self._session_start = _utc_now_iso()
        self._latency_summaries: List[dict] = []

        log.info(
            f"ObservabilityEngine initialised — ledger_dir={self.ledger_dir}"
        )

    # ══════════════════════════════════════════════════════════════
    # 1. CANDIDATE LEDGER
    # ══════════════════════════════════════════════════════════════

    # Full list of fields for the candidate ledger record
    _CANDIDATE_FIELDS = (
        "timestamp", "scan_cycle", "symbol", "security_id", "side",
        "stage", "observations_count", "score", "timing_score",
        "base_score", "v10r_decision", "v853_decision", "v11_stage",
        "legacy_v10r_stage", "legacy_v853_stage", "move_stage",
        "entry_type", "position_pct", "ltp", "vwap", "atr", "rvol",
        "momentum_5m", "momentum_15m", "breakout_level",
        "distance_from_breakout_atr", "reason", "final_action",
    )

    def log_candidate(self, data: dict) -> bool:
        """
        Write one candidate record to the candidate ledger.

        Caller passes a dict with the relevant fields.  Missing fields
        are filled with ``None``.  ``timestamp`` is auto-set if absent.

        Returns True on success, False on write failure.
        """
        try:
            record = {f: data.get(f) for f in self._CANDIDATE_FIELDS}
            if record["timestamp"] is None:
                record["timestamp"] = _utc_now_iso()
            return self._candidate_writer.write(record)
        except Exception as e:
            log.error(f"OBSERVABILITY log_candidate error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    # 2. TRADE LIFECYCLE LEDGER
    # ══════════════════════════════════════════════════════════════

    _TRADE_FIELDS = (
        "timestamp", "trade_id", "event", "symbol", "security_id",
        "side", "qty", "fill_price", "current_price", "sl_level",
        "sl_order_id", "initial_sl", "current_sl", "entry_score",
        "entry_type", "position_pct", "current_r", "peak_r",
        "giveback_r", "mfe", "mae", "position_state", "exit_reason",
        "pnl", "pnl_r", "mfe_capture_pct",
    )

    def log_trade_event(self, data: dict) -> bool:
        """
        Write one trade lifecycle record.

        For EXIT events, ``mfe_capture_pct`` is auto-computed if
        ``peak_r`` and ``current_r`` (or ``pnl_r``) are present and
        ``mfe_capture_pct`` is not already set.

        Returns True on success.
        """
        try:
            record = {f: data.get(f) for f in self._TRADE_FIELDS}
            if record["timestamp"] is None:
                record["timestamp"] = _utc_now_iso()

            # Auto-compute mfe_capture_pct for EXIT events
            event = record.get("event", "")
            if event in ("EXIT", "EMERGENCY_CLOSE", "EOD_CLOSE"):
                if record.get("mfe_capture_pct") is None:
                    peak_r = record.get("peak_r")
                    exit_r = record.get("pnl_r") or record.get("current_r")
                    if peak_r and exit_r and peak_r != 0:
                        try:
                            record["mfe_capture_pct"] = round(
                                float(exit_r) / float(peak_r) * 100, 2
                            )
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass

            return self._trade_writer.write(record)
        except Exception as e:
            log.error(f"OBSERVABILITY log_trade_event error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    # 3. EXECUTION LEDGER
    # ══════════════════════════════════════════════════════════════

    _EXECUTION_FIELDS = (
        "timestamp", "event", "symbol", "security_id", "side",
        "order_id", "order_type", "requested_qty", "filled_qty",
        "requested_price", "fill_price", "trigger_price",
        "sl_builder_price", "broker_response", "latency_ms",
        "success", "error_message", "kill_switch_active",
    )

    def log_execution(self, data: dict) -> bool:
        """
        Write one execution record.

        ``broker_response`` is auto-truncated to 500 chars.
        Returns True on success.
        """
        try:
            record = {f: data.get(f) for f in self._EXECUTION_FIELDS}
            if record["timestamp"] is None:
                record["timestamp"] = _utc_now_iso()

            # Truncate broker_response
            if record.get("broker_response") is not None:
                record["broker_response"] = _truncate(
                    str(record["broker_response"]), 500
                )

            return self._execution_writer.write(record)
        except Exception as e:
            log.error(f"OBSERVABILITY log_execution error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    # 4. MISSED OPPORTUNITY LEDGER
    # ══════════════════════════════════════════════════════════════

    _MISSED_FIELDS = (
        "timestamp", "symbol", "security_id", "side", "veto_reason",
        "score_at_veto", "ltp_at_veto", "subsequent_high",
        "subsequent_low", "missed_r", "would_have_been_winner",
    )

    def log_missed(self, data: dict) -> bool:
        """
        Write one missed-opportunity record AND cache it for later
        ``update_missed_opportunities()`` calls.

        Returns True on success.
        """
        try:
            record = {f: data.get(f) for f in self._MISSED_FIELDS}
            if record["timestamp"] is None:
                record["timestamp"] = _utc_now_iso()

            # Initialise tracking fields if absent
            if record.get("subsequent_high") is None:
                record["subsequent_high"] = record.get("ltp_at_veto")
            if record.get("subsequent_low") is None:
                record["subsequent_low"] = record.get("ltp_at_veto")
            if record.get("missed_r") is None:
                record["missed_r"] = 0.0
            if record.get("would_have_been_winner") is None:
                record["would_have_been_winner"] = False

            # Cache for subsequent updates
            cache_key = f"{record.get('symbol')}_{record.get('side')}"
            with self._missed_lock:
                self._missed_cache[cache_key] = record.copy()

            return self._missed_writer.write(record)
        except Exception as e:
            log.error(f"OBSERVABILITY log_missed error: {e}")
            return False

    def update_missed_opportunities(self, current_prices: Dict[str, float]) -> int:
        """
        Update subsequent_high/low for all missed candidates today,
        given a map of symbol → current LTP.

        For each cached missed candidate:
          - BUY side: track subsequent_high (highest price seen after veto)
          - SELL side: track subsequent_low (lowest price seen after veto)
          - Recompute missed_r and would_have_been_winner

        Writes updated records to ledger (as new JSONL lines — append-only).
        Returns the count of records updated.
        """
        updated = 0
        with self._missed_lock:
            for cache_key, record in self._missed_cache.items():
                symbol = record.get("symbol")
                if symbol not in current_prices:
                    continue

                current_ltp = current_prices[symbol]
                side = record.get("side", "").upper()
                ltp_at_veto = record.get("ltp_at_veto")
                changed = False

                if side == "BUY":
                    old_high = record.get("subsequent_high") or 0.0
                    if current_ltp > old_high:
                        record["subsequent_high"] = current_ltp
                        changed = True
                elif side == "SELL":
                    old_low = record.get("subsequent_low")
                    if old_low is None or current_ltp < old_low:
                        record["subsequent_low"] = current_ltp
                        changed = True

                if changed and ltp_at_veto and ltp_at_veto > 0:
                    # Compute missed_r
                    # missed_r = what the R-multiple WOULD have been
                    # For BUY: (subsequent_high - ltp_at_veto) / SL_distance
                    # For SELL: (ltp_at_veto - subsequent_low) / SL_distance
                    # We approximate SL distance as 0.75% of ltp_at_veto
                    # (standard hard SL from V10.1)
                    sl_distance = ltp_at_veto * 0.0075
                    if sl_distance > 0:
                        if side == "BUY":
                            missed_r = (
                                (record.get("subsequent_high", 0) - ltp_at_veto)
                                / sl_distance
                            )
                        else:  # SELL
                            missed_r = (
                                (ltp_at_veto - record.get("subsequent_low", ltp_at_veto))
                                / sl_distance
                            )
                        record["missed_r"] = round(missed_r, 3)
                        record["would_have_been_winner"] = missed_r > 1.0

                    record["timestamp"] = _utc_now_iso()
                    self._missed_writer.write(record)
                    updated += 1

        return updated

    # ══════════════════════════════════════════════════════════════
    # LATENCY TRACING
    # ══════════════════════════════════════════════════════════════

    def start_latency_trace(self, scan_cycle: int) -> str:
        """
        Begin a latency trace for a scan cycle.

        Returns a trace_id (UUID) to be passed to mark_latency()
        and finish_latency_trace().
        """
        trace_id = f"LT_{scan_cycle}_{uuid.uuid4().hex[:8]}"
        trace = _LatencyTrace(trace_id, scan_cycle)
        with self._traces_lock:
            self._traces[trace_id] = trace
        return trace_id

    def mark_latency(self, trace_id: str, stage: str) -> bool:
        """
        Record the current timestamp for a pipeline stage.

        Valid stages: scan_start, scan_ltp_done, scan_ohlc_done,
        scan_scoring_done, decision_done, order_submitted, fill_confirmed.

        Returns True if recorded, False if trace_id unknown.
        """
        with self._traces_lock:
            trace = self._traces.get(trace_id)
        if trace is None:
            log.warning(f"LATENCY: unknown trace_id {trace_id}")
            return False
        trace.mark(stage)
        return True

    def finish_latency_trace(self, trace_id: str) -> Optional[dict]:
        """
        Finish a latency trace: compute deltas, write LATENCY_TRACE
        event to the execution ledger, and return the summary dict.

        Returns the latency summary dict, or None if trace_id unknown.
        """
        with self._traces_lock:
            trace = self._traces.pop(trace_id, None)
        if trace is None:
            log.warning(f"LATENCY: cannot finish unknown trace_id {trace_id}")
            return None

        trace.finished = True
        deltas = trace.compute_deltas()
        summary = {
            "trace_id": trace_id,
            "scan_cycle": trace.scan_cycle,
            "marks": {stage: round(ts, 6) for stage, ts in trace.marks.items()},
            "deltas": deltas,
        }

        # Write as LATENCY_TRACE event in the execution ledger
        self._execution_writer.write({
            "timestamp": _utc_now_iso(),
            "event": "LATENCY_TRACE",
            "symbol": None,
            "security_id": None,
            "side": None,
            "order_id": None,
            "order_type": None,
            "requested_qty": None,
            "filled_qty": None,
            "requested_price": None,
            "fill_price": None,
            "trigger_price": None,
            "sl_builder_price": None,
            "broker_response": None,
            "latency_ms": deltas.get("total_ms"),
            "success": True,
            "error_message": None,
            "kill_switch_active": None,
            "trace_id": trace_id,
            "scan_cycle": trace.scan_cycle,
            "latency_marks": summary["marks"],
            "latency_deltas": deltas,
        })

        self._latency_summaries.append(summary)
        return summary

    # ══════════════════════════════════════════════════════════════
    # SESSION SUMMARY
    # ══════════════════════════════════════════════════════════════

    def get_session_summary(self) -> dict:
        """
        Compute a session-level summary across all ledgers.

        Returns a dict with:
          - ``session_start``: when ObservabilityEngine was created
          - ``candidate_count``: total candidate records today
          - ``trade_count``: total trade records today
          - ``execution_count``: total execution records today
          - ``missed_count``: total missed records today
          - ``candidate_funnel``: {stage: count} breakdown
          - ``avg_latency_ms``: average total_ms across all traces
          - ``latency_traces``: count of completed traces
          - ``missed_would_have_won``: count of missed that were winners
        """
        try:
            candidates = self._candidate_writer.read_today()
            trades = self._trade_writer.read_today()
            executions = self._execution_writer.read_today()
            missed = self._missed_writer.read_today()

            # Candidate funnel by final_action
            funnel: Dict[str, int] = {}
            for c in candidates:
                action = c.get("final_action") or "UNKNOWN"
                funnel[action] = funnel.get(action, 0) + 1

            # Stage breakdown
            stage_counts: Dict[str, int] = {}
            for c in candidates:
                stage = c.get("stage") or "UNKNOWN"
                stage_counts[stage] = stage_counts.get(stage, 0) + 1

            # Average latency
            total_latencies = [
                s["deltas"].get("total_ms", 0)
                for s in self._latency_summaries
                if "deltas" in s and s["deltas"].get("total_ms") is not None
            ]
            avg_latency = (
                round(sum(total_latencies) / len(total_latencies), 2)
                if total_latencies else 0.0
            )

            # Missed winners
            missed_winners = sum(
                1 for m in missed
                if m.get("would_have_been_winner") is True
            )

            return {
                "session_start": self._session_start,
                "summary_generated_at": _utc_now_iso(),
                "candidate_count": len(candidates),
                "trade_count": len(trades),
                "execution_count": len(executions),
                "missed_count": len(missed),
                "candidate_funnel": funnel,
                "candidate_stages": stage_counts,
                "avg_latency_ms": avg_latency,
                "latency_traces": len(self._latency_summaries),
                "missed_would_have_won": missed_winners,
            }
        except Exception as e:
            log.error(f"OBSERVABILITY get_session_summary error: {e}")
            return {
                "session_start": self._session_start,
                "error": str(e),
            }

    # ══════════════════════════════════════════════════════════════
    # ACCESSOR PROPERTIES
    # ══════════════════════════════════════════════════════════════

    @property
    def candidate_writer(self) -> _LedgerWriter:
        return self._candidate_writer

    @property
    def trade_writer(self) -> _LedgerWriter:
        return self._trade_writer

    @property
    def execution_writer(self) -> _LedgerWriter:
        return self._execution_writer

    @property
    def missed_writer(self) -> _LedgerWriter:
        return self._missed_writer


# ═══════════════════════════════════════════════════════════════════════
# SELF-TEST — runs when executed directly
# ═══════════════════════════════════════════════════════════════════════

def _self_test():
    """Quick smoke tests — mirrors Patch 1 pattern."""
    import tempfile, shutil

    print("=" * 60)
    print("v11_observability.py — self-test")
    print("=" * 60)

    passed = 0
    failed = 0
    total = 0

    def check(name, condition):
        nonlocal passed, failed, total
        total += 1
        if condition:
            passed += 1
            print(f"  ✓ {name}")
        else:
            failed += 1
            print(f"  ✗ {name}")

    # Setup temp dir
    tmp = tempfile.mkdtemp(prefix="v11_obs_test_")
    try:
        engine = ObservabilityEngine(tmp)
        check("Ledger dir created", engine.ledger_dir.exists())

        # 1. Candidate write + read
        engine.log_candidate({
            "scan_cycle": 1, "symbol": "RELIANCE", "security_id": "1234",
            "side": "BUY", "stage": "DETECTED", "final_action": "WATCH",
            "ltp": 2500.0, "score": None,
        })
        recs = engine._candidate_writer.read_today()
        check("Candidate written", len(recs) == 1)
        check("Candidate symbol", recs[0]["symbol"] == "RELIANCE")
        check("Candidate has timestamp", recs[0]["timestamp"] is not None)

        # 2. Trade write
        engine.log_trade_event({
            "trade_id": "T_RELIANCE_001", "event": "INTENT_CREATED",
            "symbol": "RELIANCE", "security_id": "1234", "side": "BUY",
            "qty": 10, "fill_price": 2500.0,
        })
        trecs = engine._trade_writer.read_today()
        check("Trade written", len(trecs) == 1)
        check("Trade event", trecs[0]["event"] == "INTENT_CREATED")

        # 3. Trade EXIT auto-computes mfe_capture_pct
        engine.log_trade_event({
            "trade_id": "T_RELIANCE_001", "event": "EXIT",
            "symbol": "RELIANCE", "peak_r": 2.0, "pnl_r": 1.5,
        })
        trecs2 = engine._trade_writer.read_today()
        check("MFE capture auto-computed",
              trecs2[-1].get("mfe_capture_pct") == 75.0)

        # 4. Execution write with truncation
        long_response = "x" * 1000
        engine.log_execution({
            "event": "ORDER_SUBMIT", "symbol": "INFY",
            "order_id": "O123", "success": True,
            "broker_response": long_response,
        })
        erecs = engine._execution_writer.read_today()
        check("Execution written", len(erecs) == 1)
        check("Broker response truncated",
              len(erecs[0]["broker_response"]) == 500)

        # 5. Missed opportunity
        engine.log_missed({
            "symbol": "TCS", "security_id": "5678", "side": "BUY",
            "veto_reason": "EXHAUSTED", "ltp_at_veto": 3500.0,
        })
        mrecs = engine._missed_writer.read_today()
        check("Missed written", len(mrecs) == 1)
        check("Missed has subsequent_high", mrecs[0]["subsequent_high"] == 3500.0)

        # 6. Update missed opportunities
        updated = engine.update_missed_opportunities({"TCS": 3600.0})
        check("Missed updated", updated == 1)
        # Read again — should have 2 records (original + update)
        mrecs2 = engine._missed_writer.read_today()
        check("Missed update appended", len(mrecs2) == 2)
        check("Subsequent high updated", mrecs2[-1]["subsequent_high"] == 3600.0)
        check("Missed_r positive", mrecs2[-1]["missed_r"] > 0)

        # 7. Latency trace
        tid = engine.start_latency_trace(scan_cycle=1)
        check("Trace ID starts with LT_", tid.startswith("LT_1_"))
        engine.mark_latency(tid, "scan_start")
        time.sleep(0.01)
        engine.mark_latency(tid, "scan_ltp_done")
        time.sleep(0.01)
        engine.mark_latency(tid, "scan_ohlc_done")
        time.sleep(0.01)
        engine.mark_latency(tid, "scan_scoring_done")
        time.sleep(0.01)
        engine.mark_latency(tid, "decision_done")
        summary = engine.finish_latency_trace(tid)
        check("Latency summary returned", summary is not None)
        check("Latency has total_scan_ms",
              "total_scan_ms" in summary.get("deltas", {}))
        check("Latency total > 0",
              summary["deltas"]["total_scan_ms"] > 0)

        # 8. Unknown trace_id
        check("Unknown trace returns False",
              engine.mark_latency("BOGUS", "scan_start") is False)
        check("Finish unknown returns None",
              engine.finish_latency_trace("BOGUS") is None)

        # 9. Session summary
        ss = engine.get_session_summary()
        check("Summary has candidate_count", ss["candidate_count"] >= 1)
        check("Summary has trade_count", ss["trade_count"] >= 1)
        check("Summary has missed_count", ss["missed_count"] >= 1)
        check("Summary has avg_latency_ms", "avg_latency_ms" in ss)
        check("Summary has candidate_funnel", "candidate_funnel" in ss)

        # 10. Thread safety — concurrent writes
        import concurrent.futures
        def write_candidate(i):
            return engine.log_candidate({
                "scan_cycle": i, "symbol": f"SYM_{i}",
                "security_id": str(i), "side": "BUY",
                "stage": "DETECTED", "final_action": "WATCH",
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(write_candidate, range(100)))
        check("100 concurrent writes succeeded", all(results))
        # Re-read and verify count (1 original + 100 concurrent)
        all_candidates = engine._candidate_writer.read_today()
        check("All 101 candidate records present", len(all_candidates) == 101)

        # 11. Graceful failure — read-only dir
        ro_dir = os.path.join(tmp, "readonly_ledger")
        os.makedirs(ro_dir, exist_ok=True)
        # Create a writer pointing to a file that can't be written
        ro_writer = _LedgerWriter(Path(ro_dir), "test")
        # Make directory read-only (platform-dependent, best effort)
        try:
            os.chmod(ro_dir, 0o444)
            result = ro_writer.write({"test": True})
            # On Windows or if root, write may still succeed
            # The key check is: no exception was raised
            check("Read-only dir doesn't crash", True)
        except Exception:
            check("Read-only dir doesn't crash", False)
        finally:
            os.chmod(ro_dir, 0o755)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    if failed > 0:
        print("*** FAILURES DETECTED ***")
        return False
    else:
        print("All self-tests passed.")
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    success = _self_test()
    raise SystemExit(0 if success else 1)
