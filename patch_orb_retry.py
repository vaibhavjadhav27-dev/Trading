import ast, shutil, datetime, sys

F = "trading_bot.py"
src = open(F).read()
bak = f"{F}.bak_orb_retry_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak)
print(f"[backup] {bak}")

edits = []

# ── A. init reason tracker at top of record_orb ──
a_old = '''    def record_orb(self):
        log.info(f"Recording ORB for {len(self.candidates)} stocks...")'''
a_new = '''    def record_orb(self):
        log.info(f"Recording ORB for {len(self.candidates)} stocks...")
        self._orb_reject_reason = {}  # ORB DIAG: sid -> NO_DATA | RANGE_OUTSIDE'''
edits.append(("A init reason dict", a_old, a_new))

# ── B. retry the fetch (was single-shot) ──
b_old = '''            time.sleep(1.5)  # Rate limit: max 8/sec shared budget
            data = self.dhan.get_ohlc_intraday(str(candidate['security_id']), interval="15")
            if data:'''
b_new = '''            time.sleep(1.5)  # Rate limit: max 8/sec shared budget
            data = None
            for _attempt in range(3):  # ORB RETRY: transient empty is NOT a dead range
                data = self.dhan.get_ohlc_intraday(str(candidate['security_id']), interval="15")
                if data:
                    break
                time.sleep(4)  # backoff before retry
            if not data:
                self._orb_reject_reason[candidate['security_id']] = 'NO_DATA'
            if data:'''
edits.append(("B retry loop", b_old, b_new))

# ── C. tag RANGE_OUTSIDE so it is not confused with NO_DATA ──
c_old = '''                            orb['range'] = orb_range
                            orb['range_pct'] = range_pct
                            self.orb_data[sid] = orb'''
c_new = '''                            orb['range'] = orb_range
                            orb['range_pct'] = range_pct
                            self.orb_data[sid] = orb
                        else:
                            self._orb_reject_reason[sid] = f'RANGE_OUTSIDE ({range_pct:.2f}%)\''''
edits.append(("C tag range-outside", c_old, c_new))

# ── D. bump as_completed timeout (anchored to unique futures line above it) ──
d_old = '''            futures = {executor.submit(fetch_orb, c): c for c in self.candidates[:8]}
            for future in as_completed(futures, timeout=45):'''
d_new = '''            futures = {executor.submit(fetch_orb, c): c for c in self.candidates[:8]}
            for future in as_completed(futures, timeout=75):  # ORB RETRY headroom'''
edits.append(("D timeout 45->75", d_old, d_new))

# ── E. honest rejection logging (NO_DATA vs RANGE_INVALID) ──
e_old = '''                    'failed_filter': 'ORB_RANGE_INVALID',
                    'detail': f'Range outside {config.ORB_MIN_RANGE_PCT}-{config.ORB_MAX_RANGE_PCT}%',
                    'source': 'Watchlist'
                })'''
e_new = '''                    'failed_filter': ('ORB_NO_DATA' if self._orb_reject_reason.get(c['security_id']) == 'NO_DATA' else 'ORB_RANGE_INVALID'),
                    'detail': self._orb_reject_reason.get(c['security_id'], 'No candle data after 3 retries'),
                    'source': 'Watchlist'
                })'''
edits.append(("E honest logging", e_old, e_new))

for name, old, new in edits:
    n = src.count(old)
    if n != 1:
        print(f"[ABORT] anchor '{name}' matched {n}x (need exactly 1) — no changes written")
        sys.exit(1)
    src = src.replace(old, new)
    print(f"[ok] {name}")

# ── F. breakdown summary after 'ORB recorded:' log ──
f_old = '''        log.info(f"ORB recorded: {len(self.orb_data)} valid ranges")'''
f_new = '''        log.info(f"ORB recorded: {len(self.orb_data)} valid ranges")
        _nd = sum(1 for v in self._orb_reject_reason.values() if v == 'NO_DATA')
        _ro = sum(1 for v in self._orb_reject_reason.values() if str(v).startswith('RANGE_OUTSIDE'))
        log.info(f"ORB breakdown: {len(self.orb_data)} valid | {_nd} NO_DATA (fetch bug) | {_ro} range-outside (correct)")'''
if src.count(f_old) == 1:
    src = src.replace(f_old, f_new); print("[ok] F breakdown log")
else:
    print("[warn] F anchor not unique — skipped (non-critical)")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] result does NOT parse: {e} — original untouched (restore {bak})")
    sys.exit(1)

open(F, "w").write(src)
print("[DONE] patch applied + AST-verified. Backup:", bak)
