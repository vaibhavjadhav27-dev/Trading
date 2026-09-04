# V8.2 — Integrated Trading Bot Baseline

This package is the clean V8.2 baseline. The canonical live path is:

`market quote -> 30m NIFTY/sector context -> 15m direction-neutral candidates -> 100-point LONG/SHORT score -> final setup/entry score -> >=0.40% feasible move -> 1x/2x/4.5x desired deployment -> Dhan margin calculator -> Dhan order -> fill verification -> broker position verification -> server-side SL -> profit protection/peak exit -> Dhan exit -> position verification`.

## Dhan live requirements
- Set `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` or keep the existing secrets-manager integration and pass the resolved values into the gateway.
- Dhan order placement/modification/cancellation requires the registered static IP.
- Use `DHAN_BASE_URL=https://sandbox.dhan.co/v2` with sandbox credentials for API integration tests.
- `V82_DRY_RUN=1` tests the complete decision/order state machine without submitting live orders.

## Margin policy
- <60: no trade
- 60–70: 1x desired exposure
- 71–80: 2x desired exposure
- >80: 4.5x desired exposure
Actual Dhan margin is authoritative; quantity is reduced if the broker reports insufficient margin.

## Market policy
- No daily trade-count cap.
- No regime/day shutdown.
- Bullish, bearish, normal and choppy markets remain eligible.
- Choppy markets use setup confirmation and closer exit management.
- Sudden movers are not rejected solely because they already moved.

## Important
The package contains the full integrated V8.2 execution path in `trading_bot_v82.py`. The older `trading_bot.py` remains in the package for backward compatibility/reference, but it is NOT the V8.2 live entry path.
