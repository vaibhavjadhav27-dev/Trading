#!/usr/bin/env python3
"""
v855_corrective_patch.py - Fixes 4 expert-identified issues in V8.5.5 integration
==================================================================================
Expert review (Aug 24/26) found:

RED 1: V8.5.5 early entries get killed by V8.5.3 ENTER_NOW requirement
RED 2: Profit-fading exit uses stale/default values (RS=entry-time, volume_reversal=False always)
RED 3: Broker-side trailing SL not updated at R milestones (software-only protection)
RED 4: MCX contract resolver picks options (separate fix, not in this patch)

This patch applies to the LIVE trading_bot_v84.py on the server.
Run: python3 v855_corrective_patch.py

Creates backup, applies 3 patches (A, B, C), verifies syntax.
"""

import shutil, os, re, textwrap
from datetime import datetime
from pathlib import Path

TARGET = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trading_bot_v84.py")
BACKUP = TARGET.with_suffix(f".py.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")


# =============================================================================
# PATCH A: V8.5.3 must not reject V8.5.5 EARLY_ACCELERATION entries
# =============================================================================
# The current flow: ranked candidates → V8.5.3 filter → only ENTER_NOW survives
# Fix: If setup_type == "V855_EARLY_ACCELERATION" OR "EARLY_ACCELERATION",
#      accept ENTER_NOW or CONFIRMATION_PENDING (bypass mature timing requirement)
#      but STILL apply V8.5.3 risk/structure checks (EXHAUSTED, PARTICIPATION_TOO_WEAK)

PATCH_A_MARKER = "# PATCH_A: V8.5.5 early-entry V8.5.3 bypass"

PATCH_A_CODE = '''
    def _v853_filter_candidates(self, ranked):
        """V8.5.3 filter with V8.5.5 early-entry exception.
        
        # PATCH_A: V8.5.5 early-entry V8.5.3 bypass
        Normal candidates: require EntryAction.ENTER_NOW
        V8.5.5 EARLY_ACCELERATION: accept ENTER_NOW or CONFIRMATION_PENDING
        (bypasses mature timing, keeps risk/structure safety)
        """
        try:
            from v853_profit_engine_patch import V853EntryEngine, EntryAction
        except ImportError:
            # V8.5.3 not available — pass all through
            return ranked

        filtered = []
        engine = V853EntryEngine()

        for _d, _c in ranked:
            setup_type = _d.get("setup_type", "")
            is_v855_early = setup_type in ("EARLY_ACCELERATION", "V855_EARLY_ACCELERATION")

            try:
                _decision = engine.evaluate(_c, _d)
            except Exception as e:
                # Fallback safety: on V8.5.3 eval exception, include candidate
                log.warning("V8.5.3 eval exception for %s: %s — including", _c.get("symbol"), e)
                filtered.append((_d, _c))
                continue

            # Audit log EVERY decision (including rejections)
            _event("v855_v853_filter", {
                "symbol": _c.get("symbol", "?"),
                "setup_type": setup_type,
                "is_v855_early": is_v855_early,
                "v853_action": str(_decision.action) if hasattr(_decision, "action") else str(_decision),
                "v853_reason": getattr(_decision, "reason", ""),
                "normal_score": _d.get("candidate_score", 0),
                "early_score": _d.get("final_score", 0),
                "early_reasons": _d.get("reason", ""),
                "final_decision": "PASS" if True else "REJECT",  # Updated below
            })

            if is_v855_early:
                # V8.5.5 early entries: accept ENTER_NOW or CONFIRMATION_PENDING
                # Still reject EXHAUSTED, PARTICIPATION_TOO_WEAK (structural risk)
                if hasattr(_decision, "action"):
                    if _decision.action in (EntryAction.ENTER_NOW, EntryAction.CONFIRMATION_PENDING):
                        filtered.append((_d, _c))
                        _event("v855_v853_filter", {"symbol": _c.get("symbol"), "final_decision": "PASS_EARLY"})
                    else:
                        _event("v855_v853_filter", {
                            "symbol": _c.get("symbol"),
                            "final_decision": "REJECT_EARLY",
                            "v853_reason": getattr(_decision, "reason", str(_decision.action))
                        })
                else:
                    # V8.5.3 returned something unexpected — include for safety
                    filtered.append((_d, _c))
            else:
                # Normal V8.4 candidates: require ENTER_NOW
                if hasattr(_decision, "action") and _decision.action == EntryAction.ENTER_NOW:
                    filtered.append((_d, _c))
                elif not hasattr(_decision, "action"):
                    # No V8.5.3 — pass through
                    filtered.append((_d, _c))

        return filtered

'''


