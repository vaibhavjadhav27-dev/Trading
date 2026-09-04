#!/usr/bin/env python3
"""
patch_regime_directional.py — make the regime detector DIRECTIONAL.

Applied on the server:  cd ~/trading-bot && venv/bin/python3 patch_regime_directional.py

Confirmed settings (user, 2026-07-24):
  - Strict mirror: TRENDING_DOWN = gap < -0.3 AND slope < 0 AND not above50
  - CONSERVATIVE wins on below-VWAP days (regime block only runs ABOVE VWAP,
    so TRENDING_DOWN fires only in the narrow band: above VWAP but below EMA50).
  - Split bare "TRENDING" -> "TRENDING_UP" (up-day) and add "TRENDING_DOWN".
  - Writes to self.regime (the regime STRING), NOT self.market_regime (the mode) —
    the attribute split that caused the old regime=='BEARISH' guard to never match.

Idempotent: detects already-patched state and aborts cleanly.
AST-verified: reverts automatically on any SyntaxError.
"""
import ast, shutil, datetime, sys, os

FILE = "trading_bot.py"

# --- exact current block (from sed 585-605) ---
OLD = '''            if gap_pct < -0.2 or slope < -0.1:
                self.regime = "CHOPPY"
            elif gap_pct > 0.3 and slope > 0 and above50:
                self.regime = "TRENDING"
            else:
                self.regime = "NORMAL"'''

# --- directional replacement (priority order matters) ---
# 1. strong UP  -> TRENDING_UP   (was bare "TRENDING")
# 2. strong DOWN-> TRENDING_DOWN (strict mirror)  <-- NEW, checked before CHOPPY
# 3. weak neg   -> CHOPPY        (unchanged intent)
# 4. else       -> NORMAL
NEW = '''            if gap_pct > 0.3 and slope > 0 and above50:
                self.regime = "TRENDING_UP"
            elif gap_pct < -0.3 and slope < 0 and not above50:
                self.regime = "TRENDING_DOWN"   # strict mirror; short-bias window (above VWAP, below EMA50)
            elif gap_pct < -0.2 or slope < -0.1:
                self.regime = "CHOPPY"
            else:
                self.regime = "NORMAL"'''


def main():
    if not os.path.exists(FILE):
        print(f"[ABORT] {FILE} not found — run from ~/trading-bot"); sys.exit(1)
    src = open(FILE).read()

    if 'self.regime = "TRENDING_DOWN"' in src:
        print("[SKIP] already patched (TRENDING_DOWN present). No change."); return
    if OLD not in src:
        print("[ABORT] expected regime block not found verbatim.")
        print("        The block may have changed — inspect lines 595-600 and re-align OLD.")
        sys.exit(2)

    bak = FILE + ".bak_" + datetime.datetime.now().strftime("%H%M%S")
    shutil.copy(FILE, bak)
    patched = src.replace(OLD, NEW, 1)

    try:
        ast.parse(patched)
    except SyntaxError as e:
        print(f"[ABORT] patched source has SyntaxError: {e} — NOT written. Backup {bak} intact.")
        sys.exit(3)

    open(FILE, "w").write(patched)
    # verify round-trip
    ast.parse(open(FILE).read())
    print(f"[OK] regime detector is now directional.")
    print(f"     TRENDING -> TRENDING_UP ; added TRENDING_DOWN (strict mirror).")
    print(f"     Backup: {bak}")
    print(f"     Verify:  grep -n 'self.regime =' {FILE}")


if __name__ == "__main__":
    main()
