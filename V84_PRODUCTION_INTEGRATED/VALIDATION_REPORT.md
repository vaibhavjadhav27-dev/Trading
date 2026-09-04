# V8.4 Production Integration Validation

Date: 2026-08-16

## Baseline
- Existing V8.2 Dhan orchestration retained as the execution foundation.
- V8.4 is an overlay/integration release; the existing live V8.2 service is not modified by the staged installer.

## Checks completed
- Python compile: PASS
- V8.4 strategy dry run (LONG/SHORT): PASS
- V8.4 integration unit tests: 4/4 PASS
- Production import graph: PASS with a local `dhanhq` interface stub; actual EC2 environment must provide the installed `dhanhq` package.
- No live broker orders were placed during validation.

## Strategy diagnostics
The supplied archive contains 5 trading dates of 5-minute candle directories. Because the archived candle CSVs do not expose reliable timestamps and the dataset is small for statistical inference, no claim of a production win rate, 0.06% daily return, or 25% monthly return is made from this replay. The included replay is diagnostic only and must not be treated as a backtest guarantee.

## Live blockers that must be checked on EC2
1. Existing V8.2 service status and systemd unit.
2. Dhan static-IP preflight.
3. Current Dhan token validity.
4. Actual `dhanhq`/Python package versions in the existing venv.
5. Broker order/SL behaviour in dry-run/sandbox before any live canary.
6. Swing and MCX live gateways must be validated separately before promoting them from their existing orchestrators.

## Risk model
- Base intraday risk: 0.60% of usable balance per trade.
- Maximum open risk: 1.50%.
- Daily soft stop: 1.00%.
- Daily hard stop: 1.75%.
- Three consecutive losses lock new entries.
- 10% cash reserve.
- No score-to-4.5x leverage ladder.

## Deployment rule
Do not set `V84_ENABLE_LIVE=1` until EC2 preflight and a controlled canary are completed.
