# V8.4 Production Integration — staged overlay for the existing V8.2 bot

## Purpose
This package integrates the redesigned V8.4 opportunity/entry/risk logic into the existing V8.2 Dhan orchestration. It does **not** replace the proven V8.2 Dhan gateway blindly.

### Canonical entry architecture
1. Existing V8.2 market-data/candidate funnel
2. V8.4 five-mode opportunity engine:
   - ORB breakout
   - ORB breakdown
   - Momentum continuation
   - VWAP reversal
   - Late momentum
3. 100-point directional score:
   - NIFTY 7
   - Sector 8
   - RS 15
   - Momentum 18
   - Directional RVOL 12
   - VWAP/trend 10
   - Mode setup 15
   - Entry quality 10
   - Remaining opportunity 5
4. Risk-based quantity instead of score-to-leverage tiers
5. Three 10-second entry confirmations
6. Broker-side hard SL before a position is considered protected
7. Conservative profit/reversal management

## Safety
- Current V8.2 service must remain untouched until EC2 preflight passes.
- Live trading requires `V84_ENABLE_LIVE=1` and `V82_DRY_RUN=0`.
- Do not commit tokens, `.env`, AWS credentials, or Dhan secrets.
- V8.4 does not guarantee 0.06% daily or 25% monthly returns; those are objectives, not guaranteed outcomes.

## Server install
Copy this package beside the existing bot. Do not overwrite the live service initially.
Run:
`./deploy/v84_preflight_ec2.sh`
Then:
`./v84_dry_run.py`
Then:
`python3 -m pytest -q tests`

The staged service file is intentionally disabled until manual review of the EC2/Dhan environment.

## MCX / Swing status
The V8.4 swing and MCX strategy modules are included for the redesigned decision logic, while the existing server-side swing/MCX orchestrators remain separate. Do not enable new live swing/MCX order paths from this package until their actual Dhan segment/order contract is validated on EC2. This avoids silently replacing working production infrastructure with an unverified gateway.
