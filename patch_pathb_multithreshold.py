import ast, shutil, sys, os
F = "path_b_shadow.py"
src = open(F).read()
shutil.copy(F, F + ".bak_mt")

# --- Edit 1: split the early-return so _mt_observe runs every tick (additive) ---
A_old = ("        if not self.regime_ok or sid in self._fired:\n"
         "            return False\n")
A_new = ("        if not self.regime_ok:\n"
         "            return False\n"
         "        # Multi-threshold RVol observation (ADDITIVE, log-only; does NOT affect _fired)\n"
         "        self._mt_observe(sid, symbol, price, rvol, gap_pct)\n"
         "        if sid in self._fired:\n"
         "            return False\n")
assert A_old in src, "Edit1 anchor not found (evaluate early-return)"
src = src.replace(A_old, A_new, 1)

# --- Edit 2: insert _mt_observe method just before summary() ---
MT = '''    def _mt_observe(self, sid, symbol, price, rvol, gap_pct):
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
            f.write(json.dumps(rec) + "\\n")
        self.log.info("[SHADOW-PATHB-MT] %s confirmed cross @%.2f RVol=%.2f -> "
                      "fire@4.5=%s fire@3.5=%s fire@2.5=%s (log-only)"
                      % (symbol, price, rvol if rvol is not None else 0.0,
                         fires["fire@4.5"], fires["fire@3.5"], fires["fire@2.5"]))

    def summary(self):'''
anchor = "    def summary(self):"
assert src.count(anchor) == 1, "summary anchor not unique/found"
src = src.replace(anchor, MT, 1)

# --- Edit 3: extend summary() return with MT fields ---
S_old = ('        return {"regime": self.market_regime, "regime_ok": self.regime_ok,\n'
         '                "shadow_entries": sorted(self._fired),\n'
         '                "candidates_tracked": len(self._streak)}')
S_new = ('        return {"regime": self.market_regime, "regime_ok": self.regime_ok,\n'
         '                "shadow_entries": sorted(self._fired),\n'
         '                "candidates_tracked": len(self._streak),\n'
         '                "mt_shadow_entries": sorted(getattr(self, "_mt_fired", set())),\n'
         '                "mt_thresholds": getattr(self, "_mt_thresholds", [])}')
assert S_old in src, "Edit3 anchor not found (summary return)"
src = src.replace(S_old, S_new, 1)

# --- ast verify, else auto-revert ---
try:
    ast.parse(src)
except SyntaxError as e:
    print("SYNTAX ERROR — reverting:", e); sys.exit(1)
open(F, "w").write(src)
print("PATCHED OK — 3 edits applied, syntax verified. Backup: %s.bak_mt" % F)
