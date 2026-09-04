"""
COMBINED REFORM PATCH — 2026-08-04 (v5 — split strategy)
==========================================================
Python patches: short_live.py (R3+R4), patch_integrate.py (R6),
                writes orb_rescan.py (R1+R2+R5+CF)
trading_bot.py: ONLY via 3 printed sed commands (run manually)

Run:
    cd ~/trading-bot && venv/bin/python3 apply_reform_patch_20260804.py
Then run the 3 sed commands it prints.
"""

import shutil, ast, re, sys
from datetime import datetime

BACKUPS = {}

def backup(path):
    ts  = datetime.now().strftime("%H%M%S")
    dst = f"{path}.bak_reform_{ts}"
    shutil.copy(path, dst)
    BACKUPS[path] = dst
    print(f"  ✅ Backup → {dst}")

def read(path):
    with open(path) as f: return f.readlines()

def write(path, lines):
    with open(path, 'w') as f: f.writelines(lines)

def syntax_ok(path):
    with open(path) as f: src = f.read()
    try:
        ast.parse(src); return True, ""
    except SyntaxError as e:
        return False, str(e)

def restore_all():
    for path, bak in BACKUPS.items():
        shutil.copy(bak, path)
        print(f"  ↩️  Restored {path}")

changes = []

# ══════════════════════════════════════════════════════════════════════════════
# FILE 1 — short_live.py  (R3 + R4)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("PATCHING short_live.py  (R3 + R4)")
print("="*65)

SL_PATH = '/home/ubuntu/trading-bot/short_live.py'
backup(SL_PATH)
lines = read(SL_PATH)

# R3a: REGIME_GATES before pick_side
for i, line in enumerate(lines):
    if re.match(r'^def pick_side\s*\(', line):
        if 'REGIME_GATES' not in ''.join(lines[max(0,i-10):i]):
            ins = ['\n','# Reform 2026-08-04: regime-specific confidence gates\n',
                   'REGIME_GATES = {\n',
                   "    'TRENDING_UP':   85, 'BULLISH':       85,\n",
                   "    'TRENDING_DOWN': 85, 'BEARISH':       80,\n",
                   "    'NORMAL':        90, 'CHOPPY':        90,\n",
                   "    'CONSERVATIVE': 999,\n",'}\n','\n']
            lines = lines[:i] + ins + lines[i:]
            changes.append(f"[short_live.py L{i+1}] REGIME_GATES dict")
        break

# R3b: pick_side signature
for i, line in enumerate(lines):
    if re.match(r'^def pick_side\s*\(', line) and 'sector_boost_L' not in line:
        lines[i] = re.sub(r'\)\s*:\s*\n?$', ", sector_boost_L=0, sector_boost_S=0):\n", line)
        changes.append(f"[short_live.py L{i+1}] pick_side(): sector_boost params")
        break

# R3c: L/S init + passes_confidence → REGIME_GATES
in_pick = False
for i, line in enumerate(lines):
    if re.match(r'^def pick_side\s*\(', line): in_pick = True; continue
    if in_pick and re.match(r'^def ', line): break
    if in_pick:
        if re.search(r'^\s+L\s*=\s*long_score if long_score is not None else -1', line):
            lines[i] = line.replace('L = long_score if long_score is not None else -1',
                                    'L = (long_score if long_score is not None else -1) + sector_boost_L')
            changes.append(f"[short_live.py L{i+1}] L += sector_boost_L")
        if re.search(r'^\s+S\s*=\s*short_score if short_score is not None else -1', line):
            lines[i] = line.replace('S = short_score if short_score is not None else -1',
                                    'S = (short_score if short_score is not None else -1) + sector_boost_S')
            changes.append(f"[short_live.py L{i+1}] S += sector_boost_S")
        if 'not passes_confidence(' in line:
            lines[i] = (line
                .replace('not passes_confidence(L)', 'L < REGIME_GATES.get(r, 90)')
                .replace('not passes_confidence(S)', 'S < REGIME_GATES.get(r, 90)')
                .replace('not passes_confidence(score)', 'score < REGIME_GATES.get(r, 90)'))
            changes.append(f"[short_live.py L{i+1}] regime-gate replaces passes_confidence()")

