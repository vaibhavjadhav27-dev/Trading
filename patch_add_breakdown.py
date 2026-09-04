import ast, shutil, datetime, sys, os
FILE = "trading_bot.py"
ANCHOR = '''            return True, min(strength, 1.0)
        return False, 0

    def check_rs_persistence(self, candidate, current_ltp):'''
NEW = '''            return True, min(strength, 1.0)
        return False, 0

    def check_breakdown(self, candidate, ltp):
        """DOWNSIDE mirror of check_breakout. Returns (is_breakdown, strength)."""
        sid = candidate['security_id']
        orb = self.orb_data.get(sid)
        if not orb:
            return False, 0
        buffer = orb['low'] * (config.ORB_BUFFER_PCT / 100)
        if ltp and orb.get("low") and ltp < orb['low'] - buffer:
            strength = (orb['low'] - ltp) / orb['range'] if orb['range'] > 0 else 0
            lower_wick = min(orb['open'], orb['close']) - orb['low']
            wick_max = getattr(config, "CANDLE_LOWER_WICK_MAX",
                               getattr(config, "CANDLE_UPPER_WICK_MAX", 0.5))
            if orb['range'] > 0 and lower_wick / orb['range'] > wick_max:
                return False, 0
            return True, min(strength, 1.0)
        return False, 0

    def check_rs_persistence(self, candidate, current_ltp):'''
src = open(FILE).read()
if "def check_breakdown(self, candidate, ltp):" in src:
    print("[SKIP] already patched."); sys.exit(0)
if ANCHOR not in src:
    print("[ABORT] anchor not found — inspect ~1140-1144."); sys.exit(2)
bak = FILE + ".bak_" + datetime.datetime.now().strftime("%H%M%S")
shutil.copy(FILE, bak)
patched = src.replace(ANCHOR, NEW, 1)
try:
    ast.parse(patched)
except SyntaxError as e:
    print(f"[ABORT] SyntaxError: {e} — not written. Backup {bak} intact."); sys.exit(3)
open(FILE, "w").write(patched)
ast.parse(open(FILE).read())
print(f"[OK] check_breakdown() added. Backup: {bak}")
