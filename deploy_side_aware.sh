#!/bin/bash
# deploy_side_aware.sh
# Paste this entire file into the server terminal.
# Creates patch_wire_pick_side.py then runs it.
# cd ~/trading-bot first.

cat > patch_wire_pick_side.py << 'PYEOF'
import ast, shutil, datetime, sys, os
FILE = "trading_bot.py"

if not os.path.exists(FILE):
    print("[ABORT] trading_bot.py not found. Run from ~/trading-bot"); sys.exit(1)

src = open(FILE).read()
bak = FILE + ".bak_" + datetime.datetime.now().strftime("%H%M%S")
shutil.copy(FILE, bak)

patched = src
applied = 0
skipped = 0
missed  = []

def ap(old, new, tag):
    global patched, applied, skipped
    if old == new:
        skipped += 1; return
    if new in patched:
        print(f"  [SKIP] {tag}"); skipped += 1; return
    if old not in patched:
        missed.append(tag); print(f"  [MISS] {tag}"); return
    patched = patched.replace(old, new, 1)
    applied += 1; print(f"  [OK]   {tag}")

# ── 1. place_entry signature ───────────────────────────────────────────────
ap(
    '    def place_entry(self, candidate, ltp, score):',
    '    def place_entry(self, candidate, ltp, score, side="LONG"):',
    'place_entry sig'
)

# ── 2. place_entry: side-aware SL + order side ────────────────────────────
ap(
    '        sl_price = round(ltp - sl_distance, 2)\n'
    '        log.info(f"PLACING ORDER: {candidate[\'ticker\']} qty={qty} @ \u20b9{ltp:.2f} SL=\u20b9{sl_price:.2f}")\n'
    '        start_time = time.time()\n'
    '        order_resp = self.dhan.place_order(candidate[\'security_id\'], qty, ltp, "BUY", "LIMIT")',
    '        side = candidate.get(\'_side\', side)  # prefer scan-tagged side\n'
    '        _is_short = (side == "SHORT")\n'
    '        sl_price = round(ltp + sl_distance, 2) if _is_short else round(ltp - sl_distance, 2)\n'
    '        _txn = "SELL" if _is_short else "BUY"\n'
    '        log.info(f"PLACING ORDER [{side}]: {candidate[\'ticker\']} qty={qty} @ \u20b9{ltp:.2f} SL=\u20b9{sl_price:.2f}")\n'
    '        start_time = time.time()\n'
    '        order_resp = self.dhan.place_order(candidate[\'security_id\'], qty, ltp, _txn, "LIMIT")',
    'place_entry order+SL'
)

# ── 3. place_entry: actual_sl inversion + hard SL order for SHORT ─────────
ap(
    '        actual_sl = round(filled_price - sl_distance, 2)',
    '        actual_sl = round(filled_price + sl_distance, 2) if _is_short else round(filled_price - sl_distance, 2)\n'
    '        if _is_short:\n'
    '            try:\n'
    '                _hsl = {"dhanClientId": self.dhan.client_id,\n'
    '                        "transactionType": "BUY", "exchangeSegment": "NSE_EQ",\n'
    '                        "productType": "INTRADAY", "orderType": "STOP_LOSS_MARKET",\n'
    '                        "validity": "DAY", "securityId": str(candidate[\'security_id\']),\n'
    '                        "quantity": int(filled_qty), "price": 0, "triggerPrice": actual_sl}\n'
    '                _hr = self.dhan._request("POST", "/orders", _hsl)\n'
    '                log.info(f"Hard SL BUY-cover @ \u20b9{actual_sl:.2f} resp={_hr}")\n'
    '            except Exception as _sle:\n'
    '                log.error(f"Hard SL failed: {_sle} -- bot-loop SL active")',
    'place_entry actual_sl+hardSL'
)

# ── 4. place_entry: thread side into active_trade ─────────────────────────
ap(
    '            trade_journal.log_trade(self.active_trade)',
    '            self.active_trade[\'side\'] = side\n'
    '            trade_journal.log_trade(self.active_trade)',
    'place_entry side->active_trade'
)

