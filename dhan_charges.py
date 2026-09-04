"""Dhan MIS (intraday) round-trip charge model -- verified vs dhan.co/pricing."""

def dhan_charges_mis(qty, buy_px, sell_px=None):
    if sell_px is None:
        sell_px = buy_px
    if qty <= 0:
        return 0.0
    buy_val, sell_val = qty * buy_px, qty * sell_px
    turnover = buy_val + sell_val
    brok = min(20.0, 0.0003 * buy_val) + min(20.0, 0.0003 * sell_val)
    stt = round(0.00025 * sell_val)
    txn = 0.0000307 * turnover
    sebi = 0.000001 * turnover
    stamp = round(0.00003 * buy_val)
    gst = 0.18 * (brok + txn + sebi)
    return brok + stt + txn + sebi + stamp + gst

if __name__ == "__main__":
    for q in (15, 54, 100):
        c = dhan_charges_mis(q, 258.2, 260.4)
        print(f"qty={q:3d}  round-trip = Rs.{c:.2f}")
