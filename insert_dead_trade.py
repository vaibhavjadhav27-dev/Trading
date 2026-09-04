import shutil
src = "trading_bot.py"
shutil.copy2(src, src + ".pre_deadtrade2")
with open(src, "r") as f:
    lines = f.readlines()
patch = [
    "        # -- PATCH: Dead trade check (20min at <0.5R = kill) --\n",
    "        if self.active_trade:\n",
    '            _ltp_chk = self.fetch_ltp_concurrent([self.active_trade["security_id"]])\n',
    '            _dtltp = _ltp_chk.get(str(self.active_trade["security_id"]), 0)\n',
    "            if _dtltp > 0 and check_and_kill_dead_trade(self, _dtltp):\n",
    "                return\n",
    "\n",
]
lines[1352:1352] = patch
with open(src, "w") as f:
    f.writelines(lines)
print("Done: dead trade check inserted at line 1353")