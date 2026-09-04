# ENGINEER INTEGRATION — MCX + SWING V8.5.1

## Rule 0 — shadow only

Do not call any live Dhan order-placement method from these modules.

Expected flow:

MARKET DATA
 -> feature builder
 -> strategy evaluate
 -> size
 -> shadow ledger
 -> logger

## 1. Equal capital snapshot

At the beginning of the trading day:

```python
from shadow_comparator import new_day_snapshot, create_ledgers

available = dhan.get_available_balance()  # use the project's existing authenticated gateway
snapshot = new_day_snapshot(available)
ledgers = create_ledgers(snapshot)
```

Persist the snapshot. Do not refresh the starting capital later.

If the account has existing live positions, they must NOT be treated as new shadow positions. Record them separately.

## 2. MCX integration

The existing `mcx_native_orb_v2.py` can remain as a data/ORB provider, but the new strategy must receive native MCX fields:

```text
price
vwap
atr
orb_high
orb_low
mom5
mom15
rvol
spread_pct
symbol
lot_size
global_bias
fx_bias
compression
```

Do not pass US price into MCX `price` or ORB fields.

Use the Dhan instrument master to resolve the actual MCX security ID, expiry and lot size.

Before every shadow entry:

```python
signal = evaluate_mcx(snapshot)

if signal:
    sizing = size_mcx(
        signal,
        capital=mcx_ledger.starting_capital,
        existing_risk=current_shadow_risk,
        margin_per_lot=current_dhan_margin_per_lot
    )
```

If `qty == 0`, log the rejection.

## 3. Swing integration

Build daily features from the existing 30d+ history plus the project's available data.

Required fields:

```text
ticker
price
sma20
sma50
sma200
atr
rs10
rs20
rvol
sector_rs
market_rs
high20
low20
base_tightness
accumulation
breakout
retest
close_strength
catalyst
```

Then:

```python
signal = evaluate_swing(snapshot)

if signal:
    sizing = size_swing(
        signal,
        capital=swing_ledger.starting_capital,
        existing_risk=current_shadow_risk
    )
```

## 4. Existing swing modules

Do not allow multiple independent entry/exit rules to coexist.

`Swing V8.5.1` becomes the authoritative strategy.

Existing `swing_scanner.py`, `swing_daily.py`, `swing_policy_v8.py` and `swing_exit.py` can remain temporarily for reference, but production shadow orchestration must call the new strategy exactly once.

Avoid double-entry from two scanners.

## 5. Existing MCX modules

Do not run two independent MCX entry engines.

Use one authoritative signal path:

`native MCX data -> mcx_v851_strategy.evaluate_mcx()`

The older MCX modules may provide data/ORB only.

## 6. Shadow mark-to-market

MCX:
- mark each open virtual lot to native MCX LTP.
- apply estimated costs on close.
- track MFE/MAE.

Swing:
- mark open virtual positions daily or at configured intraday checkpoints.
- apply the actual strategy exit decision.
- do not force a +6% exit.

## 7. Comparison report

Create:

`shadow_comparison/YYYY-MM-DD.json`

and:

`shadow_comparison/YYYY-MM-DD.csv`

with:

```text
engine
starting_capital
ending_equity
gross_pnl
fees
net_pnl
return_pct
max_drawdown_pct
trades
wins
losses
win_rate
profit_factor
capital_utilisation_pct
average_trade
average_winner
average_loser
mfe
mae
```

## 8. Do not rank only by return

After 20-30 sessions calculate:

```text
score =
return_quality
+ drawdown_quality
+ consistency
+ profit_factor
+ cost_efficiency
+ capital_efficiency
```

Then decide whether capital should be shifted.

## 9. Live enablement gate

Do not enable MCX/Swing live merely because one week is profitable.

Minimum recommended observation:
- 20 sessions
- preferably 30+

Require:
- no execution exceptions
- no unprotected simulated positions
- no duplicate entries
- no missing exit events
- complete candidate audit
- stable risk accounting
- stable net-of-cost results

Only after this should a separate live adapter be evaluated.
