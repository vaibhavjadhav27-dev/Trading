# V8.2 FINAL — Integrated NSE/Dhan Trading Bot

## Canonical live path
`trading_bot_v82.py` is the only live intraday order orchestrator in this package.
The old `trading_bot.py` remains as inherited infrastructure/reference; it must NOT be started as a second live service.

### Pipeline
1. Dhan profile/funds/static-IP preflight.
2. Broker/local position reconciliation; refuse new trades on mismatch.
3. NIFTY/sector market context every 30 minutes.
4. Direction-neutral stock discovery every 15 minutes using current intraday movement, not opening gap alone.
5. Candidate score 0–100; >=50 enters WATCH.
6. Live re-evaluation with actual LTP and 5-minute candles.
7. Final LONG/SHORT score 0–100.
8. Entry confirmation: ORB, momentum continuation, pullback/reclaim/rejection, failed-breakout or failed-breakdown.
9. Remaining directional opportunity must be >=0.40%.
10. Score <60 no trade; 60–70 = 1x; 71–80 = 2x; >80 = 4.5x desired exposure.
11. Dhan margin calculator is authoritative before every order.
12. Dhan market order -> fill verification -> broker position verification -> hard SL -> active state.
13. +0.40% is a protection trigger, not a fixed take-profit. Peak/retrace logic can continue the trade.
14. Exit order -> fill verification -> broker position must be zero before the trade is marked closed.

## Dhan correctness
The gateway uses the documented Dhan v2 endpoints and response envelopes, separate rate limits, `PART_TRADED`, 202 cancellation success, and `filledQty`/`remainingQuantity`/`averageTradedPrice`.

## Safety
- No daily trade-count cap.
- No choppy/bearish-day shutdown.
- No arbitrary rejection because a stock already moved.
- Static-IP preflight is enforced for live mode.
- Unmanaged broker positions cause the bot to refuse new trades.
- Failed hard-SL placement triggers emergency exit and is not marked active.

## AI / Swing / MCX
Grok/Gemini/Groq remain advisory/research components and do not override deterministic entry, margin or risk controls. Swing and MCX remain separate modules; MCX remains shadow unless separately enabled and validated. Post-market hooks are not part of the live order decision.

## Testing
Run:
`python v82_dhan_integration_test.py`
`python v82_complete_flow_test.py`
`python v82_validation.py`
`python v82_end_to_end_validation_final.py`
`python -m compileall -q .`

These are offline/mock tests. No real Dhan order was submitted from the development environment.