# ── 5. exit_trade: side-aware position guard ──────────────────────────────
ap(
    '        # GUARD: verify a live long exists at broker before selling (prevents phantom short)\n'
    '        _live = 0\n'
    '        try:\n'
    '            _pos = self.dhan.get_positions()\n'
    '            if isinstance(_pos, dict): _pos = _pos.get(\'data\', [])\n'
    '            if isinstance(_pos, list):\n'
    '                for _p in _pos:\n'
    '                    if str(_p.get(\'securityId\', \'\')) == str(sid):\n'
    '                        _live = int(_p.get(\'netQty\', 0) or 0)\n'
    '                        break\n'
    '        except Exception as _pe:\n'
    '            log.error(f"Exit position-check failed ({_pe}) - proceeding with recorded qty")\n'
    '            _live = qty  # feed error: fall back to recorded qty rather than skip a real exit\n'
    '        if _live <= 0:\n'
    '            log.warning(f"EXIT SKIPPED: {symbol} not live at broker (netQty={_live}) - clearing stale record")\n'
    '            self.active_trade = None\n'
    '            self.dynamo.clear_active_trade()\n'
    '            return\n'
    '        qty = min(qty, _live)  # never sell more than actually held',
    '        # GUARD: side-aware broker check (prevents phantom orders on both sides)\n'
    '        _side_x = self.active_trade.get(\'side\', \'LONG\')\n'
    '        _is_short_x = (_side_x == "SHORT")\n'
    '        _live = 0\n'
    '        try:\n'
    '            _pos = self.dhan.get_positions()\n'
    '            if isinstance(_pos, dict): _pos = _pos.get(\'data\', [])\n'
    '            if isinstance(_pos, list):\n'
    '                for _p in _pos:\n'
    '                    if str(_p.get(\'securityId\', \'\')) == str(sid):\n'
    '                        _nq   = int(_p.get(\'netQty\', 0) or 0)\n'
    '                        _live = abs(_nq)\n'
    '                        if _is_short_x and _nq >= 0: _live = 0\n'
    '                        elif not _is_short_x and _nq <= 0: _live = 0\n'
    '                        break\n'
    '        except Exception as _pe:\n'
    '            log.error(f"Exit position-check failed ({_pe}) - proceeding with recorded qty")\n'
    '            _live = qty\n'
    '        if _live <= 0:\n'
    '            log.warning(f"EXIT SKIPPED: {symbol} side={_side_x} not live at broker - clearing stale record")\n'
    '            self.active_trade = None\n'
    '            self.dynamo.clear_active_trade()\n'
    '            return\n'
    '        qty = min(qty, _live)',
    'exit_trade guard'
)

# ── 6. exit_trade: BUY-to-cover for SHORT ─────────────────────────────────
ap(
    '        # Place sell order\n'
    '        log.info(f"SELLING: {symbol} qty={qty} @ MARKET (reason: {reason})")\n'
    '        start_time = time.time()\n'
    '        sell_resp = self.dhan.place_order(int(sid), qty, 0, "SELL", "MARKET")',
    '        _close_txn = "BUY" if _is_short_x else "SELL"\n'
    '        log.info(f"{\'COVERING\' if _is_short_x else \'SELLING\'}: {symbol} qty={qty} @ MARKET (reason: {reason})")\n'
    '        start_time = time.time()\n'
    '        sell_resp = self.dhan.place_order(int(sid), qty, 0, _close_txn, "MARKET")',
    'exit_trade close order'
)

# ── 7. exit_trade: inverted PnL ───────────────────────────────────────────
ap(
    '        pnl = (actual_exit - entry_price) * qty\n'
    '        r_mult = (actual_exit - entry_price) / r_value if r_value > 0 else 0',
    '        pnl    = ((entry_price - actual_exit) if _is_short_x else (actual_exit - entry_price)) * qty\n'
    '        r_mult = ((entry_price - actual_exit) if _is_short_x else (actual_exit - entry_price)) / r_value if r_value > 0 else 0',
    'exit_trade PnL inversion'
)

# ── 8. monitor: track extreme price (min for SHORT) ───────────────────────
ap(
    '        # Update max price\n'
    '        max_price = max(max_price, current_ltp)',
    '        # Track extreme price (min for SHORT, max for LONG)\n'
    '        _mon_short = (self.active_trade.get(\'side\', \'LONG\') == \'SHORT\')\n'
    '        max_price  = min(max_price, current_ltp) if _mon_short else max(max_price, current_ltp)',
    'monitor max_price'
)

# ── 9. monitor: inverted pnl_per_share ───────────────────────────────────
ap(
    '        pnl_per_share = current_ltp - entry_price\n'
    '        r_multiple = pnl_per_share / r_value if r_value > 0 else 0',
    '        pnl_per_share = (entry_price - current_ltp) if _mon_short else (current_ltp - entry_price)\n'
    '        r_multiple    = pnl_per_share / r_value if r_value > 0 else 0',
    'monitor pnl_per_share'
)

# ── 10. monitor: inverted R-SKATE peak_r ─────────────────────────────────
ap(
    '            _peak_r = (max_price - entry_price) / r_value if r_value > 0 else 0.0',
    '            _peak_r = ((entry_price - max_price) if _mon_short else (max_price - entry_price)) / r_value if r_value > 0 else 0.0',
    'monitor rskate peak_r'
)

