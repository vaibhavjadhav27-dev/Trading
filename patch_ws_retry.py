import ast, shutil, datetime, sys
F = "trading_bot.py"
src = open(F).read()
bak = f"{F}.bak_ws_retry_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak); print(f"[backup] {bak}")

# Anchor: the WS thread join + result assign + log line (unique to this scan block)
old = '''            _t = threading.Thread(target=_ws_fetch, daemon=True)
            _t.start()
            _t.join(timeout=30)
            ltp_map = _ltp_result if isinstance(_ltp_result, dict) else {}
            log.info(f"WebSocket LTP: {len(ltp_map)}/{len(stock_pairs)} stocks (0 API calls)")'''

new = '''            # WS RETRY: cold-start returns 0/552, recovers ~60s later (confirmed 2026-07-22).
            # Retry 3x before the lossy bounded-120 REST fallback ever fires.
            ltp_map = {}
            for _ws_try in range(3):
                _ltp_result = {}
                _t = threading.Thread(target=_ws_fetch, daemon=True)
                _t.start()
                _t.join(timeout=30)
                ltp_map = _ltp_result if isinstance(_ltp_result, dict) else {}
                log.info(f"WebSocket LTP attempt {_ws_try+1}/3: {len(ltp_map)}/{len(stock_pairs)} stocks (0 API calls)")
                if len(ltp_map) >= 0.5 * len(stock_pairs):  # >=50% coverage = good enough
                    break
                if _ws_try < 2:
                    log.warning(f"WS thin ({len(ltp_map)}/{len(stock_pairs)}) - retry in 20s")
                    time.sleep(20)'''

n = src.count(old)
if n != 1:
    print(f"[ABORT] anchor matched {n}x (need 1) — no changes written"); sys.exit(1)
src = src.replace(old, new); print("[ok] WS retry loop")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] does NOT parse: {e} — original untouched (restore {bak})"); sys.exit(1)
open(F,"w").write(src)
print("[DONE] WS retry applied + AST-verified. Backup:", bak)
