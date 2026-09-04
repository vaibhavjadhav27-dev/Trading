"""
PATCH: Periodic Rescan — 10:00 IST, then every 15 min until 14:00 IST
========================================================================
Date: 2026-08-07

Fixes the "DEVYANI problem": stocks whose big move starts AFTER the
09:34/09:45 IST scoring checkpoints are never looked at again. This adds
a rolling full-universe rescan every 15 minutes from 10:00 to 14:00 IST,
merging any newly-qualifying stocks into the live watchlist without
disturbing active trades or already-tracked ORBs.

Adds to orb_rescan.py:
  - trigger_periodic_rescan(bot) : call from main loop each cycle;
    self-throttles to fire only once per 15-min slot between 10:00-14:00 IST.

Wiring into trading_bot.py:
  - One call to orb_rescan.trigger_periodic_rescan(self) added right next
    to the existing orb_rescan.trigger_post_orb(self) call in the run loop.

Run: cd ~/trading-bot && venv/bin/python3 apply_patch_periodic_rescan_20260807.py
"""

import shutil, ast, re, sys
from datetime import datetime

changes = []

# ══════════════════════════════════════════════════════════════════════════
# FILE 1: orb_rescan.py — add trigger_periodic_rescan()
# ══════════════════════════════════════════════════════════════════════════
ORB_PATH = '/home/ubuntu/trading-bot/orb_rescan.py'
ts = datetime.now().strftime("%H%M%S")
bak1 = f"{ORB_PATH}.bak_periodic_{ts}"
shutil.copy(ORB_PATH, bak1)
print(f"✅ Backup → {bak1}")

with open(ORB_PATH) as f:
    orb_src = f.read()

PERIODIC_FN = '''

def trigger_periodic_rescan(bot):
    """
    Reform 2026-08-07: Full-universe rescan every 15 min, 10:00-14:00 IST
    (04:30-08:30 UTC). Fixes stocks whose move starts AFTER the
    09:34/09:45 IST scoring checkpoints (e.g. late-morning momentum names
    that were flat/negative at open).

    Self-throttling: fires at most once per 15-min UTC slot. Does NOT
    touch active_trade or already-tracked ORBs -- only re-runs
    select_candidates() to refresh/expand the candidate pool so the
    existing monitoring loop can pick up newly-qualifying names.
    """
    utc_now = datetime.utcnow()

    # Window: 04:30 UTC (10:00 IST) through 08:30 UTC (14:00 IST)
    window_start_min = 4 * 60 + 30
    window_end_min    = 8 * 60 + 30
    now_min = utc_now.hour * 60 + utc_now.minute
    if now_min < window_start_min or now_min > window_end_min:
        return False

    # Throttle to once per 15-min slot (e.g. 04:30, 04:45, 05:00, ...)
    slot = now_min - (now_min % 15)
    last_slot = getattr(bot, "_last_periodic_rescan_slot", None)
    if last_slot == slot:
        return False  # already ran this slot

    bot._last_periodic_rescan_slot = slot
    ist_hh = (slot // 60 + 5) % 24
    ist_mm = (slot % 60 + 30) % 60
    if (slot % 60) + 30 >= 60:
        ist_hh = (ist_hh + 1) % 24

    log.info(f"PERIODIC RESCAN slot {slot} (~{ist_hh:02d}:{ist_mm:02d} IST): "
             f"re-scoring full universe...")
    try:
        _before_long  = set(c.get("security_id") for c in getattr(bot, "_long_candidates", []))
        _before_short = set(c.get("security_id") for c in getattr(bot, "_short_candidates", []))

        bot.select_candidates()

        _after_long  = getattr(bot, "_long_candidates", [])
        _after_short = getattr(bot, "_short_candidates", [])
        _new_long  = [c for c in _after_long  if c.get("security_id") not in _before_long]
        _new_short = [c for c in _after_short if c.get("security_id") not in _before_short]

        if _new_long or _new_short:
            log.info(f"PERIODIC RESCAN: +{len(_new_long)} new LONG "
                      f"{[c.get('ticker','?') for c in _new_long][:5]}, "
                      f"+{len(_new_short)} new SHORT "
                      f"{[c.get('ticker','?') for c in _new_short][:5]}")
        else:
            log.info("PERIODIC RESCAN: no new qualifying candidates this slot")
        return True
    except Exception as e:
        log.warning(f"Periodic rescan failed: {e}")
        return False
'''

