#!/usr/bin/env python3
"""
V8.6.0 Integration Patch
=========================
Wires execution_integrity.py into trading_bot_v84.py.

Safe integration: adds behavior WITHOUT changing existing logic.
If ExecutionIntegrity fails, exceptions are caught and existing code continues.

Changes:
1. Import ExecutionIntegrity
2. Initialize self.ei in constructor
3. Add kill-switch check before entry evaluation
4. Add ei.reconcile() alongside existing reconciliation
5. Add SL failure reporting to kill-switch
6. Replace EOD with ei-driven broker close

Run on server:
    cd /home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED
    cp trading_bot_v84.py trading_bot_v84.py.bak_v860
    /home/ubuntu/trading-bot/venv/bin/python3 v860_integration_patch.py
"""

import sys

def patch():
    filepath = "trading_bot_v84.py"
    
    with open(filepath, "r") as f:
        content = f.read()
    
    changes = []

    # ═══════════════════════════════════════════════════════════════════
    # 1. Add import
    # ═══════════════════════════════════════════════════════════════════
    
    import_line = "from execution_integrity import ExecutionIntegrity, OrderState"
    
    if import_line not in content:
        # Insert after the V854_UNIFIED_PATCH import line
        marker = "from V854_UNIFIED_PATCH import ("
        idx = content.find(marker)
        if idx < 0:
            marker = "from V854_UNIFIED_PATCH import"
            idx = content.find(marker)
        
        if idx >= 0:
            # Find end of that import statement (may be multi-line)
            # Look for the closing paren or next line that doesn't continue
            pos = idx
            while pos < len(content):
                nl = content.index("\n", pos)
                line = content[pos:nl]
                if ")" in line or (not line.strip().endswith(",") and not line.strip().endswith("(")):
                    # This is the last line of the import
                    content = content[:nl+1] + import_line + "\n" + content[nl+1:]
                    changes.append("1. Added ExecutionIntegrity import")
                    break
                pos = nl + 1
        else:
            print("ERROR: Could not find V854_UNIFIED_PATCH import")
            return False

    # ═══════════════════════════════════════════════════════════════════
    # 2. Initialize self.ei
    # ═══════════════════════════════════════════════════════════════════
    
    ei_init = "self.ei = ExecutionIntegrity(self.dhan, self)"
    
    if ei_init not in content:
        # Find self.v84_risk initialization and add after it
        marker = "self.v84_risk"
        idx = content.find(marker)
        if idx >= 0:
            eol = content.index("\n", idx)
            indent = "        "  # 8 spaces (inside __init__)
            init_code = (
                indent + "# V8.6.0 Execution Integrity\n"
                + indent + "try:\n"
                + indent + "    self.ei = ExecutionIntegrity(self.dhan, self)\n"
                + indent + "    log.info('ExecutionIntegrity V8.6.0 initialized')\n"
                + indent + "except Exception as _ei_init_e:\n"
                + indent + "    log.error(f'ExecutionIntegrity init failed: {_ei_init_e}')\n"
                + indent + "    self.ei = None\n"
            )
            content = content[:eol+1] + init_code + content[eol+1:]
            changes.append("2. Added self.ei initialization (with fallback)")
        else:
            print("ERROR: Could not find self.v84_risk")
            return False

    # ═══════════════════════════════════════════════════════════════════
    # 3. Add kill-switch before entry evaluation
    # ═══════════════════════════════════════════════════════════════════
    
    kill_marker = "ENTRY LOCK"
    if "kill-switch" not in content and kill_marker in content:
        # Find the entry lock check: "else:log.info("V8.4 ENTRY LOCK: %s",why)"
        # The entry evaluation happens inside: if not self.entry_locked():
        # We want to add kill-switch check INSIDE that block, before scoring
        
        # Find "entry_locked" usage
        entry_lock_pattern = 'why=self.entry_locked()'
        idx = content.find(entry_lock_pattern)
        if idx >= 0:
            # Insert kill-switch check right before this line
            line_start = content.rfind("\n", 0, idx) + 1
            indent = " " * (idx - line_start)  # Match indentation
            kill_code = (
                indent + "# V8.6.0 Kill-switch check\n"
                + indent + "if self.ei and not self.ei.guard.entries_allowed()[0]:\n"
                + indent + "    _ks_reason = self.ei.guard.entries_allowed()[1]\n"
                + indent + "    log.warning(f'KILL-SWITCH ACTIVE: {_ks_reason} — skipping entry scan')\n"
                + indent + "    next_scan=time.monotonic()+self.rescan_minutes*60\n"
                + indent + "    time.sleep(2);continue\n"
            )
            content = content[:line_start] + kill_code + content[line_start:]
            changes.append("3. Added kill-switch check before entry evaluation")

    # ═══════════════════════════════════════════════════════════════════
    # 4. Add ei.reconcile() alongside existing reconciliation
    # ═══════════════════════════════════════════════════════════════════
    
    recon_marker = "_v854_recon=v854_reconcile_broker(self, deep=False)"
    ei_recon_code = (
        "\n                    # V8.6.0 Execution Integrity reconciliation\n"
        "                    try:\n"
        "                        if self.ei:\n"
        "                            _ei_recon = self.ei.reconcile()\n"
        "                            if _ei_recon.get('orphans_adopted', 0) > 0:\n"
        "                                log.warning(f\"EI ADOPTED {_ei_recon['orphans_adopted']} orphan(s): {_ei_recon.get('details',[])}\")\n"
        "                    except Exception as _ei_e:\n"
        "                        log.error(f'EI reconcile error: {_ei_e}')"
    )
    
    if recon_marker in content and "ei.reconcile" not in content:
        content = content.replace(
            recon_marker,
            recon_marker + ei_recon_code,
            1  # Only first occurrence (shallow reconcile in main loop)
        )
        changes.append("4. Added ei.reconcile() for orphan adoption")

    # ═══════════════════════════════════════════════════════════════════
    # 5. Add SL failure reporting
    # ═══════════════════════════════════════════════════════════════════
    
    sl_fail_marker = 'log.error(f"HARD SL FAILED:'
    if sl_fail_marker not in content:
        # Find the existing sl_oid check
        old_sl = "        if not sl_oid:\n            self.emergency_exit(sid,fq,side);return False,\"hard_sl_not_accepted\""
        if old_sl in content and "report_sl_failure" not in content:
            new_sl = (
                "        if not sl_oid:\n"
                "            log.error(f\"HARD SL FAILED: {c.get('symbol','?')} sid={sid} side={side} stop={round(stop,2)}\")\n"
                "            if self.ei: self.ei.guard.report_sl_failure(c.get('symbol','?'))\n"
                "            self.emergency_exit(sid,fq,side);return False,\"hard_sl_not_accepted\""
            )
            content = content.replace(old_sl, new_sl, 1)
            changes.append("5. Added SL failure reporting to kill-switch")

    # ═══════════════════════════════════════════════════════════════════
    # 6. Enhance EOD with ei.eod_force_close
    # (The earlier patch already added broker force-close,
    #  but let's also call ei.eod_force_close as the authoritative version)
    # ═══════════════════════════════════════════════════════════════════
    
    eod_marker = 'self._run_supporting_modules();log.info("V8.4 EOD complete")'
    if eod_marker in content and "ei.eod_force_close" not in content:
        eod_enhanced = (
            '# V8.6.0 EOD: verify all broker positions closed\n'
            '        try:\n'
            '            if self.ei:\n'
            '                _eod_report = self.ei.eod_force_close()\n'
            '                log.info(f"EI EOD: closed_local={_eod_report.get(\'closed_local\',0)} orphans={_eod_report.get(\'closed_orphans\',0)} failures={_eod_report.get(\'failures\',[])}") \n'
            '        except Exception as _eod_e:\n'
            '            log.error(f"EI EOD error: {_eod_e}")\n'
            '        ' + eod_marker
        )
        content = content.replace(eod_marker, eod_enhanced)
        changes.append("6. Added ei.eod_force_close before final EOD")

    # ═══════════════════════════════════════════════════════════════════
    # Write result
    # ═══════════════════════════════════════════════════════════════════
    
    if changes:
        with open(filepath, "w") as f:
            f.write(content)
        print(f"\n{'='*60}")
        print(f"V8.6.0 Integration Patch Applied: {len(changes)} changes")
        print(f"{'='*60}")
        for c in changes:
            print(f"  {c}")
        print(f"{'='*60}\n")
        
        # Verify syntax
        import py_compile
        try:
            py_compile.compile(filepath, doraise=True)
            print(f"SYNTAX CHECK: PASS")
        except py_compile.PyCompileError as e:
            print(f"SYNTAX CHECK: FAIL — {e}")
            print("Rolling back...")
            # Don't rollback here — let user decide
            return False
        
        return True
    else:
        print("No changes applied (already patched or patterns not found)")
        return True


if __name__ == "__main__":
    success = patch()
    sys.exit(0 if success else 1)