# R4: size_position regime param + mult
for i, line in enumerate(lines):
    if re.match(r'^def size_position\s*\(', line) and 'regime=' not in line:
        lines[i] = re.sub(r'\)\s*:\s*\n?$', ", regime='NORMAL'):\n", line)
        changes.append(f"[short_live.py L{i+1}] size_position(): regime= param")
        for j in range(i+1, i+60):
            if j >= len(lines): break
            if re.search(r'^\s+return qty,\s*leverage,\s*deploy', lines[j]):
                ind = '    '
                mult = (
                    f"{ind}# Reform R4: regime multiplier\n"
                    f"{ind}_rmult = {{'TRENDING_UP':1.25,'BULLISH':1.25,"
                    f"'TRENDING_DOWN':1.25,'BEARISH':0.75}}"
                    f".get((regime or 'NORMAL').upper().strip(), 1.0)\n"
                    f"{ind}if _rmult != 1.0 and leverage > 0:\n"
                    f"{ind}    leverage = min(5.0, max(1.0, round(leverage * _rmult, 1)))\n"
                    f"{ind}    deploy   = balance * leverage\n"
                    f"{ind}    qty      = int(deploy // price) if price > 0 else 0\n"
                    f"{ind}    if qty < 3: return 0, 0, 0\n"
                )
                lines.insert(j, mult)
                changes.append(f"[short_live.py L{j+1}] regime mult block")
                break
        break

write(SL_PATH, lines)
ok, err = syntax_ok(SL_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}"); restore_all(); sys.exit(1)
print(f"  ✅ short_live.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# FILE 2 — patch_integrate.py  (R4 regime to size_position | R6 DH-906 SL)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("PATCHING patch_integrate.py  (R4 + R6)")
print("="*65)

PI_PATH = '/home/ubuntu/trading-bot/patch_integrate.py'
backup(PI_PATH)
lines = read(PI_PATH)

for i, line in enumerate(lines):
    if 'sl_result, sl_actual = place_hard_sl(' in line and '_sl_anchor' not in line:
        indent = ' ' * (len(line) - len(line.lstrip()))
        lines[i] = (
            f"{indent}# Reform R6: re-fetch LTP before SL (fixes DH-906 race condition)\n"
            f"{indent}try:\n"
            f"{indent}    _sl_ltp_d = bot.fetch_ltp_concurrent([str(security_id)])\n"
            f"{indent}    _sl_ltp = float(_sl_ltp_d.get(str(security_id), 0) or 0)\n"
            f"{indent}except Exception:\n"
            f"{indent}    _sl_ltp = 0\n"
            f"{indent}_sl_anchor = price\n"
            f"{indent}if side == 'LONG' and _sl_ltp > 0 and _sl_ltp < price: _sl_anchor = _sl_ltp\n"
            f"{indent}if side == 'SHORT' and _sl_ltp > 0 and _sl_ltp > price: _sl_anchor = _sl_ltp\n"
            f"{indent}sl_result, sl_actual = place_hard_sl(bot.dhan, security_id, qty, _sl_anchor, side)\n"
        )
        changes.append(f"[patch_integrate.py L{i+1}] DH-906: LTP re-fetch before SL")
        break

for i, line in enumerate(lines):
    if 'qty, leverage, deployed = size_position(balance, price, score=score)' in line:
        indent = ' ' * (len(line) - len(line.lstrip()))
        lines[i] = (
            f"{indent}_regime_sz = getattr(bot, 'regime',\n"
            f"{indent}    getattr(bot, 'market_regime', 'NORMAL')) or 'NORMAL'\n"
            f"{indent}qty, leverage, deployed = size_position(\n"
            f"{indent}    balance, price, score=score, regime=_regime_sz)\n"
        )
        changes.append(f"[patch_integrate.py L{i+1}] size_position(): regime param")
        break

write(PI_PATH, lines)
ok, err = syntax_ok(PI_PATH)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}"); restore_all(); sys.exit(1)
print(f"  ✅ patch_integrate.py OK")


# ══════════════════════════════════════════════════════════════════════════════
# FILE 3 — orb_rescan.py  (NEW — R1+R2+R5+CF)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("WRITING orb_rescan.py  (R1+R2+R5+CF)")
print("="*65)

