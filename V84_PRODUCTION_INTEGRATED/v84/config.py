from dataclasses import dataclass

@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.60
    max_open_risk_pct: float = 1.50
    daily_soft_stop_pct: float = 1.00
    daily_hard_stop_pct: float = 1.75
    max_consecutive_losses: int = 3
    cash_reserve_pct: float = 10.0
    max_intraday_positions: int = 3
    max_swing_positions: int = 5
    max_mcx_positions: int = 1
    max_single_notional_pct: float = 60.0
    max_total_notional_pct: float = 80.0
    slippage_pct: float = 0.10
    transaction_cost_buffer_pct: float = 0.12

@dataclass(frozen=True)
class IntradayConfig:
    # Existing V8.2 weights retained deliberately.
    market_points: float = 7.0
    sector_points: float = 8.0
    rs_points: float = 15.0
    momentum_points: float = 18.0
    rvol_points: float = 12.0
    vwap_points: float = 10.0
    setup_points: float = 15.0
    entry_points: float = 10.0
    opportunity_points: float = 5.0
    min_score_by_mode: tuple = (68.0, 70.0, 66.0, 78.0, 72.0)
    min_edge: float = 8.0
    min_expected_move_pct: float = 0.60
    min_rvol: float = 1.20
    strong_rvol: float = 1.80
    orb_buffer_pct: float = 0.05
    max_extension_atr: float = 1.75
    hard_stop_min_pct: float = 0.35
    hard_stop_max_pct: float = 0.90
    entry_cutoff: str = "14:45"
    eod_exit: str = "15:09"
    confirmation_seconds: int = 30
    daily_target_pct: float = 0.06

@dataclass(frozen=True)
class SwingConfig:
    min_score: float = 72.0
    risk_per_trade_pct: float = 0.75
    min_expected_gain_pct: float = 6.0
    preferred_hold_days: int = 5
    max_hold_days: int = 10
    hard_stop_max_pct: float = 5.0
    breakeven_r: float = 1.0
    trail_start_gain_pct: float = 4.0
    peak_reversal_pct: float = 3.0

@dataclass(frozen=True)
class McxConfig:
    min_score: float = 72.0
    risk_per_trade_pct: float = 0.50
    max_extension_atr: float = 1.50
    min_expected_move_pct: float = 0.70
    native_orb_bars: int = 3
    entry_cutoff: str = "22:15"
    eod_exit: str = "22:55"

RISK=RiskConfig(); INTRADAY=IntradayConfig(); SWING=SwingConfig(); MCX=McxConfig()
