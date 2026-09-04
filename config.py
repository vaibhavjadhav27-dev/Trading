PRICE_FLOOR = 60
PRICE_CEIL_TIER1 = 5000
PRICE_CEIL_TIER2 = 1500
GAP_MIN = 0.3
GAP_MAX_TIER2 = 8.0
GAP_REJECT = 15.0
VOLUME_BYPASS_RVOL = 2.5      # If RVOL > 2.5, bypass gap minimum filter
ADT_MIN_CR = 50               # Min avg daily turnover in Crores
ADT_MAX_CR = 70               # Max avg daily turnover in Crores (sweet spot)
TIER2_GAP_MIN = 1.0
RVOL_THRESHOLD = 2.0
TURNOVER_MIN_CR = 5.0
AVG_DAILY_TURNOVER_CR = 20.0
CIRCUIT_BUFFER_PCT = 3

WEIGHT_RS = 0.25
WEIGHT_TREND = 0.25
WEIGHT_RVOL = 0.20
WEIGHT_ATR_EXPANSION = 0.15
WEIGHT_BREAKOUT = 0.15

BONUS_RSI_SWEET = 0.05
BONUS_VOL_COMPRESSION = 0.05
BONUS_SECTOR_TOP = 0.05
BONUS_CANDLE = 0.03
PENALTY_TIER2 = -0.10
PENALTY_RSI_WEAK = -0.05
PENALTY_SECTOR_WEAK = -0.03

ORB_BUFFER_PCT = 0.1
ORB_MIN_RANGE_PCT = 0.8
ORB_MAX_RANGE_PCT = 3.0
CANDLE_UPPER_WICK_MAX = 0.6
RS_PERSISTENCE_MIN = 0.8
SPREAD_MAX_PCT = 0.5
ORDER_WAIT_SECONDS = 120
PARTIAL_FILL_MIN_PCT = 0.50

RISK_PER_TRADE_PCT = 1.5
ATR_SL_MULTIPLIER = 0.75  # aligned with server-side 0.75% hard SL
MIN_QUANTITY = 3
CASH_BUFFER_PCT = 5.0
TIER2_POSITION_FACTOR = 0.9
TIER2_TOP_N = 20

TRAIL_PHASE1_TRIGGER = 1.0
TRAIL_PHASE1_LEVEL = 0.25
TRAIL_PHASE2_TRIGGER = 1.5
TRAIL_PHASE2_LEVEL = 0.75
TRAIL_PHASE3_TRIGGER = 2.0
TRAIL_PHASE3_FACTOR = 0.60

MAX_CONSECUTIVE_LOSSES = 3
DAILY_LOSS_LIMIT_PCT = 5.0  # 10% of available balance per day
# DAILY_LOSS_LIMIT_RS removed — now 10% of available balance dynamically

MARKET_OPEN = "09:15"
ORB_END = "09:30"
SCAN_START = "09:31"
DEAD_ZONE_START = "14:30"
DEAD_ZONE_END = "14:30"
ENTRY_CUTOFF = "14:45"
MANDATORY_EXIT = "15:09"
BOT_SHUTDOWN = "15:12"

SCAN_INTERVAL_NORMAL = 15
SCAN_INTERVAL_NEAR_ORB = 15
NEAR_ORB_THRESHOLD_PCT = 0.3
MONITOR_INTERVAL_SECONDS = 30
LTP_BATCH_SIZE = 10
MAX_WORKERS = 10

VOL_COMPRESSION_RATIO = 0.8
VOL_EXHAUSTION_RATIO = 1.5

GEMINI_ENABLED = True
GEMINI_TIMEOUT_SECONDS = 10
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_MAX_CALLS_PER_DAY = 2

TABLE_TRADES = "TradingBot_Trades"
TABLE_DAILY_STATE = "TradingBot_DailyState"
TABLE_ACTIVE_TRADE = "TradingBot_ActiveTrade"
TABLE_ORDER_AUDIT = "TradingBot_OrderAudit"