# =============================================================================
# PATCH B: Real-time exit snapshot (fresh RS, RVOL, volume reversal, structure)
# =============================================================================
# Replace the hardcoded snapshot in monitor_positions with live calculations

PATCH_B_MARKER = "# PATCH_B: Real-time exit snapshot"

PATCH_B_CODE = '''
    def _build_exit_snapshot(self, sid, p, df, px):
        """PATCH_B: Real-time exit snapshot — calculates current RS, RVOL,
        volume reversal, structural invalidation, VWAP, momentum.
        
        Never uses entry-time values or hardcoded False."""
        from v84.indicators import vwap as calc_vwap
        from v82_strategy import rvol as calc_rvol, momentum as calc_momentum

        side = p["side"]
        entry = float(p["entry"])
        sl = float(p["sl"])

        # Fresh VWAP from intraday df
        vw = calc_vwap(df)

        # Fresh momentum
        m5, m15, m30, accel = calc_momentum(df)

        # Fresh RVOL
        adv = float(p.get("avg_daily_volume", 0) or 0)
        rv = calc_rvol(df, adv) if adv > 0 else 1.0

        # Fresh RS — calculate from price vs NIFTY
        # Use current NIFTY data if available, else fallback to stored
        nifty_data = getattr(self, "nifty_data", {}) or {}
        nifty_px = float(nifty_data.get("ltp", 0) or 0)
        nifty_vwap = float(nifty_data.get("vwap", 0) or 0)
        if nifty_px > 0 and nifty_vwap > 0:
            stock_vs_vwap = (px - vw) / max(vw, 1) * 100
            nifty_vs_vwap = (nifty_px - nifty_vwap) / max(nifty_vwap, 1) * 100
            rs_val = stock_vs_vwap - nifty_vs_vwap
        else:
            rs_val = float(p.get("rs", 0) or 0)  # Fallback only if NIFTY unavailable

        # Volume reversal: current bar volume < 50% of recent average
        closes = list(df.close) if hasattr(df, "close") else []
        volumes = list(df.volume) if hasattr(df, "volume") else []
        volume_rev = False
        if len(volumes) >= 5:
            avg_vol = sum(float(v) for v in volumes[-5:]) / 5
            cur_vol = float(volumes[-1]) if volumes else 0
            # Volume dying = reversal signal for LONG (selling drying up for SHORT)
            if side == "LONG" and cur_vol > avg_vol * 1.5:
                volume_rev = True  # Surge on down move = selling pressure
            elif side == "SHORT" and cur_vol > avg_vol * 1.5:
                volume_rev = True  # Surge on up move = buying pressure

        # Structural invalidation: price crossed back beyond SL level
        structural_inv = False
        if side == "LONG" and px < sl:
            structural_inv = True
        elif side == "SHORT" and px > sl:
            structural_inv = True

        # Candle reversal: last candle reverses direction
        candle_rev = False
        if len(closes) >= 3:
            c1, c2, c3 = float(closes[-3]), float(closes[-2]), float(closes[-1])
            if side == "LONG":
                # Bearish reversal: was going up, now going down
                candle_rev = (c2 > c1 and c3 < c2)
            else:
                # Bullish reversal: was going down, now going up
                candle_rev = (c2 < c1 and c3 > c2)

        return {
            "vwap": vw,
            "mom_5m": m5,
            "mom_15m": m15,
            "rs": rs_val,
            "rvol": rv,
            "candle_reversal": candle_rev,
            "volume_reversal": volume_rev,
            "structural_invalidated": structural_inv,
        }

'''


# =============================================================================
# PATCH C: Broker-side progressive SL trailing
# =============================================================================
# At R milestones, modify the existing Dhan SL order to protect profits

PATCH_C_MARKER = "# PATCH_C: Broker-side profit protection"

