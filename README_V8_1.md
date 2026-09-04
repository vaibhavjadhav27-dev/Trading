# Trading Bot V8.1 — Strategy Revision

This package is a revision of the supplied V8 bot focused on the scoring/entry problem identified in replay.

## Core changes
- Exactly 100 points per LONG/SHORT side.
- Candidate stage starts at 50, not 60.
- Final entry requires 60+ plus directional edge and setup/entry/0.40% feasibility.
- Momentum/acceleration increased to 18 points.
- NIFTY/sector context reduced to 7/8 points so context does not overpower stock action.
- RVOL is directionally allocated rather than giving both sides full conviction.
- Sudden movers are not automatically rejected.
- ORB is not the only supported setup; continuation, pullback/reclaim, failed breakout and failed breakdown are supported by the strategy layer.
- Choppy markets remain tradable through strong setup confirmation rather than an impossible score gate.
- Dhan deployment tiers: <60=0x, 60-70=1x, 71-80=2x, >80=4.5x.
- Target calculation is directional and never uses abs() to convert a wrong-side target into a positive move.

## Important
The strategy layer is validated offline. The live execution loop must be wired to v81_live_adapter.py and shadow-tested before live orders are enabled.