EMAIL_ON_START = True
EMAIL_ON_NO_TRADE = True
EMAIL_ON_ENTRY = True
EMAIL_ON_EXIT = True
EMAIL_ON_LOSS_LOCK = True
EMAIL_ON_EOD_SUMMARY = True
EMAIL_ON_ERROR = True
EMAIL_ON_SHORTLIST = True

MIN_ENTRY_SCORE = 0.55

# Direction filter: Only allow gap-up + breakout above ORB_H (no bearish breakdowns)
DIRECTION_FILTER = False  # replaced by SIDE_SELECTION
MIN_GAP_FOR_ENTRY = 0.0  # Minimum positive gap required (0 = any green gap)

# P0: RISK CONTROLS
SLIPPAGE_PCT = 0.2
MAX_NOTIONAL_MULT = 0.50  # 50% capital cap (was 0.08)
ADV_MAX_PCT = 1.0
MAX_EFFECTIVE_RISK_PCT = 3.0
ORDER_BUFFER_PCT = 0.1

# P1: VIX-ADAPTIVE
NIFTY_ORB_MEDIAN = 0.6
VIX_SCALE_MIN = 0.7
VIX_SCALE_MAX = 1.8
GAP_MIN_BASE = 0.75

# P2: SAFETY
FNO_BAN_CHECK = True
SERIES_BLACKLIST = ['T2T', 'BZ', 'BE', 'SM', 'ST']

# Max % of capital per single trade
MAX_CAPITAL_PCT = 100  # kept in sync with MAX_POSITION_PCT

# -- v6.8 patch: name the sizer actually reads --
MAX_POSITION_PCT = 100  # full available (5% reserve via CASH_BUFFER_PCT)

# -- regime measured-move multiples (lever B) --
MEASURED_MOVE_M_NORMAL = 1.25
MEASURED_MOVE_M_TRENDING = 1.50
MEASURED_MOVE_M_BEARISH = 1.25

# -- bearish-day live trading --
BEARISH_LIVE_ENABLED = True
BEARISH_SIZE_MULT = 0.75
BEARISH_MAX_CANDIDATES = 3

# -- dead-trade timer re-based for tight stop --
DEAD_TRADE_MIN_R = 0.5
DEAD_TRADE_MINUTES = 20


# ===== FILTERS_V2 (5-state regime redesign) -- default OFF, backtest-gated =====
FILTERS_V2 = True            # master flag. OFF = legacy behavior, module is no-op.
# RVol gates (time-adjusted vs adv_20d)
RVOL_TRENDING = 1.5
RVOL_NORMAL   = 0.30   # lowered: RVOL unavailable pre-09:45 IST
GAP_RVOL_BYPASS_PCT = 4.0  # stocks with gap >= 4% bypass RVOL gate (PINELABS fix)
RVOL_BEARISH  = 1.0
# TRENDING gap window (loosened ceiling)
GAP_FLOOR_TRENDING = 2.5
GAP_CEIL_TRENDING  = 7.5
# BEARISH-DEFENSIVE flat-opener window
BEARISH_GAP_LO = -0.5
BEARISH_GAP_HI =  0.5
# CHOPPY behavior
CHOPPY_PAUSE = False  # disabled: TRADE_ALL_REGIMES handles this           # True = hard pause to cash (shadow-logs would-have trades)
SHADOW_MODE = False           # True = run FILTERS_V2 non-trading, log per-state to filters_v2_shadow.log
# 2nd-position R-gate (Step 6, separate live opt-in)
R_GATE_TRENDING   = 1.5
R_GATE_NORMAL     = 1.0
POS2_LIVE_ENABLED = False     # leveraged 2nd position -- explicit opt-in required
# ===== end FILTERS_V2 =====


# ===== LIVE MARKET/OPPORTUNITY CADENCE =====
MARKET_SNAPSHOT_MINUTES = 30       # formal NIFTY + sector snapshot cadence
OPPORTUNITY_RESCAN_MINUTES = 15    # full stock opportunity rescan cadence
MARKET_SNAPSHOT_START = "09:30"
MARKET_SNAPSHOT_END = "15:00"
OPPORTUNITY_RESCAN_START = "09:30"
OPPORTUNITY_RESCAN_END = "14:45"

