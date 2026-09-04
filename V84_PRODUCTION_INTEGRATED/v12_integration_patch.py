"""
V12 Integration Patch — Wires Early Discovery + New Exit Engine into trading_bot_v84.py
========================================================================================

This is a ONE-TIME deployment script. Run on the server to patch trading_bot_v84.py.

Changes:
  1. Import: profit_fading_exit from v12_exit_engine (replaces V10.1 version)
  2. Import: EarlyDiscoveryEngine from v12_early_discovery
  3. Init: self.discovery = EarlyDiscoveryEngine() in __init__
  4. Scan loop: Early discovery feature enrichment before final_decision()
  5. DECISION log: discovery_score + phase added to log line

DOES NOT CHANGE:
  - V853 filter, V851 execution, _v10r_gate, V12 shadow, V11 observability
  - acceleration_score still from V10_1_STRATEGY_PATCH
  - update_peak, validate_entry, ExitState, EarlyConfig still from V10_1_STRATEGY_PATCH

Usage:
  python3 v12_integration_patch.py --source-dir /home/ubuntu/trading-bot/V84_PRODUCTION_INTEGRATED
"""

from __future__ import annotations

import os
import re
import sys
import shutil
from datetime import datetime


def patch_file(filepath: str, dry_run: bool = False) -> list:
    """Apply V12 integration patches to trading_bot_v84.py.
    
    Returns list of applied patch descriptions.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    original = content
    applied = []
    
    # ── Patch 1: Change profit_fading_exit import source ──────────────
    # BEFORE:
    #   from V10_1_STRATEGY_PATCH import (
    #       acceleration_score, profit_fading_exit, update_peak,
    #       validate_entry, ExitState, EarlyConfig
    #   )
    # AFTER:
    #   from V10_1_STRATEGY_PATCH import (
    #       acceleration_score, update_peak,
    #       validate_entry, ExitState, EarlyConfig
    #   )
    #   from v12_exit_engine import profit_fading_exit  # V12: volatility-adjusted exit
    
    old_import = "from V10_1_STRATEGY_PATCH import (\n    acceleration_score, profit_fading_exit, update_peak,\n    validate_entry, ExitState, EarlyConfig\n)"
    new_import = """from V10_1_STRATEGY_PATCH import (
    acceleration_score, update_peak,
    validate_entry, ExitState, EarlyConfig
)
from v12_exit_engine import profit_fading_exit  # V12: volatility-adjusted exit"""
    
    if old_import in content:
        content = content.replace(old_import, new_import)
        applied.append("Patch 1: profit_fading_exit import → v12_exit_engine")
    else:
        # Try flexible match
        pattern = r"from V10_1_STRATEGY_PATCH import \(\s*acceleration_score,\s*profit_fading_exit,\s*update_peak,\s*validate_entry,\s*ExitState,\s*EarlyConfig\s*\)"
        if re.search(pattern, content):
            content = re.sub(pattern, 
                "from V10_1_STRATEGY_PATCH import (\n    acceleration_score, update_peak,\n    validate_entry, ExitState, EarlyConfig\n)\nfrom v12_exit_engine import profit_fading_exit  # V12: volatility-adjusted exit",
                content)
            applied.append("Patch 1 (regex): profit_fading_exit import → v12_exit_engine")
        else:
            applied.append("Patch 1: SKIPPED (import pattern not found — may need manual)")
    
    # ── Patch 2: Add early discovery import ──────────────────────────
    discovery_import = "\n# V12 Early Discovery Engine\ntry:\n    from v12_early_discovery import EarlyDiscoveryEngine\n    _V12_DISCOVERY = True\nexcept ImportError:\n    _V12_DISCOVERY = False\n    log.warning('V12 Early Discovery not available')\n"
    
    if "v12_early_discovery" not in content:
        # Insert after the v12_exit_engine import or after the V10.1 import block
        marker = "from v12_exit_engine import profit_fading_exit"
        if marker in content:
            content = content.replace(marker, marker + discovery_import)
            applied.append("Patch 2: Added EarlyDiscoveryEngine import")
        else:
            # Fallback: insert after V10.1 import block
            v10_marker = "from V10_1_STRATEGY_PATCH import"
            idx = content.find(v10_marker)
            if idx >= 0:
                # Find end of the import block (next line that doesn't start with space or ))
                end = content.find("\n)", idx)
                if end >= 0:
                    insert_at = end + 2
                    content = content[:insert_at] + discovery_import + content[insert_at:]
                    applied.append("Patch 2 (fallback): Added EarlyDiscoveryEngine import")
                else:
                    applied.append("Patch 2: SKIPPED (could not find import block end)")
            else:
                applied.append("Patch 2: SKIPPED (V10.1 import not found)")
    else:
        applied.append("Patch 2: SKIPPED (already present)")
    
    # ── Patch 3: Add self.discovery init in __init__ ──────────────────
    discovery_init = """        # V12 Early Discovery Engine
        self.discovery = None
        if _V12_DISCOVERY:
            try:
                self.discovery = EarlyDiscoveryEngine(min_bars=5)
                log.info("V12 Early Discovery Engine initialized (min_bars=5)")
            except Exception as _de:
                log.warning(f"V12 Early Discovery init failed: {_de}")
"""
    
    if "self.discovery" not in content:
        # Insert after self.ei or self.obs init
        for marker in ["self.ei = ExecutionIntegrity", "self.obs = ObservabilityEngine", "self.cm = CandidateManager"]:
            idx = content.find(marker)
            if idx >= 0:
                # Find end of the try/except block for this init
                next_nl = content.find("\n\n", idx)
                if next_nl >= 0:
                    content = content[:next_nl] + "\n" + discovery_init + content[next_nl:]
                    applied.append(f"Patch 3: Added self.discovery init after {marker[:30]}")
                    break
        else:
            applied.append("Patch 3: SKIPPED (no suitable insertion point found)")
    else:
        applied.append("Patch 3: SKIPPED (self.discovery already present)")
    
    # ── Patch 4: Early discovery enrichment in scan loop ──────────────
    # Insert before: d=final_decision(f)
    # Check if features df has < 20 bars, enrich if discovery available
    
    discovery_enrichment = """                        # V12: Early discovery feature enrichment
                        if self.discovery and f.get("df") is not None:
                            try:
                                _df = f["df"]
                                _bars = len(_df) if _df is not None else 0
                                if _bars < 20 and self.discovery.can_score_early(_df):
                                    _prev_close = float(c.get("prev_close", 0) or 0)
                                    f = self.discovery.enrich_features_early(_df, f, _prev_close)
                                    _disc = self.discovery.compute_discovery_score(
                                        c.get("symbol", "?"), _df, f, _prev_close)
                                    c["_discovery_score"] = _disc.get("discovery_score", 0)
                                    c["_discovery_phase"] = _disc.get("phase", "UNKNOWN")
                                    log.info(f"  [V12_DISC] {c.get('symbol','?')} disc={_disc['discovery_score']:.0f} "
                                             f"phase={_disc['phase']} bars={_bars} "
                                             f"rvol={_disc.get('rvol_raw',0):.1f} rs={_disc.get('rs_raw',0):.2f}")
                            except Exception as _disc_e:
                                log.debug(f"V12 discovery error: {_disc_e}")
"""
    
    if "V12_DISC" not in content and "V12: Early discovery" not in content:
        # Find the line: d=final_decision(f) or d = final_decision(f)
        pattern = r"(\s+)(d\s*=\s*None\s*\n\s+try:\s*\n\s+d\s*=\s*final_decision\(f\))"
        match = re.search(pattern, content)
        if match:
            indent = match.group(1)
            content = content[:match.start()] + indent + discovery_enrichment.rstrip() + "\n" + match.group(0) + content[match.end():]
            applied.append("Patch 4: Added early discovery enrichment before final_decision()")
        else:
            # Simpler pattern
            simple = "d=final_decision(f)"
            idx = content.find(simple)
            if idx < 0:
                simple = "d = final_decision(f)"
                idx = content.find(simple)
            if idx >= 0:
                # Find the line start
                line_start = content.rfind("\n", 0, idx)
                content = content[:line_start+1] + discovery_enrichment + content[line_start+1:]
                applied.append("Patch 4 (simple): Added early discovery enrichment before final_decision()")
            else:
                applied.append("Patch 4: SKIPPED (final_decision call not found)")
    else:
        applied.append("Patch 4: SKIPPED (already present)")
    
    # ── Patch 5: Add discovery info to DECISION log line ──────────────
    # Current: log.info(f"DECISION: {c.get('symbol','?'):12s} | ...")
    # Add: disc={c.get('_discovery_score','-')} phase={c.get('_discovery_phase','-')}
    
    if "_discovery_score" not in content or "disc={" not in content:
        # Find the DECISION log line pattern
        decision_pattern = r"(log\.info\(f\"DECISION:.*?pct=\{d\.get\('position_pct',0\)\}\")"
        match = re.search(decision_pattern, content, re.DOTALL)
        if match:
            old_line = match.group(1)
            # Append discovery info before the closing quote
            new_line = old_line[:-2] + " | disc={c.get('_discovery_score','-')} phase={c.get('_discovery_phase','-')}\")"
            content = content.replace(old_line, new_line)
            applied.append("Patch 5: Added discovery_score + phase to DECISION log")
        else:
            applied.append("Patch 5: SKIPPED (DECISION log pattern not found)")
    else:
        applied.append("Patch 5: SKIPPED (already present)")
    
    # ── Write result ──────────────────────────────────────────────────
    if content == original:
        print("  No changes made.")
        return applied
    
    if dry_run:
        print(f"  DRY RUN — would write {len(content)} chars")
        return applied
    
    # Backup
    backup_path = filepath + f".bak_v12_integration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(filepath, backup_path)
    print(f"  Backup: {backup_path}")
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    
    return applied


def main():
    import argparse
    parser = argparse.ArgumentParser(description="V12 Integration Patch")
    parser.add_argument("--source-dir", default=".", help="Directory containing trading_bot_v84.py")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying")
    args = parser.parse_args()
    
    target = os.path.join(args.source_dir, "trading_bot_v84.py")
    if not os.path.exists(target):
        print(f"ERROR: {target} not found")
        sys.exit(1)
    
    print(f"V12 Integration Patch")
    print(f"Target: {target}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()
    
    applied = patch_file(target, dry_run=args.dry_run)
    
    print()
    print("Applied patches:")
    for desc in applied:
        status = "✅" if "SKIPPED" not in desc else "⚠️"
        print(f"  {status} {desc}")
    
    if not args.dry_run:
        # Verify syntax
        print()
        import py_compile
        try:
            py_compile.compile(target, doraise=True)
            print("✅ SYNTAX OK")
        except py_compile.PyCompileError as e:
            print(f"❌ SYNTAX ERROR: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
