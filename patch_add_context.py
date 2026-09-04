import ast, shutil, datetime, sys
F = "trading_bot.py"
src = open(F).read()
ANCHOR = '''            with open(_btmp, "w") as _bf:
                _bj.dump({"date": _bday, "candidates": _brows}, _bf)'''
if src.count(ANCHOR) != 1:
    print(f"ABORT: anchor found {src.count(ANCHOR)}x (need 1)."); sys.exit(1)
REPLACE = '''            _bctx = {
                "market_regime": getattr(self, "market_regime", None),
                "regime": getattr(self, "regime", None),
                "nifty_ltp": (self.nifty_data or {}).get("ltp"),
                "nifty_vwap": (self.nifty_data or {}).get("vwap"),
                "nifty_prev_close": (self.nifty_data or {}).get("prev_close"),
                "nifty_below_vwap": (
                    (self.nifty_data or {}).get("ltp") is not None
                    and (self.nifty_data or {}).get("vwap") is not None
                    and (self.nifty_data or {}).get("ltp") < (self.nifty_data or {}).get("vwap")
                ),
            }
            with open(_btmp, "w") as _bf:
                _bj.dump({"date": _bday, "candidates": _brows, "context": _bctx}, _bf)'''
new = src.replace(ANCHOR, REPLACE, 1)
bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try: ast.parse(new)
except SyntaxError as e: print(f"SYNTAX ERROR — not writing. {e}"); sys.exit(1)
open(F, "w").write(new); ast.parse(open(F).read())
print(f"OK patched context. backup={bak}")
