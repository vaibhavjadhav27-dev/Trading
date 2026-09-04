"""Data-driven post-market analysis: opening-gap enrichment + cost-aware AI prompt."""
import json
import config

def _get_open_and_prevclose(dhan, sid):
    today_open = None
    try:
        d = dhan.get_ohlc_intraday(str(sid), "NSE_EQ", "5")
        opens = d.get("open") if isinstance(d, dict) else None
        today_open = float(opens[0]) if opens else None
    except Exception:
        pass
    prev_close = None
    try:
        with open("prev_close_cache.json") as f:
            cache = json.load(f)
        data = cache.get("data", cache)
        prev_close = float(data.get(str(sid), 0)) or None
    except Exception:
        pass
    return today_open, prev_close

def classify_gainer(g):
    price = g.get("ltp", 0) or 0
    prev  = g.get("prev_close", 0) or 0
    opn   = g.get("open", 0) or 0
    gap   = ((opn - prev) / prev * 100) if (opn and prev) else None
    floor = getattr(config, "PRICE_FLOOR", 60)
    ceil  = getattr(config, "PRICE_CEIL_TIER1", 5000)
    gmin  = getattr(config, "GAP_MIN", 0.3)
    grej  = getattr(config, "GAP_REJECT", 15.0)
    if price and price < floor:  return False, f"price<{floor} (too cheap)"
    if price and price > ceil:   return False, f"price>{ceil} (above ceiling)"
    if gap is None:              return None,  "no open/prev data"
    if gap < 0:                  return False, f"gapped DOWN {gap:.2f}% (not a gap-up setup)"
    if gap < gmin:               return False, f"gap {gap:.2f}%<{gmin}% (ground-up day, not ORB gap-up)"
    if gap > grej:               return False, f"gap {gap:.2f}%>{grej}% (circuit risk)"
    return True, f"gap {gap:.2f}% VALID -> real miss; check ORB/score/RVOL"

def enrich_gainers(gainers, dhan):
    out = []
    for g in gainers:
        sid = g.get("security_id") or g.get("sid")
        if sid and not g.get("open"):
            opn, prev = _get_open_and_prevclose(dhan, sid)
            if opn:  g["open"] = opn
            if prev and not g.get("prev_close"): g["prev_close"] = prev
        prev = g.get("prev_close", 0) or 0
        opn  = g.get("open", 0) or 0
        g["gap_pct"] = round((opn - prev) / prev * 100, 2) if (opn and prev) else None
        s, r = classify_gainer(g)
        g["is_true_setup"] = s
        g["block_reason"] = r
        out.append(g)
    return out

def format_gainers_table(gainers):
    rows = ["| Stock | LTP | DayChg% | OpenGap% | Classification |", "|---|---|---|---|---|"]
    for g in gainers[:10]:
        s = g.get("is_true_setup")
        tag = "TRUE MISS" if s is True else "not our setup" if s is False else "unknown"
        gp = g.get("gap_pct")
        gps = f"{gp:+.2f}" if isinstance(gp, (int, float)) else "n/a"
        rows.append(f"| {g.get('ticker','?')} | {g.get('ltp','?')} | {g.get('gain_pct',0):+.1f} | {gps} | {tag}: {g.get('block_reason','')} |")
    return "\n".join(rows)

def build_data_driven_prompt(gainers, bot_log):
    from datetime import date
    today = date.today().strftime("%A, %B %d, %Y")
    true_misses = [g for g in gainers if g.get("is_true_setup") is True]
    table = format_gainers_table(gainers)
    risk = getattr(config, "RISK_PER_TRADE_PCT", 2.0)
    gmin = getattr(config, "GAP_MIN", 0.3)
    rvol = getattr(config, "RVOL_THRESHOLD", 2.0)
    return f"""You are an expert NSE intraday ORB trader reviewing {today}. Be strictly data-driven.

=== SYSTEM CONSTRAINTS (never suggest violating these) ===
- ORB GAP-UP system: only trades stocks that GAP UP at open and break the opening range.
  A stock that opened flat/near-flat and ground up during the day is NOT a valid setup
  and was CORRECTLY skipped. It is NOT a miss.
- Product=MIS, round-trip cost floor ~Rs.24. Cost gate: target_gross >= 3x charges.
  A +1-2% mover usually CANNOT clear costs at our size.
- Risk={risk}% (~Rs.1020), one trade/day. GOAL = POSITIVE EXPECTANCY, not trade count.
- A NO-TRADE day on a weak/flat tape is CORRECT and DESIRED. Do NOT recommend loosening
  filters (GAP_MIN={gmin}%, RVOL={rvol}) just to trade more.

=== TOP GAINERS (with OPENING GAP + classification) ===
{table}

TRUE gap-up misses today: {len(true_misses)} of {len(gainers)}
Regime: {bot_log.get('regime','Unknown')} | Shortlisted: {bot_log.get('shortlisted','None')} | Traded: {bot_log.get('traded','No trade')}

Q1. TRUE MISSES ONLY: Of stocks classified 'TRUE MISS', which specific filter blocked each?
    IGNORE 'not our setup' stocks — they were correctly skipped.
Q2. WAS SKIPPING CORRECT? If 0 true misses, state explicitly that no-trade was CORRECT and why.
Q3. COST-VIABILITY: For any true miss, could it clear 3x MIS costs at Rs.1020 risk? If not, correctly filtered.
Q4. REGIME CHECK: Did regime match the tape? (Best gainer only +6% = FLAT tape, not trending.)
Q5. ACTIONABLE ONLY IF WARRANTED: Recommend a filter change ONLY if a cost-viable TRUE miss was
    blocked by a specific threshold — give exact param+value. Else say 'No filter change warranted today.'"""
