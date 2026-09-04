"""
Patch script — 2026-08-04
Fixes:
  1. Import: remove check_and_kill_dead_trade
  2. scan_for_breakdown(): restrict to _short_candidates only (3 lines)
  3. Remove artificial _sscore injection block (was fabricating 60.0 score)
  4. Remove DEAD_TRADE block — Place B (LONG monitor, line ~1550)
  5. Remove DEAD_TRADE gate — Place C (SHORT monitor, line ~2413)

Run on server:
    cd /home/ubuntu/trading-bot
    python3 apply_patch_20260804.py
"""

import shutil
import sys

filepath = '/home/ubuntu/trading-bot/trading_bot.py'
backup   = filepath + '.bak_20260804'

# ── Safety backup ──────────────────────────────────────────────────────────────
shutil.copy(filepath, backup)
print(f"✅ Backup saved → {backup}")

with open(filepath, 'r') as f:
    lines = f.readlines()

changes = []

# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 1 — Remove check_and_kill_dead_trade from import (line 38)
# ══════════════════════════════════════════════════════════════════════════════
for i, line in enumerate(lines):
    if 'from patch_integrate import' in line and 'check_and_kill_dead_trade' in line:
        lines[i] = line.replace(', check_and_kill_dead_trade', '')
        changes.append(f"[Line {i+1}] Removed check_and_kill_dead_trade from import")
        break

# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 2 — scan_for_breakdown(): use _short_pool instead of self.candidates
#   2a. Insert _short_pool var + fix sids list comp  (line ~2084)
#   2b. Fix rank_of dict                             (line ~2098)
#   2c. Fix for-loop                                 (line ~2102)
# ══════════════════════════════════════════════════════════════════════════════

# 2a — find the sids line inside scan_for_breakdown and insert _short_pool before it
breakdown_func_idx = None
for i, line in enumerate(lines):
    if '    def scan_for_breakdown(self):' in line:
        breakdown_func_idx = i
        break

if breakdown_func_idx is None:
    print("❌ Could not find scan_for_breakdown — aborting")
    sys.exit(1)

# Search for the sids list-comp within 40 lines of the function start
for j in range(breakdown_func_idx, breakdown_func_idx + 40):
    if "sids = [str(c['security_id']) for c in self.candidates" in lines[j]:
        indent = '        '   # 8 spaces (matches existing indentation)
        new_lines = [
            f"{indent}# FIX 2026-08-04: only scan SHORT-pool stocks\n",
            f"{indent}# LONG candidates must never be shorted via breakdown\n",
            f"{indent}_short_pool = getattr(self, '_short_candidates', None) or self.candidates\n",
        ]
        lines[j] = lines[j].replace('self.candidates', '_short_pool')
        lines = lines[:j] + new_lines + lines[j:]
        changes.append(f"[Line {j+1}] Inserted _short_pool var; updated sids list-comp")
        break

# 2b & 2c — after insertion, scan within scan_for_breakdown for rank_of and for-loop
in_fn = False
for i, line in enumerate(lines):
    if '    def scan_for_breakdown(self):' in line:
        in_fn = True
        continue
    if in_fn and line.startswith('    def ') and 'scan_for_breakdown' not in line:
        break   # left the function
    if in_fn:
        if "rank_of = {c['security_id']: i for i, c in enumerate(self.candidates)}" in line:
            lines[i] = line.replace('self.candidates', '_short_pool')
            changes.append(f"[Line {i+1}] rank_of: self.candidates → _short_pool")
        if 'for candidate in self.candidates:' in line:
            lines[i] = line.replace('self.candidates', '_short_pool')
            changes.append(f"[Line {i+1}] for-loop: self.candidates → _short_pool")

# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 3 — Remove artificial _sscore injection block (~lines 2474-2480)
#   if (_sscore is None or _sscore == 0.0) and short_result:
#       _s_expr = ...
#       if _s_expr ...
#           _sscore = min(...)
#           log.info(...)
# ══════════════════════════════════════════════════════════════════════════════
i = 0
while i < len(lines):
    if 'if (_sscore is None or _sscore == 0.0) and short_result:' in lines[i]:
        block_indent = len(lines[i]) - len(lines[i].lstrip())
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped == '':
                break
            curr_indent = len(lines[j]) - len(lines[j].lstrip())
            if curr_indent <= block_indent:
                break
            j += 1
        removed = j - i
        del lines[i:j]
        changes.append(f"[Line {i+1}] Removed _sscore injection block ({removed} lines)")
        break
    i += 1

# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 4 — Remove DEAD_TRADE block — Place B (~line 1550)
#   # -- PATCH: Dead trade check (20min at <0.5R = kill) --
#   if self.active_trade:
#       _ltp_chk = ...
#       _dtltp = ...
#       if _dtltp > 0 and check_and_kill_dead_trade(self, _dtltp):
#           return
# ══════════════════════════════════════════════════════════════════════════════
i = 0
while i < len(lines):
    if '# -- PATCH: Dead trade check (20min at <0.5R = kill) --' in lines[i]:
        block_indent = len(lines[i]) - len(lines[i].lstrip())
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped == '':
                break
            curr_indent = len(lines[j]) - len(lines[j].lstrip())
            if curr_indent < block_indent:
                break
            j += 1
        removed = j - i
        del lines[i:j]
        changes.append(f"[Line {i+1}] Removed DEAD_TRADE block Place-B ({removed} lines)")
        break
    i += 1

# ══════════════════════════════════════════════════════════════════════════════
# CHANGE 5 — Remove DEAD_TRADE gate — Place C (~line 2413)
#   if not check_and_kill_dead_trade(self, _sm_ltp):
#       side_aware_monitor(self, _sm_ltp)
#   →
#   side_aware_monitor(self, _sm_ltp)
# ══════════════════════════════════════════════════════════════════════════════
i = 0
while i < len(lines):
    if 'if not check_and_kill_dead_trade(self, _sm_ltp):' in lines[i]:
        gate_indent = len(lines[i]) - len(lines[i].lstrip())
        # next line is side_aware_monitor with +4 extra indent — dedent it
        lines[i+1] = ' ' * gate_indent + lines[i+1].lstrip()
        del lines[i]   # remove the if-gate line
        changes.append(f"[Line {i+1}] Removed check_and_kill_dead_trade gate (Place-C); kept side_aware_monitor")
        break
    i += 1

# ── Write back ─────────────────────────────────────────────────────────────────
with open(filepath, 'w') as f:
    f.writelines(lines)

# ── Report ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"✅ Patch applied — {len(changes)} changes:")
for c in changes:
    print(f"   {c}")

# ── Verification ───────────────────────────────────────────────────────────────
with open(filepath, 'r') as f:
    result = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")

checks = [
    ("check_and_kill_dead_trade FULLY REMOVED",
     result.count('check_and_kill_dead_trade') == 0),
    ("_short_pool inserted in scan_for_breakdown",
     '_short_pool' in result),
    ("sids list-comp uses _short_pool",
     "sids = [str(c['security_id']) for c in _short_pool" in result),
    ("rank_of uses _short_pool",
     "rank_of = {c['security_id']: i for i, c in enumerate(_short_pool)}" in result),
    ("for-loop uses _short_pool",
     'for candidate in _short_pool:' in result),
    ("_sscore injection block removed",
     '(_sscore is None or _sscore == 0.0)' not in result),
    ("DEAD_TRADE block Place-B removed",
     '# -- PATCH: Dead trade check (20min at <0.5R = kill) --' not in result),
    ("DEAD_TRADE gate Place-C removed",
     'if not check_and_kill_dead_trade(self, _sm_ltp):' not in result),
]

all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED — safe to restart the bot")
else:
    print("❌ SOME CHECKS FAILED — review above, restore from backup if needed:")
    print(f"   cp {backup} {filepath}")
