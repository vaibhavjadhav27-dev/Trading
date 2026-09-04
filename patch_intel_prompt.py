import ast, shutil, datetime, sys
F = "market_intel.py"
src = open(F).read()

# --- A: enrich gainers_text with intraday% + vs-prev% (anchor verbatim from discovery) ---
OLD_G = '''        gainers_text = chr(10).join([
            f"  {i+1}. {g['symbol']}: +{g['pChange']:.2f}%, LTP Rs.{g['ltp']}, Vol {g['volume']:,.0f}, Value Rs.{g['value_cr']:.1f}Cr"
            for i, g in enumerate(top_gainers[:5])
        ])'''
NEW_G = '''        def _intra(g):
            o = g.get('open') or 0
            return ((g.get('ltp',0) - o) / o * 100) if o else 0
        gainers_text = chr(10).join([
            f"  {i+1}. {g['symbol']}: Open Rs.{g.get('open',0)} -> LTP Rs.{g['ltp']} | Intraday {_intra(g):+.2f}% | vs-PrevClose +{g['pChange']:.2f}% | Vol {g['volume']:,.0f} | Value Rs.{g['value_cr']:.1f}Cr"
            for i, g in enumerate(top_gainers[:5])
        ])'''
if OLD_G not in src:
    print("ABORT A: gainers_text anchor not found"); sys.exit(1)
src = src.replace(OLD_G, NEW_G, 1)

# --- B: read shortlist from pending_{date}.json (today's wired capture) ---
OLD_S = '''        missed_text = chr(10).join(['''
NEW_S = '''        # shortlist from scan-time capture (populates from first live 09:15 scan onward)
        _short_text = "  (no shortlist captured yet - scan-time write fires from tomorrow's 09:15 run)"
        try:
            import glob
            _pf = f"candle_archive/pending_{date.today()}.json"
            if os.path.exists(_pf):
                _pj = json.load(open(_pf))
                _rows = _pj.get("candidates", [])
                if _rows:
                    _short_text = chr(10).join([
                        f"  #{r.get('rank')}: {r.get('symbol')} | gap {r.get('gap')}% | RS {r.get('rs')} | score {r.get('score')} | tier {r.get('tier')} | kept={r.get('kept')}"
                        for r in _rows[:15]])
        except Exception as _e:
            _short_text = f"  (shortlist read failed: {_e})"

        missed_text = chr(10).join(['''
if OLD_S not in src:
    print("ABORT B: missed_text anchor not found"); sys.exit(1)
src = src.replace(OLD_S, NEW_S, 1)

# --- C: replace stale filter line + 5 generic questions with grounded ones ---
OLD_P = '''Our filter criteria: Price Rs.100-5000, Gap 0.3%-5%, ORB range >1%, 30-sec breakout confirmation.

Please analyze:
1. WHAT DROVE THE TOP GAINERS: News, sector rotation, FII buying, results, crude/USD impact?
2. WHY WE MISSED THEM: Would any filter change have caught them? What specific criteria failed?
3. PATTERN OBSERVATION: Any common patterns in today's gainers (sector, cap-size, technical setup)?
4. FILTER IMPROVEMENT: Specific actionable changes to catch more winners without increasing false signals.
5. TOMORROW'S WATCHLIST: Based on today's patterns, which stocks/sectors to watch tomorrow?

Be specific with numbers and reasoning."""'''
NEW_P = '''== OUR ACTUAL FILTER THRESHOLDS (config.py) ==
  Price floor Rs.60; ceiling Rs.5000 (tier1) / Rs.1500 (tier2)
  Gap: 0.3% min to 8% max (reject >15%); tier2 gap min 1.0%
  RVOL threshold >=2.0 (RVOL >2.5 bypasses gap minimum)
  ORB range: 0.8%-3.0% of price
  ADT (avg daily turnover): sweet spot Rs.50-70Cr; min Rs.5Cr

== OUR SHORTLISTED CANDIDATES (scan-time capture) ==
{_short_text}

== ECONOMICS (weigh before recommending we "trade more") ==
  MIS round-trip cost ~Rs.24/trade; we require reward >= 3x cost.
  Goal is EXPECTANCY, not frequency. On weak/CONSERVATIVE tape, NO-TRADE is the CORRECT outcome, not a miss.
  You are ALERT/ANALYSIS only - never recommend autonomous live orders; all signals are for manual decision.

Please analyze:
1. WHAT DROVE THE TOP GAINERS: News, sector rotation, FII buying, results, crude/USD impact? Be specific.
2. WHY WE MISSED EACH: For EVERY missed gainer, name the ONE specific criterion that most likely rejected it (price / gap / RVOL / ORB-range / ADT) and state its value vs our threshold. If you lack a field, say which field is needed - do NOT guess vaguely.
3. SELECTION ACCURACY: Compare OUR shortlisted candidates above vs the actual top gainers. Which did we rank well? Which gainers were absent from our shortlist and why? Was our ranking order justified by outcome?
4. ENTRY/EXIT TIMING (patterns, not price levels): Describe the winning setups in terms of VWAP position, RVOL, volume profile, and ORB-range behaviour - the repeatable signal, not the rupee level.
5. ONE FILTER CHANGE FOR TOMORROW: A single, specific, testable threshold change (e.g. "lower ADT_MIN to Rs.40Cr") with the expected trade-off in false signals. Only if the economics above justify it.

Be specific with numbers and reasoning."""'''
if OLD_P not in src:
    print("ABORT C: prompt-questions anchor not found"); sys.exit(1)
src = src.replace(OLD_P, NEW_P, 1)

bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try:
    ast.parse(src)
except SyntaxError as e:
    print("SYNTAX ERROR — not writing. " + str(e)); sys.exit(1)
open(F, "w").write(src); ast.parse(open(F).read())
print("OK patched intel prompt (gainers_text + shortlist + questions). backup=" + bak)
