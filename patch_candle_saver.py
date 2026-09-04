import ast, shutil, datetime, sys
F = "save_candle_data.py"
src = open(F).read()
ANCHOR = "stocks_to_save = []"
if ANCHOR not in src:
    print("ABORT: anchor not found — file changed since review."); sys.exit(1)
head = src.split(ANCHOR)[0]   # keep imports, token, headers, fetch_intraday, CANDLE_DIR, today

NEW_BODY = r'''# ═══ BACKTEST-GRADE UNIVERSE (patched 2026-07-16) ═══
# Captures traded stock(s) + top-5/all shortlist (if scan persisted it)
# + NSE gainers + context universe. All with 5-min OHLCV + TIMESTAMPS.
def _candles(sid, exch="NSE_EQ"):
    d = fetch_intraday(sid, exch)
    if d and "open" in d:
        return {"o": d["open"], "h": d["high"], "l": d["low"],
                "c": d["close"], "v": d.get("volume", []), "t": d.get("timestamp", [])}
    return None

def _grab(sid, exch="NSE_EQ"):
    time.sleep(0.5)
    return _candles(sid, exch)

archive = {"date": today, "traded": [], "shortlist_top5": [],
           "shortlist_all": [], "gainers": [], "context_universe": []}
seen = set()

# 1) TRADED STOCK(S) — durable journal/{date}.json
jpath = os.path.join("journal", f"{today}.json")
if os.path.exists(jpath):
    try:
        j = json.load(open(jpath))
        trades = j.get("trades") or ([j["trade"]] if "trade" in j else [])
        for t in trades:
            sid = str(t.get("security_id", ""))
            rec = dict(t); rec["candles"] = _grab(sid) if sid else None
            archive["traded"].append(rec)
            if sid: seen.add(sid)
        print(f"  traded: {len(archive['traded'])} from journal")
    except Exception as e:
        print(f"  journal read failed: {e}")
else:
    print(f"  no journal {jpath} (no trade today)")

# 2) SHORTLIST — scan-time file candle_archive/pending_{date}.json (deliverable B)
ppath = os.path.join(CANDLE_DIR, f"pending_{today}.json")
if os.path.exists(ppath):
    try:
        cands = json.load(open(ppath)).get("candidates", [])
        cands = sorted(cands, key=lambda c: c.get("rank", 999))
        for idx, c in enumerate(cands):
            sid = str(c.get("sid") or c.get("security_id", ""))
            row = {"sid": sid, "symbol": c.get("symbol"), "rank": c.get("rank", idx+1),
                   "gap": c.get("gap"), "rs": c.get("rs"), "clv": c.get("clv"),
                   "score": c.get("score"), "tier": c.get("tier")}
            archive["shortlist_all"].append(row)
            if idx < 5:
                trow = dict(row)
                trow["candles"] = ("see_traded" if sid in seen else (_grab(sid) if sid else None))
                archive["shortlist_top5"].append(trow)
                if sid: seen.add(sid)
        print(f"  shortlist: all={len(archive['shortlist_all'])} top5 w/candles")
    except Exception as e:
        print(f"  pending read failed: {e}")
else:
    print(f"  no {ppath} — scan-time write (deliverable B) not deployed yet")

# 3) NSE GAINERS — best-effort from /tmp/today_analysis.json (transient; non-fatal)
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
        print(f"  gainers read failed (non-fatal): {e}")

# 4) CONTEXT UNIVERSE — top-30 prev_close baseline (continuity with old archive)
if os.path.exists("prev_close_cache.json"):
    data = json.load(open("prev_close_cache.json")).get("data", {})
    for i, (sid, pc) in enumerate(list(data.items())[:30]):
        sid = str(sid)
        if sid in seen: continue
        c = _grab(sid)
        if c: archive["context_universe"].append({"sid": sid, "pc": pc, "candles": c})
        if (i+1) % 10 == 0: print(f"  context {i+1}/30...")

out_file = os.path.join(CANDLE_DIR, f"{today}.json.gz")
with gzip.open(out_file, "wt") as f:
    json.dump(archive, f)
print(f"Saved: {out_file} ({os.path.getsize(out_file)/1024:.1f} KB) | "
      f"traded={len(archive['traded'])} top5={len(archive['shortlist_top5'])} "
      f"all={len(archive['shortlist_all'])} gainers={len(archive['gainers'])} "
      f"context={len(archive['context_universe'])}")
'''

new = head + NEW_BODY
bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try:
    ast.parse(new)
except SyntaxError as e:
    print(f"SYNTAX ERROR — not writing. {e}"); sys.exit(1)
open(F, "w").write(new)
ast.parse(open(F).read())  # re-validate on disk
print(f"OK patched. backup={bak}")
