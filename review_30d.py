import os, gzip, json, glob
os.chdir(os.path.expanduser("~/trading-bot"))

def _load(p):
    try:
        return json.load(gzip.open(p)) if p.endswith(".gz") else json.load(open(p))
    except Exception:
        return None

def syms(v):
    if not isinstance(v, list): return []
    return [ (x.get("symbol") or x.get("ticker")) if isinstance(x, dict) else x for x in v if x ]

# discover every archive we actually have
arch_files = sorted(glob.glob("candle_archive/2026-07-*.json.gz"))
print("="*74)
print("ARCHIVE INVENTORY (what each day actually contains)")
print("="*74)
print(f"{'Date':<12}{'keys present':<52}")
print("-"*74)
days = {}
for f in arch_files:
    d = os.path.basename(f).replace(".json.gz","")
    a = _load(f)
    if not isinstance(a, dict): 
        print(f"{d:<12}(unreadable)"); continue
    ks = list(a.keys())
    days[d] = a
    print(f"{d:<12}{', '.join(ks)}")
print()

print("="*74)
print("PIPELINE REVIEW  (shortlist + gainers read from SAME archive file)")
print("="*74)
print(f"{'Date':<12}{'SL_all':<7}{'SL_top5':<8}{'Gain':<6}{'Hits':<6}Hit list")
print("-"*74)
S={"days":0,"hits":0,"gain":0,"sl":0}
for d in sorted(days):
    a = days[d]
    sl_all  = syms(a.get("shortlist_all"))
    sl_top5 = syms(a.get("shortlist_top5"))
    gain    = syms(a.get("gainers"))
    sl = sl_all or sl_top5
    hits = sorted(set(sl) & set(gain))
    if not (sl or gain): continue
    S["days"]+=1; S["hits"]+=len(hits); S["gain"]+=len(gain); S["sl"]+=len(sl)
    print(f"{d:<12}{len(sl_all):<7}{len(sl_top5):<8}{len(gain):<6}{len(hits):<6}{', '.join(hits) or '-'}")
    if sl and gain:
        missed = sorted(set(gain)-set(sl))
        if missed: print(f"    missed gainers (in gainers, not shortlist): {', '.join(missed[:12])}")
    elif gain and not sl:
        print(f"    NO SHORTLIST captured this day (persist went live 07-16) - gainers: {', '.join(gain[:8])}")
    elif sl and not gain:
        print(f"    NO GAINERS captured this day - shortlist: {', '.join(sl[:8])}")
print("-"*74)
if S["days"] and S["sl"] and S["gain"]:
    pr = 100.0*S["hits"]/S["gain"]
    print(f"SUMMARY: {S['days']} days | shortlisted {S['sl']} | gainers {S['gain']} | hits {S['hits']} ({pr:.0f}% caught)")
    print("NOTE: only days where BOTH are populated are a real comparison.")
else:
    print("SUMMARY: not enough co-populated days yet - shortlist persist went live 07-16,")
    print("so a clean shortlist-vs-gainer pairing starts from today's first live scan onward.")