# ── 11. monitor: inverted new_sl after rskate ────────────────────────────
ap(
    "            new_sl = entry_price + (_exit_r * r_value)\n"
    "            phase = 'RSKATE_S1' if _peak_r < 1.5 else ('RSKATE_S2' if _peak_r < 3.0 else 'RSKATE_S3')",
    "            new_sl = (entry_price - _exit_r * r_value) if _mon_short else (entry_price + _exit_r * r_value)\n"
    "            phase = 'RSKATE_S1' if _peak_r < 1.5 else ('RSKATE_S2' if _peak_r < 3.0 else 'RSKATE_S3')",
    'monitor rskate new_sl'
)

# ── 12. monitor: inverted PHASE1/2 + trail update + exit trigger ─────────
ap(
    "        elif r_multiple >= config.TRAIL_PHASE2_TRIGGER:\n"
    "            phase = 'PHASE2'\n"
    "            new_sl = entry_price + (r_value * config.TRAIL_PHASE2_LEVEL)\n"
    "        elif r_multiple >= config.TRAIL_PHASE1_TRIGGER:\n"
    "            phase = 'PHASE1'\n"
    "            new_sl = entry_price + (r_value * config.TRAIL_PHASE1_LEVEL)\n"
    "\n"
    "        trailing_sl = max(trailing_sl, new_sl)",
    "        elif r_multiple >= config.TRAIL_PHASE2_TRIGGER:\n"
    "            phase = 'PHASE2'\n"
    "            new_sl = (entry_price - r_value * config.TRAIL_PHASE2_LEVEL) if _mon_short else (entry_price + r_value * config.TRAIL_PHASE2_LEVEL)\n"
    "        elif r_multiple >= config.TRAIL_PHASE1_TRIGGER:\n"
    "            phase = 'PHASE1'\n"
    "            new_sl = (entry_price - r_value * config.TRAIL_PHASE1_LEVEL) if _mon_short else (entry_price + r_value * config.TRAIL_PHASE1_LEVEL)\n"
    "\n"
    "        trailing_sl = min(trailing_sl, new_sl) if _mon_short else max(trailing_sl, new_sl)\n"
    "        # Side-aware trailing SL exit trigger (SHORT: exit when price rises through SL)\n"
    "        if _mon_short and current_ltp >= trailing_sl:\n"
    "            self.exit_trade(current_ltp, 'TRAIL_SL_SHORT')\n"
    "            return",
    'monitor PHASE1/2 + trail + exit trigger'
)

# ── 13. scan_for_breakout: confidence gate + SHORT branch ─────────────────
ap(
    '            survivors.append({"candidate": candidate, "entry": buffered_entry,\n'
    '                              "sl": sl, "expected_r": expected_r,\n'
    '                              "rank_index": ri, "qty": qty})',
    '            # Confidence: expected_r/2.0*100 >= 85 (i.e. expected_r >= 1.70)\n'
    '            _conf = min((expected_r / 2.0) * 100, 100)\n'
    '            if _conf < 85:\n'
    '                continue\n'
    '            survivors.append({"candidate": candidate, "entry": buffered_entry,\n'
    '                              "sl": sl, "expected_r": expected_r,\n'
    '                              "rank_index": ri, "qty": qty, "side": "LONG"})\n'
    '            # SHORT mirror branch\n'
    '            _bd_ok, _ = self.check_breakdown(candidate, ltp)\n'
    '            if _bd_ok:\n'
    '                _entry_s = orb_low - rank_frac * orb_range\n'
    '                if ltp <= _entry_s and _entry_s > 0:\n'
    '                    _sl_s   = orb_low + 0.3 * orb_range\n'
    '                    _risk_s = _sl_s - _entry_s\n'
    '                    if _risk_s > 0:\n'
    '                        _tgt_s = orb_low - M * orb_range\n'
    '                        _er_s  = (_entry_s - _tgt_s) / _risk_s\n'
    '                        _conf_s = min((_er_s / 2.0) * 100, 100)\n'
    '                        if _er_s >= MIN_R and _conf_s >= 85:\n'
    '                            _qty_s = int(risk_rupees / _risk_s)\n'
    '                            if _qty_s >= 1:\n'
    '                                survivors.append({"candidate": candidate,\n'
    '                                                  "entry": _entry_s, "sl": _sl_s,\n'
    '                                                  "expected_r": _er_s, "rank_index": ri,\n'
    '                                                  "qty": _qty_s, "side": "SHORT"})',
    'scan_for_breakout confidence+SHORT'
)

