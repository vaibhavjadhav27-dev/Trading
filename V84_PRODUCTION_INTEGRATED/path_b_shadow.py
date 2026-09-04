#!/usr/bin/env python3
# path_b_shadow.py  --  Path B (Early PDH-cross entry) — SHADOW ONLY.
#
# PLACES NO ORDERS. Logs would-be entries only. Gated behind FILTERS_V2 at the
# call site. Purpose: watch Path B fire on REAL ticks before it is ever armed
# live (executor is n=1; a new pre-09:30 order trigger must be shadow-proven).
#
# DUAL-TRIGGER MODEL (user-confirmed 2026-07-14):
#   Path A (Standard ORB)  -> existing live logic: gap-up, wait 09:30, ORB lock,
#                             buy 15-min high breakout. UNCHANGED. Not in this file.
#   Path B (Early PDH)     -> flat open + CONSERVATIVE/NORMAL regime + RVol explosion
#                             + price crosses pre-stored PDH, BEFORE 09:30.
#
# CONFIRMED PARAMETERS:
#   RVOL_GATE            = 4.5   (RVol must exceed this)
#   FLAT_LO, FLAT_HI     = -0.5%, +0.5%  (open gap must be inside this band)
#   REGIMES              = {"CONSERVATIVE", "NORMAL"}   (NOT TRENDING; NOT "BEARISH")
#   CONFIRM_CHECKS       = 2     (cross must hold this many CONSECUTIVE 30s checks)
#   WINDOW               = 09:16 .. 09:30 IST (Path B only; after 09:30 Path A owns it)
#
# REGIME-LABEL CORRECTNESS (same trap fixed at line 1654):
#   The system emits market_regime "CONSERVATIVE"/"NORMAL"/"TRENDING" and NEVER
#   the string "BEARISH". Path B keys off market_regime in REGIMES — do NOT test
#   == "BEARISH". self.regime stays "NORMAL" on below-VWAP days, so we use
#   market_regime (the mode) as the authoritative bearish-day signal.
#
# PDH source: pdh_cache.build_pdh_map(candidates) resolved ONCE at selection.

RVOL_GATE = 4.5
FLAT_LO, FLAT_HI = -0.5, 0.5
REGIMES = {"CONSERVATIVE", "NORMAL"}
CONFIRM_CHECKS = 2