ORB_RESCAN = '/home/ubuntu/trading-bot/orb_rescan.py'
with open(ORB_RESCAN, 'w') as f:
    f.write('''"""
orb_rescan.py — Post-ORB rescan + entry guard + balance persist + candle fallback
Reform 2026-08-04
"""
import json, logging
from datetime import datetime

log = logging.getLogger("trading_bot")
BALANCE_JSON = "/home/ubuntu/trading-bot/last_balance.json"


def persist_balance(balance):
    """R5: Write balance to JSON so restarts don't lose it."""
    try:
        with open(BALANCE_JSON, "w") as f:
            json.dump({"balance": float(balance), "ts": str(datetime.now())}, f)
    except Exception:
        pass


def load_balance():
    """R5: Load last persisted balance. Returns 0.0 if unavailable."""
    try:
        with open(BALANCE_JSON) as f:
            return float(json.load(f).get("balance", 0) or 0)
    except Exception:
        return 0.0


def is_entry_allowed(bot):
    """R2: Return True only after post-ORB rescan has completed."""
    return getattr(bot, "_post_orb_ready", False)


def trigger_post_orb(bot):
    """
    R1: Run select_candidates() at 10:00 IST (04:30 UTC) once per session.
    Sets bot._post_orb_ready = True when done.
    Call this from the main run loop after ORB recording.
    """
    if getattr(bot, "_post_orb_ready", False):
        return True
    utc_now = datetime.utcnow()
    ready = utc_now.hour > 4 or (utc_now.hour == 4 and utc_now.minute >= 30)
    if ready:
        log.info("POST-ORB RESCAN: Re-scoring candidates (10:00 IST, full ATR/VWAP/ST)...")
        try:
            bot.select_candidates()
            bot._post_orb_ready = True
            top_l = [c.get("ticker","?") for c in getattr(bot, "_long_candidates", [])[:5]]
            top_s = [c.get("ticker","?") for c in getattr(bot, "_short_candidates", [])[:5]]
            log.info(f"POST-ORB DONE: {len(bot.candidates)} candidates re-ranked")
            log.info(f"  Top LONG:  {top_l}")
            log.info(f"  Top SHORT: {top_s}")
            return True
        except Exception as e:
            log.warning(f"Post-ORB rescan failed: {e} — entries still blocked")
            return False
    else:
        mins = (4 - utc_now.hour) * 60 + (30 - utc_now.minute) if utc_now.hour < 4 else (30 - utc_now.minute)
        log.info(f"POST-ORB RESCAN pending — ~{max(0,mins)}min to go (10:00 IST / 04:30 UTC)")
        return False


def run_candle_fallback(bot, long_pool, short_pool, check_candles_fn):
    """
    CF: Scan ALL 552 self.watchlist stocks for 3 consecutive green/red 1-min candles.
    Stocks not already in any pool added as CANDLE_MOMENTUM tier. No cap.
    """
    existing = set(str(c.get("security_id", "")) for c in long_pool + short_pool)
    long_added, short_added = [], []
    for stock in getattr(bot, "watchlist", []):
        sid = stock.get("security_id")
        if str(sid) in existing:
            continue
        try:
            if check_candles_fn(sid, "LONG"):
                fc = dict(stock)
                fc.update({"direction":"LONG","tier":"CANDLE_MOMENTUM",
                           "gap_pct":0,"rs":0,"long_score":0,"short_score":0})
                long_pool.append(fc)
                existing.add(str(sid))
                long_added.append(stock.get("ticker","?"))
            elif check_candles_fn(sid, "SHORT"):
                fc = dict(stock)
                fc.update({"direction":"SHORT","tier":"CANDLE_MOMENTUM",
                           "gap_pct":0,"rs":0,"long_score":0,"short_score":0})
                short_pool.append(fc)
                existing.add(str(sid))
                short_added.append(stock.get("ticker","?"))
        except Exception:
            pass
    if long_added:
        log.info(f"CANDLE_MOMENTUM LONG ({len(long_added)}): {long_added}")
    if short_added:
        log.info(f"CANDLE_MOMENTUM SHORT ({len(short_added)}): {short_added}")
    return long_added, short_added
''')

ok, err = syntax_ok(ORB_RESCAN)
if not ok:
    print(f"  ❌ SYNTAX ERROR: {err}"); restore_all(); sys.exit(1)
print(f"  ✅ orb_rescan.py written OK")
changes.append("[orb_rescan.py] NEW module: R1+R2+R5+CF")


# ══════════════════════════════════════════════════════════════════════════════
# trading_bot.py — print sed commands only (no Python patching)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("VERIFICATION:")
with open(SL_PATH) as f: sl = f.read()
with open(PI_PATH) as f: pi = f.read()
with open(ORB_RESCAN) as f: orr = f.read()

