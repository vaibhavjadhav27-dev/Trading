src = open("trading_bot.py", encoding="utf-8").read()
CHECKS = [
  ("CLV import",               ["from clv_scorer import get_clv_scores"], ["#from clv_scorer import get_clv_scores"]),
  ("CLV bonus in ranking",     ['get_clv_bonus(str(c.get("sid"'],          []),
  ("CLV scores computed",      ["self._clv_scores = get_clv_scores()"],    ["#            self._clv_scores = get_clv_scores()"]),
  ("LTP dict-init",            ["_ltp_result = {}"],                       ["_ltp_result = [{}]"]),
  ("LTP guard#1 removed",      [],                                        ["if isinstance(_ltp_result, list)"]),
  ("ltp_map dict-check",       ["if isinstance(_ltp_result, dict)"],       []),
  ("REST fallback re-enabled", ["bounded REST fallback"],                  ["REST fallback DISABLED"]),
  ("ltp_map guard#2 removed",  [],                                        ["if isinstance(ltp_map, list): ltp_map"]),
  ("ltp_map guard#3 removed",  [],                                        ["if isinstance(ltp_map, list) and len(ltp_map)"]),
  ("scan deadline 5.5h",       ["time.time() + 19800"],                   ["time.time() + 5400"]),
  ("dead-zone log text",       ["Dead zone (12:30-13:45)"],               []),
]
fx = 0
for name, pres, absn in CHECKS:
    ok = all(p in src for p in pres) and all(a not in src for a in absn)
    if ok: fx += 1
    print("  %-26s -> %s" % (name, "FIXED" if ok else "NOT FIXED"))
print("\nSUMMARY: %d/11 FIXED" % fx)