# ── 14. scan_for_breakout: replace sort/pick with pick_side ───────────────
ap(
    '        if not survivors:\n'
    '            return None\n'
    '        survivors.sort(key=lambda d: (-d["expected_r"], d["rank_index"]))\n'
    '        best = survivors[0]\n'
    '        log.info(f"{len(survivors)} qualified breakouts | regime={regime} M={M} | "\n'
    '                 f"pick={best[\'candidate\'].get(\'ticker\',\'?\')} "\n'
    '                 f"rank#{best[\'rank_index\']+1} entry={best[\'entry\']:.2f} "\n'
    '                 f"R={best[\'expected_r\']:.2f} qty~{best[\'qty\']}")',
    '        if not survivors:\n'
    '            return None\n'
    '        try:\n'
    '            from short_live import pick_side as _pside\n'
    '        except ImportError:\n'
    '            _pside = None\n'
    '        _longs  = sorted([d for d in survivors if d.get("side") == "LONG"],  key=lambda d: (-d["expected_r"], d["rank_index"]))\n'
    '        _shorts = sorted([d for d in survivors if d.get("side") == "SHORT"], key=lambda d: (-d["expected_r"], d["rank_index"]))\n'
    '        _bl, _bs = (_longs[0]  if _longs  else None), (_shorts[0] if _shorts else None)\n'
    '        _lr = (_bl["expected_r"] * 100) if _bl else -1\n'
    '        _sr = (_bs["expected_r"] * 100) if _bs else -1\n'
    '        if _pside:\n'
    '            _sp, _why = _pside(regime, _lr, _sr, short_needs_margin=0)\n'
    '        else:\n'
    '            _sp  = "LONG" if (_lr >= _sr) else "SHORT"\n'
    '            _why = "fallback"\n'
    '        best = (_bs if _sp == "SHORT" and _bs else (_bl if _bl else _bs))\n'
    '        if not best:\n'
    '            return None\n'
    '        best["candidate"]["_side"] = best.get("side", "LONG")\n'
    '        log.info(f"{len(survivors)} survivors | regime={regime} M={M} "\n'
    '                 f"side={best.get(\'side\',\'?\')} ({_why}) | "\n'
    '                 f"pick={best[\'candidate\'].get(\'ticker\',\'?\')} "\n'
    '                 f"rank#{best[\'rank_index\']+1} entry={best[\'entry\']:.2f} R={best[\'expected_r\']:.2f}")',
    'scan_for_breakout pick_side'
)

# ── 15. call site: log side + pass to place_entry ─────────────────────────
ap(
    '                    candidate, ltp, score = result\n'
    '                    log.info(f"\U0001f3af BREAKOUT DETECTED: {candidate[\'ticker\']} @ \u20b9{ltp:.2f} Score={score:.3f}")',
    '                    candidate, ltp, score = result\n'
    '                    _trade_side = candidate.get(\'_side\', \'LONG\')\n'
    '                    log.info(f"\U0001f3af {\'BREAKDOWN\' if _trade_side==\'SHORT\' else \'BREAKOUT\'} DETECTED: {candidate[\'ticker\']} @ \u20b9{ltp:.2f} Score={score:.3f} [{_trade_side}]")',
    'call site log'
)
ap(
    '                    if not self.active_trade:\n'
    '                        self.place_entry(candidate, ltp, score)',
    '                    if not self.active_trade:\n'
    '                        self.place_entry(candidate, ltp, score, side=_trade_side)',
    'call site place_entry side'
)

# ── Finalize ──────────────────────────────────────────────────────────────
if missed:
    print(f"\n[ABORT] {len(missed)} anchor(s) not found: {missed}")
    print(f"        File NOT written. Backup {bak} intact.")
    sys.exit(2)

try:
    ast.parse(patched)
except SyntaxError as e:
    print(f"\n[ABORT] SyntaxError: {e}")
    print(f"        File NOT written. Backup {bak} intact.")
    sys.exit(3)

open(FILE, "w").write(patched)
ast.parse(open(FILE).read())
print(f"\n[DONE] {applied} applied, {skipped} skipped. Backup: {bak}")
print('Verify: grep -n "_side\\|_mon_short\\|_is_short\\|pick_side\\|BREAKDOWN" trading_bot.py | head -30')
PYEOF

echo "=== Running patch ==="
venv/bin/python3 patch_wire_pick_side.py
echo ""
echo "=== AST check ==="
venv/bin/python3 -c "import ast; ast.parse(open('trading_bot.py').read()); print('[AST OK]')"
echo ""
echo "=== Verify key lines ==="
grep -n "_side\|_mon_short\|_is_short_x\|pick_side\|BREAKDOWN\|BUY-to-cover\|COVERING\|Hard SL" trading_bot.py | head -35
