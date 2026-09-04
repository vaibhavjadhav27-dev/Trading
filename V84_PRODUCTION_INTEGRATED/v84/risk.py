from dataclasses import dataclass
from .config import RISK
@dataclass
class RiskState:
    starting_balance:float; realized_pnl:float=0; open_risk:float=0; consecutive_losses:int=0; open_positions:int=0; total_notional:float=0
    @property
    def daily_pnl_pct(self): return self.realized_pnl/self.starting_balance*100 if self.starting_balance else 0

def trading_allowed(s):
    if s.daily_pnl_pct<=-RISK.daily_hard_stop_pct:return False,'DAILY_HARD_STOP'
    if s.consecutive_losses>=RISK.max_consecutive_losses:return False,'CONSECUTIVE_LOSS_LOCK'
    if s.open_risk/max(s.starting_balance,1)*100>=RISK.max_open_risk_pct:return False,'OPEN_RISK_CAP'
    if s.daily_pnl_pct<=-RISK.daily_soft_stop_pct:return False,'DAILY_SOFT_STOP'
    return True,'OK'

def risk_budget(balance,risk_pct=None):
    rp=RISK.risk_per_trade_pct if risk_pct is None else risk_pct
    return max(0,balance*(1-RISK.cash_reserve_pct/100)*rp/100)

def size_from_risk(price,stop,balance,risk_pct=None,max_notional_pct=None,lot_size=1):
    if min(price,stop,balance)<=0:return {'qty':0,'reason':'invalid_inputs'}
    dist=abs(price-stop)
    if dist<=0:return {'qty':0,'reason':'zero_stop_distance'}
    budget=risk_budget(balance,risk_pct); slip=price*RISK.slippage_pct/100; per=dist+slip
    raw=int(budget/per); cap=int(balance*(max_notional_pct or RISK.max_single_notional_pct)/100/price); qty=(min(raw,cap)//lot_size)*lot_size
    if qty<lot_size:return {'qty':0,'reason':'risk_budget_too_small','risk_budget':budget}
    actual=qty*per
    return {'qty':qty,'risk_budget':budget,'actual_risk':actual,'risk_pct':actual/balance*100,'notional':qty*price,'notional_pct':qty*price/balance*100,'reason':'OK'}
