#!/usr/bin/env python3
"""Install MCX V12.4 rebuild patch into an existing trading-bot directory.
Shadow-only. Creates timestamped backups and can run a compile check.
Usage: python3 apply_mcx_v12_rebuild_patch.py /home/ubuntu/trading-bot
"""
from pathlib import Path
from datetime import datetime
import shutil, sys, subprocess

if len(sys.argv) != 2:
    raise SystemExit("Usage: python3 apply_mcx_v12_rebuild_patch.py /path/to/trading-bot")
root = Path(sys.argv[1]).resolve()
patch_src = Path(__file__).with_name("mcx_v12_rebuild_patch.py")
orch = root / "shadow_orchestrator.py"
if not patch_src.is_file(): raise SystemExit("mcx_v12_rebuild_patch.py must be beside this installer")
if not orch.is_file(): raise SystemExit(f"Missing {orch}")
text = orch.read_text()
old = "from mcx_v12_engine import MCXEngine, MCXConfig"
new = "from mcx_v12_rebuild_patch import MCXEngine, MCXConfig"
if old not in text and new not in text:
    raise SystemExit("Expected MCX V12 import anchor not found; refusing to patch unknown orchestrator")
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = root / f"backup_mcx_v12_rebuild_{stamp}"
backup.mkdir()
shutil.copy2(orch, backup / orch.name)
if (root / "mcx_v12_rebuild_patch.py").exists():
    shutil.copy2(root / "mcx_v12_rebuild_patch.py", backup / "mcx_v12_rebuild_patch.py")
shutil.copy2(patch_src, root / "mcx_v12_rebuild_patch.py")
if old in text:
    orch.write_text(text.replace(old, new, 1))
# compile only; never starts trading.
for f in [root / "mcx_v12_rebuild_patch.py", orch]:
    r = subprocess.run([sys.executable, "-m", "py_compile", str(f)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"Compile failed for {f}:\n{r.stderr}")
print("PATCH_OK")
print("backup=", backup)
print("installed=", root / "mcx_v12_rebuild_patch.py")
print("mode=SHADOW_ONLY (live is force-disabled by patch)")
