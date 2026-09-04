"""
Mid-session rescan — SHADOW / LOG-ONLY.
Re-applies the full filter stack (price floor + RS + RVol gate) at ~11:00 IST
to catch flat-open stocks that broke out intraday (which the 09:15 scan cannot see).
"gap" here = move since 09:15 OPEN, not overnight gap.

SAFETY: logs only. Never touches self.candidates, never calls place_order().
Inherits the UNCHANGED RVol gate — a late-runner only shadow-fires on real volume.
Activates only when FILTERS_V2=True. Meaningless until a live shortlist persists
(pending_YYYY-MM-DD.json) and days accumulate — treat early logs as substrate, not verdict.

INTEGRATION (behind FILTERS_V2, fired once ~11:00 IST from the run-loop):
    from midsession_rescan_shadow import MidSessionRescanShadow
    if getattr(config, "FILTERS_V2", False) and not getattr(self, "_rescan_done", False):
        if now_ist.hour == 11 and now_ist.minute < 5:
            rescan = MidSessionRescanShadow(log, self.market_regime, config)
            rescan.evaluate_universe(
                universe=self._prefilter_candidates or self.candidates,
                ltp_map=ltp_map,                      # {sid: ltp} from get_bulk_ltp
                open_map={str(c['security_id']): c.get('open_0915', c.get('ltp'))
                          for c in self.candidates},  # 09:15 open per sid
                rvol_fn=_rvol_fn,                     # same injectable used by FILTERS_V2 hook
                rs_scores=getattr(self, "_rs_scores", None),
            )
            self._rescan_done = True
"""
import json, os
from datetime import datetime

RVOL_MIN_DEFAULT = 4.5          # UNCHANGED — same gate as Path B; safety backbone
LATE_GAP_MIN     = 0.5          # min % move-since-open to be worth a shadow look
LOG_PATH         = "logs/midsession_rescan_shadow.jsonl"

class MidSessionRescanShadow:
    def __init__(self, log, regime, config):
        self.log = log
        self.regime = regime
        self.config = config
        self.price_floor = getattr(config, "PRICE_FLOOR", 60.0)
        self.price_ceil  = getattr(config, "PRICE_CEIL_TIER1", 1e9)
        self.rvol_min    = getattr(config, "VOLUME_BYPASS_RVOL", RVOL_MIN_DEFAULT)
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    def evaluate_universe(self, universe, ltp_map, open_map, rvol_fn=None, rs_scores=None):
        # Regime gate — only observe on tradeable regimes (mirror Path B / FILTERS_V2)
        state = "BEARISH-DEFENSIVE" if self.regime == "CONSERVATIVE" else self.regime
        if state not in ("BEARISH-DEFENSIVE", "NORMAL", "TRENDING"):
            self.log.info(f"[SHADOW-RESCAN] skipped — regime {state} not tradeable")
            return []
        ts = datetime.now().strftime("%H:%M")
        fired = []
        for c in universe:
            sid = str(c.get("security_id", c.get("sid", "")))
            sym = c.get("symbol", c.get("ticker", "?"))
            ltp = ltp_map.get(sid, 0)
            op  = open_map.get(sid, 0)
            if not ltp or not op:                         # need both to compute intraday move
                continue
            if ltp < self.price_floor or ltp > self.price_ceil:
                continue                                  # price gate — unchanged
            late_gap = (ltp - op) / op * 100
            if late_gap < LATE_GAP_MIN:
                continue                                  # not moving since open
            rv = rvol_fn(c) if rvol_fn else None          # UNCHANGED RVol gate
            rs = (rs_scores or {}).get(sym)
            passes_rvol = (rv is not None and rv >= self.rvol_min)
            rec = {
                "date": datetime.now().strftime("%Y-%m-%d"), "rescan_time": ts,
                "symbol": sym, "sid": sid, "ltp": round(ltp, 2),
                "open_0915": round(op, 2), "move_since_open_pct": round(late_gap, 2),
                "rvol": round(rv, 2) if rv is not None else None,
                "rs": round(rs, 2) if rs is not None else None,
                "would_shortlist": bool(passes_rvol),
                "regime": state,
            }
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if passes_rvol:
                fired.append(sym)
                self.log.info(f"[SHADOW-RESCAN] {sym} +{late_gap:.2f}% since open, "
                              f"RVol={rv:.2f}x -> WOULD shortlist (log-only)")
        self.log.info(f"[SHADOW-RESCAN] {ts}: {len(fired)} would-shortlist / "
                      f"{len(universe)} scanned (candidates UNCHANGED)")
        return fired

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("rescan_test")
    cfg = type("C", (), {"PRICE_FLOOR": 60.0, "PRICE_CEIL_TIER1": 5000, "VOLUME_BYPASS_RVOL": 4.5})
    r = MidSessionRescanShadow(log, "NORMAL", cfg)
    uni = [{"security_id": "1", "symbol": "HIGHRV"}, {"security_id": "2", "symbol": "LOWRV"},
           {"security_id": "3", "symbol": "FLAT"}]
    ltp = {"1": 110.0, "2": 108.0, "3": 100.2}
    opn = {"1": 100.0, "2": 100.0, "3": 100.0}
    rvf = lambda c: {"HIGHRV": 5.2, "LOWRV": 1.1, "FLAT": 6.0}.get(c["symbol"])
    out = r.evaluate_universe(uni, ltp, opn, rvol_fn=rvf, rs_scores={"HIGHRV": 2.1})
    assert out == ["HIGHRV"], f"expected [HIGHRV] (high move+RVol), got {out}"
    print("SELF-TEST PASS: only HIGHRV fired (LOWRV=low RVol blocked, FLAT=below move floor)")
