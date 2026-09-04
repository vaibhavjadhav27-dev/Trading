"""
MCX V8.6 Production Readiness — Configuration
==============================================
All parameters governing MCX commodity futures trading.
Separated from logic for auditability and hot-reload capability.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: Master Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCXSessionTiming:
    """MCX trading session hours (IST)."""
    pre_open_start: str = "08:55"
    market_open: str = "09:00"
    market_close: str = "23:30"
    # Agri commodities close earlier
    agri_close: str = "17:00"
    # No new entries in last N minutes before close
    no_entry_minutes_before_close: int = 15
    # Data init warmup period (minutes after open)
    warmup_minutes: int = 5


@dataclass(frozen=True)
class MCXContractConfig:
    """Contract resolution parameters."""
    # Allowed commodity symbols for trading
    allowed_symbols: Tuple[str, ...] = (
        "CRUDEOIL", "NATURALGAS", "NATGASMINI", "GOLD", "GOLDM", "SILVER",
        "SILVERM", "GOLDPETAL", "COPPER", "ZINC", "LEAD", "ALUMINIUM",
        "NICKEL", "COTTONCANDY", "MENTHAOIL",
    )
    # Lot sizes per commodity (units per lot)
    lot_sizes: Dict[str, int] = field(default_factory=lambda: {
        "CRUDEOIL": 100,
        "NATURALGAS": 1250,
        "GOLD": 100,        # grams
        "GOLDM": 10,        # grams (mini)
        "SILVER": 30,       # kg
        "SILVERM": 5,       # kg (mini)
        "COPPER": 2500,     # kg
        "ZINC": 5000,       # kg
        "LEAD": 5000,       # kg
        "ALUMINIUM": 5000,  # kg
        "NICKEL": 1500,     # kg
        "COTTONCANDY": 25,  # bales
        "MENTHAOIL": 360,   # kg
    })
    # Tick sizes (minimum price movement)
    tick_sizes: Dict[str, float] = field(default_factory=lambda: {
        "CRUDEOIL": 1.0,
        "NATURALGAS": 0.10,
        "GOLD": 1.0,
        "GOLDM": 1.0,
        "SILVER": 1.0,
        "SILVERM": 1.0,
        "COPPER": 0.05,
        "ZINC": 0.05,
        "LEAD": 0.05,
        "ALUMINIUM": 0.05,
        "NICKEL": 0.10,
        "COTTONCANDY": 10.0,
        "MENTHAOIL": 0.10,
    })
    # Only trade nearest-month future (no far-month, no options)
    use_nearest_expiry: bool = True
    # Minimum days to expiry before rolling to next month
    min_days_to_expiry: int = 3
    # Never fall back to options
    allow_options_fallback: bool = False
    # Exchange segment code for MCX futures
    exchange_segment: str = "MCX"


@dataclass(frozen=True)
class MCXDataConfig:
    """Data feed and candle construction parameters."""
    # Maximum age of tick data before considered stale (seconds)
    max_tick_staleness_sec: int = 5
    # Candle timeframes to construct from ticks
    candle_timeframes: Tuple[int, ...] = (1, 5, 15)  # minutes
    # Minimum ticks required to form a valid 1-minute candle
    min_ticks_per_candle: int = 10
    # Data health check interval (seconds)
    health_check_interval_sec: int = 3
    # Consecutive stale readings before declaring data unhealthy
    stale_threshold_count: int = 3
    # Minimum candle history needed for indicators
    min_candles_1m: int = 30
    min_candles_5m: int = 20
    min_candles_15m: int = 10
    # Volume spike detection (multiple of 20-period avg)
    volume_spike_multiplier: float = 2.0


@dataclass(frozen=True)
class MCXScoringConfig:
    """MCX-specific candidate scoring parameters (separate from NSE)."""
    # Scoring weights (must sum to 1.0)
    weight_momentum: float = 0.25
    weight_relative_strength: float = 0.20
    weight_volume: float = 0.20
    weight_structure: float = 0.20
    weight_spread: float = 0.15
    # Minimum score to consider entry (0-100 scale)
    min_entry_score: int = 65
    # Momentum: rate of change lookback (candles)
    momentum_lookback: int = 14
    # Relative strength: vs sector/index benchmark
    rs_lookback: int = 20
    # RVOL: relative volume vs 20-period average
    rvol_baseline_periods: int = 20


@dataclass(frozen=True)
class MCXAccelerationConfig:
    """Early acceleration detection thresholds."""
    # Velocity thresholds (per-minute rate of change)
    score_velocity_threshold: float = 2.0       # points/min
    momentum_velocity_threshold: float = 0.5    # %/min
    rs_velocity_threshold: float = 0.3          # ratio/min
    rvol_velocity_threshold: float = 0.5        # multiple/min
    # Minimum velocities that must exceed threshold
    min_velocities_passing: int = 3
    # Lookback window for velocity calculation (candles)
    velocity_lookback: int = 5


@dataclass(frozen=True)
class MCXAntiChaseConfig:
    """Anti-chase and room-to-run filter parameters."""
    # Maximum % already moved from day's open (reject if exceeded)
    max_move_from_open_pct: float = 3.0
    # Minimum room to next resistance as multiple of ATR
    min_room_atr_multiple: float = 1.5
    # Maximum distance from VWAP (ATR multiples)
    max_vwap_distance_atr: float = 2.0
    # Reject if price is within this % of day high (for longs)
    reject_near_high_pct: float = 0.5
    # Reject if RSI > this value (overbought)
    reject_rsi_above: int = 75
    # Reject if RSI < this value (oversold, for shorts)
    reject_rsi_below: int = 25


@dataclass(frozen=True)
class MCXPositionSizingConfig:
    """Position sizing and risk parameters."""
    # Maximum risk per trade (% of capital)
    max_risk_per_trade_pct: float = 1.0
    # Maximum total MCX exposure (% of capital)
    max_mcx_exposure_pct: float = 20.0
    # Maximum number of concurrent MCX positions
    max_concurrent_positions: int = 3
    # Maximum lots per single trade
    max_lots_per_trade: int = 5
    # Margin requirement multiplier (safety buffer over exchange minimum)
    margin_safety_multiplier: float = 1.25
    # Minimum free margin required to enter (% of capital)
    min_free_margin_pct: float = 30.0


@dataclass(frozen=True)
class MCXStopLossConfig:
    """Structural/ATR stop-loss parameters."""
    # ATR period for stop calculation
    atr_period: int = 14
    # ATR multiplier for initial stop distance
    atr_multiplier: float = 2.0
    # Minimum stop distance (ticks)
    min_stop_ticks: int = 5
    # Maximum stop distance (% of entry price)
    max_stop_pct: float = 3.0
    # Use structure (swing low/high) if tighter than ATR stop
    use_structure_stop: bool = True
    # Buffer below structure (ticks)
    structure_buffer_ticks: int = 3
    # Initial stop is IMMUTABLE once set
    initial_sl_immutable: bool = True


@dataclass(frozen=True)
class MCXTrailingConfig:
    """Progressive R-based trailing stop configuration."""
    # R-multiple thresholds and trail levels
    # Format: (trigger_R, trail_to_R)
    trail_levels: Tuple[Tuple[float, float], ...] = (
        (1.0, 0.5),    # At 1R profit -> trail to 0.5R
        (2.0, 1.25),   # At 2R profit -> trail to 1.25R
        (2.5, 1.75),   # At 2.5R profit -> trail to 1.75R
        (3.0, 2.25),   # At 3R profit -> trail to 2.25R
    )
    # Check trailing every N seconds
    trail_check_interval_sec: int = 5
    # Only trail in direction of trade (never widen)
    one_direction_only: bool = True


@dataclass(frozen=True)
class MCXReversalExitConfig:
    """Confirmed reversal exit parameters (not immediate on fade)."""
    # Require ALL of these for confirmed reversal:
    # 1. Structure break (close below/above key level)
    require_structure_break: bool = True
    # 2. Momentum confirmation (MACD crossover or RSI divergence)
    require_momentum_confirm: bool = True
    # 3. Volume confirmation (above average on reversal candle)
    require_volume_confirm: bool = True
    # Minimum candles to hold before reversal exit allowed
    min_hold_candles: int = 3
    # Structure break: close beyond this level
    structure_break_atr_multiple: float = 0.5
    # Volume threshold for reversal confirmation
    reversal_volume_multiplier: float = 1.5


@dataclass(frozen=True)
class MCXBrokerConfig:
    """Broker interaction and order verification parameters."""
    # Order verification timeout (seconds)
    order_verify_timeout_sec: int = 10
    # If order fills during timeout, adopt the position
    adopt_if_filled: bool = True
    # Maximum retries for order placement
    max_order_retries: int = 3
    # Quantity reconciliation interval (seconds)
    reconciliation_interval_sec: int = 30
    # Minimum reconciliation interval
    reconciliation_min_sec: int = 15
    # Maximum reconciliation interval
    reconciliation_max_sec: int = 60
    # Sync SL quantity with actual position
    sync_sl_quantity: bool = True
    # Rate limit: max orders per minute
    max_orders_per_minute: int = 10
    # Rate limit: max orders per second
    max_orders_per_second: int = 2


@dataclass(frozen=True)
class MCXRiskConfig:
    """Hard safety gate parameters."""
    # Maximum daily loss before shutdown (% of capital)
    max_daily_loss_pct: float = 3.0
    # Maximum weekly loss before shutdown (% of capital)
    max_weekly_loss_pct: float = 6.0
    # Maximum drawdown from peak (% of capital)
    max_drawdown_pct: float = 8.0
    # Minimum capital required to trade
    min_capital: float = 100_000.0
    # Kill switch: shut down if any safety check fails
    fail_closed: bool = True
    # Maximum slippage tolerance (ticks)
    max_slippage_ticks: int = 5


@dataclass(frozen=True)
class MCXShadowConfig:
    """Shadow-mode validation parameters."""
    # MCX_LIVE=False by default — shadow only until validation passes
    mcx_live: bool = False
    # Validation criteria thresholds
    min_shadow_days: int = 10
    min_shadow_trades: int = 30
    required_win_rate: float = 0.55
    required_profit_factor: float = 1.5
    max_allowed_drawdown_pct: float = 5.0
    # Track all decisions even in shadow mode
    log_all_decisions: bool = True
    # Paper trade in shadow mode (send no real orders)
    paper_trade: bool = True


@dataclass(frozen=True)
class MCXAuditConfig:
    """Audit trail configuration."""
    # Log file path
    audit_log_path: str = "logs/mcx_v86_audit.jsonl"
    # Events to capture
    capture_contract_resolved: bool = True
    capture_data_health: bool = True
    capture_score_computed: bool = True
    capture_entry_decision: bool = True
    capture_order_placed: bool = True
    capture_order_verified: bool = True
    capture_sl_updated: bool = True
    capture_trail_triggered: bool = True
    capture_reversal_exit: bool = True
    capture_position_closed: bool = True
    capture_reconciliation: bool = True
    capture_state_transition: bool = True
    # Include full context in audit events
    include_full_context: bool = True
    # Retention days
    retention_days: int = 90


# ─────────────────────────────────────────────────────────────────────────────
# MASTER CONFIG AGGREGATOR
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MCXConfig:
    """Master configuration aggregating all sub-configs."""
    session: MCXSessionTiming = field(default_factory=MCXSessionTiming)
    contract: MCXContractConfig = field(default_factory=MCXContractConfig)
    data: MCXDataConfig = field(default_factory=MCXDataConfig)
    scoring: MCXScoringConfig = field(default_factory=MCXScoringConfig)
    acceleration: MCXAccelerationConfig = field(default_factory=MCXAccelerationConfig)
    anti_chase: MCXAntiChaseConfig = field(default_factory=MCXAntiChaseConfig)
    position_sizing: MCXPositionSizingConfig = field(default_factory=MCXPositionSizingConfig)
    stop_loss: MCXStopLossConfig = field(default_factory=MCXStopLossConfig)
    trailing: MCXTrailingConfig = field(default_factory=MCXTrailingConfig)
    reversal_exit: MCXReversalExitConfig = field(default_factory=MCXReversalExitConfig)
    broker: MCXBrokerConfig = field(default_factory=MCXBrokerConfig)
    risk: MCXRiskConfig = field(default_factory=MCXRiskConfig)
    shadow: MCXShadowConfig = field(default_factory=MCXShadowConfig)
    audit: MCXAuditConfig = field(default_factory=MCXAuditConfig)


# Singleton instance
CONFIG = MCXConfig()
