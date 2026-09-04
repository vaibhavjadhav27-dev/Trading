"""Fair shadow comparator for NSE/MCX/Swing.

The same Dhan available balance is snapshotted once at the start of each day.
Each engine gets an independent virtual ledger with the same starting capital.
This is a comparison framework only; it must never place orders.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any

@dataclass
class ShadowLedger:
    engine: str
    starting_capital: float
    cash: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def equity(self):
        return self.cash + self.unrealized_pnl

    @property
    def return_pct(self):
        return (self.equity-self.starting_capital)/self.starting_capital*100 if self.starting_capital else 0.0

    @property
    def net_pnl(self):
        return self.realized_pnl + self.unrealized_pnl - self.fees

def new_day_snapshot(dhan_available_balance: float):
    """Use exactly this snapshot as starting capital for all three virtual engines."""
    if dhan_available_balance <= 0:
        raise ValueError("Dhan available balance must be positive")
    return {
        "date": datetime.now().date().isoformat(),
        "starting_capital": float(dhan_available_balance),
        "source": "Dhan available balance snapshot",
        "engines": ["NSE_INTRADAY","MCX_SHADOW","SWING_SHADOW"]
    }

def create_ledgers(snapshot):
    c=float(snapshot["starting_capital"])
    return {e:ShadowLedger(e,c,c) for e in snapshot["engines"]}

def record_closed_trade(ledger: ShadowLedger, pnl: float, fees: float):
    ledger.realized_pnl += pnl
    ledger.cash += pnl
    ledger.fees += max(0.0,fees)
    ledger.trades += 1
    if pnl>0: ledger.wins += 1
    elif pnl<0: ledger.losses += 1

def daily_report(ledgers: Dict[str,ShadowLedger]):
    rows=[]
    for e,l in ledgers.items():
        rows.append({
            "engine":e,"starting_capital":round(l.starting_capital,2),
            "net_pnl":round(l.net_pnl,2),"return_pct":round(l.return_pct,3),
            "trades":l.trades,"win_rate_pct":round(100*l.wins/l.trades,1) if l.trades else 0,
            "fees":round(l.fees,2)
        })
    return sorted(rows,key=lambda x:x["return_pct"],reverse=True)
