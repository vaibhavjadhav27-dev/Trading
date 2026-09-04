#!/usr/bin/env python3
"""
MCX Virtual Trader — tracks shadow positions with SL + progressive trailing.
Standalone module, wired into mcx_v12_engine.py via callback hooks.

Writes trades to mcx_virtual_trades.jsonl for expert review.
"""

import json, os, logging, time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("mcx_virtual")

IST = timezone(timedelta(hours=5, minutes=30))

# MCX lot sizes
MCX_LOT_SIZES = {
    "CRUDEOIL": 100,   # 100 barrels
    "GOLD": 100,       # 100 grams (1 kg lot)
    "GOLDPETAL": 1,    # 1 gram
    "SILVER": 30,      # 30 kg
    "SILVERM": 5,      # 5 kg
    "NATURALGAS": 1250,  # 1250 MMBtu
    "NATGASMINI": 250,   # 250 MMBtu
}

# Progressive trailing milestones (same as NSE V8.5.4)
TRAIL_MILESTONES = [
    (3.0, 2.25),   # at 3R → trail to 2.25R
    (2.5, 1.75),   # at 2.5R → trail to 1.75R
    (2.0, 1.25),   # at 2R → trail to 1.25R
    (1.0, 0.50),   # at 1R → trail to 0.5R
]


