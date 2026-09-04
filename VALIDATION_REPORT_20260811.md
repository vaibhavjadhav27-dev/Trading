# Trading Bot vNext — validation report (2026-08-11)

## Implemented corrections
- Unified LONG/SHORT selection using the real dual-scorer maximum of 195 points.
- NIFTY regime is contextual bias only; no NO_TRADE shutdown for bearish/below-VWAP/choppy conditions.
- LONG and SHORT compete in every regime.
- Exact conviction/margin ladder: <60% no trade; 60–70% 1x; 71–80% 3x; >80% 5x.
- Dhan margin calculator is checked before order placement; theoretical leverage is never assumed to be available.
- Actual order fill is polled and tradebook/position sign is reconciled before a trade becomes ACTIVE.
- SHORT recovery/emergency exits use negative broker netQty and BUY-to-cover.
- Server-side SL failure causes immediate emergency exit rather than leaving an unprotected position.
- +0.40% is the profit-management trigger; profit is protected and then trailed rather than using a fixed target.
- NIFTY + sector snapshots are recorded every 30 minutes during market hours.
- Full opportunity universe is rescanned every 15 minutes from 09:45–14:45 IST.
- No daily trade-count cap and no market-regime day shutdown. Account-level loss protection remains as an execution safety circuit breaker.

## Tests run
- Runtime production-module compile: PASS.
- Runtime import test with Dhan SDK/network stub: PASS.
- Exact margin ladder unit tests: PASS.
- LONG/SHORT selection across TRENDING_UP, TRENDING_DOWN, NORMAL and CHOPPY: PASS.
- Choppy low-edge WAIT behaviour: PASS.
- LONG live-entry mock: PASS.
- SHORT live-entry mock: PASS.
- SL-failure -> emergency exit mock: PASS.
- SHORT exit -> BUY-to-cover mock: PASS.
- 30-minute NIFTY/sector snapshot write test: PASS.
- 15-minute rescan throttle test: PASS.
- Synthetic dual-score scale test: PASS.

## Live validation limitation
No live Dhan/AWS credentials are present in this environment, so no real NSE order, Dhan margin request, AWS SSM read, DynamoDB write, or live market-data call was executed. The uploaded archive's original integration suite also fails these credential-dependent tests for that reason.

## Supplied archive issues discovered
- Three legacy patch-helper files are syntactically broken (`do_rolling_patch.py`, `fix_side.py`, `side_block_fixed_2579.py`). They are not imported by the live orchestrator and should not be executed/deployed as runtime code.
- The supplied crontab references missing `update_stock_metrics.py` and `trading_dashboard.py`.
- The supplied crontab scheduled `download_nse_calendar.py` twice.
- A separate 15-minute `nifty_regime_scanner.py` cron is redundant after the integrated 30-minute market snapshot engine is enabled.
