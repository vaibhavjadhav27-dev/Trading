# V8.5.4 Unified Safety + Profit Patch

## Purpose
This final patch combines the two live problems found in the supplied 24-Aug-2026 code/logs:

1. **Safety/state problem:** broker ghost positions, quantity mismatches, unknown orders, invalid/crossed stops, and audit corruption.
2. **Friday profit problem:** winners were not reliably protected at the broker and the old monitor could silently skip the V8.5.3 exit block.

## What is changed
- Dhan is authoritative for actual live quantity/position state.
- Reconciliation every ~60 seconds while the engine is running.
- Deep broker order/trade audit every 15 minutes.
- Unknown positions are adopted and logged instead of silently ignored.
- Dhan order book/correlation lookup is used to identify bot vs external/unknown orders.
- Structural stop is correctly below support for LONG and above resistance for SHORT.
- SLM payload uses `price=0` and `triggerPrice=...`, consistent with Dhan's documented SLM request.
- `initial_sl` is stored permanently. Profit R is calculated from `initial_sl`, not the moving SL.
- Peak/MFE is monotonic.
- 1R/2R/2.5R/3R profit protection moves the broker-side SL.
- Temporary pullback alone does not exit.
- Software exit requires multi-factor reversal; setup invalidation remains an immediate thesis exit.
- Missing/rejected broker protection is explicit; the system never assumes a position is protected merely because an API call returned.
- Entry audit validation catches the observed `expected_r=score` corruption.
- The old `df`-before-assignment V8.5.3 monitor problem is removed in the supplied `trading_bot_v84_v854.py`.

## Critical deployment instruction
Use the supplied **gateway and bot copies as the reference implementation**, or apply the accompanying diff to the exact current server versions. Do not mix only one function from the patch with the old monitor. The profit manager must be the single authority for software exit/trailing; do not run the old V8.5.1 trailing/giveback block after it.

## Reconciliation policy
- Dhan position quantity is authoritative.
- If a local position is absent at Dhan, remove it from active local state and log the mismatch.
- If Dhan has an unknown position, adopt it with source `BROKER_ORPHAN_ADOPTED`, identify its order/trade if possible, and establish valid protection.
- If protection cannot be established, invoke the existing emergency safety path; do not leave the position naked.

## Timing
- Position reconciliation: about every 60 seconds.
- Deep order/trade audit: every 15 minutes.
- For real-time order attribution, add Dhan Live Order Update WebSocket or postback in the next iteration. The REST reconciliation remains the fallback/source-of-truth check.

## Tests
`test_v854_unified.py` passes pure deterministic tests for structural stop direction, crossed-stop recovery, original-risk trailing, temporary-pullback hold, confirmed reversal exit, and today's audit corruption.

These are strategy/state tests, not a live Dhan execution test. A small canary is still appropriate to verify broker-side SL modification and order attribution before restoring full risk.
