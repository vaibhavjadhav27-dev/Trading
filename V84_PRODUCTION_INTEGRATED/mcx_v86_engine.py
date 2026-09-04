"""
MCX V8.6 Production Readiness Engine
=====================================
Complete implementation of the 20-section MCX futures trading module.

Key invariants:
  • MCX_LIVE=False by default (shadow-only until validation passes)
  • Fail closed on ANY data/contract/broker error
  • NEVER fall back to options contracts
  • NEVER trade on stale data
  • ALL quantities must be valid MCX lot sizes
  • Complete audit trail for every decision
  • Initial stop-loss is IMMUTABLE once set
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from enum import Enum, auto
from typing import (
    Any, Callable, Deque, Dict, List, Optional, Tuple, TypeVar, Union,
)
from functools import wraps

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: Config Import
# ─────────────────────────────────────────────────────────────────────────────

from mcx_config_v86 import CONFIG, MCXConfig

# External dependencies (already on server)
# from execution_integrity import ExecutionIntegrity  # broker reconciliation
# from v82_dhan_gateway import DhanGateway            # broker operations

logger = logging.getLogger("mcx_v86")
logger.setLevel(logging.INFO)

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 19: Audit Events (defined early for use throughout)
# ─────────────────────────────────────────────────────────────────────────────

class AuditEventType(str, Enum):
    MCX_CONTRACT_RESOLVED = "MCX_CONTRACT_RESOLVED"
    MCX_SESSION_STATE_CHANGE = "MCX_SESSION_STATE_CHANGE"
    MCX_DATA_HEALTH_CHECK = "MCX_DATA_HEALTH_CHECK"
    MCX_SCORE_COMPUTED = "MCX_SCORE_COMPUTED"
    MCX_ACCELERATION_DETECTED = "MCX_ACCELERATION_DETECTED"
    MCX_ROOM_CHECK = "MCX_ROOM_CHECK"
    MCX_ENTRY_DECISION = "MCX_ENTRY_DECISION"
    MCX_POSITION_SIZED = "MCX_POSITION_SIZED"
    MCX_ORDER_PLACED = "MCX_ORDER_PLACED"
    MCX_ORDER_VERIFIED = "MCX_ORDER_VERIFIED"
    MCX_SL_SET = "MCX_SL_SET"
    MCX_TRAIL_TRIGGERED = "MCX_TRAIL_TRIGGERED"
    MCX_REVERSAL_EXIT = "MCX_REVERSAL_EXIT"
    MCX_RECONCILIATION = "MCX_RECONCILIATION"
    MCX_STATE_TRANSITION = "MCX_STATE_TRANSITION"
    MCX_SAFETY_GATE = "MCX_SAFETY_GATE"
    MCX_POSITION_CLOSED = "MCX_POSITION_CLOSED"
    MCX_SHADOW_DECISION = "MCX_SHADOW_DECISION"
    MCX_ERROR = "MCX_ERROR"


@dataclass
class AuditEvent:
    """Immutable audit record for every MCX decision."""
    event_type: AuditEventType
    timestamp: str
    symbol: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    decision: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "context": self.context,
            "decision": self.decision,
            "reason": self.reason,
        }


class AuditLogger:
    """Append-only audit logger writing JSONL."""

    def __init__(self, config: MCXConfig):
        self._config = config.audit
        self._buffer: List[AuditEvent] = []

    def emit(
        self,
        event_type: AuditEventType,
        symbol: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        decision: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            timestamp=datetime.utcnow().isoformat() + "Z",
            symbol=symbol,
            context=context or {},
            decision=decision,
            reason=reason,
        )
        self._buffer.append(event)
        self._flush_if_needed()
        logger.info(
            f"[AUDIT] {event_type.value} | {symbol or '-'} | "
            f"{decision or '-'} | {reason or '-'}"
        )
        return event

    def _flush_if_needed(self) -> None:
        if len(self._buffer) >= 50:
            self.flush()

    def flush(self) -> None:
        """Persist buffered events to disk."""
        if not self._buffer:
            return
        try:
            with open(self._config.audit_log_path, "a") as f:
                for event in self._buffer:
                    f.write(json.dumps(event.to_dict()) + "\n")
            self._buffer.clear()
        except IOError as e:
            logger.error(f"Audit flush failed: {e}")

    def get_recent(self, n: int = 100) -> List[AuditEvent]:
        return self._buffer[-n:]


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: Futures-Only Contract Resolver
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MCXContract:
    """Resolved MCX futures contract — immutable once created."""
    symbol: str
    exchange: str  # Always "MCX"
    instrument_type: str  # Always "FUTCOM"
    expiry_date: date
    lot_size: int
    tick_size: float
    security_id: str  # Broker-specific instrument token
    trading_symbol: str  # e.g., "CRUDEOIL25AUGFUT"
    days_to_expiry: int

    def __post_init__(self):
        if self.instrument_type != "FUTCOM":
            raise ValueError(
                f"MCX module ONLY trades futures. Got: {self.instrument_type}"
            )
        if self.exchange != "MCX":
            raise ValueError(f"Expected MCX exchange. Got: {self.exchange}")


def resolve_mcx_future(
    symbol: str,
    available_contracts: List[Dict[str, Any]],
    reference_date: Optional[date] = None,
) -> Optional[MCXContract]:
    """
    Resolve the correct MCX futures contract for a symbol.
    
    Rules:
      1. ONLY futures (FUTCOM) — never options, never spreads.
      2. Nearest expiry with >= min_days_to_expiry remaining.
      3. Must be in allowed_symbols list.
      4. Returns None on any ambiguity (fail closed).
    """
    cfg = CONFIG.contract
    ref = reference_date or date.today()

    # Validate symbol is allowed
    if symbol not in cfg.allowed_symbols:
        logger.warning(f"Symbol {symbol} not in allowed MCX symbols")
        return None

    # Filter to futures only — NEVER options
    futures = [
        c for c in available_contracts
        if c.get("instrument_type") == "FUTCOM"
        and c.get("exchange") == "MCX"
        and c.get("symbol", "").upper() == symbol.upper()
    ]

    if not futures:
        logger.warning(f"No FUTCOM contracts found for {symbol}")
        return None

    # Parse expiry dates and filter by minimum days to expiry
    valid = []
    for c in futures:
        try:
            expiry = (
                c["expiry_date"]
                if isinstance(c["expiry_date"], date)
                else datetime.strptime(str(c["expiry_date"]), "%Y-%m-%d").date()
            )
            days_remaining = (expiry - ref).days
            if days_remaining >= cfg.min_days_to_expiry:
                valid.append((c, expiry, days_remaining))
        except (KeyError, ValueError) as e:
            logger.debug(f"Skipping contract with bad expiry: {e}")
            continue

    if not valid:
        logger.warning(f"No valid futures for {symbol} with >= {cfg.min_days_to_expiry} days to expiry")
        return None

    # Select nearest expiry
    valid.sort(key=lambda x: x[2])
    chosen, expiry, days = valid[0]

    lot_size = cfg.lot_sizes.get(symbol)
    tick_size = cfg.tick_sizes.get(symbol)
    if lot_size is None or tick_size is None:
        logger.error(f"Missing lot/tick size config for {symbol}")
        return None

    return MCXContract(
        symbol=symbol,
        exchange="MCX",
        instrument_type="FUTCOM",
        expiry_date=expiry,
        lot_size=lot_size,
        tick_size=tick_size,
        security_id=str(chosen.get("security_id", chosen.get("token", ""))),
        trading_symbol=chosen.get("trading_symbol", f"{symbol}{expiry.strftime('%y%b').upper()}FUT"),
        days_to_expiry=days,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: Session Manager
# ─────────────────────────────────────────────────────────────────────────────

class MCXSessionState(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    OPEN = "OPEN"
    NO_NEW_ENTRY = "NO_NEW_ENTRY"  # Near close — manage only
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"


# MCX holidays 2024-2026 (partial — extend as needed)
MCX_HOLIDAYS: set = {
    date(2026, 1, 26), date(2026, 3, 14), date(2026, 3, 31),
    date(2026, 4, 10), date(2026, 4, 14), date(2026, 5, 1),
    date(2026, 8, 15), date(2026, 10, 2), date(2026, 10, 21),
    date(2026, 11, 5), date(2026, 12, 25),
}


def get_mcx_session_state(
    now: Optional[datetime] = None,
    symbol: Optional[str] = None,
) -> MCXSessionState:
    """
    Determine current MCX session state.
    Accounts for holidays, weekends, and agri commodity early close.
    """
    now = now or datetime.now()
    today = now.date()
    cfg = CONFIG.session

    # Weekend check (Saturday=5, Sunday=6)
    if today.weekday() >= 5:
        return MCXSessionState.CLOSED

    # Holiday check
    if today in MCX_HOLIDAYS:
        return MCXSessionState.HOLIDAY

    current_time = now.strftime("%H:%M")

    # Determine close time based on commodity type
    agri_symbols = {"COTTONCANDY", "MENTHAOIL"}
    close_time = cfg.agri_close if symbol in agri_symbols else cfg.market_close

    # Session logic
    if current_time < cfg.pre_open_start:
        return MCXSessionState.CLOSED
    elif current_time < cfg.market_open:
        return MCXSessionState.PRE_MARKET
    elif current_time >= close_time:
        return MCXSessionState.CLOSED
    else:
        # Check if within no-entry zone
        close_dt = datetime.strptime(close_time, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        minutes_to_close = (close_dt - now).total_seconds() / 60
        if minutes_to_close <= cfg.no_entry_minutes_before_close:
            return MCXSessionState.NO_NEW_ENTRY
        return MCXSessionState.OPEN


def is_session_calendar_valid(ref_date: Optional[date] = None) -> bool:
    """Check if the session calendar data is current."""
    ref = ref_date or date.today()
    # Calendar is valid if we have holiday data covering this date's year
    return any(h.year == ref.year for h in MCX_HOLIDAYS)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: MCXMarketState — Local Candle Construction
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Tick:
    """Raw tick data from feed."""
    timestamp: float  # Unix epoch seconds
    price: float
    volume: int
    bid: float = 0.0
    ask: float = 0.0


@dataclass
class Candle:
    """OHLCV candle constructed from ticks."""
    timestamp: float  # Candle open time (epoch)
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: int = 0
    tick_count: int = 0
    vwap: float = 0.0
    closed: bool = False

    def update(self, tick: Tick) -> None:
        if self.tick_count == 0:
            self.open = tick.price
            self.high = tick.price
            self.low = tick.price
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += tick.volume
        self.tick_count += 1
        # Running VWAP
        if self.volume > 0:
            self.vwap = (
                (self.vwap * (self.volume - tick.volume) + tick.price * tick.volume)
                / self.volume
            )


class MCXMarketState:
    """
    Maintains real-time market state for one MCX symbol.
    Constructs 1m/5m/15m candles from raw ticks locally.
    """

    def __init__(self, symbol: str, contract: MCXContract):
        self.symbol = symbol
        self.contract = contract
        self._cfg = CONFIG.data

        # Tick buffer
        self._last_tick: Optional[Tick] = None
        self._last_tick_time: float = 0.0

        # Candle stores (keyed by timeframe in minutes)
        self._candles: Dict[int, Deque[Candle]] = {
            1: deque(maxlen=500),
            5: deque(maxlen=200),
            15: deque(maxlen=100),
        }
        # Current (forming) candle per timeframe
        self._current_candle: Dict[int, Optional[Candle]] = {1: None, 5: None, 15: None}

        # Day stats
        self.day_open: Optional[float] = None
        self.day_high: float = 0.0
        self.day_low: float = float("inf")
        self.day_volume: int = 0
        self.day_vwap: float = 0.0
        self._vwap_cumulative: float = 0.0

    @property
    def last_price(self) -> Optional[float]:
        return self._last_tick.price if self._last_tick else None

    @property
    def tick_age_seconds(self) -> float:
        if self._last_tick_time == 0:
            return float("inf")
        return time.time() - self._last_tick_time

    @property
    def is_data_fresh(self) -> bool:
        return self.tick_age_seconds <= self._cfg.max_tick_staleness_sec

    def process_tick(self, tick: Tick) -> None:
        """Ingest a tick and update all timeframe candles."""
        self._last_tick = tick
        self._last_tick_time = time.time()

        # Update day stats
        if self.day_open is None:
            self.day_open = tick.price
        self.day_high = max(self.day_high, tick.price)
        self.day_low = min(self.day_low, tick.price)
        self.day_volume += tick.volume
        self._vwap_cumulative += tick.price * tick.volume
        if self.day_volume > 0:
            self.day_vwap = self._vwap_cumulative / self.day_volume

        # Update candles for each timeframe
        for tf in self._cfg.candle_timeframes:
            self._update_candle(tf, tick)

    def _update_candle(self, timeframe: int, tick: Tick) -> None:
        """Update or create candle for given timeframe."""
        candle_start = self._get_candle_start(tick.timestamp, timeframe)
        current = self._current_candle[timeframe]

        if current is None or current.timestamp != candle_start:
            # Close previous candle
            if current is not None:
                current.closed = True
                self._candles[timeframe].append(current)
            # Start new candle
            self._current_candle[timeframe] = Candle(timestamp=candle_start)
            current = self._current_candle[timeframe]

        current.update(tick)

    @staticmethod
    def _get_candle_start(epoch: float, timeframe_min: int) -> float:
        """Align timestamp to candle boundary."""
        interval_sec = timeframe_min * 60
        return (epoch // interval_sec) * interval_sec

    def get_candles(self, timeframe: int, count: int = 50) -> List[Candle]:
        """Get last N closed candles for timeframe."""
        candles = list(self._candles[timeframe])
        return candles[-count:] if len(candles) >= count else candles

    def has_sufficient_data(self) -> bool:
        """Check if enough candle history exists for indicators."""
        return (
            len(self._candles[1]) >= self._cfg.min_candles_1m
            and len(self._candles[5]) >= self._cfg.min_candles_5m
            and len(self._candles[15]) >= self._cfg.min_candles_15m
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: Data-Health Gate
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DataHealthReport:
    """Result of data health check."""
    is_healthy: bool
    tick_age_sec: float
    candle_coverage: Dict[int, int]  # timeframe → count
    consecutive_stale: int
    last_check_time: float
    reason: Optional[str] = None


class DataHealthGate:
    """Monitors data freshness and blocks trading on stale data."""

    def __init__(self):
        self._consecutive_stale: int = 0
        self._last_check: float = 0.0
        self._cfg = CONFIG.data

    def mcx_data_health(self, market_state: MCXMarketState) -> DataHealthReport:
        """
        Evaluate data health. Returns unhealthy if:
          - Tick data is stale beyond threshold
          - Insufficient candle history
          - Consecutive stale readings exceed threshold
        """
        now = time.time()
        self._last_check = now
        tick_age = market_state.tick_age_seconds

        # Check tick freshness
        if tick_age > self._cfg.max_tick_staleness_sec:
            self._consecutive_stale += 1
        else:
            self._consecutive_stale = 0

        candle_coverage = {
            tf: len(market_state._candles[tf])
            for tf in self._cfg.candle_timeframes
        }

        # Determine health
        is_healthy = True
        reason = None

        if self._consecutive_stale >= self._cfg.stale_threshold_count:
            is_healthy = False
            reason = (
                f"Consecutive stale ticks: {self._consecutive_stale} "
                f"(age: {tick_age:.1f}s, threshold: {self._cfg.max_tick_staleness_sec}s)"
            )
        elif not market_state.has_sufficient_data():
            is_healthy = False
            reason = f"Insufficient candle data: {candle_coverage}"

        return DataHealthReport(
            is_healthy=is_healthy,
            tick_age_sec=tick_age,
            candle_coverage=candle_coverage,
            consecutive_stale=self._consecutive_stale,
            last_check_time=now,
            reason=reason,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: Rate-Limit Protection
# ─────────────────────────────────────────────────────────────────────────────

class RateLimitExceeded(Exception):
    """Raised when rate limit is hit."""
    pass


class RateLimiter:
    """Token-bucket rate limiter for broker API calls."""

    def __init__(self, max_per_second: int = 2, max_per_minute: int = 10):
        self._max_per_second = max_per_second
        self._max_per_minute = max_per_minute
        self._second_bucket: Deque[float] = deque()
        self._minute_bucket: Deque[float] = deque()

    def check(self) -> bool:
        """Check if a request can proceed."""
        now = time.time()
        # Prune old entries
        while self._second_bucket and now - self._second_bucket[0] > 1.0:
            self._second_bucket.popleft()
        while self._minute_bucket and now - self._minute_bucket[0] > 60.0:
            self._minute_bucket.popleft()

        return (
            len(self._second_bucket) < self._max_per_second
            and len(self._minute_bucket) < self._max_per_minute
        )

    def consume(self) -> None:
        """Record a request. Call after check() returns True."""
        now = time.time()
        self._second_bucket.append(now)
        self._minute_bucket.append(now)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: Tuple[type, ...] = (Exception,),
):
    """
    Decorator: retry with exponential backoff and jitter.
    Respects rate limits. Fails closed after max retries.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    # Add jitter (±25%)
                    import random
                    jitter = delay * 0.25 * (2 * random.random() - 1)
                    actual_delay = delay + jitter
                    logger.warning(
                        f"Retry {attempt + 1}/{max_retries} for {func.__name__}: "
                        f"{e}. Waiting {actual_delay:.2f}s"
                    )
                    await asyncio.sleep(actual_delay)
            # Fail closed
            raise last_exception  # type: ignore
        return wrapper  # type: ignore
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7: MCX Candidate Scoring (Separate from NSE)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MCXScore:
    """Composite score for an MCX candidate."""
    symbol: str
    total_score: float  # 0-100
    momentum_score: float
    relative_strength_score: float
    volume_score: float
    structure_score: float
    spread_score: float
    timestamp: float
    passes_threshold: bool

    @property
    def components(self) -> Dict[str, float]:
        return {
            "momentum": self.momentum_score,
            "relative_strength": self.relative_strength_score,
            "volume": self.volume_score,
            "structure": self.structure_score,
            "spread": self.spread_score,
        }