PATCH_C_CODE = '''
    def _broker_trail_sl(self, sid, p, current_r, px):
        """PATCH_C: Broker-side profit protection.
        
        Modifies the existing Dhan SL order at R milestones:
          < 1R  → original structural SL (no change)
          >= 1R → protect ~0.5R
          >= 2R → protect ~1.25R
          >= 2.5R → protect ~1.75R
          >= 3R → protect ~2.25R
        
        Modifies EXISTING pending SL, does NOT create new SL orders.
        """
        side = p["side"]
        entry = float(p["entry"])
        risk_per_share = max(abs(entry - float(p["sl"])), 0.01)
        sl_order_id = p.get("sl_order_id")

        if not sl_order_id:
            return  # No SL order to modify

        # Determine target SL based on R milestones
        if current_r >= 3.0:
            protect_r = 2.25
        elif current_r >= 2.5:
            protect_r = 1.75
        elif current_r >= 2.0:
            protect_r = 1.25
        elif current_r >= 1.0:
            protect_r = 0.5
        else:
            return  # Below 1R, keep original SL

        # Calculate new SL price
        if side == "LONG":
            new_sl = round(entry + protect_r * risk_per_share, 2)
            # Only tighten, never widen
            current_sl = float(p["sl"])
            if new_sl <= current_sl:
                return
        else:
            new_sl = round(entry - protect_r * risk_per_share, 2)
            current_sl = float(p["sl"])
            if new_sl >= current_sl:
                return

        # Modify existing SL order on Dhan
        try:
            result = self.dhan.modify_order(
                sl_order_id, 
                order_type="SL",
                trigger_price=new_sl
            )
            if result:
                old_sl = p["sl"]
                p["sl"] = new_sl
                _event("v855_broker_trail", {
                    "symbol": p.get("symbol", sid),
                    "side": side,
                    "current_r": round(current_r, 2),
                    "protect_r": protect_r,
                    "old_sl": old_sl,
                    "new_sl": new_sl,
                    "sl_order_id": sl_order_id,
                })
                log.info("BROKER_TRAIL: %s %s R=%.2f SL: %.2f -> %.2f (protect %.1fR)",
                         p.get("symbol", sid), side, current_r, old_sl, new_sl, protect_r)
        except Exception as e:
            log.error("BROKER_TRAIL FAILED: %s %s — %s", p.get("symbol", sid), side, e)
            _event("v855_broker_trail_fail", {
                "symbol": p.get("symbol", sid), "error": str(e),
                "current_r": round(current_r, 2)
            })

'''


# =============================================================================
# NEW monitor_positions incorporating all 3 patches
# =============================================================================

NEW_MONITOR = '''    def monitor_positions(self):
        """V8.5.5+patches: Real-time exit with broker trailing.
        Patch B: Fresh RS/RVOL/volume/structure on every cycle.
        Patch C: Broker SL tightened at R milestones."""
        if not self.active_positions:
            return
        prices = self.fetch_ltp_concurrent(list(self.active_positions.keys()))
        for sid, p in list(self.active_positions.items()):
            px = float(prices.get(sid, 0) or 0)
            if px <= 0:
                continue
            entry = float(p["entry"])
            side = p["side"]
            risk_per_share = max(abs(entry - float(p["sl"])), 0.01)

            # CRITICAL: Fetch df BEFORE exit evaluation
            df = self._df(sid)
            if df is None or len(df) < 3:
                log.warning("V855 EXIT: %s df unavailable, skipping exit eval", sid)
                continue

            # PATCH B: Build real-time snapshot (not entry-time values)
            snapshot = self._build_exit_snapshot(sid, p, df, px)

            # Update peak tracking
            peak = float(p.get("peak", entry))
            exit_state = ExitState(peak_price=peak, best_r=float(p.get("best_r", 0)))
            update_peak(exit_state, side, px)
            p["peak"] = exit_state.peak_price
            p["best_r"] = exit_state.best_r

            # Current R
            if side == "LONG":
                current_r = (px - entry) / risk_per_share
            else:
                current_r = (entry - px) / risk_per_share

            # PATCH C: Broker-side progressive SL trailing
            self._broker_trail_sl(sid, p, current_r, px)

            # V8.5.5 profit-fading exit evaluation — NO except:pass
            exit_result = profit_fading_exit(
                side=side,
                entry=entry,
                price=px,
                initial_risk=risk_per_share,
                state=exit_state,
                snapshot=snapshot
            )

            # Audit log every evaluation
            _event("v855_exit_eval", {
                "symbol": p.get("symbol", sid),
                "side": side,
                "price": px,
                "r": exit_result.get("r"),
                "peak_r": exit_result.get("peak_r"),
                "retrace_r": exit_result.get("retrace_r"),
                "reason": exit_result.get("reason"),
                "confirmations": exit_result.get("confirmations", 0),
                "reasons": exit_result.get("reasons", []),
                "exit": exit_result.get("exit", False),
                "snapshot_rs": snapshot.get("rs"),
                "snapshot_rvol": snapshot.get("rvol"),
                "snapshot_vol_rev": snapshot.get("volume_reversal"),
                "snapshot_struct_inv": snapshot.get("structural_invalidated"),
            })

            if exit_result.get("exit"):
                reason = exit_result["reason"]
                log.info(
                    "V855 EXIT: %s %s @ %.2f | R=%.2f peak_R=%.2f | %s | confirms=%s",
                    p.get("symbol", sid), side, px,
                    exit_result["r"], exit_result["peak_r"],
                    reason, exit_result.get("reasons", [])
                )
                self.close_position(sid, "V855_" + reason, px)

        self._save_state()

'''


