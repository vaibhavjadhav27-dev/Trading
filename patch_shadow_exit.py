import ast, shutil, datetime, sys
F = "trading_bot.py"
src = open(F, encoding="utf-8").read()
bak = f"{F}.bak_shadow_exit_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak); print(f"[backup] {bak}")

edits = []

# ── 1. NIFTY refresh in-loop (prerequisite for RS) ──
one_old = '''        # Get current LTP
            # Use WebSocket for real-time price if available
        ltp_data = self.fetch_ltp_concurrent([sid])
        current_ltp = ltp_data.get(str(sid), ltp_data.get(sid, 0))'''
one_new = '''        # Get current LTP
            # Use WebSocket for real-time price if available
        # RS SHADOW: fetch NIFTY (sid 13) in the SAME call — no extra round-trip
        ltp_data = self.fetch_ltp_concurrent([sid, '13'])
        current_ltp = ltp_data.get(str(sid), ltp_data.get(sid, 0))
        _nifty_ltp_live = ltp_data.get('13', 0) or self.nifty_data.get('ltp', 0)'''
edits.append(("1 NIFTY refresh", one_old, one_new))

# ── 2+3. Shadow weighted score, reusing SMART_EXIT_v2 signals + RS/fails/HL ──
two_old = '''                    bearish_count = sum([momentum_decay, volume_exhaust, reversal_candle, below_vwap])'''
two_new = '''                    bearish_count = sum([momentum_decay, volume_exhaust, reversal_candle, below_vwap])

                    # \u2550\u2550\u2550 SHADOW EXIT SCORE v1 \u2014 computes + logs, ACTS ON NOTHING \u2550\u2550\u2550
                    try:
                        if not hasattr(self, '_shadow_state'):
                            self._shadow_state = {}
                        _ss = self._shadow_state.setdefault(str(sid), {})

                        # RS (25): stock day-return minus NIFTY day-return (live NIFTY from tick fetch)
                        _stk_ref = self._prev_closes.get(str(sid), 0) or entry_price
                        _nif_ref = self.nifty_data.get('prev_close', 0) or 0
                        _stk_ret = (current_ltp - _stk_ref) / _stk_ref * 100 if _stk_ref > 0 else 0.0
                        _nif_ret = (_nifty_ltp_live - _nif_ref) / _nif_ref * 100 if _nif_ref > 0 else 0.0
                        _rs = _stk_ret - _nif_ret
                        _rs_peak = max(_ss.get('rs_peak', _rs), _rs)
                        _ss['rs_peak'] = _rs_peak
                        _rs_drop = ((_rs_peak - _rs) / _rs_peak * 100) if _rs_peak > 0 else 0.0
                        _sig_rs = (_rs_peak > 0 and _rs_drop >= 40)

                        # Failed new highs (20) + HL failure \u2014 update ONLY on a new 5-min candle
                        _ncand = len(closes)
                        _new_candle = _ncand > _ss.get('ncand', 0)
                        _ss['ncand'] = _ncand
                        _hi_max = _ss.get('hi_max', h3)
                        _fails = _ss.get('hi_fails', 0)
                        _prev_low = _ss.get('prev_low', l3)
                        _sig_ll = False
                        if _new_candle:
                            if h3 > _hi_max + 1e-9:
                                _hi_max = h3; _fails = 0
                            else:
                                _fails += 1
                            _sig_ll = (l3 < _prev_low - 1e-9)
                            _prev_low = l3
                        _ss['hi_max'] = _hi_max; _ss['hi_fails'] = _fails; _ss['prev_low'] = _prev_low
                        _sig_fails = (_fails >= 3)

                        # Reuse SMART_EXIT_v2 signals + VWAP extension magnitude
                        _sig_wick = bool(reversal_candle)   # 15
                        _sig_vol  = bool(volume_exhaust)    # 15
                        _sig_mom  = bool(momentum_decay)    # 10
                        _vwap = None
                        if len(volumes) >= 5 and sum(volumes[-5:]) > 0:
                            _vwap = sum(cc*vv for cc, vv in zip(closes[-5:], volumes[-5:])) / sum(volumes[-5:])
                        _vwap_ext = ((current_ltp - _vwap) / _vwap * 100) if _vwap else 0.0
                        _sig_vwap = (_vwap_ext >= 6.0)      # 10
                        _sig_mkt = (_nif_ret < 0)           # 5: market weak

                        _score = (25*_sig_rs + 20*_sig_fails + 15*_sig_wick +
                                  15*_sig_vol + 10*_sig_mom + 10*_sig_vwap + 5*_sig_mkt)
                        # AND-gate = FUTURE live danger-exit (confluence, no weight calibration)
                        _and_gate = (_sig_rs and _sig_fails and _sig_wick)

                        _p = []
                        if _sig_rs:    _p.append(f"RS\u2193{_rs_drop:.0f}%(25)")
                        if _sig_fails: _p.append(f"fails{_fails}(20)")
                        if _sig_wick:  _p.append("wick(15)")
                        if _sig_vol:   _p.append("vol(15)")
                        if _sig_mom:   _p.append("mom(10)")
                        if _sig_vwap:  _p.append(f"vwapext{_vwap_ext:.1f}%(10)")
                        if _sig_mkt:   _p.append("mktweak(5)")
                        log.info(
                            f"SHADOW_EXIT {symbol} score={_score} r={r_multiple:.2f} "
                            f"RS={_rs:+.2f}(pk{_rs_peak:+.2f}) HLfail={_sig_ll} | "
                            f"{' '.join(_p) or 'none'} | AND-gate={'TRUE' if _and_gate else 'false'}")
                    except Exception as _se:
                        log.debug(f"SHADOW_EXIT skipped: {_se}")'''
edits.append(("2+3 shadow score", two_old, two_new))

for name, old, new in edits:
    n = src.count(old)
    if n != 1:
        print(f"[ABORT] anchor '{name}' matched {n}x (need 1) \u2014 no changes written")
        sys.exit(1)
    src = src.replace(old, new); print(f"[ok] {name}")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] does NOT parse: {e} \u2014 original untouched (restore {bak})")
    sys.exit(1)
open(F, "w", encoding="utf-8").write(src)
print("[DONE] shadow exit applied + AST-verified. Backup:", bak)