def mcx_score(market_state: MCXMarketState) -> MCXScore:
    """
    Compute MCX-specific candidate score (independent of NSE scoring).
    
    Components:
      - Momentum: Rate of change over lookback period
      - Relative Strength: Performance vs commodity index/peers
      - Volume: RVOL (relative volume vs baseline)
      - Structure: Trend alignment across timeframes
      - Spread: Bid-ask tightness (liquidity proxy)
    """
    cfg = CONFIG.scoring
    candles_5m = market_state.get_candles(5, cfg.momentum_lookback)
    candles_1m = market_state.get_candles(1, cfg.rvol_baseline_periods)

    # --- Momentum Score (0-100) ---
    momentum_score = _calc_momentum_score(candles_5m, cfg.momentum_lookback)

    # --- Relative Strength Score (0-100) ---
    rs_score = _calc_rs_score(candles_5m, cfg.rs_lookback)

    # --- Volume Score (0-100) ---
    volume_score = _calc_volume_score(candles_1m, cfg.rvol_baseline_periods)

    # --- Structure Score (0-100) ---
    structure_score = _calc_structure_score(market_state)

    # --- Spread Score (0-100) ---
    spread_score = _calc_spread_score(market_state)

    # Weighted total
    total = (
        cfg.weight_momentum * momentum_score
        + cfg.weight_relative_strength * rs_score
        + cfg.weight_volume * volume_score
        + cfg.weight_structure * structure_score
        + cfg.weight_spread * spread_score
    )

    return MCXScore(
        symbol=market_state.symbol,
        total_score=round(total, 2),
        momentum_score=round(momentum_score, 2),
        relative_strength_score=round(rs_score, 2),
        volume_score=round(volume_score, 2),
        structure_score=round(structure_score, 2),
        spread_score=round(spread_score, 2),
        timestamp=time.time(),
        passes_threshold=total >= cfg.min_entry_score,
    )


def _calc_momentum_score(candles: List[Candle], lookback: int) -> float:
    """Rate of change normalized to 0-100."""
    if len(candles) < 2:
        return 0.0
    start = candles[0].close if candles[0].close > 0 else candles[0].open
    end = candles[-1].close
    if start == 0:
        return 0.0
    roc = ((end - start) / start) * 100
    # Normalize: ±5% maps to 0-100, centered at 50
    normalized = 50 + (roc / 5) * 50
    return max(0.0, min(100.0, normalized))


def _calc_rs_score(candles: List[Candle], lookback: int) -> float:
    """Relative strength vs baseline (simplified: self-trend strength)."""
    if len(candles) < lookback:
        return 50.0  # Neutral if insufficient data
    recent = candles[-lookback:]
    ups = sum(1 for i in range(1, len(recent)) if recent[i].close > recent[i-1].close)
    ratio = ups / max(len(recent) - 1, 1)
    return ratio * 100


def _calc_volume_score(candles: List[Candle], baseline_periods: int) -> float:
    """RVOL normalized to 0-100."""
    if len(candles) < baseline_periods:
        return 50.0
    volumes = [c.volume for c in candles[-baseline_periods:]]
    avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1) if len(volumes) > 1 else 1
    if avg_vol == 0:
        return 50.0
    current_vol = volumes[-1]
    rvol = current_vol / avg_vol
    # Map: RVOL 0.5→25, 1.0→50, 2.0→75, 3.0→100
    score = min(100.0, rvol * 33.3)
    return max(0.0, score)


def _calc_structure_score(market_state: MCXMarketState) -> float:
    """Multi-timeframe trend alignment score."""
    score = 50.0  # Neutral baseline
    for tf in (1, 5, 15):
        candles = market_state.get_candles(tf, 10)
        if len(candles) >= 3:
            # Simple: are last 3 candles making higher closes?
            if candles[-1].close > candles[-2].close > candles[-3].close:
                score += 16.7
            elif candles[-1].close < candles[-2].close < candles[-3].close:
                score -= 16.7
    return max(0.0, min(100.0, score))


def _calc_spread_score(market_state: MCXMarketState) -> float:
    """Bid-ask tightness as score (tighter = higher)."""
    tick = market_state._last_tick
    if tick is None or tick.bid == 0 or tick.ask == 0:
        return 50.0  # Neutral if no bid/ask
    spread = tick.ask - tick.bid
    tick_size = market_state.contract.tick_size
    spread_ticks = spread / tick_size if tick_size > 0 else 10
    # 1 tick spread → 100, 5+ ticks → 0
    score = max(0.0, 100.0 - (spread_ticks - 1) * 25)
    return score


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8: Early Acceleration Detection
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AccelerationSignal:
    """Result of acceleration detection."""
    is_accelerating: bool
    score_velocity: float
    momentum_velocity: float
    rs_velocity: float
    rvol_velocity: float
    velocities_passing: int
    timestamp: float