class MCXVirtualTrader:
    """Track virtual MCX positions with SL + progressive trailing."""

    def __init__(self, capital: float = 200000, max_positions: int = 5,
                 max_risk_pct: float = 0.02, base_path: str = "."):
        self.capital = capital
        self.max_positions = max_positions
        self.max_risk_pct = max_risk_pct
        self.positions: Dict[str, dict] = {}
        self.closed: List[dict] = []
        self.ledger_path = os.path.join(base_path, "mcx_virtual_trades.jsonl")
        self._summary_path = os.path.join(base_path,
                                          f"mcx_virtual_summary_{datetime.now(IST).strftime('%Y-%m-%d')}.json")

    def on_signal(self, symbol: str, side: str, price: float, sl: float,
                  lot_size: int, reason: str = "", score: float = 0,
                  **kwargs) -> bool:
        """Open a virtual position on signal. Returns True if opened."""
        if symbol in self.positions:
            log.info(f"[VIRTUAL] SKIP {symbol}: already in position")
            return False
        if len(self.positions) >= self.max_positions:
            log.info(f"[VIRTUAL] SKIP {symbol}: max positions ({self.max_positions})")
            return False

        qty = lot_size or MCX_LOT_SIZES.get(symbol.split("-")[0], 1)
        risk_per_lot = abs(price - sl) * qty
        if risk_per_lot > self.capital * self.max_risk_pct:
            log.info(f"[VIRTUAL] SKIP {symbol}: risk Rs.{risk_per_lot:.0f} > "
                     f"{self.max_risk_pct*100:.0f}% of capital Rs.{self.capital:.0f}")
            return False

        self.positions[symbol] = {
            "side": side, "entry": price, "sl": sl, "initial_sl": sl,
            "qty": qty, "lot_size": lot_size,
            "peak": price, "trough": price,
            "entry_time": datetime.now(IST).isoformat(),
            "reason": reason, "score": score,
            "ticks": 0, "mfe": 0.0, "mae": 0.0,
        }
        self._log("VIRTUAL_ENTRY", symbol, side, price, qty,
                  sl=sl, reason=reason, score=score)
        return True

    def on_tick(self, symbol: str, price: float):
        """Update position with new price. Check SL, trailing."""
        if symbol not in self.positions:
            return
        p = self.positions[symbol]
        p["ticks"] += 1

        risk = abs(p["entry"] - p["initial_sl"])
        if risk <= 0:
            return

        is_long = p["side"] in ("BUY", "LONG")

        if is_long:
            p["peak"] = max(p["peak"], price)
            p["trough"] = min(p["trough"], price)
            pnl = (price - p["entry"]) * p["qty"]
            current_r = (price - p["entry"]) / risk
            peak_r = (p["peak"] - p["entry"]) / risk
            mfe = (p["peak"] - p["entry"]) / risk
            mae = (p["trough"] - p["entry"]) / risk  # negative when losing

            # SL check
            if price <= p["sl"]:
                self._close(symbol, p["sl"], "SL_HIT")  # exit at SL level
                return

            # Progressive trailing
            new_sl = p["sl"]
            for threshold, trail_r in TRAIL_MILESTONES:
                if peak_r >= threshold:
                    candidate_sl = p["entry"] + trail_r * risk
                    new_sl = max(new_sl, candidate_sl)
                    break

            if new_sl > p["sl"]:
                old_sl = p["sl"]
                p["sl"] = new_sl
                log.info(f"[VIRTUAL] TRAIL {symbol}: SL {old_sl:.2f} -> "
                         f"{new_sl:.2f} (peak_r={peak_r:.2f})")

        else:  # SHORT
            p["peak"] = min(p["peak"], price)
            p["trough"] = max(p["trough"], price)
            pnl = (p["entry"] - price) * p["qty"]
            current_r = (p["entry"] - price) / risk
            peak_r = (p["entry"] - p["peak"]) / risk
            mfe = (p["entry"] - p["peak"]) / risk
            mae = (p["entry"] - p["trough"]) / risk

            # SL check
            if price >= p["sl"]:
                self._close(symbol, p["sl"], "SL_HIT")
                return

            # Progressive trailing
            new_sl = p["sl"]
            for threshold, trail_r in TRAIL_MILESTONES:
                if peak_r >= threshold:
                    candidate_sl = p["entry"] - trail_r * risk
                    new_sl = min(new_sl, candidate_sl)
                    break

            if new_sl < p["sl"]:
                old_sl = p["sl"]
                p["sl"] = new_sl
                log.info(f"[VIRTUAL] TRAIL {symbol}: SL {old_sl:.2f} -> "
                         f"{new_sl:.2f} (peak_r={peak_r:.2f})")

        # Update MFE/MAE
        p["mfe"] = max(p.get("mfe", 0), mfe)
        p["mae"] = min(p.get("mae", 0), mae)

    def close_all(self, prices: Dict[str, float], reason: str = "SESSION_END"):
        """Close all open positions at current prices."""
        for symbol in list(self.positions.keys()):
            px = prices.get(symbol, 0)
            if px > 0:
                self._close(symbol, px, reason)
            else:
                self._close(symbol, self.positions[symbol]["entry"],
                           f"{reason}_NO_PRICE")

    def _close(self, symbol: str, price: float, reason: str):
        p = self.positions.pop(symbol)
        risk = abs(p["entry"] - p["initial_sl"])
        is_long = p["side"] in ("BUY", "LONG")

        if is_long:
            pnl = (price - p["entry"]) * p["qty"]
        else:
            pnl = (p["entry"] - price) * p["qty"]

        r = pnl / (risk * p["qty"]) if risk > 0 and p["qty"] > 0 else 0

        trade = {
            "symbol": symbol, "side": p["side"],
            "entry": p["entry"], "exit": round(price, 2),
            "qty": p["qty"], "lot_size": p["lot_size"],
            "initial_sl": p["initial_sl"], "final_sl": round(p["sl"], 2),
            "peak": round(p["peak"], 2),
            "entry_time": p["entry_time"],
            "exit_time": datetime.now(IST).isoformat(),
            "exit_reason": reason,
            "pnl": round(pnl, 2),
            "r": round(r, 2),
            "mfe": round(p.get("mfe", 0), 2),
            "mae": round(p.get("mae", 0), 2),
            "ticks": p["ticks"],
            "score": p.get("score", 0),
            "reason": p["reason"],
        }
        self.closed.append(trade)
        self._log("VIRTUAL_EXIT", symbol, p["side"], price, p["qty"],
                  pnl=round(pnl, 2), r=round(r, 2), reason=reason,
                  entry=p["entry"], peak=round(p["peak"], 2),
                  mfe=round(p.get("mfe", 0), 2))

    def _log(self, event: str, symbol: str, side: str, price: float,
             qty: int, **kwargs):
        entry = {
            "ts": datetime.now(IST).isoformat(), "event": event,
            "symbol": symbol, "side": side, "price": price, "qty": qty,
            **kwargs
        }
        try:
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass
        pnl_str = f" pnl=Rs.{kwargs['pnl']}" if 'pnl' in kwargs else ""
        log.info(f"[VIRTUAL] {event}: {symbol} {side} {qty}x @ {price:.2f}"
                 f"{pnl_str} {kwargs.get('reason', '')}")

    def summary(self) -> dict:
        total_pnl = sum(t["pnl"] for t in self.closed)
        winners = sum(1 for t in self.closed if t["pnl"] > 0)
        losers = sum(1 for t in self.closed if t["pnl"] <= 0)
        avg_r = (sum(t["r"] for t in self.closed) / len(self.closed)
                 if self.closed else 0)
        avg_mfe = (sum(t["mfe"] for t in self.closed) / len(self.closed)
                   if self.closed else 0)
        result = {
            "date": datetime.now(IST).strftime("%Y-%m-%d"),
            "total_pnl": round(total_pnl, 2),
            "trades": len(self.closed),
            "winners": winners,
            "losers": losers,
            "win_rate": round(winners / max(len(self.closed), 1) * 100, 1),
            "avg_r": round(avg_r, 2),
            "avg_mfe": round(avg_mfe, 2),
            "open": len(self.positions),
            "closed_trades": self.closed,
        }
        # Save summary to file
        try:
            with open(self._summary_path, "w") as f:
                json.dump(result, f, indent=2, default=str)
        except Exception:
            pass
        return result