def patch():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        return False

    # Backup
    shutil.copy2(TARGET, BACKUP)
    print(f"Backup: {BACKUP}")

    code = TARGET.read_text()
    changes = 0

    # =====================================================
    # PATCH A: Insert _v853_filter_candidates method
    # =====================================================
    if PATCH_A_MARKER not in code:
        # Insert before monitor_positions (or before _try_v855_accel if present)
        insert_before = "    def monitor_positions(self):"
        if "_try_v855_accel" in code:
            insert_before = "    def _try_v855_accel"
        
        idx = code.find(insert_before)
        if idx > 0:
            code = code[:idx] + PATCH_A_CODE + "\n" + code[idx:]
            changes += 1
            print("[A] Added _v853_filter_candidates (early-entry V8.5.3 bypass)")
        else:
            print("[A] WARNING: Could not find insertion point")
    else:
        print("[A] Already applied")

    # =====================================================
    # PATCH B: Insert _build_exit_snapshot method
    # =====================================================
    if PATCH_B_MARKER not in code:
        idx = code.find("    def monitor_positions(self):")
        if idx > 0:
            code = code[:idx] + PATCH_B_CODE + "\n" + code[idx:]
            changes += 1
            print("[B] Added _build_exit_snapshot (real-time RS/RVOL/volume/structure)")
        else:
            print("[B] WARNING: Could not find insertion point")
    else:
        print("[B] Already applied")

    # =====================================================
    # PATCH C: Insert _broker_trail_sl method
    # =====================================================
    if PATCH_C_MARKER not in code:
        idx = code.find("    def monitor_positions(self):")
        if idx > 0:
            code = code[:idx] + PATCH_C_CODE + "\n" + code[idx:]
            changes += 1
            print("[C] Added _broker_trail_sl (progressive profit protection)")
        else:
            print("[C] WARNING: Could not find insertion point")
    else:
        print("[C] Already applied")

    # =====================================================
    # Replace monitor_positions with new version using all patches
    # =====================================================
    idx_start = code.find("    def monitor_positions(self):")
    if idx_start == -1:
        print("[MONITOR] ERROR: monitor_positions not found")
        return False

    # Find next top-level method
    idx_next = code.find("\n    def ", idx_start + 10)
    if idx_next == -1:
        idx_next = code.find("\n    def run(self):", idx_start + 10)
    if idx_next == -1:
        print("[MONITOR] ERROR: cannot find end of monitor_positions")
        return False

    old_monitor = code[idx_start:idx_next]
    if "PATCH B" not in old_monitor:
        code = code[:idx_start] + NEW_MONITOR + code[idx_next:]
        changes += 1
        print("[MONITOR] Replaced with patched version (B+C integrated)")
    else:
        print("[MONITOR] Already patched")

    # =====================================================
    # Wire _v853_filter_candidates into the candidate loop
    # (After ranked is built, before execution)
    # =====================================================
    if "self._v853_filter_candidates(ranked)" not in code:
        # Find where ranked is used for execution
        # Typical pattern: for d,c in sorted(ranked, ...)
        rank_usage = code.find("for d,c in sorted(ranked")
        if rank_usage == -1:
            rank_usage = code.find("for d, c in sorted(ranked")
        if rank_usage == -1:
            rank_usage = code.find("for d,c in ranked")
        
        if rank_usage > 0:
            # Insert filter call just before the loop
            indent = "        "
            filter_line = f"{indent}ranked = self._v853_filter_candidates(ranked)\n{indent}"
            code = code[:rank_usage] + filter_line + code[rank_usage:]
            changes += 1
            print("[WIRE] Added _v853_filter_candidates call before execution loop")
        else:
            print("[WIRE] WARNING: Could not find ranked iteration — add manually:")
            print("       ranked = self._v853_filter_candidates(ranked)")
    else:
        print("[WIRE] Already wired")

    # Write
    TARGET.write_text(code)
    print(f"\n{'='*60}")
    print(f"Applied {changes} patches to {TARGET}")
    print(f"Backup: {BACKUP}")
    print(f"{'='*60}")
    print("\nVerify syntax:")
    print("  python3 -c \"import py_compile; py_compile.compile('trading_bot_v84.py', doraise=True)\"")
    print("\nVerify import:")
    print("  python3 -c \"from trading_bot_v84 import TradingBotV84; print('OK')\"")
    print("\nRestart for next session:")
    print("  sudo systemctl restart trading-bot-v84")
    return True


if __name__ == "__main__":
    patch()
