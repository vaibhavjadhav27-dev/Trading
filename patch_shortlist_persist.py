import ast, shutil, datetime, sys
F = "trading_bot.py"
src = open(F).read()
ANCHOR = "        self.email.notify_shortlist(self.candidates, self.funnel_counts)\n        return self.candidates"
if src.count(ANCHOR) != 1:
    print(f"ABORT: anchor found {src.count(ANCHOR)}x (need exactly 1) — file changed."); sys.exit(1)

INSERT = '''        self.email.notify_shortlist(self.candidates, self.funnel_counts)
        # \u2550\u2550\u2550 BACKTEST persist: dump shortlist for 15:45 candle saver (added 2026-07-16) \u2550\u2550\u2550
        # Non-fatal, atomic. Normalizes keys to what save_candle_data.py reads.
        try:
            import json as _bj, os as _bo, datetime as _bdt
            _bdir = _bo.path.join(_bo.path.dirname(_bo.path.abspath(__file__)), "candle_archive")
            _bo.makedirs(_bdir, exist_ok=True)
            _bday = _bdt.date.today().isoformat()
            _brows = []
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
                })
            _btmp = _bo.path.join(_bdir, f"pending_{_bday}.json.tmp")
            _bfin = _bo.path.join(_bdir, f"pending_{_bday}.json")
            with open(_btmp, "w") as _bf:
                _bj.dump({"date": _bday, "candidates": _brows}, _bf)
            _bo.replace(_btmp, _bfin)
            log.info(f"BACKTEST persist: wrote {len(_brows)} candidates -> {_bfin}")
        except Exception as _bpe:
            log.warning(f"BACKTEST persist failed (non-fatal): {_bpe}")
        return self.candidates'''

new = src.replace(ANCHOR, INSERT, 1)
bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try:
    ast.parse(new)
except SyntaxError as e:
    print(f"SYNTAX ERROR — not writing. {e}"); sys.exit(1)
open(F, "w").write(new)
ast.parse(open(F).read())
print(f"OK patched. backup={bak}")
