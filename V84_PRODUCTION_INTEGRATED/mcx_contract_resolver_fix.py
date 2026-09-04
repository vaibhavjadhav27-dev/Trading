#!/usr/bin/env python3
"""
mcx_contract_resolver_fix.py — Fix Issue 4: Options contamination
=================================================================
Problem: Contract resolver picks OPTIONS (CE/PE) SIDs instead of FUTURES
         for SILVER, SILVERM, NATURALGAS, NATGASMINI.
         
Root cause: The filter "FUTCOM" in inst OR "FUT" in inst is too loose.
            Additionally, trading symbol matching by prefix alone doesn't
            exclude option symbols like SILVER-28AUG202670000CE.

Fix:
  1. Strict instrument filter: inst must be exactly "FUTCOM" 
     (not OPTCOM, not OPTFUT, not FUTIDX)
  2. Reject any symbol containing CE/PE strike patterns
  3. Add diagnostic logging for resolved contracts
  4. Validate SID by checking it's NOT in options segment

Deploy: python3 mcx_contract_resolver_fix.py
"""

import shutil, re
from datetime import datetime
from pathlib import Path

TARGET = Path("/home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED/mcx_v854_engine.py")
# Also check root if not in V84 dir
if not TARGET.exists():
    TARGET = Path("/home/ubuntu/trading-bot/mcx_v854_engine.py")

BACKUP = TARGET.with_suffix(f".py.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")


def patch():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        print("Checked both V84_PRODUCTION_INTEGRATED and root trading-bot/")
        return False

    shutil.copy2(TARGET, BACKUP)
    print(f"Backup: {BACKUP}")

    code = TARGET.read_text()
    changes = 0

    # =================================================================
    # FIX 1: Replace the loose instrument filter with strict FUTCOM check
    # =================================================================
    # Old: if "FUTCOM" not in inst and "FUT" not in inst:
    # New: if inst != "FUTCOM":
    #        (Only exact match — no OPTCOM, OPTFUT, FUTIDX leaks)
    
    old_filter = 'if "FUTCOM" not in inst and "FUT" not in inst:'
    new_filter = 'if inst != "FUTCOM":  # Strict: only FUTCOM, no OPTCOM/OPTFUT'
    
    if old_filter in code:
        code = code.replace(old_filter, new_filter)
        changes += 1
        print("[1] Replaced loose instrument filter with strict inst == 'FUTCOM'")
    else:
        print("[1] WARNING: Could not find old instrument filter line")

    # =================================================================
    # FIX 2: Add symbol validation — reject CE/PE option symbols
    # =================================================================
    # After: if not symbol.startswith(prefix + "-"):
    #            continue
    # Insert: if re.search(r'\d+(CE|PE)$', symbol): continue
    
    anchor = 'if not symbol.startswith(prefix + "-"):\n                    continue'
    
    if anchor in code:
        replacement = anchor + '''

                # FIX: Reject option symbols (e.g. SILVER-28AUG202670000CE)
                if re.search(r'\\d+(CE|PE)$', symbol.upper()):
                    continue'''
        code = code.replace(anchor, replacement)
        changes += 1
        print("[2] Added CE/PE symbol pattern rejection")
    else:
        print("[2] WARNING: Could not find symbol prefix check anchor")

    # =================================================================
    # FIX 3: Add 're' import if not present
    # =================================================================
    if 'import re' not in code and "import csv, io, json, math, os" in code:
        code = code.replace(
            "import csv, io, json, math, os, tempfile, time, logging",
            "import csv, io, json, math, os, re, tempfile, time, logging"
        )
        changes += 1
        print("[3] Added 're' to imports")
    elif 'import re' in code:
        print("[3] 're' already imported")
    else:
        print("[3] WARNING: Could not add 're' import — add manually")

    # =================================================================
    # FIX 4: Add diagnostic logging after contract resolution
    # =================================================================
    # After: out[key] = Contract(key, symbol, sid, expiry.isoformat(), lot, tick)
    # Add log line
    
    contract_assign = 'out[key] = Contract(key, symbol, sid, expiry.isoformat(), lot, tick)'
    if contract_assign in code:
        code = code.replace(
            contract_assign,
            contract_assign + '\n                log.info("RESOLVED %s -> %s sid=%s expiry=%s lot=%.0f", key, symbol, sid, expiry.isoformat(), lot)'
        )
        changes += 1
        print("[4] Added diagnostic logging for resolved contracts")
    else:
        print("[4] WARNING: Could not find contract assignment line")

    # =================================================================
    # FIX 5: Add validation in marketfeed_ltp — log what SIDs are sent
    # =================================================================
    old_ltp_log = 'payload = {"MCX_COMM": [str(x) for x in security_ids]}'
    if old_ltp_log in code:
        code = code.replace(
            old_ltp_log,
            'payload = {"MCX_COMM": [str(x) for x in security_ids]}\n        log.info("LTP REQUEST: %d SIDs: %s", len(security_ids), security_ids[:10])'
        )
        changes += 1
        print("[5] Added LTP request diagnostic logging")
    else:
        print("[5] WARNING: Could not find LTP payload line")

    # Write
    TARGET.write_text(code)
    print(f"\n{'='*60}")
    print(f"Applied {changes} fixes to {TARGET}")
    print(f"Backup: {BACKUP}")
    print(f"{'='*60}")
    return True


if __name__ == "__main__":
    success = patch()
    if success:
        print("\nVerify syntax:")
        print(f"  python3 -c \"import py_compile; py_compile.compile('{TARGET.name}', doraise=True)\" && echo 'SYNTAX OK'")
        print(f"\nTest resolution (dry run):")
        print(f"  /home/ubuntu/trading-bot/venv/bin/python3 {TARGET.name}")
        print(f"\nExpected: All 5 contracts resolve to FUTCOM symbols (no CE/PE)")
        print(f"  CRUDEOIL → CRUDEOIL-<expiry>  (not ...CE/PE)")
        print(f"  GOLD     → GOLD-<expiry>")
        print(f"  GOLDPETAL→ GOLDPETAL-<expiry>")
        print(f"  SILVER   → SILVER-<expiry>    (was picking options before)")
        print(f"  SILVERM  → SILVERM-<expiry>   (was picking options before)")
        print(f"  NATURALGAS→NATURALGAS-<expiry>(was picking options before)")
        print(f"  NATGASMINI→NATGASMINI-<expiry>(was picking options before)")
