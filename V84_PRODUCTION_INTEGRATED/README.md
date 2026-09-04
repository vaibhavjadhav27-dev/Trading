# MCX + Swing V8.5.1 Shadow Strategy Patch

## Purpose

This patch creates **new independent MCX and Swing alpha engines** for shadow/paper validation. It does not enable live orders.

The comparison objective is:

> Give NSE Intraday, MCX Shadow and Swing Shadow the **same virtual starting capital**, equal to the Dhan available-balance snapshot at the start of the trading day, then compare **net return %, risk, drawdown, fees and opportunity quality**.

This is a fair strategy comparison, not a statement that all three can simultaneously consume the same real capital.

---

## Files

- `mcx_v851_strategy.py` — native MCX strategy, sizing and trailing.
- `swing_v851_strategy.py` — new 3-15 day swing strategy, sizing, runner and opportunity switching.
- `shadow_comparator.py` — equal-capital virtual ledgers and daily comparison.
- `tests/test_v851_mcx_swing.py` — pure strategy tests.
- `ENGINEER_INTEGRATION.md` — wiring requirements.

---

# MCX V8.5.1 strategy

## 1. Native MCX first

The previous currency/benchmark mismatch must never return.

MCX native price/ORB/VWAP/momentum are the primary signals.

International/FX data is **context only**.

### Score

- ORB continuation: 22%
- VWAP alignment: 16%
- Momentum: 16%
- RVOL: 12%
- volatility quality: 12%
- global context: 8%
- USDINR context: 5%
- native structure: 7%
- spread quality: 2%

External context is deliberately capped.

## 2. Three setups

### ORB_CONTINUATION
Native MCX 15-minute range breaks with momentum and VWAP alignment.

### VWAP_TREND_PULLBACK
Trend remains intact, price pulls toward VWAP without structural failure, then momentum resumes.

### COMPRESSION_BREAKOUT
A tight range contracts volatility and breaks with volume/momentum.

No trade is forced when none qualifies.

## 3. Contract selection

Do not hard-code a commodity or expiry.

Select among valid contracts using:

- liquidity
- spread
- expected move
- margin requirement
- contract multiplier/lot size

Risk is calculated first. Margin is only a feasibility constraint.

## 4. MCX sizing

Default shadow risk:

- 0.40% per MCX trade
- 1.20% aggregate MCX risk
- 20% capital reserve

If one lot itself exceeds remaining risk, **do not enter**.

## 5. MCX exits

No fixed profit target.

Use:

- initial ATR/structure stop
- R-based protection
- ATR trailing
- momentum/structure deterioration

Strong trends are allowed to become runners.

---

# Swing V8.5.1 strategy

The old swing logic used a +15% trigger to start the runner. That does not match the desired objective of identifying stocks capable of a 6%+ move in roughly a week.

The new strategy treats **6% as an opportunity threshold, not an automatic exit**.

## Three setups

### EARLY_ACCUMULATION

Before breakout:

- rising trend
- relative strength
- tight base
- accumulation/volume
- limited ATR extension

This is the highest-risk/highest-asymmetry setup.

### BREAKOUT

- 20-day/structural breakout
- RVOL expansion
- trend alignment
- not excessively extended

### BREAKOUT_RETEST

Preferred lower-risk setup:

- breakout
- controlled pullback
- former resistance holds
- momentum returns

## Swing score

- market context 7%
- sector strength 10%
- 10-day RS 15%
- 20-day RS 10%
- trend 15%
- accumulation 13%
- volume 10%
- structure 12%
- catalyst 3%
- entry location 5%

Market regime is context, not a blind hard gate.

## Entry

Minimum score: 74.

Expected move: at least 6%.

The strategy anticipates a move; it does not require the stock to have already moved 6%.

## Holding period

Expected holding period:

- Early accumulation: ~7 sessions
- Breakout: ~6 sessions
- Retest: ~5 sessions

Maximum default: 15 sessions.

A time stop exits a stagnant position when the thesis fails to develop.

## Profit management

6% is a milestone.

It does **not** mean:

`+6% -> sell everything`

Instead:

- below +6%: normal thesis management
- +6%: activate runner protection
- +10%: tighten protection
- strong momentum: keep holding
- structural/momentum deterioration: trail/exit

## Opportunity-cost rotation

If an existing swing has only a small remaining expected move and a new candidate has materially higher expected risk-adjusted opportunity, the framework can recommend replacing the weaker holding.

This prevents capital becoming trapped in stagnant trades.

---

# Equal-capital comparison

At the start of each trading day:

1. Fetch Dhan available balance.
2. Save one immutable snapshot.
3. Give that exact amount to:
   - NSE Intraday shadow ledger
   - MCX shadow ledger
   - Swing shadow ledger
4. Do not change starting capital during the day.
5. Each ledger independently applies its own risk/margin rules.
6. Record gross P&L, fees, net P&L, return %, drawdown and trade count.

Example:

| Engine | Starting virtual capital | Net P&L | Return |
|---|---:|---:|---:|
| NSE | ₹100,000 | ₹450 | +0.45% |
| MCX | ₹100,000 | ₹720 | +0.72% |
| Swing | ₹100,000 | ₹310 | +0.31% |

This does **not** mean ₹300,000 of real capital exists. It is three independent simulations using the same benchmark capital.

---

# What to compare after 20-30 sessions

Do NOT select the winner by return % alone.

Rank using:

1. Net return %
2. Maximum drawdown
3. Return / drawdown
4. Profit factor
5. Win rate
6. Average winner
7. Average loser
8. MFE / MAE
9. Fees as % of gross P&L
10. Capital utilisation
11. Number of valid opportunities missed
12. Consistency across market regimes

A strategy making +8% with 7% drawdown is not automatically better than one making +6% with 2% drawdown.

---

# Important live-safety rule

These files are strategy/paper modules only.

Engineers must NOT connect:

`evaluate_mcx()` or `evaluate_swing()` directly to a live order function.

The integration must initially:

`market data -> strategy -> sizing -> shadow ledger -> log`

and stop there.

Only after validation should a separate live execution adapter be considered.

---

# Required logging

For every candidate, even rejected ones, store:

- timestamp
- engine
- symbol/contract
- setup
- score
- all component scores
- entry
- stop
- expected move
- expected days
- risk/share or risk/lot
- calculated quantity
- margin requirement
- estimated costs
- expected net edge
- rejection reason
- MFE
- MAE
- exit
- exit reason
- net P&L
- return on virtual capital

This data is essential for deciding which engine deserves more capital later.

---

# Acceptance criteria

MCX:

- no US-vs-MCX price comparison
- native ORB
- dynamic contract selection
- risk-first sizing
- no forced one-lot trade
- margin checked
- broker-independent shadow mode
- runner/trailing logic
- 20-30 session validation

Swing:

- no fixed +15% exit
- 6% is an opportunity threshold
- three setup types
- risk-first sizing
- time stop
- runner
- opportunity-cost switching
- 20-30 session validation

Do not enable live MCX or live Swing until the shadow report is stable and the strategy passes the acceptance criteria.
