import ast, shutil, datetime, sys
F = "swing_daily.py"
src = open(F, encoding="utf-8").read()
bak = f"{F}.bak_htmltotals_{datetime.datetime.now():%Y%m%d_%H%M%S}"
shutil.copy(F, bak); print(f"[backup] {bak}")

edits = []

# ── A: drop the separate "New paper entries" blurb; sort active by entry_date ──
a_old = '''        if new_entries:
            H.append("<p><b>New paper entries:</b> " + ", ".join(
                e["ticker"] + " @ Rs." + str(e["entry_price"]) + " (Qty " + str(e["qty"]) + ")" for e in new_entries) + "</p>")
        H.append("<h3 style='color:#2c3e50;'>Active Positions (" + str(len(active)) + ")</h3>")
        if active:'''
a_new = '''        # New entries now render INLINE in the Active table (sorted by entry date) with a NEW badge
        _today_str = date.today().isoformat()
        active = sorted(active, key=lambda _p: str(_p.get("entry_date", "")))
        H.append("<h3 style='color:#2c3e50;'>Active Positions (" + str(len(active)) + ")</h3>")
        if active:'''
edits.append(("A drop-blurb + sort", a_old, a_new))

# ── B: init totals accumulators + NEW badge on ticker cell ──
b_old = '''            H.append("</tr>")
            for p in active:
                ltp, flag = cur_price(p)
                entry = float(p.get("entry_price", 0)); qty = int(p.get("qty", 0))
                pnl = (ltp - entry) * qty
                pnlpct = (ltp / entry - 1) * 100 if entry else 0
                H.append("<tr>")
                H.append("<td style='" + td + "'>" + p["ticker"] + "</td>")'''
b_new = '''            H.append("</tr>")
            _tot_pnl = 0.0; _tot_inv = 0.0
            for p in active:
                ltp, flag = cur_price(p)
                entry = float(p.get("entry_price", 0)); qty = int(p.get("qty", 0))
                pnl = (ltp - entry) * qty
                pnlpct = (ltp / entry - 1) * 100 if entry else 0
                _tot_pnl += pnl; _tot_inv += entry * qty
                _isnew = (str(p.get("entry_date", "")) == _today_str)
                _badge = (" <span style='background:#27ae60;color:#fff;font-size:9px;padding:1px 4px;border-radius:3px;'>NEW</span>" if _isnew else "")
                H.append("<tr>")
                H.append("<td style='" + td + "'>" + p["ticker"] + _badge + "</td>")'''
edits.append(("B totals-init + NEW badge", b_old, b_new))

# ── C: bold TOTAL row before closing the active table ──
c_old = '''                H.append("<td style='" + td + clr(pnlpct) + "'>" + format(pnlpct, "+.2f") + "%</td>")
                H.append("</tr>")
            H.append("</table>")
            H.append("<p style='font-size:10px;color:#888;'>*Est. days'''
c_new = '''                H.append("<td style='" + td + clr(pnlpct) + "'>" + format(pnlpct, "+.2f") + "%</td>")
                H.append("</tr>")
            _tot_pct = (_tot_pnl / _tot_inv * 100) if _tot_inv else 0.0
            H.append("<tr style='font-weight:bold;background:#f4f6f7;'>")
            H.append("<td style='" + td + "' colspan='7'>TOTAL &mdash; " + str(len(active)) + " positions, invested Rs." + format(_tot_inv, ",.0f") + "</td>")
            H.append("<td style='" + td + clr(_tot_pnl) + "'>" + money(_tot_pnl) + "</td>")
            H.append("<td style='" + td + clr(_tot_pct) + "'>" + format(_tot_pct, "+.2f") + "%</td>")
            H.append("</tr>")
            H.append("</table>")
            H.append("<p style='font-size:10px;color:#888;'>*Est. days'''
edits.append(("C total row", c_old, c_new))

for name, old, new in edits:
    n = src.count(old)
    if n != 1:
        print(f"[ABORT] anchor '{name}' matched {n}x (need 1) - no changes written"); sys.exit(1)
    src = src.replace(old, new); print(f"[ok] {name}")

try:
    ast.parse(src)
except SyntaxError as e:
    print(f"[ABORT] does NOT parse: {e} - original untouched (restore {bak})"); sys.exit(1)
open(F, "w", encoding="utf-8").write(src)
print("[DONE] swing HTML totals applied + AST-verified. Backup:", bak)
