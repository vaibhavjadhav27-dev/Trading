import ast, shutil, datetime, sys
F = "swing_daily.py"
src = open(F, encoding="utf-8").read()
bak = f"{F}.bak_loadfix_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak); print(f"[backup] {bak}")

old = '''    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    return {"active": [], "closed": []}'''

new = '''    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                _raw = f.read().strip()
            if _raw:
                _d = json.loads(_raw)
                if isinstance(_d, dict):
                    _d.setdefault("active", [])
                    _d.setdefault("closed", [])
                    return _d
            else:
                log.warning("swing_positions.json is EMPTY (0 bytes) - using empty default")
        except (json.JSONDecodeError, ValueError) as _je:
            log.warning(f"swing_positions.json corrupt ({_je}) - using empty default")
    return {"active": [], "closed": []}'''

n = src.count(old)
if n != 1:
    print(f"[ABORT] anchor matched {n}x (need 1) - checking if already patched...")
    if "setdefault" in src and "0 bytes" in src:
        print("  → looks ALREADY patched. No action needed."); sys.exit(0)
    print("  → anchor text not found as-is. Paste lines 22-27 of swing_daily.py so I can re-anchor."); sys.exit(1)
src = src.replace(old, new); print("[ok] load_positions empty-safe")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] does NOT parse: {e} - original untouched (restore {bak})"); sys.exit(1)
open(F, "w", encoding="utf-8").write(src)
print("[DONE] swing load fix applied + AST-verified. Backup:", bak)
