#!/usr/bin/env python3
"""
v855_corrective_patch_v2.py — Precise line-targeted fixes
=========================================================
Targets: /home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trading_bot_v84.py

Patch A: Line 420 — V8.5.3 filter accepts CONFIRMATION_PENDING for V8.5.5 early entries
Patch B: Lines 266-287 — Replace stale snapshot with real-time RS/RVOL/volume/structure
Patch C: Insert _broker_trail_sl method + wire into monitor_positions

Run: python3 v855_corrective_patch_v2.py
"""

import shutil
from datetime import datetime
from pathlib import Path

TARGET = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trading_bot_v84.py")
BACKUP = TARGET.with_suffix(f".py.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")


def patch():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        return False

    shutil.copy2(TARGET, BACKUP)
    print(f"Backup: {BACKUP}")

    lines = TARGET.read_text().split('\n')
    changes = 0

    # =================================================================
    # PATCH A: V8.5.3 filter — accept CONFIRMATION_PENDING for early entries
    # =================================================================
    # Current line 420 (0-indexed: 419):
    #   if _decision.action == EntryAction.ENTER_NOW:
    # Replace the block at lines 420-424 with early-entry aware logic

    patched_a = False
    for i, line in enumerate(lines):
        if ('_decision.action == EntryAction.ENTER_NOW' in line 
            and '_v853_filtered.append' in lines[i+1] if i+1 < len(lines) else False):
            # Found the filter decision point
            indent = '                                        '  # match existing indent
            old_lines_count = 0
            # Count lines to replace: the if + append + log.info
            # Original:
            #   if _decision.action == EntryAction.ENTER_NOW:
            #       _v853_filtered.append((_d, _c))
            #       log.info(f"V853 ENTER: ...")
            #   else:
            #       log.info(f"V853 SKIP: ...")
            
            new_block = [
                f'{indent}# PATCH A: V8.5.5 early entries bypass timing gate',
                f'{indent}_is_early = _d.get("setup_type","") in ("EARLY_ACCELERATION","V855_EARLY_ACCELERATION")',
                f'{indent}_accept = (_decision.action == EntryAction.ENTER_NOW) or (_is_early and hasattr(EntryAction,"CONFIRMATION_PENDING") and _decision.action == EntryAction.CONFIRMATION_PENDING)',
                f'{indent}if _accept:',
                f'{indent}    _v853_filtered.append((_d, _c))',
                f'{indent}    _pass_type = "EARLY_BYPASS" if (_is_early and _decision.action != EntryAction.ENTER_NOW) else "ENTER_NOW"',
                f'{indent}    log.info(f"V853 ENTER: {{_c.get(\'symbol\',\'?\')}}" + f" phase={{_decision.phase.value}} q={{_decision.quality_score:.0f}} t={{_decision.timing_score:.0f}} f={{_decision.final_score:.0f}} pass={{_pass_type}}")',
            ]
            
            # Replace the original "if _decision.action == EntryAction.ENTER_NOW:" line
            # and the next 2 lines (append + log)
            lines[i] = '\n'.join(new_block)
            # Remove the old append and log.info lines (i+1, i+2)
            del lines[i+1]  # _v853_filtered.append
            del lines[i+1]  # log.info(f"V853 ENTER...")
            
            patched_a = True
            changes += 1
            print(f"[A] Patched V8.5.3 filter at line {i+1} — EARLY_ACCELERATION now accepted")
            break

    if not patched_a:
        print("[A] WARNING: Could not find V8.5.3 filter decision point")

    # =================================================================
    # PATCH B: Replace stale snapshot (lines ~266-287) with real-time calc
    # =================================================================
    # Find "# Build live snapshot for profit_fading_exit" and replace through
    # the snapshot = {...} closing brace

    patched_b = False
    for i, line in enumerate(lines):
        if '# Build live snapshot for profit_fading_exit' in line:
            # Find the end: "}" line of snapshot dict (line with just "}" and same indent)
            end_idx = i + 1
            while end_idx < len(lines):
                if lines[end_idx].strip() == '}':
                    break
                end_idx += 1
            
            indent = '            '  # 12 spaces (inside for loop)
            new_snapshot = [
                f'{indent}# PATCH B: Real-time exit snapshot (fresh RS, RVOL, volume reversal, structural)',
                f'{indent}vw = vwap(df)',
                f'{indent}from v82_strategy import momentum as calc_momentum',
                f'{indent}m5, m15, m30, accel = calc_momentum(df)',
                f'{indent}',
                f'{indent}# Fresh RVOL',
                f'{indent}adv = float(p.get("avg_daily_volume", 0) or 0)',
                f'{indent}from v82_strategy import rvol as calc_rvol',
                f'{indent}rv = calc_rvol(df, adv) if adv > 0 else 1.0',
                f'{indent}',
                f'{indent}# Fresh RS: stock vs NIFTY relative strength',
                f'{indent}nifty_px = float(getattr(self, "_nifty_ltp", 0) or 0)',
                f'{indent}nifty_vwap = float(getattr(self, "_nifty_vwap", 0) or 0)',
                f'{indent}if nifty_px > 0 and nifty_vwap > 0 and vw > 0:',
                f'{indent}    stock_vs_vwap = (px - vw) / vw * 100',
                f'{indent}    nifty_vs_vwap = (nifty_px - nifty_vwap) / nifty_vwap * 100',
                f'{indent}    rs_val = stock_vs_vwap - nifty_vs_vwap',
                f'{indent}else:',
                f'{indent}    rs_val = float(p.get("rs", 0) or 0)  # fallback only if NIFTY unavailable',
                f'{indent}',
                f'{indent}# Volume reversal: selling/buying surge against position',
                f'{indent}volumes = [float(x) for x in df.volume.tail(5)] if hasattr(df, "volume") and len(df) >= 5 else []',
                f'{indent}volume_rev = False',
                f'{indent}if len(volumes) >= 5:',
                f'{indent}    avg_vol = sum(volumes) / len(volumes)',
                f'{indent}    cur_vol = volumes[-1] if volumes else 0',
                f'{indent}    if avg_vol > 0 and cur_vol > avg_vol * 1.5:',
                f'{indent}        volume_rev = True  # Volume surge = potential reversal',
                f'{indent}',
                f'{indent}# Structural invalidation: price beyond SL',
                f'{indent}structural_inv = False',
                f'{indent}if side == "LONG" and px < float(p["sl"]):',
                f'{indent}    structural_inv = True',
                f'{indent}elif side == "SHORT" and px > float(p["sl"]):',
                f'{indent}    structural_inv = True',
                f'{indent}',
                f'{indent}# Candle reversal: 2-bar direction change',
                f'{indent}closes = [float(x) for x in df.close.tail(3)]',
                f'{indent}candle_rev = False',
                f'{indent}if len(closes) >= 3:',
                f'{indent}    if side == "LONG" and closes[-2] > closes[-3] and closes[-1] < closes[-2]:',
                f'{indent}        candle_rev = True',
                f'{indent}    elif side == "SHORT" and closes[-2] < closes[-3] and closes[-1] > closes[-2]:',
                f'{indent}        candle_rev = True',
                f'{indent}',
                f'{indent}snapshot = {{',
                f'{indent}    "vwap": vw,',
                f'{indent}    "mom_5m": m5,',
                f'{indent}    "mom_15m": m15,',
                f'{indent}    "rs": rs_val,',
                f'{indent}    "rvol": rv,',
                f'{indent}    "candle_reversal": candle_rev,',
                f'{indent}    "volume_reversal": volume_rev,',
                f'{indent}    "structural_invalidated": structural_inv,',
                f'{indent}}}',
            ]
            
            # Replace lines from i to end_idx (inclusive)
            lines[i:end_idx+1] = new_snapshot
            patched_b = True
            changes += 1
            print(f"[B] Replaced stale snapshot at lines {i+1}-{end_idx+1} with real-time calculation")
            break

    if not patched_b:
        print("[B] WARNING: Could not find snapshot block")

    # =================================================================
    # PATCH C: Broker-side progressive SL trailing
    # =================================================================
    # Insert _broker_trail_sl method before monitor_positions
    # Then add call inside monitor_positions after peak tracking

    # Part 1: Insert method before monitor_positions
    patched_c1 = False
    for i, line in enumerate(lines):
        if 'def monitor_positions(self):' in line:
            indent = '    '  # class method indent
            broker_method = [
                f'{indent}def _broker_trail_sl(self, sid, p, current_r):',
                f'{indent}    """PATCH_C: Broker-side profit protection.',
                f'{indent}    Modifies existing Dhan SL order at R milestones:',
                f'{indent}      <1R: original SL | >=1R: 0.5R | >=2R: 1.25R | >=2.5R: 1.75R | >=3R: 2.25R"""',
                f'{indent}    sl_order_id = p.get("sl_order_id")',
                f'{indent}    if not sl_order_id:',
                f'{indent}        return',
                f'{indent}    side = p["side"]',
                f'{indent}    entry = float(p["entry"])',
                f'{indent}    risk_per_share = max(abs(entry - float(p["sl"])), 0.01)',
                f'{indent}    ',
                f'{indent}    if current_r >= 3.0: protect_r = 2.25',
                f'{indent}    elif current_r >= 2.5: protect_r = 1.75',
                f'{indent}    elif current_r >= 2.0: protect_r = 1.25',
                f'{indent}    elif current_r >= 1.0: protect_r = 0.5',
                f'{indent}    else: return  # Below 1R, keep original SL',
                f'{indent}    ',
                f'{indent}    if side == "LONG":',
                f'{indent}        new_sl = round(entry + protect_r * risk_per_share, 2)',
                f'{indent}        if new_sl <= float(p["sl"]): return  # Only tighten',
                f'{indent}    else:',
                f'{indent}        new_sl = round(entry - protect_r * risk_per_share, 2)',
                f'{indent}        if new_sl >= float(p["sl"]): return  # Only tighten',
                f'{indent}    ',
                f'{indent}    try:',
                f'{indent}        result = self.dhan.modify_order(sl_order_id, order_type="SL", trigger_price=new_sl)',
                f'{indent}        if result:',
                f'{indent}            old_sl = p["sl"]',
                f'{indent}            p["sl"] = new_sl',
                f'{indent}            _event("v855_broker_trail", {{"symbol": p.get("symbol", sid), "side": side, "current_r": round(current_r, 2), "protect_r": protect_r, "old_sl": old_sl, "new_sl": new_sl}})',
                f'{indent}            log.info("BROKER_TRAIL: %s %s R=%.2f SL: %.2f -> %.2f (protect %.1fR)", p.get("symbol", sid), side, current_r, old_sl, new_sl, protect_r)',
                f'{indent}    except Exception as e:',
                f'{indent}        log.error("BROKER_TRAIL FAILED: %s %s — %s", p.get("symbol", sid), side, e)',
                f'{indent}        _event("v855_broker_trail_fail", {{"symbol": p.get("symbol", sid), "error": str(e)}})',
                f'',
            ]
            lines[i:i] = broker_method
            patched_c1 = True
            changes += 1
            print(f"[C1] Inserted _broker_trail_sl method at line {i+1}")
            break

    if not patched_c1:
        print("[C1] WARNING: Could not find monitor_positions for method insertion")

    # Part 2: Add broker trail call inside monitor_positions after peak tracking
    patched_c2 = False
    for i, line in enumerate(lines):
        if 'p["best_r"] = exit_state.best_r' in line:
            indent = '            '  # inside for loop
            trail_call = [
                f'{indent}',
                f'{indent}# PATCH C: Broker-side progressive SL trailing',
                f'{indent}if side == "LONG":',
                f'{indent}    current_r = (px - entry) / risk_per_share',
                f'{indent}else:',
                f'{indent}    current_r = (entry - px) / risk_per_share',
                f'{indent}self._broker_trail_sl(sid, p, current_r)',
            ]
            # Insert after this line
            lines[i+1:i+1] = trail_call
            patched_c2 = True
            changes += 1
            print(f"[C2] Wired _broker_trail_sl call after peak tracking at line {i+2}")
            break

    if not patched_c2:
        print("[C2] WARNING: Could not find peak tracking line for broker trail wiring")

    # =================================================================
    # PATCH D: Enhanced audit logging in exit eval
    # =================================================================
    # Add snapshot values to the existing _event("v855_exit_eval", ...) call
    patched_d = False
    for i, line in enumerate(lines):
        if '"exit": exit_result.get("exit", False),' in line:
            indent = '                '
            extra_fields = [
                f'{indent}"snapshot_rs": snapshot.get("rs"),',
                f'{indent}"snapshot_rvol": snapshot.get("rvol"),',
                f'{indent}"snapshot_vol_rev": snapshot.get("volume_reversal"),',
                f'{indent}"snapshot_struct_inv": snapshot.get("structural_invalidated"),',
            ]
            lines[i+1:i+1] = extra_fields
            patched_d = True
            changes += 1
            print(f"[D] Added snapshot fields to exit audit log at line {i+2}")
            break

    if not patched_d:
        print("[D] INFO: Could not add extra audit fields (non-critical)")

    # Write result
    TARGET.write_text('\n'.join(lines))
    print(f"\n{'='*60}")
    print(f"Applied {changes} patches to {TARGET}")
    print(f"Backup: {BACKUP}")
    print(f"{'='*60}")
    return True


if __name__ == "__main__":
    success = patch()
    if success:
        print("\nVerify:")
        print("  python3 -c \"import py_compile; py_compile.compile('trading_bot_v84.py', doraise=True)\" && echo 'SYNTAX OK'")
        print("  python3 -c \"from trading_bot_v84 import TradingBotV84; print('IMPORT OK')\"")
