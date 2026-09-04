import ast, shutil, datetime, sys
F = "market_intel.py"
src = open(F).read()

# --- A: gainer row builder -> Open/LTP/Intraday%/vs-Prev%/Volume ---
OLD_G = '''            gainers_html = ''.join([
                f"<tr><td>{g['symbol']}</td><td>+{g['pChange']:.2f}%</td><td>Rs.{g['ltp']}</td><td>{g['volume']:,.0f}</td></tr>"
                for g in gainers[:5]
            ])'''
NEW_G = '''            def _intra_pct(g):
                o = g.get('open') or 0
                return ((g.get('ltp', 0) - o) / o * 100) if o else 0.0
            def _grow(g):
                ip = _intra_pct(g)
                ic = 'green' if ip >= 0 else '#c0392b'
                return (f"<tr><td>{g['symbol']}</td>"
                        f"<td>Rs.{g.get('open', 0)}</td>"
                        f"<td>Rs.{g['ltp']}</td>"
                        f"<td style='color:{ic}'>{ip:+.2f}%</td>"
                        f"<td style='color:green'>+{g['pChange']:.2f}%</td>"
                        f"<td>{g['volume']:,.0f}</td></tr>")
            gainers_html = ''.join([_grow(g) for g in gainers[:5]])'''
if OLD_G not in src:
    print("ABORT A: gainers_html builder anchor not found"); sys.exit(1)
src = src.replace(OLD_G, NEW_G, 1)

# --- B: gainer table header -> matching columns ---
OLD_H = '''<h3>Top 5 NSE Gainers</h3>
<table border="1" cellpadding="5"><tr><th>Stock</th><th>Change</th><th>LTP</th><th>Volume</th></tr>
{gainers_html}</table>'''
NEW_H = '''<h3>Top 5 NSE Gainers</h3>
<table border="1" cellpadding="5"><tr><th>Stock</th><th>Open</th><th>LTP</th><th>Intraday %</th><th>vs Prev-Close</th><th>Volume</th></tr>
{gainers_html}</table>
<p style="font-size:10px;color:#888;">Intraday % = LTP vs today's open; vs Prev-Close = official day change.</p>'''
if OLD_H not in src:
    print("ABORT B: gainer table header anchor not found"); sys.exit(1)
src = src.replace(OLD_H, NEW_H, 1)

bak = F + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(F, bak)
try:
    ast.parse(src)
except SyntaxError as e:
    print("SYNTAX ERROR — not writing. " + str(e)); sys.exit(1)
open(F, "w").write(src); ast.parse(open(F).read())
print("OK patched gainer table (Open/LTP/Intraday%/vs-Prev%/Volume). backup=" + bak)
