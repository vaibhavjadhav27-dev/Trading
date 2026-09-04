import ast, shutil, datetime, sys

# ── Patch A: add sid to the Dhan gainer dict (sid already in scope) ──
FA = "post_market_analysis.py"
a = open(FA).read()
OLD_A = "gainers.append({'ticker': tk,"
NEW_A = "gainers.append({'sid': sid, 'ticker': tk,"
if OLD_A not in a:
    print(f"ABORT A: anchor not found in {FA}"); sys.exit(1)
if NEW_A in a:
    print("A already patched")
else:
    a2 = a.replace(OLD_A, NEW_A, 1)
    ast.parse(a2)
    shutil.copy(FA, FA + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    open(FA, "w").write(a2); print("OK patched A (sid added to gainer dict)")

# ── Patch B: repoint saver gainers block to the durable function ──
FB = "save_candle_data.py"
b = open(FB).read()
OLD_B = '''# 3) NSE GAINERS — best-effort from /tmp/today_analysis.json (transient; non-fatal)
gpath = "/tmp/today_analysis.json"
if os.path.exists(gpath):
    try:
        an = json.load(open(gpath))
        for g in (an.get("top_gainers") or [])[:10]:
            sid = str(g.get("sid") or g.get("security_id", ""))
            if not sid or sid in seen: continue
            archive["gainers"].append({"sid": sid, "symbol": g.get("symbol"),
                "change": g.get("change") or g.get("pct"), "candles": _grab(sid)})
            seen.add(sid)
        print(f"  gainers: {len(archive['gainers'])}")
    except Exception as e:
        print(f"  gainers read failed (non-fatal): {e}")'''
NEW_B = '''# 3) NSE GAINERS — durable Dhan-fallback function, called directly (patched 2026-07-16)
try:
    from post_market_analysis import get_nse_top_gainers, get_top_gainers_from_dhan
    _gainers = get_nse_top_gainers() or get_top_gainers_from_dhan()  # empty NSE -> Dhan
    for g in (_gainers or [])[:10]:
        sid = str(g.get("sid") or g.get("security_id", ""))
        if not sid or sid in seen: continue
        archive["gainers"].append({"sid": sid,
            "symbol": g.get("ticker") or g.get("symbol"),
            "change": g.get("gain_pct") if g.get("gain_pct") is not None else g.get("change"),
            "ltp": g.get("ltp"), "prev_close": g.get("prev_close"),
            "candles": _grab(sid)})
        seen.add(sid)
    print(f"  gainers: {len(archive['gainers'])}")
except Exception as e:
    print(f"  gainers fetch failed (non-fatal): {e}")'''
if OLD_B not in b:
    print(f"ABORT B: gainers block not found verbatim in {FB} (did it change?)"); sys.exit(1)
if NEW_B.split(chr(10))[0] in b:
    print("B already patched")
else:
    b2 = b.replace(OLD_B, NEW_B, 1)
    ast.parse(b2)
    shutil.copy(FB, FB + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    open(FB, "w").write(b2); print("OK patched B (saver calls durable gainers fn)")

ast.parse(open(FA).read()); ast.parse(open(FB).read())
print("BOTH FILES SYNTAX OK")