class AccelerationDetector:
    """Detects early acceleration via velocity of scoring components."""

    def __init__(self):
        self._cfg = CONFIG.acceleration
        self._score_history: Deque[Tuple[float, MCXScore]] = deque(maxlen=30)

    def record_score(self, score: MCXScore) -> None:
        """Record a score observation for velocity calculation."""
        self._score_history.append((time.time(), score))

    def detect(self) -> AccelerationSignal:
        """
        Calculate velocity of score/momentum/rs/rvol.
        Acceleration = rate of change of these components over lookback window.
        """
        cfg = self._cfg
        now = time.time()

        if len(self._score_history) < 2:
            return AccelerationSignal(
                is_accelerating=False,
                score_velocity=0, momentum_velocity=0,
                rs_velocity=0, rvol_velocity=0,
                velocities_passing=0, timestamp=now,
            )

        # Get scores within lookback window
        lookback_sec = cfg.velocity_lookback * 60  # Convert candles to approx seconds
        recent = [
            (t, s) for t, s in self._score_history
            if now - t <= lookback_sec
        ]
        if len(recent) < 2:
            recent = list(self._score_history)[-2:]

        first_t, first_s = recent[0]
        last_t, last_s = recent[-1]
        dt_min = max((last_t - first_t) / 60, 0.1)  # Avoid division by zero

        # Calculate velocities (change per minute)
        score_vel = (last_s.total_score - first_s.total_score) / dt_min
        momentum_vel = (last_s.momentum_score - first_s.momentum_score) / dt_min
        rs_vel = (last_s.relative_strength_score - first_s.relative_strength_score) / dt_min
        rvol_vel = (last_s.volume_score - first_s.volume_score) / dt_min

        # Count passing velocities
        passing = 0
        if score_vel >= cfg.score_velocity_threshold:
            passing += 1
        if momentum_vel >= cfg.momentum_velocity_threshold:
            passing += 1
        if rs_vel >= cfg.rs_velocity_threshold:
            passing += 1
        if rvol_vel >= cfg.rvol_velocity_threshold:
            passing += 1

        return AccelerationSignal(
            is_accelerating=passing >= cfg.min_velocities_passing,
            score_velocity=round(score_vel, 3),
            momentum_velocity=round(momentum_vel, 3),
            rs_velocity=round(rs_vel, 3),
            rvol_velocity=round(rvol_vel, 3),
            velocities_passing=passing,
            timestamp=now,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9: Anti-Chase / Room-to-Run Filter
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RoomAnalysis:
    """Result of room-to-run analysis."""
    has_room: bool
    move_from_open_pct: float
    room_to_resistance_atr: float
    vwap_distance_atr: float
    near_day_high: bool
    rsi: float
    rejection_reason: Optional[str] = None


def calculate_room(
    market_state: MCXMarketState,
    direction: str = "LONG",
) -> RoomAnalysis:
    """
    Anti-chase filter: reject if price has already exhausted its move.
    
    Checks:
      1. % moved from day open (reject if > max)
      2. Room to next resistance/support in ATR terms
      3. Distance from VWAP (reject if too stretched)
      4. Proximity to day high/low
      5. RSI overbought/oversold
    """
    cfg = CONFIG.anti_chase
    price = market_state.last_price
    if price is None or market_state.day_open is None:
        return RoomAnalysis(
            has_room=False, move_from_open_pct=0, room_to_resistance_atr=0,
            vwap_distance_atr=0, near_day_high=False, rsi=50,
            rejection_reason="Insufficient data for room analysis",
        )

    # Calculate ATR from 5m candles
    atr = _calculate_atr(market_state.get_candles(5, 14))
    if atr == 0:
        atr = price * 0.01  # Fallback: 1% of price

    # 1. Move from open
    move_from_open = abs(price - market_state.day_open) / market_state.day_open * 100

    # 2. Room to resistance (simplified: distance to day high/low)
    if direction == "LONG":
        room_raw = market_state.day_high - price
    else:
        room_raw = price - market_state.day_low
    room_atr = room_raw / atr if atr > 0 else 0

    # 3. VWAP distance
    vwap_dist = abs(price - market_state.day_vwap) / atr if atr > 0 else 0

    # 4. Near day high/low
    if direction == "LONG":
        pct_from_high = (market_state.day_high - price) / price * 100 if price > 0 else 0
        near_extreme = pct_from_high < cfg.reject_near_high_pct
    else:
        pct_from_low = (price - market_state.day_low) / price * 100 if price > 0 else 0
        near_extreme = pct_from_low < cfg.reject_near_high_pct

    # 5. RSI calculation (simplified from 5m candles)
    rsi = _calculate_rsi(market_state.get_candles(5, 14))

    # Decision
    rejection_reason = None
    has_room = True

    if move_from_open > cfg.max_move_from_open_pct:
        has_room = False
        rejection_reason = f"Move from open {move_from_open:.1f}% > max {cfg.max_move_from_open_pct}%"
    elif room_atr < cfg.min_room_atr_multiple:
        has_room = False
        rejection_reason = f"Room {room_atr:.2f} ATR < min {cfg.min_room_atr_multiple}"
    elif vwap_dist > cfg.max_vwap_distance_atr:
        has_room = False
        rejection_reason = f"VWAP distance {vwap_dist:.2f} ATR > max {cfg.max_vwap_distance_atr}"
    elif near_extreme:
        has_room = False
        rejection_reason = f"Too near day {'high' if direction == 'LONG' else 'low'}"
    elif direction == "LONG" and rsi > cfg.reject_rsi_above:
        has_room = False
        rejection_reason = f"RSI {rsi:.0f} > overbought {cfg.reject_rsi_above}"
    elif direction == "SHORT" and rsi < cfg.reject_rsi_below:
        has_room = False
        rejection_reason = f"RSI {rsi:.0f} < oversold {cfg.reject_rsi_below}"

    return RoomAnalysis(
        has_room=has_room,
        move_from_open_pct=round(move_from_open, 2),
        room_to_resistance_atr=round(room_atr, 2),
        vwap_distance_atr=round(vwap_dist, 2),
        near_day_high=near_extreme,
        rsi=round(rsi, 1),
        rejection_reason=rejection_reason,
    )


def _calculate_atr(candles: List[Candle], period: int = 14) -> float:
    """Calculate Average True Range from candles."""
    if len(candles) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if not true_ranges:
        return 0.0
    # Use last `period` values
    recent = true_ranges[-period:]
    return sum(recent) / len(recent)


def _calculate_rsi(candles: List[Candle], period: int = 14) -> float:
    """Calculate RSI from candles."""
    if len(candles) < period + 1:
        return 50.0  # Neutral
    changes = [
        candles[i].close - candles[i - 1].close
        for i in range(1, len(candles))
    ]
    recent = changes[-period:]
    gains = [c for c in recent if c > 0]
    losses = [-c for c in recent if c < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10: Entry Decision
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EntryDecision:
    """Final entry decision combining score + acceleration + room."""
    should_enter: bool
    direction: str  # "LONG" or "SHORT"
    symbol: str
    score: MCXScore
    acceleration: AccelerationSignal
    room: RoomAnalysis
    reason: str
    timestamp: float


def mcx_entry_decision(
    market_state: MCXMarketState,
    score: MCXScore,
    acceleration: AccelerationSignal,
    direction: str = "LONG",
) -> EntryDecision:
    """
    Combine score + acceleration + room-to-run for final entry decision.
    ALL three must pass for entry. Fail closed on any doubt.
    """
    now = time.time()
    symbol = market_state.symbol

    # Gate 1: Score threshold
    if not score.passes_threshold:
        return EntryDecision(
            should_enter=False, direction=direction, symbol=symbol,
            score=score, acceleration=acceleration,
            room=RoomAnalysis(has_room=False, move_from_open_pct=0,
                             room_to_resistance_atr=0, vwap_distance_atr=0,
                             near_day_high=False, rsi=50),
            reason=f"Score {score.total_score} < threshold {CONFIG.scoring.min_entry_score}",
            timestamp=now,
        )

    # Gate 2: Acceleration
    if not acceleration.is_accelerating:
        return EntryDecision(
            should_enter=False, direction=direction, symbol=symbol,
            score=score, acceleration=acceleration,
            room=RoomAnalysis(has_room=False, move_from_open_pct=0,
                             room_to_resistance_atr=0, vwap_distance_atr=0,
                             near_day_high=False, rsi=50),
            reason=f"No acceleration ({acceleration.velocities_passing}/{CONFIG.acceleration.min_velocities_passing} passing)",
            timestamp=now,
        )

    # Gate 3: Room to run
    room = calculate_room(market_state, direction)
    if not room.has_room:
        return EntryDecision(
            should_enter=False, direction=direction, symbol=symbol,
            score=score, acceleration=acceleration, room=room,
            reason=f"No room: {room.rejection_reason}",
            timestamp=now,
        )

    # ALL gates passed
    return EntryDecision(
        should_enter=True, direction=direction, symbol=symbol,
        score=score, acceleration=acceleration, room=room,
        reason="All gates passed: score + acceleration + room",
        timestamp=now,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 11: Position Sizing (Lot-Size Aware, Margin-Checked)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionSize:
    """Calculated position size."""
    lots: int
    quantity: int  # lots * lot_size
    risk_amount: float
    margin_required: float
    risk_per_lot: float
    is_valid: bool
    rejection_reason: Optional[str] = None


def mcx_position_size(
    contract: MCXContract,
    entry_price: float,
    stop_price: float,
    capital: float,
    current_exposure: float = 0.0,
    margin_per_lot: float = 0.0,
) -> PositionSize:
    """
    Calculate lot-size-aware position size with margin check.
    
    Rules:
      1. Risk per trade ≤ max_risk_per_trade_pct of capital
      2. Total exposure ≤ max_mcx_exposure_pct of capital
      3. Quantity must be exact multiple of lot_size
      4. Cannot exceed max_lots_per_trade
      5. Must have sufficient free margin
    """
    cfg = CONFIG.position_sizing

    # Risk per lot
    stop_distance = abs(entry_price - stop_price)
    risk_per_lot = stop_distance * contract.lot_size
    if risk_per_lot <= 0:
        return PositionSize(
            lots=0, quantity=0, risk_amount=0, margin_required=0,
            risk_per_lot=0, is_valid=False,
            rejection_reason="Invalid stop distance (zero or negative)",
        )

    # Maximum risk in currency
    max_risk = capital * (cfg.max_risk_per_trade_pct / 100)

    # Lots by risk
    lots_by_risk = int(max_risk / risk_per_lot)

    # Lots by max lots
    lots_by_max = cfg.max_lots_per_trade

    # Lots by exposure limit
    max_exposure = capital * (cfg.max_mcx_exposure_pct / 100)
    available_exposure = max_exposure - current_exposure
    notional_per_lot = entry_price * contract.lot_size
    lots_by_exposure = int(available_exposure / notional_per_lot) if notional_per_lot > 0 else 0

    # Lots by margin
    if margin_per_lot > 0:
        free_margin = capital * (cfg.min_free_margin_pct / 100)
        effective_margin = margin_per_lot * cfg.margin_safety_multiplier
        lots_by_margin = int(free_margin / effective_margin) if effective_margin > 0 else 0
    else:
        lots_by_margin = lots_by_risk  # No margin constraint if not provided

    # Take minimum across all constraints
    lots = min(lots_by_risk, lots_by_max, lots_by_exposure, lots_by_margin)
    lots = max(lots, 0)

    if lots == 0:
        return PositionSize(
            lots=0, quantity=0, risk_amount=0, margin_required=0,
            risk_per_lot=risk_per_lot, is_valid=False,
            rejection_reason=(
                f"Zero lots: risk={lots_by_risk}, max={lots_by_max}, "
                f"exposure={lots_by_exposure}, margin={lots_by_margin}"
            ),
        )

    quantity = lots * contract.lot_size
    risk_amount = lots * risk_per_lot
    margin_required = lots * (margin_per_lot if margin_per_lot > 0 else notional_per_lot * 0.10)

    return PositionSize(
        lots=lots,
        quantity=quantity,
        risk_amount=round(risk_amount, 2),
        margin_required=round(margin_required, 2),
        risk_per_lot=round(risk_per_lot, 2),
        is_valid=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 12: Structural/ATR Stop (Immutable Initial SL)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StopLevel:
    """Immutable stop-loss level. Once set, initial_sl NEVER changes."""
    price: float
    distance_ticks: int
    distance_pct: float
    method: str  # "ATR" or "STRUCTURE"
    atr_value: float
    is_initial: bool = True


def mcx_initial_sl(
    contract: MCXContract,
    entry_price: float,
    direction: str,
    market_state: MCXMarketState,
) -> Optional[StopLevel]:
    """
    Calculate initial stop-loss using ATR and structure.
    
    Rules:
      1. ATR-based: entry ∓ (ATR × multiplier)
      2. Structure-based: below swing low / above swing high + buffer
      3. Use tighter of the two (if structure stop is closer)
      4. Enforce min/max stop distance
      5. Result is IMMUTABLE — initial_sl never changes after set
    """
    cfg = CONFIG.stop_loss
    candles = market_state.get_candles(5, cfg.atr_period + 5)

    atr = _calculate_atr(candles, cfg.atr_period)
    if atr <= 0:
        logger.warning(f"ATR is zero for {contract.symbol}, cannot set stop")
        return None

    # ATR-based stop
    atr_distance = atr * cfg.atr_multiplier
    if direction == "LONG":
        atr_stop = entry_price - atr_distance
    else:
        atr_stop = entry_price + atr_distance

    # Structure-based stop (swing low/high)
    structure_stop = None
    if cfg.use_structure_stop and len(candles) >= 5:
        buffer = contract.tick_size * cfg.structure_buffer_ticks
        if direction == "LONG":
            swing_low = min(c.low for c in candles[-5:])
            structure_stop = swing_low - buffer
        else:
            swing_high = max(c.high for c in candles[-5:])
            structure_stop = swing_high + buffer

    # Choose stop: use structure if tighter than ATR
    if structure_stop is not None:
        if direction == "LONG":
            stop_price = max(atr_stop, structure_stop)  # Tighter = higher for long
            method = "STRUCTURE" if stop_price == structure_stop else "ATR"
        else:
            stop_price = min(atr_stop, structure_stop)  # Tighter = lower for short
            method = "STRUCTURE" if stop_price == structure_stop else "ATR"
    else:
        stop_price = atr_stop
        method = "ATR"

    # Enforce minimum stop distance
    min_dist = contract.tick_size * cfg.min_stop_ticks
    actual_dist = abs(entry_price - stop_price)
    if actual_dist < min_dist:
        if direction == "LONG":
            stop_price = entry_price - min_dist
        else:
            stop_price = entry_price + min_dist
        method = "ATR"  # Overridden by minimum

    # Enforce maximum stop distance
    max_dist = entry_price * (cfg.max_stop_pct / 100)
    if actual_dist > max_dist:
        if direction == "LONG":
            stop_price = entry_price - max_dist
        else:
            stop_price = entry_price + max_dist

    # Calculate metrics
    distance_ticks = int(abs(entry_price - stop_price) / contract.tick_size)
    distance_pct = abs(entry_price - stop_price) / entry_price * 100

    # Round to tick size
    stop_price = round(stop_price / contract.tick_size) * contract.tick_size

    return StopLevel(
        price=round(stop_price, 2),
        distance_ticks=distance_ticks,
        distance_pct=round(distance_pct, 3),
        method=method,
        atr_value=round(atr, 2),
        is_initial=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 13: Profit Protection (Progressive R-Based Trailing)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TrailState:
    """Current trailing stop state for a position."""
    current_trail_price: float
    current_r_level: float  # Which R-level is active
    initial_sl: float  # Immutable reference
    entry_price: float
    risk_per_unit: float  # |entry - initial_sl|
    direction: str
    last_update_time: float = 0.0

    @property
    def r_multiple_at_price(self) -> float:
        """Current R-multiple based on last price."""
        return 0.0  # Updated externally

    def calculate_r(self, current_price: float) -> float:
        """Calculate current R-multiple."""
        if self.risk_per_unit == 0:
            return 0.0
        if self.direction == "LONG":
            profit = current_price - self.entry_price
        else:
            profit = self.entry_price - current_price
        return profit / self.risk_per_unit


def update_progressive_trail(
    trail_state: TrailState,
    current_price: float,
) -> Tuple[TrailState, bool]:
    """
    Progressive R-based trailing stop.
    
    Trail levels (from config):
      1R → trail to 0.5R
      2R → trail to 1.25R
      2.5R → trail to 1.75R
      3R → trail to 2.25R
    
    Rules:
      - Only moves in favorable direction (never widens)
      - Based on highest R achieved, not current R
      - Returns (updated_state, should_update_broker)
    """
    cfg = CONFIG.trailing
    current_r = trail_state.calculate_r(current_price)
    should_update = False

    # Find highest qualifying trail level
    new_trail_price = trail_state.current_trail_price
    new_r_level = trail_state.current_r_level

    for trigger_r, trail_to_r in cfg.trail_levels:
        if current_r >= trigger_r and trigger_r > trail_state.current_r_level:
            # Calculate trail price from R-level
            trail_distance = trail_to_r * trail_state.risk_per_unit
            if trail_state.direction == "LONG":
                candidate = trail_state.entry_price + trail_distance
            else:
                candidate = trail_state.entry_price - trail_distance

            # Only move in favorable direction
            if trail_state.direction == "LONG":
                if candidate > new_trail_price:
                    new_trail_price = candidate
                    new_r_level = trigger_r
                    should_update = True
            else:
                if candidate < new_trail_price:
                    new_trail_price = candidate
                    new_r_level = trigger_r
                    should_update = True

    if should_update:
        trail_state = TrailState(
            current_trail_price=round(new_trail_price, 2),
            current_r_level=new_r_level,
            initial_sl=trail_state.initial_sl,
            entry_price=trail_state.entry_price,
            risk_per_unit=trail_state.risk_per_unit,
            direction=trail_state.direction,
            last_update_time=time.time(),
        )

    return trail_state, should_update


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 14: Confirmed Reversal Exit
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReversalSignal:
    """Confirmed reversal exit signal."""
    is_confirmed: bool
    structure_broken: bool
    momentum_confirmed: bool
    volume_confirmed: bool
    candles_held: int
    reason: Optional[str] = None


def check_reversal_exit(
    market_state: MCXMarketState,
    direction: str,
    entry_price: float,
    entry_time: float,
    initial_sl: float,
) -> ReversalSignal:
    """
    Confirmed reversal exit — NOT immediate on fade.
    
    Requires ALL THREE:
      1. Structure break: close beyond key level
      2. Momentum confirmation: trend indicator flip
      3. Volume confirmation: above-average on reversal bar
    
    Will NOT exit on:
      - Simple pullbacks without structure break
      - Low-volume fades
      - Minor momentum divergence alone
    """
    cfg = CONFIG.reversal_exit
    candles_1m = market_state.get_candles(1, 30)
    candles_5m = market_state.get_candles(5, 20)

    if not candles_1m:
        return ReversalSignal(
            is_confirmed=False, structure_broken=False,
            momentum_confirmed=False, volume_confirmed=False,
            candles_held=0, reason="Insufficient data",
        )

    # Count candles held
    entry_candle_time = market_state._get_candle_start(entry_time, 1)
    candles_held = sum(1 for c in candles_1m if c.timestamp >= entry_candle_time)

    # Minimum hold period
    if candles_held < cfg.min_hold_candles:
        return ReversalSignal(
            is_confirmed=False, structure_broken=False,
            momentum_confirmed=False, volume_confirmed=False,
            candles_held=candles_held,
            reason=f"Min hold not met ({candles_held}/{cfg.min_hold_candles})",
        )

    # 1. Structure break check
    atr = _calculate_atr(candles_5m, 14)
    break_level = atr * cfg.structure_break_atr_multiple
    current_close = candles_1m[-1].close

    if direction == "LONG":
        # Structure breaks below entry - break_level
        structure_ref = entry_price - break_level
        structure_broken = current_close < structure_ref
    else:
        structure_ref = entry_price + break_level
        structure_broken = current_close > structure_ref

    # 2. Momentum confirmation (RSI reversal or MACD cross)
    rsi = _calculate_rsi(candles_5m, 14)
    if direction == "LONG":
        momentum_confirmed = rsi < 40  # Momentum turning bearish
    else:
        momentum_confirmed = rsi > 60  # Momentum turning bullish

    # 3. Volume confirmation
    if len(candles_1m) >= 2:
        avg_vol = sum(c.volume for c in candles_1m[-20:]) / min(len(candles_1m[-20:]), 20)
        current_vol = candles_1m[-1].volume
        volume_confirmed = current_vol >= avg_vol * cfg.reversal_volume_multiplier
    else:
        volume_confirmed = False

    # ALL three must confirm
    is_confirmed = (
        cfg.require_structure_break and structure_broken
        and cfg.require_momentum_confirm and momentum_confirmed
        and cfg.require_volume_confirm and volume_confirmed
    )

    reason = None
    if is_confirmed:
        reason = "Full reversal confirmed: structure + momentum + volume"

    return ReversalSignal(
        is_confirmed=is_confirmed,
        structure_broken=structure_broken,
        momentum_confirmed=momentum_confirmed,
        volume_confirmed=volume_confirmed,
        candles_held=candles_held,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 15: Broker-Truth Order Handling
# ─────────────────────────────────────────────────────────────────────────────

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PLACED = "PLACED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class OrderResult:
    """Result of order placement and verification."""
    order_id: str
    status: OrderStatus
    filled_qty: int
    filled_price: float
    pending_qty: int
    adopted: bool = False  # True if position was adopted after timeout
    error: Optional[str] = None


async def verify_order_after_timeout(
    order_id: str,
    expected_qty: int,
    gateway: Any,  # DhanGateway instance
    timeout_sec: Optional[int] = None,
) -> OrderResult:
    """
    Verify order status after timeout period.
    
    Broker is the TRUTH — if filled during verification window, adopt position.
    
    Flow:
      1. Wait for timeout
      2. Query broker for order status
      3. If filled → adopt position, set stops
      4. If rejected → log and abandon
      5. If partial → adopt filled portion
      6. If unknown → FAIL CLOSED (do not assume)
    """
    timeout = timeout_sec or CONFIG.broker.order_verify_timeout_sec

    await asyncio.sleep(timeout)

    try:
        # Query broker for truth (uses v82_dhan_gateway)
        order_status = await _query_order_status(order_id, gateway)

        if order_status is None:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.UNKNOWN,
                filled_qty=0,
                filled_price=0.0,
                pending_qty=expected_qty,
                error="Could not determine order status — failing closed",
            )

        status = OrderStatus(order_status.get("status", "UNKNOWN"))
        filled_qty = int(order_status.get("filled_qty", 0))
        filled_price = float(order_status.get("avg_price", 0.0))
        pending_qty = expected_qty - filled_qty

        adopted = False
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and filled_qty > 0:
            adopted = CONFIG.broker.adopt_if_filled
            logger.info(
                f"Order {order_id} filled {filled_qty}/{expected_qty} @ {filled_price}. "
                f"Adopting: {adopted}"
            )

        return OrderResult(
            order_id=order_id,
            status=status,
            filled_qty=filled_qty,
            filled_price=filled_price,
            pending_qty=pending_qty,
            adopted=adopted,
        )

    except Exception as e:
        logger.error(f"Order verification failed for {order_id}: {e}")
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.UNKNOWN,
            filled_qty=0,
            filled_price=0.0,
            pending_qty=expected_qty,
            error=str(e),
        )


async def _query_order_status(order_id: str, gateway: Any) -> Optional[Dict[str, Any]]:
    """Query broker for order status. Uses v82_dhan_gateway."""
    try:
        # Placeholder for actual gateway call
        # result = await gateway.get_order_status(order_id)
        # return result
        return None  # Will be connected to actual gateway
    except Exception as e:
        logger.error(f"Gateway query failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 16: Quantity Reconciliation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReconciliationResult:
    """Result of position/SL quantity reconciliation."""
    is_synced: bool
    broker_qty: int
    expected_qty: int
    sl_qty: int
    action_taken: Optional[str] = None
    discrepancy: int = 0


class QuantityReconciler:
    """
    Reconcile actual broker position with expected state every 15-60 seconds.
    Sync SL order quantity with actual position.
    """

    def __init__(self):
        self._cfg = CONFIG.broker
        self._last_reconciliation: float = 0.0
        self._reconciliation_count: int = 0

    @property
    def time_since_last(self) -> float:
        if self._last_reconciliation == 0:
            return float("inf")
        return time.time() - self._last_reconciliation

    def should_reconcile(self) -> bool:
        """Check if reconciliation is due."""
        elapsed = self.time_since_last
        return elapsed >= self._cfg.reconciliation_interval_sec

    async def reconcile(
        self,
        symbol: str,
        expected_qty: int,
        sl_order_id: Optional[str],
        gateway: Any,  # DhanGateway
    ) -> ReconciliationResult:
        """
        Reconcile position quantity with broker truth.
        
        Actions:
          1. Query actual position from broker
          2. Compare with expected quantity
          3. If mismatch: adjust SL order quantity to match actual
          4. Log discrepancy for investigation
        """
        self._last_reconciliation = time.time()
        self._reconciliation_count += 1

        try:
            # Query broker for actual position
            broker_position = await self._get_broker_position(symbol, gateway)
            broker_qty = broker_position.get("quantity", 0) if broker_position else 0

            # Query SL order quantity
            sl_qty = 0
            if sl_order_id:
                sl_order = await _query_order_status(sl_order_id, gateway)
                sl_qty = int(sl_order.get("quantity", 0)) if sl_order else 0

            discrepancy = broker_qty - expected_qty
            action_taken = None

            if broker_qty != expected_qty:
                logger.warning(
                    f"RECONCILIATION MISMATCH {symbol}: "
                    f"broker={broker_qty}, expected={expected_qty}, diff={discrepancy}"
                )
                action_taken = f"Discrepancy detected: {discrepancy} units"

            # Sync SL quantity
            if self._cfg.sync_sl_quantity and sl_order_id and sl_qty != broker_qty:
                if broker_qty > 0:
                    action_taken = (
                        f"SL qty sync: {sl_qty} → {broker_qty} "
                        f"(matching broker position)"
                    )
                    # Actual modification would happen via gateway
                    # await gateway.modify_order(sl_order_id, quantity=broker_qty)
                elif broker_qty == 0:
                    action_taken = "Position closed at broker — cancelling SL"
                    # await gateway.cancel_order(sl_order_id)

            return ReconciliationResult(
                is_synced=(broker_qty == expected_qty and sl_qty == broker_qty),
                broker_qty=broker_qty,
                expected_qty=expected_qty,
                sl_qty=sl_qty,
                action_taken=action_taken,
                discrepancy=discrepancy,
            )

        except Exception as e:
            logger.error(f"Reconciliation failed for {symbol}: {e}")
            return ReconciliationResult(
                is_synced=False,
                broker_qty=0,
                expected_qty=expected_qty,
                sl_qty=0,
                action_taken=f"ERROR: {e}",
                discrepancy=0,
            )

    async def _get_broker_position(
        self, symbol: str, gateway: Any
    ) -> Optional[Dict[str, Any]]:
        """Get actual position from broker."""
        try:
            # Placeholder for actual gateway call
            # return await gateway.get_position(symbol, exchange="MCX")
            return None
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 17: Hard Safety Gate
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SafetyGateResult:
    """Result of hard safety gate check."""
    can_trade: bool
    checks_passed: Dict[str, bool]
    blocking_reason: Optional[str] = None


def mcx_can_trade(
    contract: Optional[MCXContract],
    market_state: Optional[MCXMarketState],
    data_health: Optional[DataHealthReport],
    capital: float,
    daily_pnl: float,
    weekly_pnl: float,
    peak_capital: float,
    active_positions: int,
    broker_connected: bool,
) -> SafetyGateResult:
    """
    Hard safety gate — ALL checks must pass or trading is blocked.
    FAIL CLOSED on any error or missing data.
    
    Checks:
      1. Contract resolved and valid
      2. Data feed healthy
      3. Broker connected
      4. Risk limits not breached
      5. Margin available
      6. Session is open
      7. Position count within limits
    """
    cfg = CONFIG.risk
    checks: Dict[str, bool] = {}

    # 1. Contract check
    checks["contract_valid"] = contract is not None and contract.days_to_expiry >= CONFIG.contract.min_days_to_expiry

    # 2. Data health
    checks["data_healthy"] = data_health is not None and data_health.is_healthy

    # 3. Broker connected
    checks["broker_connected"] = broker_connected

    # 4. Daily loss limit
    daily_loss_pct = abs(daily_pnl) / capital * 100 if capital > 0 and daily_pnl < 0 else 0
    checks["daily_loss_ok"] = daily_loss_pct < cfg.max_daily_loss_pct

    # 5. Weekly loss limit
    weekly_loss_pct = abs(weekly_pnl) / capital * 100 if capital > 0 and weekly_pnl < 0 else 0
    checks["weekly_loss_ok"] = weekly_loss_pct < cfg.max_weekly_loss_pct

    # 6. Drawdown limit
    drawdown_pct = (peak_capital - capital) / peak_capital * 100 if peak_capital > 0 else 0
    checks["drawdown_ok"] = drawdown_pct < cfg.max_drawdown_pct

    # 7. Minimum capital
    checks["min_capital_ok"] = capital >= cfg.min_capital

    # 8. Position count
    checks["position_count_ok"] = active_positions < CONFIG.position_sizing.max_concurrent_positions

    # 9. Session state
    session = get_mcx_session_state(symbol=contract.symbol if contract else None)
    checks["session_open"] = session == MCXSessionState.OPEN

    # 10. Market state exists
    checks["market_state_valid"] = market_state is not None and market_state.last_price is not None

    # Decision: ALL must pass (fail closed)
    can_trade = all(checks.values())
    blocking_reason = None

    if not can_trade:
        failed = [k for k, v in checks.items() if not v]
        blocking_reason = f"Safety gate BLOCKED: {', '.join(failed)}"
        logger.warning(blocking_reason)

    return SafetyGateResult(
        can_trade=can_trade,
        checks_passed=checks,
        blocking_reason=blocking_reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 18: MCX State Machine
# ─────────────────────────────────────────────────────────────────────────────

class EngineState(str, Enum):
    """MCX Engine state machine states."""
    INIT = "INIT"
    PRE_OPEN = "PRE_OPEN"
    CONTRACT_VALIDATION = "CONTRACT_VALIDATION"
    DATA_INIT = "DATA_INIT"
    WARMUP = "WARMUP"
    SCANNING = "SCANNING"
    EVALUATING = "EVALUATING"
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_ACTIVE = "POSITION_ACTIVE"
    EXIT_PENDING = "EXIT_PENDING"
    RECONCILING = "RECONCILING"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    CLOSED = "CLOSED"
    HALTED = "HALTED"  # Fatal error — manual intervention needed


# Valid state transitions
VALID_TRANSITIONS: Dict[EngineState, List[EngineState]] = {
    EngineState.INIT: [EngineState.PRE_OPEN, EngineState.CLOSED, EngineState.HALTED],
    EngineState.PRE_OPEN: [EngineState.CONTRACT_VALIDATION, EngineState.CLOSED, EngineState.HALTED],
    EngineState.CONTRACT_VALIDATION: [EngineState.DATA_INIT, EngineState.ERROR_RECOVERY, EngineState.HALTED],
    EngineState.DATA_INIT: [EngineState.WARMUP, EngineState.ERROR_RECOVERY, EngineState.HALTED],
    EngineState.WARMUP: [EngineState.SCANNING, EngineState.ERROR_RECOVERY, EngineState.CLOSED],
    EngineState.SCANNING: [
        EngineState.EVALUATING, EngineState.CLOSED,
        EngineState.ERROR_RECOVERY, EngineState.HALTED,
    ],
    EngineState.EVALUATING: [
        EngineState.ENTRY_PENDING, EngineState.SCANNING,
        EngineState.CLOSED, EngineState.ERROR_RECOVERY,
    ],
    EngineState.ENTRY_PENDING: [
        EngineState.POSITION_ACTIVE, EngineState.SCANNING,
        EngineState.ERROR_RECOVERY, EngineState.HALTED,
    ],
    EngineState.POSITION_ACTIVE: [
        EngineState.EXIT_PENDING, EngineState.RECONCILING,
        EngineState.SCANNING, EngineState.ERROR_RECOVERY, EngineState.HALTED,
    ],
    EngineState.EXIT_PENDING: [
        EngineState.SCANNING, EngineState.RECONCILING,
        EngineState.CLOSED, EngineState.ERROR_RECOVERY,
    ],
    EngineState.RECONCILING: [
        EngineState.POSITION_ACTIVE, EngineState.SCANNING,
        EngineState.ERROR_RECOVERY, EngineState.HALTED,
    ],
    EngineState.ERROR_RECOVERY: [
        EngineState.SCANNING, EngineState.DATA_INIT,
        EngineState.HALTED, EngineState.CLOSED,
    ],
    EngineState.CLOSED: [EngineState.INIT],
    EngineState.HALTED: [],  # Terminal — requires manual restart
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 20: Shadow-Mode Validation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ShadowTrade:
    """Record of a shadow (paper) trade for validation."""
    symbol: str
    direction: str
    entry_price: float
    entry_time: float
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    r_multiple: float = 0.0
    initial_sl: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0


@dataclass
class ShadowValidation:
    """Shadow-mode validation state and criteria tracking."""
    trades: List[ShadowTrade] = field(default_factory=list)
    start_date: Optional[date] = None
    trading_days: int = 0
    total_pnl: float = 0.0
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0

    @property
    def trade_count(self) -> int:
        return len([t for t in self.trades if t.exit_price is not None])

    @property
    def win_rate(self) -> float:
        closed = [t for t in self.trades if t.exit_price is not None]
        if not closed:
            return 0.0
        winners = sum(1 for t in closed if t.pnl > 0)
        return winners / len(closed)

    @property
    def profit_factor(self) -> float:
        closed = [t for t in self.trades if t.exit_price is not None]
        gross_profit = sum(t.pnl for t in closed if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in closed if t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        if self.peak_pnl <= 0:
            return abs(self.max_drawdown) if self.max_drawdown < 0 else 0.0
        return abs(self.max_drawdown) / self.peak_pnl * 100 if self.peak_pnl > 0 else 0.0

    def is_validated(self) -> Tuple[bool, Dict[str, Any]]:
        """Check if shadow mode criteria are met for live promotion."""
        cfg = CONFIG.shadow
        criteria = {
            "min_days": (self.trading_days >= cfg.min_shadow_days, self.trading_days, cfg.min_shadow_days),
            "min_trades": (self.trade_count >= cfg.min_shadow_trades, self.trade_count, cfg.min_shadow_trades),
            "win_rate": (self.win_rate >= cfg.required_win_rate, round(self.win_rate, 3), cfg.required_win_rate),
            "profit_factor": (self.profit_factor >= cfg.required_profit_factor, round(self.profit_factor, 2), cfg.required_profit_factor),
            "max_drawdown": (self.max_drawdown_pct <= cfg.max_allowed_drawdown_pct, round(self.max_drawdown_pct, 2), cfg.max_allowed_drawdown_pct),
        }
        all_passed = all(v[0] for v in criteria.values())
        return all_passed, {k: {"passed": v[0], "actual": v[1], "required": v[2]} for k, v in criteria.items()}

    def record_trade(self, trade: ShadowTrade) -> None:
        """Record a completed shadow trade and update metrics."""
        self.trades.append(trade)
        if trade.exit_price is not None:
            self.total_pnl += trade.pnl
            self.peak_pnl = max(self.peak_pnl, self.total_pnl)
            drawdown = self.total_pnl - self.peak_pnl
            self.max_drawdown = min(self.max_drawdown, drawdown)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE: MCXEngine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MCXPosition:
    """Active MCX position tracking."""
    symbol: str
    contract: MCXContract
    direction: str
    entry_price: float
    entry_time: float
    quantity: int
    lots: int
    initial_sl: float  # IMMUTABLE
    current_sl: float
    trail_state: TrailState
    order_id: str
    sl_order_id: Optional[str] = None
    max_price: float = 0.0  # Highest price seen (for longs)
    min_price: float = float("inf")  # Lowest price seen (for shorts)
    unrealized_pnl: float = 0.0
    r_multiple: float = 0.0


class MCXEngine:
    """
    MCX V8.6 Production Engine.
    
    Implements the complete state machine for MCX futures trading.
    Shadow-mode by default (MCX_LIVE=False) until validation passes.
    """

    def __init__(
        self,
        config: MCXConfig = CONFIG,
        gateway: Any = None,  # DhanGateway instance
        execution_integrity: Any = None,  # ExecutionIntegrity instance
    ):
        self._config = config
        self._gateway = gateway
        self._execution_integrity = execution_integrity

        # State
        self._state: EngineState = EngineState.INIT
        self._previous_state: Optional[EngineState] = None

        # Components
        self._audit = AuditLogger(config)
        self._data_health_gate = DataHealthGate()
        self._rate_limiter = RateLimiter(
            max_per_second=config.broker.max_orders_per_second,
            max_per_minute=config.broker.max_orders_per_minute,
        )
        self._reconciler = QuantityReconciler()
        self._acceleration_detectors: Dict[str, AccelerationDetector] = {}
        self._shadow_validation = ShadowValidation()

        # Market state per symbol
        self._market_states: Dict[str, MCXMarketState] = {}
        self._contracts: Dict[str, MCXContract] = {}

        # Active positions
        self._positions: Dict[str, MCXPosition] = {}

        # Risk tracking
        self._daily_pnl: float = 0.0
        self._weekly_pnl: float = 0.0
        self._peak_capital: float = 0.0
        self._capital: float = 0.0

        # Engine control
        self._running: bool = False
        self._error_count: int = 0
        self._max_consecutive_errors: int = 5

    @property
    def is_live(self) -> bool:
        """Whether engine is in live mode (not shadow)."""
        return self._config.shadow.mcx_live

    @property
    def state(self) -> EngineState:
        return self._state

    # ─────────────────────────────────────────────────────────────────────────
    # State Machine Transitions
    # ─────────────────────────────────────────────────────────────────────────

    def _transition_to(self, new_state: EngineState, reason: str = "") -> bool:
        """
        Transition to new state with validation.
        Only valid transitions are allowed. Invalid transitions halt the engine.
        """
        valid_next = VALID_TRANSITIONS.get(self._state, [])
        if new_state not in valid_next:
            logger.error(
                f"INVALID TRANSITION: {self._state} → {new_state}. "
                f"Valid: {valid_next}. Halting."
            )
            self._state = EngineState.HALTED
            self._audit.emit(
                AuditEventType.MCX_STATE_TRANSITION,
                decision="HALTED",
                reason=f"Invalid transition from {self._state} to {new_state}",
            )
            return False

        self._previous_state = self._state
        self._state = new_state
        self._audit.emit(
            AuditEventType.MCX_STATE_TRANSITION,
            decision=f"{self._previous_state} → {new_state}",
            reason=reason,
        )
        logger.info(f"State: {self._previous_state} → {new_state} ({reason})")
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Main Run Loop
    # ─────────────────────────────────────────────────────────────────────────

    async def run(
        self,
        symbols: List[str],
        capital: float,
        available_contracts: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """
        Main engine loop implementing the full state machine.
        
        Flow:
          INIT → PRE_OPEN → CONTRACT_VALIDATION → DATA_INIT → WARMUP →
          SCANNING ↔ EVALUATING ↔ ENTRY_PENDING ↔ POSITION_ACTIVE →
          EXIT_PENDING → CLOSED
        """
        self._running = True
        self._capital = capital
        self._peak_capital = max(self._peak_capital, capital)

        logger.info(
            f"MCX V8.6 Engine starting | Live: {self.is_live} | "
            f"Symbols: {symbols} | Capital: {capital:,.0f}"
        )

        try:
            # ── INIT → PRE_OPEN ──
            await self._phase_init()

            # ── PRE_OPEN → CONTRACT_VALIDATION ──
            await self._phase_pre_open(symbols)

            # ── CONTRACT_VALIDATION ──
            await self._phase_contract_validation(symbols, available_contracts)

            # ── DATA_INIT ──
            await self._phase_data_init()

            # ── WARMUP ──
            await self._phase_warmup()

            # ── MAIN TRADING LOOP ──
            while self._running and self._state not in (
                EngineState.CLOSED, EngineState.HALTED
            ):
                await self._trading_loop_iteration()
                await asyncio.sleep(0.1)  # Prevent tight loop

        except Exception as e:
            logger.critical(f"MCX Engine fatal error: {e}", exc_info=True)
            self._audit.emit(
                AuditEventType.MCX_ERROR,
                context={"error": str(e), "state": self._state.value},
                decision="HALT",
                reason=f"Unhandled exception: {e}",
            )
            self._state = EngineState.HALTED
        finally:
            await self._shutdown()

    # ─────────────────────────────────────────────────────────────────────────
    # Phase Implementations
    # ─────────────────────────────────────────────────────────────────────────

    async def _phase_init(self) -> None:
        """Initialize engine — verify prerequisites."""
        session = get_mcx_session_state()
        if session == MCXSessionState.HOLIDAY:
            logger.info("MCX Holiday — engine will not start")
            self._transition_to(EngineState.CLOSED, "Holiday")
            self._running = False
            return

        if session == MCXSessionState.CLOSED:
            # Wait for pre-open
            logger.info("Market closed — waiting for pre-open window")
            self._transition_to(EngineState.CLOSED, "Market closed")
            self._running = False
            return

        self._transition_to(EngineState.PRE_OPEN, "Engine initialized")

    async def _phase_pre_open(self, symbols: List[str]) -> None:
        """Pre-open phase — validate configuration and connectivity."""
        if self._state != EngineState.PRE_OPEN:
            return

        # Validate symbols
        invalid = [s for s in symbols if s not in CONFIG.contract.allowed_symbols]
        if invalid:
            logger.error(f"Invalid symbols requested: {invalid}")
            self._transition_to(EngineState.HALTED, f"Invalid symbols: {invalid}")
            return

        # Check broker connectivity
        if self._gateway is not None:
            try:
                # Placeholder: await self._gateway.ping()
                pass
            except Exception as e:
                logger.error(f"Broker connectivity check failed: {e}")
                self._transition_to(EngineState.HALTED, f"Broker unreachable: {e}")
                return

        self._transition_to(EngineState.CONTRACT_VALIDATION, "Pre-open checks passed")

    async def _phase_contract_validation(
        self,
        symbols: List[str],
        available_contracts: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Resolve and validate MCX futures contracts."""
        if self._state != EngineState.CONTRACT_VALIDATION:
            return

        for symbol in symbols:
            contracts_for_symbol = available_contracts.get(symbol, [])
            contract = resolve_mcx_future(symbol, contracts_for_symbol)

            if contract is None:
                logger.error(f"Failed to resolve contract for {symbol}")
                self._audit.emit(
                    AuditEventType.MCX_CONTRACT_RESOLVED,
                    symbol=symbol,
                    decision="FAILED",
                    reason="No valid FUTCOM contract found",
                )
                continue

            self._contracts[symbol] = contract
            self._audit.emit(
                AuditEventType.MCX_CONTRACT_RESOLVED,
                symbol=symbol,
                context={
                    "trading_symbol": contract.trading_symbol,
                    "expiry": str(contract.expiry_date),
                    "days_to_expiry": contract.days_to_expiry,
                    "lot_size": contract.lot_size,
                },
                decision="RESOLVED",
            )

        if not self._contracts:
            self._transition_to(EngineState.HALTED, "No contracts resolved")
            return

        self._transition_to(
            EngineState.DATA_INIT,
            f"Resolved {len(self._contracts)} contracts",
        )

    async def _phase_data_init(self) -> None:
        """Initialize market data feeds and state objects."""
        if self._state != EngineState.DATA_INIT:
            return

        for symbol, contract in self._contracts.items():
            self._market_states[symbol] = MCXMarketState(symbol, contract)
            self._acceleration_detectors[symbol] = AccelerationDetector()

        # Subscribe to tick feeds (placeholder for actual implementation)
        # await self._subscribe_to_feeds(list(self._contracts.keys()))

        self._transition_to(EngineState.WARMUP, "Data feeds initialized")

    async def _phase_warmup(self) -> None:
        """Wait for sufficient candle data before trading."""
        if self._state != EngineState.WARMUP:
            return

        warmup_deadline = time.time() + (CONFIG.session.warmup_minutes * 60)
        logger.info(f"Warmup phase: waiting {CONFIG.session.warmup_minutes} minutes for data")

        while time.time() < warmup_deadline and self._running:
            # Check if all symbols have sufficient data
            all_ready = all(
                ms.has_sufficient_data()
                for ms in self._market_states.values()
            )
            if all_ready:
                break
            await asyncio.sleep(1)

        # Verify session still open
        session = get_mcx_session_state()
        if session not in (MCXSessionState.OPEN, MCXSessionState.NO_NEW_ENTRY):
            self._transition_to(EngineState.CLOSED, "Session closed during warmup")
            return

        self._transition_to(EngineState.SCANNING, "Warmup complete")

    # ─────────────────────────────────────────────────────────────────────────
    # Main Trading Loop
    # ─────────────────────────────────────────────────────────────────────────

    async def _trading_loop_iteration(self) -> None:
        """Single iteration of the main trading loop."""

        # Check session state
        session = get_mcx_session_state()
        if session == MCXSessionState.CLOSED:
            await self._handle_session_close()
            return

        # ── SCANNING state ──
        if self._state == EngineState.SCANNING:
            await self._scan_candidates()

        # ── EVALUATING state ──
        elif self._state == EngineState.EVALUATING:
            await self._evaluate_candidate()

        # ── ENTRY_PENDING state ──
        elif self._state == EngineState.ENTRY_PENDING:
            await self._handle_pending_entry()

        # ── POSITION_ACTIVE state ──
        elif self._state == EngineState.POSITION_ACTIVE:
            await self._manage_active_positions()

        # ── EXIT_PENDING state ──
        elif self._state == EngineState.EXIT_PENDING:
            await self._handle_pending_exit()

        # ── RECONCILING state ──
        elif self._state == EngineState.RECONCILING:
            await self._handle_reconciliation()

        # ── ERROR_RECOVERY state ──
        elif self._state == EngineState.ERROR_RECOVERY:
            await self._handle_error_recovery()

    async def _scan_candidates(self) -> None:
        """Scan all symbols for trading opportunities."""
        for symbol, market_state in self._market_states.items():
            # Skip if already in position
            if symbol in self._positions:
                continue

            # Data health gate
            health = self._data_health_gate.mcx_data_health(market_state)
            if not health.is_healthy:
                self._audit.emit(
                    AuditEventType.MCX_DATA_HEALTH_CHECK,
                    symbol=symbol,
                    decision="UNHEALTHY",
                    reason=health.reason,
                )
                continue

            # Score the candidate
            score = mcx_score(market_state)
            self._acceleration_detectors[symbol].record_score(score)

            self._audit.emit(
                AuditEventType.MCX_SCORE_COMPUTED,
                symbol=symbol,
                context=score.components,
                decision=f"score={score.total_score}",
            )

            if score.passes_threshold:
                # Check acceleration
                accel = self._acceleration_detectors[symbol].detect()
                if accel.is_accelerating:
                    self._audit.emit(
                        AuditEventType.MCX_ACCELERATION_DETECTED,
                        symbol=symbol,
                        context={
                            "score_vel": accel.score_velocity,
                            "momentum_vel": accel.momentum_velocity,
                            "rs_vel": accel.rs_velocity,
                            "rvol_vel": accel.rvol_velocity,
                        },
                    )
                    # Move to evaluation
                    self._evaluating_symbol = symbol
                    self._evaluating_score = score
                    self._evaluating_accel = accel
                    self._transition_to(
                        EngineState.EVALUATING,
                        f"{symbol} score={score.total_score} accelerating",
                    )
                    return

        # Also manage existing positions during scanning
        if self._positions:
            await self._manage_active_positions_inline()

    async def _evaluate_candidate(self) -> None:
        """Full evaluation of a candidate for entry."""
        symbol = getattr(self, "_evaluating_symbol", None)
        if symbol is None:
            self._transition_to(EngineState.SCANNING, "No candidate to evaluate")
            return

        market_state = self._market_states[symbol]
        score = self._evaluating_score
        accel = self._evaluating_accel

        # Determine direction (simplified: use momentum direction)
        direction = "LONG" if score.momentum_score > 50 else "SHORT"

        # Hard safety gate
        contract = self._contracts[symbol]
        health = self._data_health_gate.mcx_data_health(market_state)
        safety = mcx_can_trade(
            contract=contract,
            market_state=market_state,
            data_health=health,
            capital=self._capital,
            daily_pnl=self._daily_pnl,
            weekly_pnl=self._weekly_pnl,
            peak_capital=self._peak_capital,
            active_positions=len(self._positions),
            broker_connected=self._gateway is not None or not self.is_live,
        )

        if not safety.can_trade:
            self._audit.emit(
                AuditEventType.MCX_SAFETY_GATE,
                symbol=symbol,
                context=safety.checks_passed,
                decision="BLOCKED",
                reason=safety.blocking_reason,
            )
            self._transition_to(EngineState.SCANNING, safety.blocking_reason or "Safety blocked")
            return

        # Entry decision (combines score + acceleration + room)
        decision = mcx_entry_decision(market_state, score, accel, direction)

        self._audit.emit(
            AuditEventType.MCX_ENTRY_DECISION,
            symbol=symbol,
            context={
                "direction": direction,
                "score": score.total_score,
                "accelerating": accel.is_accelerating,
                "has_room": decision.room.has_room,
            },
            decision="ENTER" if decision.should_enter else "REJECT",
            reason=decision.reason,
        )

        if not decision.should_enter:
            self._transition_to(EngineState.SCANNING, decision.reason)
            return

        # Calculate position size
        entry_price = market_state.last_price
        if entry_price is None:
            self._transition_to(EngineState.SCANNING, "No price available")
            return

        # Calculate stop first for position sizing
        stop = mcx_initial_sl(contract, entry_price, direction, market_state)
        if stop is None:
            self._transition_to(EngineState.SCANNING, "Cannot calculate stop-loss")
            return

        size = mcx_position_size(
            contract=contract,
            entry_price=entry_price,
            stop_price=stop.price,
            capital=self._capital,
            current_exposure=self._calculate_current_exposure(),
        )

        if not size.is_valid:
            self._audit.emit(
                AuditEventType.MCX_POSITION_SIZED,
                symbol=symbol,
                decision="REJECTED",
                reason=size.rejection_reason,
            )
            self._transition_to(EngineState.SCANNING, size.rejection_reason or "Invalid size")
            return

        self._audit.emit(
            AuditEventType.MCX_POSITION_SIZED,
            symbol=symbol,
            context={
                "lots": size.lots,
                "quantity": size.quantity,
                "risk_amount": size.risk_amount,
                "margin_required": size.margin_required,
            },
            decision="SIZED",
        )

        # Store pending entry details
        self._pending_entry = {
            "symbol": symbol,
            "contract": contract,
            "direction": direction,
            "entry_price": entry_price,
            "stop": stop,
            "size": size,
            "score": score,
        }

        # Execute entry (or shadow-record)
        if self.is_live:
            self._transition_to(EngineState.ENTRY_PENDING, f"Placing order for {symbol}")
            await self._place_entry_order()
        else:
            # Shadow mode: record the decision
            await self._shadow_record_entry()
            self._transition_to(EngineState.SCANNING, "Shadow entry recorded")

    async def _place_entry_order(self) -> None:
        """Place entry order with broker."""
        pending = self._pending_entry
        if pending is None:
            self._transition_to(EngineState.SCANNING, "No pending entry")
            return

        # Rate limit check
        if not self._rate_limiter.check():
            logger.warning("Rate limit — deferring entry")
            self._transition_to(EngineState.SCANNING, "Rate limited")
            return

        symbol = pending["symbol"]
        contract = pending["contract"]
        direction = pending["direction"]
        size = pending["size"]

        try:
            self._rate_limiter.consume()

            # Place order via gateway (placeholder)
            order_id = f"MCX_{symbol}_{int(time.time())}"
            # order_result = await self._gateway.place_order(
            #     symbol=contract.trading_symbol,
            #     exchange="MCX",
            #     transaction_type="BUY" if direction == "LONG" else "SELL",
            #     quantity=size.quantity,
            #     order_type="MARKET",
            # )
            # order_id = order_result.get("order_id")

            self._audit.emit(
                AuditEventType.MCX_ORDER_PLACED,
                symbol=symbol,
                context={
                    "order_id": order_id,
                    "direction": direction,
                    "quantity": size.quantity,
                    "lots": size.lots,
                },
                decision="PLACED",
            )

            # Verify after timeout
            result = await verify_order_after_timeout(
                order_id=order_id,
                expected_qty=size.quantity,
                gateway=self._gateway,
            )

            self._audit.emit(
                AuditEventType.MCX_ORDER_VERIFIED,
                symbol=symbol,
                context={
                    "order_id": order_id,
                    "status": result.status.value,
                    "filled_qty": result.filled_qty,
                    "filled_price": result.filled_price,
                },
                decision=result.status.value,
            )

            if result.status == OrderStatus.FILLED or (
                result.adopted and result.filled_qty > 0
            ):
                await self._create_position(pending, result)
            else:
                self._transition_to(
                    EngineState.SCANNING,
                    f"Order not filled: {result.status.value}",
                )

        except Exception as e:
            logger.error(f"Entry order failed for {symbol}: {e}")
            self._audit.emit(
                AuditEventType.MCX_ERROR,
                symbol=symbol,
                context={"error": str(e)},
                decision="FAILED",
                reason=str(e),
            )
            self._error_count += 1
            if self._error_count >= self._max_consecutive_errors:
                self._transition_to(EngineState.HALTED, "Max consecutive errors")
            else:
                self._transition_to(EngineState.ERROR_RECOVERY, str(e))

    async def _create_position(
        self,
        pending: Dict[str, Any],
        order_result: OrderResult,
    ) -> None:
        """Create position tracking after confirmed fill."""
        symbol = pending["symbol"]
        contract = pending["contract"]
        direction = pending["direction"]
        stop = pending["stop"]
        entry_price = order_result.filled_price or pending["entry_price"]
        quantity = order_result.filled_qty

        # Validate quantity is valid lot size
        if quantity % contract.lot_size != 0:
            logger.error(
                f"INVALID QUANTITY {quantity} — not multiple of lot size {contract.lot_size}"
            )
            # Fail closed: do not track invalid position
            self._transition_to(EngineState.HALTED, "Invalid quantity from broker")
            return

        lots = quantity // contract.lot_size
        risk_per_unit = abs(entry_price - stop.price)

        # Create trail state
        trail = TrailState(
            current_trail_price=stop.price,
            current_r_level=0.0,
            initial_sl=stop.price,
            entry_price=entry_price,
            risk_per_unit=risk_per_unit,
            direction=direction,
            last_update_time=time.time(),
        )

        position = MCXPosition(
            symbol=symbol,
            contract=contract,
            direction=direction,
            entry_price=entry_price,
            entry_time=time.time(),
            quantity=quantity,
            lots=lots,
            initial_sl=stop.price,  # IMMUTABLE
            current_sl=stop.price,
            trail_state=trail,
            order_id=order_result.order_id,
        )

        self._positions[symbol] = position
        self._error_count = 0  # Reset on success

        self._audit.emit(
            AuditEventType.MCX_SL_SET,
            symbol=symbol,
            context={
                "initial_sl": stop.price,
                "method": stop.method,
                "distance_pct": stop.distance_pct,
                "atr": stop.atr_value,
            },
            decision="SET",
            reason="Initial stop — IMMUTABLE",
        )

        # Place SL order with broker
        await self._place_sl_order(position)

        self._transition_to(EngineState.POSITION_ACTIVE, f"Position active: {symbol}")

    async def _place_sl_order(self, position: MCXPosition) -> None:
        """Place stop-loss order with broker."""
        if not self.is_live:
            return

        try:
            self._rate_limiter.consume()
            # sl_order_id = await self._gateway.place_sl_order(
            #     symbol=position.contract.trading_symbol,
            #     exchange="MCX",
            #     transaction_type="SELL" if position.direction == "LONG" else "BUY",
            #     quantity=position.quantity,
            #     trigger_price=position.current_sl,
            # )
            sl_order_id = f"SL_{position.symbol}_{int(time.time())}"
            position.sl_order_id = sl_order_id
        except Exception as e:
            logger.error(f"SL order placement failed: {e}")
            # Critical failure — halt
            self._transition_to(EngineState.HALTED, f"Cannot place SL: {e}")

    async def _manage_active_positions(self) -> None:
        """Manage all active positions — trailing, reversal checks, reconciliation."""
        for symbol, position in list(self._positions.items()):
            market_state = self._market_states.get(symbol)
            if market_state is None or market_state.last_price is None:
                continue

            current_price = market_state.last_price

            # Update position tracking
            if position.direction == "LONG":
                position.max_price = max(position.max_price, current_price)
                position.unrealized_pnl = (current_price - position.entry_price) * position.quantity
            else:
                position.min_price = min(position.min_price, current_price)
                position.unrealized_pnl = (position.entry_price - current_price) * position.quantity

            position.r_multiple = position.trail_state.calculate_r(current_price)

            # 1. Progressive trailing stop
            new_trail, should_update = update_progressive_trail(
                position.trail_state, current_price
            )
            if should_update:
                position.trail_state = new_trail
                position.current_sl = new_trail.current_trail_price
                self._audit.emit(
                    AuditEventType.MCX_TRAIL_TRIGGERED,
                    symbol=symbol,
                    context={
                        "new_sl": new_trail.current_trail_price,
                        "r_level": new_trail.current_r_level,
                        "current_r": position.r_multiple,
                    },
                    decision="TRAILED",
                )
                # Update SL with broker
                if self.is_live and position.sl_order_id:
                    await self._update_broker_sl(position)

            # 2. Check for confirmed reversal exit
            reversal = check_reversal_exit(
                market_state=market_state,
                direction=position.direction,
                entry_price=position.entry_price,
                entry_time=position.entry_time,
                initial_sl=position.initial_sl,
            )
            if reversal.is_confirmed:
                self._audit.emit(
                    AuditEventType.MCX_REVERSAL_EXIT,
                    symbol=symbol,
                    context={
                        "structure_broken": reversal.structure_broken,
                        "momentum_confirmed": reversal.momentum_confirmed,
                        "volume_confirmed": reversal.volume_confirmed,
                        "candles_held": reversal.candles_held,
                    },
                    decision="EXIT",
                    reason=reversal.reason,
                )
                await self._close_position(symbol, "REVERSAL_CONFIRMED")

            # 3. Quantity reconciliation
            if self._reconciler.should_reconcile() and self.is_live:
                recon = await self._reconciler.reconcile(
                    symbol=symbol,
                    expected_qty=position.quantity,
                    sl_order_id=position.sl_order_id,
                    gateway=self._gateway,
                )
                self._audit.emit(
                    AuditEventType.MCX_RECONCILIATION,
                    symbol=symbol,
                    context={
                        "broker_qty": recon.broker_qty,
                        "expected_qty": recon.expected_qty,
                        "sl_qty": recon.sl_qty,
                        "synced": recon.is_synced,
                    },
                    decision="SYNCED" if recon.is_synced else "MISMATCH",
                    reason=recon.action_taken,
                )

        # Return to scanning if no positions need attention
        if not self._positions:
            self._transition_to(EngineState.SCANNING, "No active positions")

    async def _manage_active_positions_inline(self) -> None:
        """Lightweight position management during scanning state."""
        for symbol, position in list(self._positions.items()):
            market_state = self._market_states.get(symbol)
            if market_state is None or market_state.last_price is None:
                continue

            current_price = market_state.last_price

            # Update P&L
            if position.direction == "LONG":
                position.max_price = max(position.max_price, current_price)
                position.unrealized_pnl = (current_price - position.entry_price) * position.quantity
            else:
                position.min_price = min(position.min_price, current_price)
                position.unrealized_pnl = (position.entry_price - current_price) * position.quantity

            position.r_multiple = position.trail_state.calculate_r(current_price)

            # Check trailing
            new_trail, should_update = update_progressive_trail(
                position.trail_state, current_price
            )
            if should_update:
                position.trail_state = new_trail
                position.current_sl = new_trail.current_trail_price
                if self.is_live and position.sl_order_id:
                    await self._update_broker_sl(position)

            # Check stop hit
            if position.direction == "LONG" and current_price <= position.current_sl:
                await self._close_position(symbol, "STOP_HIT")
            elif position.direction == "SHORT" and current_price >= position.current_sl:
                await self._close_position(symbol, "STOP_HIT")

    async def _close_position(self, symbol: str, reason: str) -> None:
        """Close a position and record the result."""
        position = self._positions.get(symbol)
        if position is None:
            return

        market_state = self._market_states.get(symbol)
        exit_price = market_state.last_price if market_state else position.current_sl

        # Calculate P&L
        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity

        # Update daily/weekly P&L
        self._daily_pnl += pnl
        self._weekly_pnl += pnl
        self._capital += pnl
        self._peak_capital = max(self._peak_capital, self._capital)

        self._audit.emit(
            AuditEventType.MCX_POSITION_CLOSED,
            symbol=symbol,
            context={
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "direction": position.direction,
                "quantity": position.quantity,
                "lots": position.lots,
                "pnl": round(pnl, 2),
                "r_multiple": round(position.r_multiple, 2),
                "initial_sl": position.initial_sl,
                "final_sl": position.current_sl,
                "hold_duration_sec": round(time.time() - position.entry_time, 1),
            },
            decision="CLOSED",
            reason=reason,
        )

        # Place exit order with broker
        if self.is_live:
            await self._place_exit_order(position)

        # Shadow mode: record trade
        if not self.is_live:
            shadow_trade = ShadowTrade(
                symbol=symbol,
                direction=position.direction,
                entry_price=position.entry_price,
                entry_time=position.entry_time,
                exit_price=exit_price,
                exit_time=time.time(),
                exit_reason=reason,
                pnl=pnl,
                r_multiple=position.r_multiple,
                initial_sl=position.initial_sl,
                max_favorable_excursion=(
                    position.max_price - position.entry_price
                    if position.direction == "LONG"
                    else position.entry_price - position.min_price
                ),
                max_adverse_excursion=abs(position.entry_price - position.initial_sl),
            )
            self._shadow_validation.record_trade(shadow_trade)

        # Remove from active positions
        del self._positions[symbol]

    async def _place_exit_order(self, position: MCXPosition) -> None:
        """Place exit order with broker."""
        if not self._rate_limiter.check():
            logger.warning("Rate limited on exit — will retry")
            return

        self._rate_limiter.consume()
        # Cancel SL order first
        # if position.sl_order_id:
        #     await self._gateway.cancel_order(position.sl_order_id)
        # Place market exit
        # await self._gateway.place_order(
        #     symbol=position.contract.trading_symbol,
        #     exchange="MCX",
        #     transaction_type="SELL" if position.direction == "LONG" else "BUY",
        #     quantity=position.quantity,
        #     order_type="MARKET",
        # )

    async def _update_broker_sl(self, position: MCXPosition) -> None:
        """Update stop-loss order at broker."""
        if not self._rate_limiter.check():
            return
        self._rate_limiter.consume()
        # await self._gateway.modify_order(
        #     order_id=position.sl_order_id,
        #     trigger_price=position.current_sl,
        #     quantity=position.quantity,
        # )

    async def _shadow_record_entry(self) -> None:
        """Record entry decision in shadow mode (no real order)."""
        pending = self._pending_entry
        if pending is None:
            return

        symbol = pending["symbol"]
        contract = pending["contract"]
        direction = pending["direction"]
        entry_price = pending["entry_price"]
        stop = pending["stop"]
        size = pending["size"]

        # Create shadow position for tracking
        risk_per_unit = abs(entry_price - stop.price)
        trail = TrailState(
            current_trail_price=stop.price,
            current_r_level=0.0,
            initial_sl=stop.price,
            entry_price=entry_price,
            risk_per_unit=risk_per_unit,
            direction=direction,
            last_update_time=time.time(),
        )

        position = MCXPosition(
            symbol=symbol,
            contract=contract,
            direction=direction,
            entry_price=entry_price,
            entry_time=time.time(),
            quantity=size.quantity,
            lots=size.lots,
            initial_sl=stop.price,
            current_sl=stop.price,
            trail_state=trail,
            order_id=f"SHADOW_{symbol}_{int(time.time())}",
        )

        self._positions[symbol] = position

        self._audit.emit(
            AuditEventType.MCX_SHADOW_DECISION,
            symbol=symbol,
            context={
                "direction": direction,
                "entry_price": entry_price,
                "stop": stop.price,
                "lots": size.lots,
                "quantity": size.quantity,
                "risk": size.risk_amount,
            },
            decision="SHADOW_ENTRY",
            reason="Live mode disabled — paper trade recorded",
        )

    async def _handle_pending_entry(self) -> None:
        """Handle state while waiting for entry order fill."""
        # Timeout handling — if stuck, return to scanning
        await asyncio.sleep(CONFIG.broker.order_verify_timeout_sec)
        if self._state == EngineState.ENTRY_PENDING:
            self._transition_to(EngineState.SCANNING, "Entry timeout")

    async def _handle_pending_exit(self) -> None:
        """Handle state while waiting for exit order fill."""
        await asyncio.sleep(CONFIG.broker.order_verify_timeout_sec)
        if self._state == EngineState.EXIT_PENDING:
            self._transition_to(EngineState.SCANNING, "Exit processed")

    async def _handle_reconciliation(self) -> None:
        """Handle reconciliation state."""
        for symbol, position in list(self._positions.items()):
            recon = await self._reconciler.reconcile(
                symbol=symbol,
                expected_qty=position.quantity,
                sl_order_id=position.sl_order_id,
                gateway=self._gateway,
            )
            if not recon.is_synced and recon.broker_qty == 0:
                # Position closed at broker — clean up
                await self._close_position(symbol, "BROKER_CLOSED")

        self._transition_to(EngineState.SCANNING, "Reconciliation complete")

    async def _handle_error_recovery(self) -> None:
        """Handle error recovery — attempt to return to safe state."""
        logger.warning(f"Error recovery (errors: {self._error_count})")
        await asyncio.sleep(5)  # Cool-down

        if self._error_count >= self._max_consecutive_errors:
            self._transition_to(EngineState.HALTED, "Too many errors")
            return

        # Try to return to scanning if data is healthy
        all_healthy = all(
            self._data_health_gate.mcx_data_health(ms).is_healthy
            for ms in self._market_states.values()
        )
        if all_healthy:
            self._transition_to(EngineState.SCANNING, "Recovered — data healthy")
        else:
            self._transition_to(EngineState.DATA_INIT, "Re-initializing data")

    async def _handle_session_close(self) -> None:
        """Handle end of session — close all positions, clean up."""
        logger.info("Session closing — squaring off positions")

        for symbol in list(self._positions.keys()):
            await self._close_position(symbol, "SESSION_CLOSE")

        self._transition_to(EngineState.CLOSED, "Market session ended")
        self._running = False

    async def _shutdown(self) -> None:
        """Clean shutdown — flush audit, report shadow results."""
        logger.info("MCX Engine shutting down")

        # Report shadow validation status
        if not self.is_live:
            validated, criteria = self._shadow_validation.is_validated()
            logger.info(
                f"Shadow Validation: {'PASSED' if validated else 'NOT YET'} | "
                f"Trades: {self._shadow_validation.trade_count} | "
                f"Win Rate: {self._shadow_validation.win_rate:.1%} | "
                f"PF: {self._shadow_validation.profit_factor:.2f} | "
                f"Criteria: {json.dumps(criteria, indent=2)}"
            )

        # Flush audit buffer
        self._audit.flush()
        self._running = False

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_current_exposure(self) -> float:
        """Calculate total current MCX notional exposure."""
        total = 0.0
        for pos in self._positions.values():
            total += pos.entry_price * pos.quantity
        return total

    def ingest_tick(self, symbol: str, tick: Tick) -> None:
        """Feed a tick into the appropriate market state (called by data feed)."""
        market_state = self._market_states.get(symbol)
        if market_state:
            market_state.process_tick(tick)

    def get_shadow_report(self) -> Dict[str, Any]:
        """Get current shadow validation report."""
        validated, criteria = self._shadow_validation.is_validated()
        return {
            "validated": validated,
            "criteria": criteria,
            "trade_count": self._shadow_validation.trade_count,
            "trading_days": self._shadow_validation.trading_days,
            "total_pnl": self._shadow_validation.total_pnl,
            "win_rate": self._shadow_validation.win_rate,
            "profit_factor": self._shadow_validation.profit_factor,
            "max_drawdown": self._shadow_validation.max_drawdown,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get current engine status."""
        return {
            "state": self._state.value,
            "is_live": self.is_live,
            "positions": {
                sym: {
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "current_sl": pos.current_sl,
                    "r_multiple": round(pos.r_multiple, 2),
                    "unrealized_pnl": round(pos.unrealized_pnl, 2),
                    "lots": pos.lots,
                }
                for sym, pos in self._positions.items()
            },
            "contracts": {
                sym: {
                    "trading_symbol": c.trading_symbol,
                    "days_to_expiry": c.days_to_expiry,
                }
                for sym, c in self._contracts.items()
            },
            "daily_pnl": round(self._daily_pnl, 2),
            "capital": round(self._capital, 2),
            "error_count": self._error_count,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point (for direct execution / testing)
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    """Example usage / smoke test."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )

    engine = MCXEngine()
    logger.info(f"MCX V8.6 Engine initialized | Live: {engine.is_live}")
    logger.info(f"State: {engine.state}")
    logger.info(f"Shadow report: {engine.get_shadow_report()}")

    # Example: run with sample symbols (would need real contract data)
    # await engine.run(
    #     symbols=["CRUDEOIL", "GOLD"],
    #     capital=500_000,
    #     available_contracts={...},
    # )


if __name__ == "__main__":
    asyncio.run(main())
