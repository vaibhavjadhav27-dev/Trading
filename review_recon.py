import json, gzip, glob, os
os.chdir(os.path.expanduser("~/trading-bot"))
try:
    import config
except Exception as e:
    config = None; print("WARN: config import failed:", e)

GAP_MIN = getattr(config, "GAP_MIN", 1.0) if config else 1.0
BYPASS  = getattr(config, "VOLUME_BYPASS_RVOL", 2.5) if config else 2.5
print(f"Using config.GAP_MIN={GAP_MIN}  VOLUME_BYPASS_RVOL={BYPASS}")

def L(p):
    try: return json.load(gzip.open(p)) if p.endswith(".gz") else json.load(open(p))
    except Exception: return None

# --- daily history: {ticker:[{date,open,high,low,close,volume}]} ---
H = L("stock_history_30d.json") or {}
HIST = H.get("stocks", {}) if isinstance(H, dict) else {}
# build per-date -> {ticker:(prev_close, open)}
def day_gaps(date):
    out = {}
    for t, rows in HIST.items():
        if not isinstance(rows, list): continue
        for i in range(1, len(rows)):
            if rows[i].get("date") == date:
                pc = rows[i-1].get("close"); op = rows[i].get("open")
                if pc and op: out[t] = (pc, op, (op-pc)/pc*100)
                break
    return out

# --- NIFTY gap for RS denominator (fallback to 0.3 if absent) ---
def nifty_gap(date, gaps):
    for key in ("NIFTY","NIFTY50","^NSEI"):
        if key in gaps: return gaps[key][2]
    return None

TRADING = [f"2026-07-{d:02d}" for d in (6,7,8,9,10,13,14,15,16)]

print("="*80)
print(f"RECONSTRUCTED SHORTLIST vs GAINERS  ({len(TRADING)} trading days, Jul6-16)")
print("="*80)
print(f"{'Date':<12}{'Univ':<6}{'Short':<7}{'Gain':<6}{'Hits':<6}{'Src':<10}Hit list")
print("-"*80)
S={"hits":0,"gain":0,"short":0,"days":0}
for d in TRADING:
    gaps = day_gaps(d)
    if not gaps: 
        print(f"{d:<12}(no daily history for this date)"); continue
    ng = nifty_gap(d, gaps) or 0.3
    # reconstruct shortlist: gap>=GAP_MIN, rank by RS = gap/nifty_gap
    shortlist = []
    for t,(pc,op,g) in gaps.items():
        if g >= GAP_MIN:
            rs = g/ng if ng>0 else g*2
            shortlist.append((t, round(g,2), round(rs,2)))
    shortlist.sort(key=lambda x:-x[2])
    short_syms = [t for t,_,_ in shortlist]
    # gainers: REAL if new-schema archive, else DERIVED top-% from daily
    arch = L(f"candle_archive/{d}.json.gz")
    if isinstance(arch, dict) and isinstance(arch.get("gainers"), list) and arch["gainers"]:
        gain_syms = [g.get("symbol") for g in arch["gainers"] if g.get("symbol")]
        src = "REAL"
    else:
        ranked = sorted(gaps.items(), key=lambda kv:-kv[1][2])
        gain_syms = [t for t,_ in ranked[:10]]
        src = "derived"
    hits = sorted(set(short_syms) & set(gain_syms))
    S["hits"]+=len(hits); S["gain"]+=len(gain_syms); S["short"]+=len(short_syms); S["days"]+=1
    print(f"{d:<12}{len(gaps):<6}{len(short_syms):<7}{len(gain_syms):<6}{len(hits):<6}{src:<10}{', '.join(hits) or '-'}")
    missed = [g for g in gain_syms if g not in short_syms]
    if missed: print(f"    missed: {', '.join(missed[:10])}")
print("-"*80)
if S["gain"]:
    print(f"SUMMARY: {S['days']} days | shortlisted {S['short']} | gainers {S['gain']} | hits {S['hits']} ({100.0*S['hits']/S['gain']:.0f}% of gainers appeared in shortlist)")
print("\nFIDELITY: gap approx = (open - prev_close)/prev_close (daily) ~ live 09:15 gap.")
print("RVol/FILTERS_V2 gate NOT applied here (no intraday RVol historically) - piece 2.")
print("REAL gainers = 07-15/16 (captured). 'derived' = top-% from daily closes (proxy, not ground-truth).")
