import ast, shutil, datetime, sys, re
F = "swing_daily.py"
src = open(F).read()
START = "def send_email(candidates, new_entries, positions):"
i = src.find(START)
if i < 0:
    print("ABORT: send_email not found"); sys.exit(1)
rest = src[i+len(START):]
m = re.search(r"\n(def |class |if __name__)", rest)
end = i + len(START) + (m.start() if m else len(rest))

NEW = r'''def send_email(candidates, new_entries, positions):
    try:
        import boto3, csv, math
        from save_candle_data import fetch_intraday
        ses = boto3.client("ses", region_name="ap-south-1")
        sender = get_parameter("/trading-engine/ses/sender-email")
        recipient = get_parameter("/trading-engine/ses/recipient-email")
        today = date.today().strftime("%d %b %Y")
        active = positions.get("active", [])
        closed = positions.get("closed", [])
        # ---- fresh LTP via Dhan intraday (ws_ltp path is dead) ----
        tickers = [p["ticker"] for p in active]
        sid_map = {}
        try:
            for row in csv.reader(open("watchlist.csv")):
                if not row or row[0] == "ticker": continue
                if row[0] in tickers:
                    try: sid_map[row[0]] = str(int(float(row[1])))
                    except Exception: pass
        except Exception as _e:
            log.warning("watchlist read failed: " + str(_e))
        ltp_map = {}
        for t, sid in sid_map.items():
            try:
                d = fetch_intraday(sid, "NSE_EQ")
                if d and d.get("close"): ltp_map[t] = float(d["close"][-1])
            except Exception: pass
        cmp_map = {c.get("ticker"): c.get("cmp") for c in (candidates or [])}
        rs_map = {c.get("ticker"): c.get("rs_10d", 0) for c in (candidates or [])}
        def cur_price(p):
            t = p["ticker"]
            if ltp_map.get(t, 0) > 0: return ltp_map[t], ""
            if cmp_map.get(t): return float(cmp_map[t]), "*"
            return float(p.get("peak_price", p.get("entry_price", 0))), "**"
        def est_days(p, ltp):
            tgt = float(p.get("target", 0))
            if ltp <= 0 or tgt <= ltp: return "&mdash;"
            rem = (tgt - ltp) / ltp * 100.0
            rs = rs_map.get(p["ticker"], 0) or 0
            dmove = max(0.8, min(3.0, (rs / 10.0) if rs > 0 else 1.5))
            return "~" + str(int(math.ceil(rem / dmove)))
        css = "border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px;"
        th = "background:#2c3e50;color:#fff;padding:6px 8px;text-align:left;border:1px solid #ccc;"
        td = "padding:5px 8px;border:1px solid #ddd;"
        def money(x): return ("+" if x >= 0 else "-") + "Rs." + format(abs(x), ",.0f")
        def clr(x): return "color:green;" if x >= 0 else "color:#c0392b;"
        H = ["<div style='font-family:Arial,sans-serif;'>"]
        H.append("<h2 style='color:#2c3e50;'>Swing Trading Daily Scan &mdash; " + today + "</h2>")
        if new_entries:
            H.append("<p><b>New paper entries:</b> " + ", ".join(
                e["ticker"] + " @ Rs." + str(e["entry_price"]) + " (Qty " + str(e["qty"]) + ")" for e in new_entries) + "</p>")
        H.append("<h3 style='color:#2c3e50;'>Active Positions (" + str(len(active)) + ")</h3>")
        if active:
            H.append("<table style='" + css + "'><tr>")
            for h in ["Stock","Entry date","Est. days*","Buy","Target","SL","Current","P&L Rs.","P&L %"]:
                H.append("<th style='" + th + "'>" + h + "</th>")
            H.append("</tr>")
            for p in active:
                ltp, flag = cur_price(p)
                entry = float(p.get("entry_price", 0)); qty = int(p.get("qty", 0))
                pnl = (ltp - entry) * qty
                pnlpct = (ltp / entry - 1) * 100 if entry else 0
                H.append("<tr>")
                H.append("<td style='" + td + "'>" + p["ticker"] + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("entry_date","")) + "</td>")
                H.append("<td style='" + td + "'>" + est_days(p, ltp) + "</td>")
                H.append("<td style='" + td + "'>" + str(entry) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("target","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("trailing_sl", p.get("sl",""))) + "</td>")
                H.append("<td style='" + td + "'>" + format(ltp, ",.2f") + flag + "</td>")
                H.append("<td style='" + td + clr(pnl) + "'>" + money(pnl) + "</td>")
                H.append("<td style='" + td + clr(pnlpct) + "'>" + format(pnlpct, "+.2f") + "%</td>")
                H.append("</tr>")
            H.append("</table>")
            H.append("<p style='font-size:10px;color:#888;'>*Est. days = rough projection to target at momentum-derived daily move (0.8&ndash;3%/day). Current: no flag=live Dhan intraday; *=candidate CMP; **=last stored price.</p>")
        else:
            H.append("<p>None.</p>")
        H.append("<h3 style='color:#2c3e50;'>Top 15 Candidates</h3>")
        if candidates:
            H.append("<table style='" + css + "'><tr>")
            for h in ["#","Stock","Score","CMP","RS %","RVOL","R:R"]:
                H.append("<th style='" + th + "'>" + h + "</th>")
            H.append("</tr>")
            for idx, c in enumerate(candidates[:15]):
                H.append("<tr>")
                H.append("<td style='" + td + "'>" + str(idx+1) + "</td>")
                H.append("<td style='" + td + "'>" + str(c.get("ticker","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(c.get("score","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(c.get("cmp","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(c.get("rs_10d","")) + "%</td>")
                H.append("<td style='" + td + "'>" + str(c.get("rvol","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(c.get("rr_ratio","")) + "</td>")
                H.append("</tr>")
            H.append("</table>")
        else:
            H.append("<p>None.</p>")
        H.append("<h3 style='color:#2c3e50;'>Closed Trades (" + str(len(closed)) + ")</h3>")
        if closed:
            wins = [p for p in closed if p.get("pnl", 0) > 0]
            H.append("<p><b>Win rate:</b> " + str(len(wins)) + "/" + str(len(closed)) + " (" + format(len(wins)/len(closed)*100, ".0f") + "%)</p>")
            H.append("<table style='" + css + "'><tr>")
            for h in ["Stock","Entry","Exit","Days","Buy","Exit Px","P&L Rs.","P&L %","Reason"]:
                H.append("<th style='" + th + "'>" + h + "</th>")
            H.append("</tr>")
            for p in closed:
                pnl = float(p.get("pnl", 0)); pnlpct = float(p.get("pnl_pct", 0))
                H.append("<tr>")
                H.append("<td style='" + td + "'>" + str(p.get("ticker","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("entry_date","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("exit_date","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("days_held","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("entry_price","")) + "</td>")
                H.append("<td style='" + td + "'>" + str(p.get("exit_price","")) + "</td>")
                H.append("<td style='" + td + clr(pnl) + "'>" + money(pnl) + "</td>")
                H.append("<td style='" + td + clr(pnlpct) + "'>" + format(pnlpct, "+.2f") + "%</td>")
                H.append("<td style='" + td + "'>" + str(p.get("exit_reason","")) + "</td>")
                H.append("</tr>")
            H.append("</table>")
        else:
            H.append("<p>No closed trades yet.</p>")
        H.append("</div>")
        html = "".join(H)
        subj = "Swing Scan: " + str(len(new_entries)) + " new | " + str(len(active)) + " active | " + today
        ses.send_email(Source=sender, Destination={"ToAddresses": [recipient]},
                       Message={"Subject": {"Data": subj}, "Body": {"Html": {"Data": html}}})
        log.info("Email sent successfully")
    except Exception as e:
        log.warning("Email failed: " + str(e))
'''

new = src[:i] + NEW.rstrip("\n") + "\n" + src[end:]
bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try:
    ast.parse(new)
except SyntaxError as e:
    print("SYNTAX ERROR — not writing. " + str(e)); sys.exit(1)
open(F, "w").write(new); ast.parse(open(F).read())
print("OK patched swing email. backup=" + bak)
