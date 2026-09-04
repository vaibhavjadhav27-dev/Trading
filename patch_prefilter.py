import ast, shutil, datetime, sys
F = "trading_bot.py"
src = open(F).read()

# ── Patch 1: snapshot pre-filter top-10 before FILTERS_V2 trims ──
A1 = "        self.candidates = candidates[:10]\n"
if src.count(A1) != 1:
    print(f"ABORT 1: anchor found {src.count(A1)}x (need 1)."); sys.exit(1)
R1 = ("        self.candidates = candidates[:10]\n"
      "        self._prefilter_candidates = list(self.candidates)  # snapshot before FILTERS_V2 (backtest)\n")
src = src.replace(A1, R1, 1)

# ── Patch 2: persist loop -> pre-filter source + kept flag ──
A2 = '''            _brows = []
            for _bi, _bc in enumerate(self.candidates):
                _brows.append({
                    "rank": _bi + 1,
                    "sid": str(_bc.get("security_id", "")),
                    "symbol": _bc.get("ticker"),
                    "gap": _bc.get("gap_pct"),
                    "rs": _bc.get("rs"),
                    "clv": _bc.get("clv"),
                    "score": _bc.get("rank_score"),
                    "tier": _bc.get("tier"),
                    "ltp": _bc.get("ltp"),
                    "prev_close": _bc.get("prev_close"),
                })'''
R2 = '''            _brows = []
            _bkept_sids = {str(c.get("security_id", "")) for c in self.candidates}
            _bsource = getattr(self, "_prefilter_candidates", None) or self.candidates
            for _bi, _bc in enumerate(_bsource):
                _bsid = str(_bc.get("security_id", ""))
                _brows.append({
                    "rank": _bi + 1,
                    "sid": _bsid,
                    "symbol": _bc.get("ticker"),
                    "gap": _bc.get("gap_pct"),
                    "rs": _bc.get("rs"),
                    "clv": _bc.get("clv"),
                    "score": _bc.get("rank_score"),
                    "tier": _bc.get("tier"),
                    "ltp": _bc.get("ltp"),
                    "prev_close": _bc.get("prev_close"),
                    "kept": _bsid in _bkept_sids,
                })'''
if src.count(A2) != 1:
    print(f"ABORT 2: persist-loop anchor found {src.count(A2)}x (need 1)."); sys.exit(1)
src = src.replace(A2, R2, 1)

bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try:
    ast.parse(src)
except SyntaxError as e:
    print(f"SYNTAX ERROR — not writing. {e}"); sys.exit(1)
open(F, "w").write(src); ast.parse(open(F).read())
print(f"OK patched pre-filter capture + kept flag. backup={bak}")