class PathBShadow:
    """Stateful shadow evaluator. One instance per trading day.

    Feed it a tick snapshot every ~30s during 09:16-09:30. It logs (never trades)
    when a candidate satisfies ALL Path B conditions for CONFIRM_CHECKS consecutive
    calls. Tracks per-sid confirmation streak and fires the shadow log ONCE per sid.
    """

    def __init__(self, log, pdh_map, market_regime,
                 rvol_gate=RVOL_GATE, confirm_checks=CONFIRM_CHECKS):
        self.log = log
        self.pdh_map = pdh_map or {}
        self.market_regime = market_regime
        self.rvol_gate = rvol_gate
        self.confirm_checks = confirm_checks
        self._streak = {}     # sid -> consecutive-confirm count
        self._fired = set()   # sid -> already shadow-logged (fire once)
        self.regime_ok = market_regime in REGIMES
        if not self.regime_ok:
            log.info("[SHADOW-PATHB] regime=%s not in %s -> Path B dormant today"
                     % (market_regime, sorted(REGIMES)))

    def evaluate(self, sid, symbol, price, rvol, gap_pct):
        """Call every ~30s per candidate. Returns True the tick a shadow-entry is
        logged (for counting only). NEVER places an order."""
        if not self.regime_ok:
            return False
        # Multi-threshold RVol observation (ADDITIVE, log-only; does NOT affect _fired)
        self._mt_observe(sid, symbol, price, rvol, gap_pct)
        if sid in self._fired:
            return False
        pdh = self.pdh_map.get(sid)
        # Hard gates (any fail resets the streak)
        gates_ok = (
            pdh is not None and pdh > 0
            and FLAT_LO <= gap_pct <= FLAT_HI
            and rvol is not None and rvol > self.rvol_gate
            and price is not None and price > pdh
        )
        if not gates_ok:
            self._streak[sid] = 0
            return False
        # Multi-check confirmation: cross must HOLD across consecutive checks
        self._streak[sid] = self._streak.get(sid, 0) + 1
        if self._streak[sid] < self.confirm_checks:
            self.log.info("[SHADOW-PATHB] %s cross holding %d/%d (px=%.2f > PDH=%.2f, "
                          "RVol=%.1f, gap=%+.2f%%)"
                          % (symbol, self._streak[sid], self.confirm_checks,
                             price, pdh, rvol, gap_pct))
            return False
        # Confirmed -> SHADOW ENTRY (log only, no order)
        self._fired.add(sid)
        self.log.warning("[SHADOW-PATHB] WOULD-ENTER %s @%.2f | PDH=%.2f RVol=%.1f "
                         "gap=%+.2f%% regime=%s | (shadow: NO order placed)"
                         % (symbol, price, pdh, rvol, gap_pct, self.market_regime))
        return True

    def _mt_observe(self, sid, symbol, price, rvol, gap_pct):
        """ADDITIVE multi-threshold RVol logger. Streak driven by price>PDH +
        flat-open ONLY (RVol NOT in the streak gate) so lower thresholds can be
        evaluated honestly. Records RVol at the confirmed cross and logs which of
        4.5 / 3.5 / 2.5x it clears. Fires once per sid. Never affects self._fired
        or any order path. Writes logs/path_b_multithreshold_shadow.jsonl."""
        import json, os, datetime
        if not hasattr(self, "_mt_thresholds"):
            self._mt_thresholds = [4.5, 3.5, 2.5]
            self._mt_streak = {}
            self._mt_fired = set()
            os.makedirs("logs", exist_ok=True)
        if sid in self._mt_fired:
            return
        pdh = self.pdh_map.get(sid)
        cross_ok = (pdh is not None and pdh > 0
                    and FLAT_LO <= gap_pct <= FLAT_HI
                    and price is not None and price > pdh)
        if not cross_ok:
            self._mt_streak[sid] = 0
            return
        self._mt_streak[sid] = self._mt_streak.get(sid, 0) + 1
        if self._mt_streak[sid] < self.confirm_checks:
            return
        self._mt_fired.add(sid)
        fires = {("fire@%s" % t): (rvol is not None and rvol > t)
                 for t in self._mt_thresholds}
        now = datetime.datetime.now()
        rec = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
               "symbol": symbol, "sid": sid, "price": round(price, 2),
               "pdh": round(pdh, 2),
               "rvol_at_cross": round(rvol, 2) if rvol is not None else None,
               "gap_pct": round(gap_pct, 2), "regime": self.market_regime}
        rec.update(fires)
        with open("logs/path_b_multithreshold_shadow.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.log.info("[SHADOW-PATHB-MT] %s confirmed cross @%.2f RVol=%.2f -> "
                      "fire@4.5=%s fire@3.5=%s fire@2.5=%s (log-only)"
                      % (symbol, price, rvol if rvol is not None else 0.0,
                         fires["fire@4.5"], fires["fire@3.5"], fires["fire@2.5"]))

    def summary(self):
        return {"regime": self.market_regime, "regime_ok": self.regime_ok,
                "shadow_entries": sorted(self._fired),
                "candidates_tracked": len(self._streak),
                "mt_shadow_entries": sorted(getattr(self, "_mt_fired", set())),
                "mt_thresholds": getattr(self, "_mt_thresholds", [])}


# ------------------------------------------------------------------
# INTEGRATION (shadow-first — behind FILTERS_V2, in the run-loop):
#
#   from pdh_cache import build_pdh_map
#   from path_b_shadow import PathBShadow
#
#   # after select_candidates(), before the 09:30 ORB wait:
#   if getattr(config, "FILTERS_V2", False):
#       pdh_map, missing = build_pdh_map(self.candidates)
#       self._pathb = PathBShadow(log, pdh_map, self.market_regime)
#       log.info("[SHADOW-PATHB] armed for %d candidates (%d missing PDH)"
#                % (len(pdh_map), len(missing)))
#
#   # inside the existing 09:16->09:30 wait loop, every ~30s per candidate:
#   if getattr(self, "_pathb", None):
#       for c in self.candidates:
#           sid = str(c["security_id"])
#           self._pathb.evaluate(sid, c.get("symbol"),
#                                live_price(sid), live_rvol(sid), c.get("gap_pct"))
#
# NOTE: this only LOGS. Wiring these shadow entries to place_order() is a SEPARATE,
# later step — only after a clean shadow log is observed on a real CONSERVATIVE/
# NORMAL flat-open day. Do NOT arm live before then (executor n=1).
# ------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("pathb-test")
    # Synthetic test: one qualifying sid, confirm streak of 2 fires once.
    pdh_map = {"111": 645.0, "222": 100.0}
    ev = PathBShadow(log, pdh_map, "CONSERVATIVE")
    print("--- tick 1: cross but first check (should hold 1/2) ---")
    ev.evaluate("111", "ABDL", 646.0, 5.2, 0.1)
    print("--- tick 2: still crossing (should FIRE shadow entry) ---")
    fired = ev.evaluate("111", "ABDL", 646.5, 5.0, 0.2)
    print("fired:", fired)
    print("--- tick 3: same sid (should NOT re-fire) ---")
    print("re-fire:", ev.evaluate("111", "ABDL", 647.0, 5.0, 0.2))
    print("--- sid 222: RVol too low (should never fire) ---")
    ev.evaluate("222", "TESTLO", 101.0, 3.0, 0.0)
    print("summary:", ev.summary())
