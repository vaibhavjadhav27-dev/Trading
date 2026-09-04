
"""V8.1 live adapter.

Use this module from the main bot after candidate/market data are collected and
before order placement. It does NOT place orders. It returns the deterministic
decision and Dhan deployment tier. This is intentionally broker-agnostic so the
live execution layer can remain unchanged until dry-run validation is complete.
"""
from v81_entry_engine import evaluate_candidate

def evaluate_live_candidate(candidate, *, side=None, regime="NORMAL",
                            ltp=None, target=None, resistance=None, support=None,
                            atr_pct=0, momentum_5m=0, momentum_15m=0, momentum_30m=0,
                            confirmed=False, orb_confirmed=False,
                            pullback_reclaim=False, failed_breakout=False,
                            failed_breakdown=False, sudden_move=False,
                            entry_quality=0, setup_quality=0, df=None):
    f=dict(candidate or {})
    f.update({
        "side":side,"regime":regime,"ltp":ltp or f.get("ltp"),
        "target":target,"resistance":resistance,"support":support,
        "atr_pct":atr_pct,"momentum_5m":momentum_5m,"momentum_15m":momentum_15m,
        "momentum_30m":momentum_30m,"confirmed":confirmed,"orb_confirmed":orb_confirmed,
        "pullback_reclaim":pullback_reclaim,"failed_breakout":failed_breakout,
        "failed_breakdown":failed_breakdown,"sudden_move":sudden_move,
        "entry_quality":entry_quality,"setup_quality":setup_quality,"df":df
    })
    return evaluate_candidate(f, final_stage=True)
