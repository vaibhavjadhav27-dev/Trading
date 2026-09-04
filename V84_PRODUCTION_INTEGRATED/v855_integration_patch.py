#!/usr/bin/env python3
"""
v855_integration_patch.py - Wires V8.5.5 into trading_bot_v84.py
================================================================
Run on server to patch the live bot. Creates a backup first.

Integration points:
1. Import V8.5.5 acceleration_score + profit_fading_exit
2. Add EARLY_ACCELERATION entry path (parallel to normal V8.4 evaluate)
3. Replace monitor_positions exit logic with multi-factor reversal
4. Fetch df/snapshot BEFORE exit evaluation (fixes V8.5.4 bug)
5. No except:pass around exit engine

Run: python3 v855_integration_patch.py
"""

import shutil, os
from datetime import datetime
from pathlib import Path

TARGET = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/trading_bot_v84.py")
BACKUP = TARGET.with_suffix(f".py.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")


def patch():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        return False

    # Backup
    shutil.copy2(TARGET, BACKUP)
    print(f"Backup: {BACKUP}")

    code = TARGET.read_text()

    # =================================================================
    # PATCH 1: Add V8.5.5 imports
    # =================================================================
    import_line = "from v84_strategy import final_decision"
    v855_imports = (
        "from v84_strategy import final_decision\n"
        "from V8_5_5_EARLY_ENTRY_PROFIT_PROTECTION_PATCH import (\n"
        "    acceleration_score, profit_fading_exit, update_peak,\n"
        "    validate_entry, ExitState, EarlyConfig\n"
        ")"
    )

    if "V8_5_5_EARLY_ENTRY_PROFIT_PROTECTION_PATCH" not in code:
        code = code.replace(import_line, v855_imports, 1)
        print("[1/4] Added V8.5.5 imports")
    else:
        print("[1/4] V8.5.5 imports already present")

    # =================================================================
    # PATCH 2: Add early acceleration entry in candidate loop
    # Find the line: if d.get("status")=="ENTER":ranked.append((d,c))
    # Add an else branch for acceleration_score
    # =================================================================
    old_enter = 'if d.get("status")=="ENTER":ranked.append((d,c))'
    new_enter = (
        'if d.get("status")=="ENTER":ranked.append((d,c))\n'
        '                        else:\n'
        '                            # V8.5.5: Try early acceleration entry\n'
        '                            _try_v855_accel(f, c, ranked)\n'
    )

    if "_try_v855_accel" not in code:
        code = code.replace(old_enter, new_enter)
        print("[2/4] Added V8.5.5 acceleration entry hook")
    else:
        print("[2/4] Acceleration hook already present")

    # =================================================================
    # PATCH 3: Add _try_v855_accel helper method before monitor_positions
    # =================================================================
    accel_method = '''
    def _try_v855_accel(self, f, c, ranked):
        """V8.5.5: Try early acceleration entry when normal eval says WATCH."""
        for accel_side in ("LONG", "SHORT"):
            accel_snap = {
                "price": f.get("ltp", 0),
                "vwap": float(f["df"]["close"].mean()) if f.get("df") is not None and len(f["df"]) > 0 else 0,
                "atr": 0,
                "rvol": f.get("rvol", 0),
                "rvol_prev": f.get("rvol_prev", 0),
                "rs": float(f.get("rs", 0) or 0),
                "rs_prev": float(f.get("rs_prev", 0) or 0),
                "mom_5m": f.get("momentum_5m", 0),
                "mom_5m_prev": f.get("mom_5m_prev", 0),
                "mom_15m": f.get("momentum_15m", 0),
                "support": float(f.get("support", 0) or 0),
                "resistance": float(f.get("resistance", 0) or 0),
                "remaining_room_pct": float(f.get("remaining_room_pct", 2.0) or 2.0),
                "bars_since_break": int(f.get("bars_since_break", 0) or 0),
                "distance_from_break_pct": float(f.get("distance_from_break_pct", 0) or 0),
                "volume_expansion": f.get("volume_expansion", False),
                "breakout": f.get("breakout", False),
                "breakdown": f.get("breakdown", False),
            }
            accel_result = acceleration_score(
                accel_side, accel_snap,
                quality_score=float(f.get("candidate_score", 0) or 0)
            )
            if accel_result.get("enter"):
                price = float(f.get("ltp", 0))
                atr = float(f.get("atr", price * 0.01) or price * 0.01)
                stop = round(price - 1.5 * atr, 2) if accel_side == "LONG" else round(price + 1.5 * atr, 2)
                target = round(price + 3 * atr, 2) if accel_side == "LONG" else round(price - 3 * atr, 2)
                accel_d = {
                    "status": "ENTER",
                    "symbol": str(f.get("symbol", "?")),
                    "side": accel_side,
                    "candidate_score": float(f.get("candidate_score", 0)),
                    "final_score": accel_result["score"],
                    "edge": 0.0,
                    "setup_type": "EARLY_ACCELERATION",
                    "entry_price": price,
                    "stop": stop,
                    "target": target,
                    "expected_move_pct": abs(target - price) / price * 100 if price else 0,
                    "risk_pct": abs(price - stop) / price * 100 if price else 0,
                    "reason": "V855_ACCEL_" + "+".join(accel_result["reasons"][:3]),
                }
                _event("v855_early_entry", {
                    "symbol": c.get("symbol"), "side": accel_side,
                    "score": accel_result["score"], "reasons": accel_result["reasons"]
                })
                ranked.append((accel_d, c))
                break  # Only one side per candidate

'''

    # Insert before monitor_positions
    monitor_marker = "    def monitor_positions(self):"
    if "_try_v855_accel" not in code and monitor_marker in code:
        code = code.replace(monitor_marker, accel_method + monitor_marker)
        print("[3/4] Added _try_v855_accel helper method")
    else:
        print("[3/4] Helper method already present or monitor not found")

    # =================================================================
    # PATCH 4: Replace monitor_positions with V8.5.5 profit-fading exit
    # =================================================================
    # Find old monitor_positions and replace entirely
    old_monitor_start = "    def monitor_positions(self):"
    old_monitor_end = "        self._save_state()"

    # Find the full old monitor_positions function
    idx_start = code.find("    def monitor_positions(self):")
    if idx_start == -1:
        print("[4/4] ERROR: monitor_positions not found")
        return False

    # Find the next method (def run) to know where monitor ends
    idx_run = code.find("    def run(self):", idx_start)
    if idx_run == -1:
        print("[4/4] ERROR: run() not found after monitor_positions")
        return False

    old_monitor = code[idx_start:idx_run]

    new_monitor = '''    def monitor_positions(self):
        """V8.5.5: Multi-factor reversal exit. Fetch df BEFORE exit eval."""
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

            # CRITICAL: Fetch df/snapshot BEFORE exit evaluation (V8.5.4 bug fix)
            df = self._df(sid)
            if df is None or len(df) < 3:
                log.warning("V855 EXIT: %s df unavailable, skipping exit eval", sid)
                continue

            # Build live snapshot for profit_fading_exit
            vw = vwap(df)
            from v82_strategy import momentum as calc_momentum
            m5, m15, m30, accel = calc_momentum(df)
            closes = [float(x) for x in df.close.tail(3)]
            rs_val = float(p.get("rs", 0) or 0)

            # Candle reversal detection
            candle_rev = False
            if len(closes) >= 2:
                if side == "LONG" and closes[-1] < closes[-2]:
                    candle_rev = True
                elif side == "SHORT" and closes[-1] > closes[-2]:
                    candle_rev = True

            snapshot = {
                "vwap": vw,
                "mom_5m": m5,
                "mom_15m": m15,
                "rs": rs_val,
                "candle_reversal": candle_rev,
                "volume_reversal": False,
                "structural_invalidated": False,
            }

            # Update peak tracking
            peak = float(p.get("peak", entry))
            exit_state = ExitState(peak_price=peak, best_r=float(p.get("best_r", 0)))
            update_peak(exit_state, side, px)
            p["peak"] = exit_state.peak_price
            p["best_r"] = exit_state.best_r

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

    if "V8.5.5: Multi-factor reversal exit" not in code:
        code = code[:idx_start] + new_monitor + code[idx_run:]
        print("[4/4] Replaced monitor_positions with V8.5.5 profit-fading exit")
    else:
        print("[4/4] monitor_positions already patched")

    # Update startup message
    old_msg = 'log.info("V8.4 production integration loaded; V8.2 Dhan/orchestration retained")'
    new_msg = 'log.info("V8.4+V8.5.5 loaded; early-entry + profit-protection active")'
    code = code.replace(old_msg, new_msg)

    # Write patched file
    TARGET.write_text(code)
    print(f"\nPatched: {TARGET}")
    print(f"Backup:  {BACKUP}")
    print(f"\n{'='*60}")
    print("IMPORTANT - Also upload the V8.5.5 module:")
    print("  1. SCP the patch file to the server")
    print("  2. RENAME to V8_5_5_EARLY_ENTRY_PROFIT_PROTECTION_PATCH.py")
    print("     (dots->underscores for Python import)")
    print("  3. Run self-test: python3 V8_5_5_EARLY_ENTRY_PROFIT_PROTECTION_PATCH.py")
    print("  4. Restart bot: sudo systemctl restart trading-bot")
    print(f"{'='*60}")
    return True


if __name__ == "__main__":
    patch()