# ===== PROFIT MANAGEMENT =====
MIN_EXPECTED_MOVE_PCT = 0.40       # minimum feasible move before entry
PROFIT_PROTECT_TRIGGER_PCT = 0.40  # begin profit protection here
PROFIT_PROTECT_FLOOR_PCT = 0.35    # small execution/noise buffer; not a guarantee
PROFIT_TRAIL_STEP_PCT = 0.30
PEAK_REVERSAL_PCT = 0.15

# ===== SIDE SELECTION v7.0 =====
SIDE_SELECTION = "REGIME"
TRADE_ALL_REGIMES = True
BULLISH_SIDE = "LONG"
NORMAL_SIDE = "BEST_SCORE"
BEARISH_SIDE = "SHORT"
CHOPPY_SIDE = "MEAN_REVERT"
BULLISH_SIZE_MULT = 1.0
NORMAL_SIZE_MULT = 1.0
CHOPPY_SIZE_MULT = 0.50
CHOPPY_RSI_SHORT = 70
CHOPPY_RSI_LONG = 30
CHOPPY_SL_PCT = 0.30
CHOPPY_TARGET = "MID_ORB"
CHOPPY_REQUIRE_WICK = True
CHOPPY_WICK_MIN_PCT = 50

# ===== ROLLING TARGET EXIT v7.0 =====
ROLLING_EXIT_ENABLED = False
ROLLING_T1_PCT = 0.60
ROLLING_STEP_PCT = 0.95
ROLLING_BUFFER_PCT = 0.10
ROLLING_GRACE_SECONDS = 60
ROLLING_RSI_EXIT = 40
ROLLING_VOL_DROP_PCT = 30
ROLLING_RED_CANDLES = 2
ROLLING_HOLD_RSI = 50
ROLLING_HOLD_RECOVERY_PCT = 0.20

# ===== REGIME SIZE MULTIPLIERS =====
BULLISH_SIZE_MULT = 1.0
NORMAL_SIZE_MULT = 1.0
BEARISH_SIZE_MULT = 0.75

# ===== REGIME-AWARE GAP RULES =====
# Format: {regime: (allow_gap_up, allow_gap_down, min_gap, max_gap)}
REGIME_GAP_RULES = {
    'BULLISH':  (True,  False, 0.3, 8.0),
    'NORMAL':   (True,  True,  0.3, 5.0),
    'BEARISH':  (False, True,  0.3, 5.0),
    'CHOPPY':   (True,  True,  0.5, 3.0),
}

# RVOL threshold (lowered from 2.0 to 1.2 for more candidates)
RVOL_MIN = 1.2

ENABLE_SHORTS = True   # live shorts armed 07-29

MAX_POSITIONS = 999  # intentionally no daily/position-count cap; margin and execution checks govern entries
# Non-F&O stocks — LONG intraday only, cannot be shorted
LONG_ONLY_SIDS = {13310, 13147, 11491}  # KEI, PVRINOX, APARINDS

# ===== V8 FINAL POLICY (2026-08-12) =====
SCORING_VERSION = "V8_100"
MIN_CONVICTION_SCORE = 60.0
DIRECTIONAL_EDGE_MIN = 8.0
CHOPPY_EDGE_MIN = 6.0
MIN_EXPECTED_MOVE_PCT = 0.40
MARKET_SNAPSHOT_MINUTES = 30
OPPORTUNITY_RESCAN_MINUTES = 15
TRADE_ALL_REGIMES = True
ALLOW_SUDDEN_MOVE_ENTRY = True
SUDDEN_MOVE_RVOL_MIN = 1.5
SUDDEN_MOVE_MIN_SCORE = 70.0
SUDDEN_MOVE_MIN_EXPECTED_MOVE_PCT = 0.40
DYNAMIC_MARGIN_TIERS = ((60.0,70.0,1.0),(71.0,80.0,2.0),(80.0,100.0,4.5))
MAX_DAILY_TRADES = None
# Account safety remains separate from trade count: no arbitrary trade/day cap.
DAILY_LOSS_LIMIT_PCT = 5.0
