import ast, shutil, datetime, sys
F = "swing_monitor.py"
src = open(F, encoding="utf-8").read()
bak = f"{F}.bak_daysheld_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak); print(f"[backup] {bak}")

old = '''    # Increment days_held for all active positions
    for i, p in enumerate(positions["active"]):
        if p.get("entry_date") != str(date.today()):
            positions["active"][i]["days_held"] = positions["active"][i].get("days_held", 0) + 1
    save_positions(positions)'''

new = '''    # Derive days_held from entry_date (idempotent) — weekday count in [entry, today).
    # Replaces the old per-run increment, which double-counted on monitor restarts / manual
    # dry-runs. Weekday count faithfully reproduces the old once-per-trading-day semantics
    # (cron ran Mon-Fri), so weekends are skipped and behavior is unchanged.
    _today = date.today()
    for i, p in enumerate(positions["active"]):
        _ed = p.get("entry_date")
        if not _ed:
            continue
        try:
            _d0 = date.fromisoformat(str(_ed))
            _n = (_today - _d0).days
            if _n <= 0:
                _wd = 0
            else:
                _fw, _rem = divmod(_n, 7)
                _wd = _fw * 5
                _st = _d0.weekday()
                for _k in range(_rem):
                    if (_st + _k) % 7 < 5:
                        _wd += 1
            positions["active"][i]["days_held"] = _wd
        except Exception as _e:
            log.warning("days_held derive failed for %s (%s) - keeping stored value" % (p.get("ticker"), _e))
    save_positions(positions)'''

n = src.count(old)
if n != 1:
    print(f"[ABORT] anchor matched {n}x (need 1) - no changes written")
    if "Derive days_held from entry_date" in src:
        print("  -> looks ALREADY patched. No action needed.")
    else:
        print("  -> anchor text differs. Paste lines 200-210 of swing_monitor.py to re-anchor.")
    sys.exit(1)
src = src.replace(old, new); print("[ok] days_held now derived from entry_date (idempotent)")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] does NOT parse: {e} - original untouched (restore {bak})"); sys.exit(1)
open(F, "w", encoding="utf-8").write(src)
print("[DONE] days_held derive-fix applied + AST-verified. Backup:", bak)