if 'trigger_periodic_rescan' not in orb_src:
    orb_src = orb_src.rstrip() + "\n" + PERIODIC_FN
    changes.append("orb_rescan.py: trigger_periodic_rescan() appended")

try:
    ast.parse(orb_src)
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR in orb_rescan.py: {e}")
    sys.exit(1)

with open(ORB_PATH, 'w') as f:
    f.write(orb_src)
print("✅ orb_rescan.py syntax OK")


# ══════════════════════════════════════════════════════════════════════════
# FILE 2: trading_bot.py — wire the call into the run loop
# ══════════════════════════════════════════════════════════════════════════
TB_PATH = '/home/ubuntu/trading-bot/trading_bot.py'
bak2 = f"{TB_PATH}.bak_periodic_{ts}"
shutil.copy(TB_PATH, bak2)
print(f"✅ Backup → {bak2}")

with open(TB_PATH) as f:
    tb_src = f.read()

OLD_TRIGGER = "orb_rescan.trigger_post_orb(self)  # Reform R1"
NEW_TRIGGER = (
    "orb_rescan.trigger_post_orb(self)  # Reform R1\n"
    "                orb_rescan.trigger_periodic_rescan(self)  # Reform 2026-08-07: 15-min rescan 10:00-14:00 IST"
)

if OLD_TRIGGER in tb_src and 'trigger_periodic_rescan' not in tb_src:
    tb_src = tb_src.replace(OLD_TRIGGER, NEW_TRIGGER, 1)
    changes.append("trading_bot.py: trigger_periodic_rescan(self) wired next to trigger_post_orb")
else:
    print("⚠️  Anchor 'orb_rescan.trigger_post_orb(self)  # Reform R1' not found or already patched")

try:
    ast.parse(tb_src)
except SyntaxError as e:
    print(f"❌ SYNTAX ERROR in trading_bot.py: {e}")
    shutil.copy(bak2, TB_PATH)
    sys.exit(1)

with open(TB_PATH, 'w') as f:
    f.write(tb_src)
print("✅ trading_bot.py syntax OK")


# ══════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"✅ {len(changes)} changes applied:")
for c in changes:
    print(f"   {c}")

with open(ORB_PATH) as f: orb_final = f.read()
with open(TB_PATH) as f:  tb_final  = f.read()

print(f"\n{'='*60}")
print("VERIFICATION:")
checks = [
    ("trigger_periodic_rescan() defined in orb_rescan.py", 'def trigger_periodic_rescan' in orb_final),
    ("Self-throttle logic (15-min slot) present",          '_last_periodic_rescan_slot' in orb_final),
    ("Window 10:00-14:00 IST (04:30-08:30 UTC) present",   'window_start_min' in orb_final and 'window_end_min' in orb_final),
    ("trading_bot.py calls trigger_periodic_rescan",        'orb_rescan.trigger_periodic_rescan(self)' in tb_final),
    ("Syntax OK orb_rescan.py",  ast.parse(orb_final) is not None or True),
    ("Syntax OK trading_bot.py", ast.parse(tb_final) is not None or True),
]
all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed:
        all_ok = False

print(f"\n{'='*60}")
if all_ok:
    print("✅ ALL CHECKS PASSED")
    print()
    print("WHAT HAPPENS NOW:")
    print("  10:00, 10:15, 10:30, ... 13:45, 14:00 IST -> full re-score")
    print("  New qualifying stocks (like DEVYANI-type late movers) get")
    print("  merged into _long_candidates / _short_candidates automatically.")
    print("  Existing active trades and ORBs are never touched.")
    print()
    print("Restart:")
    print("  sudo systemctl restart trading-bot && sleep 3 && sudo systemctl status trading-bot | head -5")
else:
    print("❌ SOME CHECKS FAILED — restoring backups")
    shutil.copy(bak1, ORB_PATH)
    shutil.copy(bak2, TB_PATH)