checks = [
    ("R3: REGIME_GATES in short_live.py",          "REGIME_GATES" in sl),
    ("R3: pick_side uses regime gates",             "REGIME_GATES.get(r," in sl),
    ("R3: pick_side accepts sector_boost",          "sector_boost_L" in sl),
    ("R4: size_position has regime param",          "regime=" in sl and "_rmult" in sl),
    ("R4: regime passed in patch_integrate",        "_regime_sz" in pi),
    ("R6: DH-906 LTP re-fetch",                    "_sl_anchor" in pi),
    ("R1: trigger_post_orb in orb_rescan.py",      "trigger_post_orb" in orr),
    ("R2: is_entry_allowed in orb_rescan.py",      "is_entry_allowed" in orr),
    ("R5: persist_balance in orb_rescan.py",       "persist_balance" in orr),
    ("CF: run_candle_fallback scans watchlist",     "run_candle_fallback" in orr),
    ("Syntax OK — short_live.py",                  syntax_ok(SL_PATH)[0]),
    ("Syntax OK — patch_integrate.py",             syntax_ok(PI_PATH)[0]),
    ("Syntax OK — orb_rescan.py",                  syntax_ok(ORB_RESCAN)[0]),
]
all_ok = True
for label, passed in checks:
    icon = "✅" if passed else "❌"
    print(f"   {icon} {label}")
    if not passed: all_ok = False

if not all_ok:
    print("\n❌ Checks failed — restoring:"); restore_all(); sys.exit(1)

# Find the scan_for_breakout call line number for sed
TB_PATH = '/home/ubuntu/trading-bot/trading_bot.py'
tb_lines = read(TB_PATH)
scan_line = next((i+1 for i,l in enumerate(tb_lines)
                  if 'long_result = self.scan_for_breakout()' in l
                  and 'orb_rescan' not in l), None)
balance_line = next((i+1 for i,l in enumerate(tb_lines)
                     if 'self._last_balance = balance  # update for dynamic daily loss calc' in l
                     and 'orb_rescan' not in l), None)
import_line  = next((i+1 for i,l in enumerate(tb_lines[:50])
                     if l.startswith('from patch_integrate')), None)

print(f"\n{'='*65}")
print("✅ SHORT_LIVE + PATCH_INTEGRATE + ORB_RESCAN ALL DONE")
print(f"{'='*65}")
print("""
Now run these 3 sed commands for trading_bot.py:
(copy-paste all 3 lines, run on the server)
""")

if import_line:
    print(f"# Step 1 of 3 — Add 'import orb_rescan' (before line {import_line})")
    print(f"sed -i '{import_line}i import orb_rescan  # Reform 2026-08-04' /home/ubuntu/trading-bot/trading_bot.py")
else:
    print("# Step 1 of 3 — Add import (manual — could not find patch_integrate import line)")
    print("sed -i '38i import orb_rescan  # Reform 2026-08-04' /home/ubuntu/trading-bot/trading_bot.py")

print()
if balance_line:
    print(f"# Step 2 of 3 — Persist balance after line {balance_line}")
    print(f"""sed -i '{balance_line}a\\        orb_rescan.persist_balance(self._last_balance)  # Reform R5' /home/ubuntu/trading-bot/trading_bot.py""")
else:
    print("# Step 2 of 3 — balance line not found, skip or add manually")

print()
if scan_line:
    print(f"# Step 3 of 3 — Add post-ORB trigger + entry guard before line {scan_line}")
    print(f"""sed -i '{scan_line}i\\                orb_rescan.trigger_post_orb(self)  # Reform R1' /home/ubuntu/trading-bot/trading_bot.py""")
    # The ternary guard on the scan_for_breakout line
    print(f"""sed -i 's/long_result = self.scan_for_breakout()/long_result = self.scan_for_breakout() if orb_rescan.is_entry_allowed(self) else None  # R2/' /home/ubuntu/trading-bot/trading_bot.py""")
else:
    print("# Step 3 of 3 — scan_for_breakout call line not found, skip or add manually")

print("""
# Step 4 — Verify syntax
venv/bin/python3 -c "import ast; ast.parse(open('trading_bot.py').read()); print('✅ Syntax OK')"

# Step 5 — Restart
sudo systemctl restart trading-bot && sleep 3 && sudo systemctl status trading-bot | head -5
""")
