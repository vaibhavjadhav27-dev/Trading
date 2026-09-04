import ast, shutil, datetime, sys
F = "swing_monitor.py"
src = open(F, encoding="utf-8").read()
bak = f"{F}.bak_harden_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak); print(f"[backup] {bak}")

edits = []

# ── load-guard (crash-safe on empty/corrupt) ──
l_old = '''def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    return {"active": [], "closed": []}'''
l_new = '''def load_positions():
    if os.path.exists(POSITIONS_FILE):
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
                log.warning("swing_positions.json EMPTY (0 bytes) - using empty default")
        except (json.JSONDecodeError, ValueError) as _je:
            log.warning(f"swing_positions.json corrupt ({_je}) - using empty default")
    return {"active": [], "closed": []}'''
edits.append(("monitor load-guard", l_old, l_new))

# ── atomic write (.tmp + os.replace) ──
s_old = '''def save_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)'''
s_new = '''def save_positions(positions):
    # atomic: write to .tmp then os.replace so an interrupted write can never truncate the live file
    _tmp = POSITIONS_FILE + ".tmp"
    with open(_tmp, "w") as f:
        json.dump(positions, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(_tmp, POSITIONS_FILE)'''
edits.append(("monitor atomic write", s_old, s_new))

for name, old, new in edits:
    n = src.count(old)
    if n != 1:
        print(f"[ABORT] anchor '{name}' matched {n}x (need 1) - no changes written"); sys.exit(1)
    src = src.replace(old, new); print(f"[ok] {name}")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] does NOT parse: {e} - original untouched (restore {bak})"); sys.exit(1)
open(F, "w", encoding="utf-8").write(src)
print("[DONE] swing_monitor hardened + AST-verified. Backup:", bak)