# ============================================================================
# Self-tests
# ============================================================================
if __name__ == "__main__":
    import tempfile
    _td = tempfile.mkdtemp()
    _pass = 0
    _fail = 0

    def _check(name, condition):
        global _pass, _fail
        if condition:
            _pass += 1
        else:
            _fail += 1
            print(f"  FAIL: {name}")

    # Test 1: Basic LONG trade with SL hit
    vt = MCXVirtualTrader(capital=5000000, base_path=_td)
    opened = vt.on_signal("GOLD", "BUY", 50000, 49500, 100, "ORB_BREAKOUT", 72.0)
    _check("LONG open", opened and "GOLD" in vt.positions)
    vt.on_tick("GOLD", 50200)  # profit
    _check("LONG peak updates", vt.positions["GOLD"]["peak"] == 50200)
    vt.on_tick("GOLD", 49400)  # below SL
    _check("LONG SL hit closes", "GOLD" not in vt.positions)
    _check("LONG trade closed", len(vt.closed) == 1)
    _check("LONG P&L negative", vt.closed[0]["pnl"] < 0)
    _check("LONG exit at SL", vt.closed[0]["exit"] == 49500)

    # Test 2: SHORT trade with trailing
    vt2 = MCXVirtualTrader(capital=5000000, base_path=_td)
    vt2.on_signal("CRUDEOIL", "SELL", 8000, 8100, 100, "ORB_BREAKDOWN", 75.0)
    # Move to 1R profit (risk=100, so 1R = entry-100 = 7900)
    for px in range(8000, 7890, -10):
        vt2.on_tick("CRUDEOIL", px)
    _check("SHORT peak tracks", vt2.positions["CRUDEOIL"]["peak"] <= 7900)
    # SL should have trailed (at 1R, trail to 0.5R = 8000 - 0.5*100 = 7950)
    _check("SHORT SL trailed from 8100",
           vt2.positions["CRUDEOIL"]["sl"] < 8100)
    # Now price reverses to hit trailed SL
    vt2.on_tick("CRUDEOIL", 7960)
    _check("SHORT closed at trailed SL",
           "CRUDEOIL" not in vt2.positions)  # SL trailed to 7950, price 7960 >= 7950 -> closed
    _check("SHORT P&L positive", vt2.closed[0]["pnl"] > 0)
    _check("SHORT exit at trailed SL", vt2.closed[0]["exit"] == 7950.0)

    # Test 3: Max positions limit
    vt3 = MCXVirtualTrader(capital=5000000, max_positions=2, base_path=_td)
    vt3.on_signal("GOLD", "BUY", 50000, 49500, 100, "ORB")
    vt3.on_signal("SILVER", "BUY", 70000, 69000, 30, "ORB")
    ok3 = vt3.on_signal("CRUDEOIL", "BUY", 8000, 7900, 100, "ORB")
    _check("Max positions enforced", not ok3)
    _check("Only 2 positions open", len(vt3.positions) == 2)

    # Test 4: Risk limit
    vt4 = MCXVirtualTrader(capital=5000000, base_path=_td)
    # Risk = |50000 - 45000| * 100 = 500000 > 2% of 200000 = 4000
    ok4 = vt4.on_signal("GOLD", "BUY", 50000, 45000, 100, "ORB")
    _check("Risk limit enforced", not ok4)

    # Test 5: close_all at session end
    vt5 = MCXVirtualTrader(capital=5000000, base_path=_td)
    vt5.on_signal("GOLD", "BUY", 50000, 49500, 100, "ORB")
    vt5.on_signal("SILVER", "SELL", 70000, 71000, 30, "ORB")
    vt5.close_all({"GOLD": 50500, "SILVER": 69500}, "SESSION_END")
    _check("close_all closes both", len(vt5.positions) == 0)
    _check("close_all 2 closed trades", len(vt5.closed) == 2)
    gold_trade = [t for t in vt5.closed if t["symbol"] == "GOLD"][0]
    silver_trade = [t for t in vt5.closed if t["symbol"] == "SILVER"][0]
    _check("GOLD profit", gold_trade["pnl"] > 0)
    _check("SILVER profit", silver_trade["pnl"] > 0)

    # Test 6: summary
    s = vt5.summary()
    _check("Summary has trades", s["trades"] == 2)
    _check("Summary has pnl", s["total_pnl"] > 0)

    # Test 7: Duplicate signal rejected
    vt6 = MCXVirtualTrader(capital=5000000, base_path=_td)
    vt6.on_signal("GOLD", "BUY", 50000, 49500, 100, "ORB")
    ok6 = vt6.on_signal("GOLD", "BUY", 50100, 49600, 100, "ORB")
    _check("Duplicate rejected", not ok6)
    _check("Still 1 position", len(vt6.positions) == 1)

    # Test 8: MFE/MAE tracking
    vt7 = MCXVirtualTrader(capital=5000000, base_path=_td)
    vt7.on_signal("GOLD", "BUY", 50000, 49500, 100, "ORB")
    vt7.on_tick("GOLD", 50300)   # +0.6R
    vt7.on_tick("GOLD", 49800)   # -0.4R
    vt7.on_tick("GOLD", 50100)   # +0.2R
    vt7.close_all({"GOLD": 50100}, "SESSION_END")
    t = vt7.closed[0]
    _check("MFE tracked", t["mfe"] >= 0.6 - 0.01)
    _check("MAE tracked", t["mae"] <= -0.4 + 0.01)

    # Test 9: JSONL ledger written
    ledger_file = os.path.join(_td, "mcx_virtual_trades.jsonl")
    _check("JSONL ledger exists", os.path.exists(ledger_file))
    with open(ledger_file) as f:
        lines = f.readlines()
    _check("JSONL has entries", len(lines) > 0)
    first = json.loads(lines[0])
    _check("JSONL has event field", "event" in first)

    print(f"\n{'='*40}")
    print(f"MCXVirtualTrader: {_pass} passed, {_fail} failed")
    if _fail == 0:
        print("ALL TESTS PASSED ✅")
    print(f"{'='*40}")
