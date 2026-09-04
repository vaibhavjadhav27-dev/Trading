"""
V8.5.4 UNIFIED SAFETY + PROFIT MANAGEMENT PATCH
================================================

Purpose:
1) Fix live-state/broker reconciliation and ghost-position risk.
2) Fix structural/ATR stop direction and invalid-stop handling.
3) Preserve the V8.5.3 profit-lock philosophy, but make it authoritative:
   broker-side SL protection is updated from ORIGINAL risk, not the mutable
   current SL; temporary pullbacks do not cause an exit by themselves.
4) Provide broker order attribution so an unknown Dhan position can be traced
   to correlation/order/trade data.
5) Produce auditable state for every position.

This module does not change the V8.2 five entry modes or scoring engine.
It is an execution/safety/profit-management layer.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Iterable
import math, time, uuid


def fnum(x, default=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def round_tick(px: float, tick: float=0.05) -> float:
    if px <= 0: return px
    tick = tick if tick > 0 else 0.05
    return round(round(px/tick)*tick, 2)


def correlation_id(prefix: str, symbol: str, side: str) -> str:
    # Dhan correlationId max length is 30 chars.
    raw=f"{prefix}-{symbol}-{side}-{uuid.uuid4().hex[:8]}"
    return raw[:30]


@dataclass(frozen=True)
class V854Config:
    structural_buffer_atr: float = 0.15
    minimum_stop_atr: float = 0.35
    fallback_stop_atr: float = 1.20
    trail_1r: float = 0.50
    trail_2r: float = 1.25
    trail_2_5r: float = 1.75
    trail_3r: float = 2.25
    warning_retracement_r: float = 0.50
    hard_retracement_r: float = 0.90
    min_exit_confirmations: int = 3
    reconcile_seconds: int = 60
    deep_audit_seconds: int = 900

CFG=V854Config()


def structural_stop(side: str, entry: float, atr: float,
                    support: Optional[float]=None,
                    resistance: Optional[float]=None,
                    trigger: Optional[float]=None,
                    cfg: V854Config=CFG) -> float:
    """Correct side-of-market structural stop.

    LONG: below support/trigger + ATR buffer.
    SHORT: above resistance/trigger + ATR buffer.
    """
    side=side.upper(); entry=fnum(entry); atr=fnum(atr)
    if entry <= 0 or atr <= 0: raise ValueError("entry and ATR must be positive")
    buf=cfg.structural_buffer_atr*atr
    if side=="LONG":
        levels=[fnum(x) for x in (support, trigger) if fnum(x)>0]
        base=min(levels) if levels else entry-cfg.fallback_stop_atr*atr
        stop=base-buf
        stop=min(stop, entry-cfg.minimum_stop_atr*atr)
        return round(stop,2)
    if side=="SHORT":
        levels=[fnum(x) for x in (resistance, trigger) if fnum(x)>0]
        base=max(levels) if levels else entry+cfg.fallback_stop_atr*atr
        stop=base+buf
        stop=max(stop, entry+cfg.minimum_stop_atr*atr)
        return round(stop,2)
    raise ValueError(f"unknown side {side}")


def broker_valid_trigger(side: str, trigger: float, ltp: float,
                         atr: float, tick: float=0.05) -> Optional[float]:
    """Repair a crossed stop without forcing a market exit."""
    side=side.upper(); trigger=fnum(trigger); ltp=fnum(ltp); atr=fnum(atr)
    if ltp<=0: return None
    gap=max(tick, 0.10*atr if atr>0 else tick)
    if side=="LONG":
        if trigger>=ltp: trigger=ltp-gap
        return round_tick(trigger,tick) if trigger>0 and trigger<ltp else None
    if side=="SHORT":
        if trigger<=ltp: trigger=ltp+gap
        return round_tick(trigger,tick) if trigger>ltp else None
    return None


def initial_r(position: Dict[str,Any]) -> float:
    entry=fnum(position.get("entry")); sl=fnum(position.get("initial_sl", position.get("sl")))
    return abs(entry-sl)


def live_r(position: Dict[str,Any], price: float) -> float:
    risk=initial_r(position); entry=fnum(position.get("entry")); side=str(position.get("side","" )).upper()
    if risk<=0 or entry<=0: return 0.0
    return ((price-entry)/risk) if side=="LONG" else ((entry-price)/risk)


def update_peak(position: Dict[str,Any], price: float) -> None:
    side=str(position.get("side","LONG")).upper(); entry=fnum(position.get("entry"), price)
    if side=="LONG": position["peak"]=max(fnum(position.get("peak"),entry), price)
    else: position["peak"]=min(fnum(position.get("peak"),entry), price)
    position["best_r"]=max(fnum(position.get("best_r")), live_r(position, fnum(position.get("peak"),price)))
    position["max_profit_pct"]=max(fnum(position.get("max_profit_pct")), abs(fnum(position.get("peak"),price)-entry)/entry*100 if entry else 0)


def trail_trigger(position: Dict[str,Any], price: float,
                  momentum_ok: bool=True, structure_ok: bool=True,
                  cfg: V854Config=CFG) -> Optional[float]:
    """Progressive profit lock based on ORIGINAL risk, never current SL."""
    update_peak(position, price)
    risk=initial_r(position); entry=fnum(position.get("entry")); side=str(position.get("side","LONG")).upper(); best=fnum(position.get("best_r")); peak=fnum(position.get("peak"),price)
    if risk<=0 or entry<=0: return None
    if best<1.0: return fnum(position.get("sl"), fnum(position.get("initial_sl")))
    if best>=3.0: protect=cfg.trail_3r
    elif best>=2.5: protect=cfg.trail_2_5r
    elif best>=2.0: protect=cfg.trail_2r
    else: protect=cfg.trail_1r
    if side=="LONG":
        trigger=entry+protect*risk
        if not momentum_ok or not structure_ok:
            trigger=max(trigger, peak-cfg.warning_retracement_r*risk)
        return round_tick(min(trigger, price-max(0.01,0.05*risk)))
    trigger=entry-protect*risk
    if not momentum_ok or not structure_ok:
        trigger=min(trigger, peak+cfg.warning_retracement_r*risk)
    return round_tick(max(trigger, price+max(0.01,0.05*risk)))


def reversal_state(position: Dict[str,Any], features: Dict[str,Any], cfg: V854Config=CFG) -> Tuple[int,Dict[str,bool]]:
    side=str(position.get("side","LONG")).upper(); d=1 if side=="LONG" else -1
    m5=d*fnum(features.get("momentum_5m")); m15=d*fnum(features.get("momentum_15m")); rs=d*fnum(features.get("rs"))
    checks={
        "momentum_reversal": m5<0 and m15<0,
        "structure_break": bool(features.get("structure_break")),
        "vwap_reversal": bool(features.get("vwap_reversal")),
        "rs_reversal": rs<0,
        "volume_climax_stall": bool(features.get("volume_climax")) and bool(features.get("price_progress_stalling")),
        "setup_invalidated": bool(features.get("setup_invalidated")),
    }
    return sum(checks.values()), checks


def evaluate_profit_exit(position: Dict[str,Any], price: float, features: Dict[str,Any], cfg: V854Config=CFG) -> Dict[str,Any]:
    update_peak(position, price)
    cr=live_r(position,price); pr=live_r(position,fnum(position.get("peak"),price)); retr=max(0.0,pr-cr)
    n, checks=reversal_state(position,features,cfg)
    if checks["setup_invalidated"]:
        action="EXIT"; reason="CONFIRMED_SETUP_INVALIDATION"
    elif pr>=1.0 and checks["momentum_reversal"] and checks["structure_break"] and n>=cfg.min_exit_confirmations and retr>=cfg.warning_retracement_r:
        action="EXIT"; reason="CONFIRMED_PEAK_REVERSAL"
    elif retr>=cfg.hard_retracement_r and n>=cfg.min_exit_confirmations:
        action="EXIT"; reason="MULTI_FACTOR_HARD_RETRACEMENT"
    elif n>=2 or retr>=cfg.warning_retracement_r:
        action="PROTECT"; reason="WEAKENING_PROTECT"
    else:
        action="HOLD"; reason="THESIS_INTACT"
    return {"action":action,"reason":reason,"current_r":cr,"peak_r":pr,"retracement_r":retr,"reversal_score":n,"checks":checks}


def modify_sl(gateway, order_id: str, qty: int, side: str, trigger: float, ltp: float, tick: float=0.05):
    trigger=broker_valid_trigger(side,trigger,ltp,max(abs(ltp-trigger),tick),tick)
    if not order_id or qty<=0 or trigger is None: return None
    txn="SELL" if side.upper()=="LONG" else "BUY"
    payload={"dhanClientId":gateway.client_id,"orderId":str(order_id),"orderType":"STOP_LOSS_MARKET","quantity":int(qty),"price":0.0,"triggerPrice":trigger,"validity":"DAY"}
    try:
        r=gateway._request("PUT",f"/orders/{order_id}",payload,kind="order") or {}
        st=str(r.get("orderStatus",r.get("status",""))).upper()
        return r if st in ("TRANSIT","PENDING","TRADED","PART_TRADED") else None
    except Exception:
        return None


def install_protective_sl(gateway, security_id: str, qty: int, side: str, trigger: float, ltp: float, tick: float=0.05):
    trigger=broker_valid_trigger(side,trigger,ltp,max(abs(ltp-trigger),tick),tick)
    if trigger is None: return None
    txn="SELL" if side.upper()=="LONG" else "BUY"
    # Dhan SLM uses triggerPrice for the trigger; price is zero for SLM.
    try:
        return gateway.place_order(security_id,qty,0.0,txn,"STOP_LOSS_MARKET",trigger_price=trigger,correlation_id=correlation_id("V854-SL",str(security_id),side))
    except Exception:
        return None


def classify_broker_order(order: Dict[str,Any], local_correlation_prefixes: Iterable[str]) -> str:
    cid=str(order.get("correlationId",order.get("correlation_id","")) or "")
    remarks=str(order.get("remarks",order.get("Remarks","")) or "")
    if any(cid.startswith(p) for p in local_correlation_prefixes): return "BOT"
    if cid or remarks: return "EXTERNAL_OR_UNKNOWN"
    return "UNKNOWN"


def reconcile_broker(bot, deep: bool=False) -> Dict[str,Any]:
    """Dhan is source of truth for actual position/order state."""
    positions=bot.dhan.get_positions() or []
    if isinstance(positions,dict): positions=positions.get("data",positions.get("positions",[])) or []
    broker={}
    for p in positions if isinstance(positions,list) else []:
        sid=str(p.get("securityId",p.get("security_id","")))
        nq=int(fnum(p.get("netQty",p.get("net_qty",0))))
        if sid and nq: broker[sid]=p
    local=bot.active_positions
    events=[]
    # Broker authoritative for quantity and average price.
    for sid,bp in broker.items():
        nq=int(fnum(bp.get("netQty",bp.get("net_qty",0))))
        side="LONG" if nq>0 else "SHORT"
        avg=fnum(bp.get("avgCostPrice",bp.get("averageCostPrice",bp.get("averageTradedPrice",bp.get("avgTradedPrice",0)))))
        if sid in local:
            p=local[sid]; p["qty"]=abs(nq)
            if avg>0: p["broker_avg_price"]=avg
            p["broker_net_qty"]=nq; p["last_reconciled"]=time.time(); p["broker_unrealized_pnl"]=fnum(bp.get("unrealizedProfit",bp.get("unrealizedPnl",0))); p["broker_side"]=side
        else:
            # Orphan: adopt only if we can identify a valid average price.
            if avg<=0:
                events.append({"type":"ORPHAN_UNRECOVERABLE","sid":sid,"qty":nq}); continue
            temp_sl=avg*0.9925 if side=="LONG" else avg*1.0075
            p={"symbol":bp.get("tradingSymbol",sid),"security_id":sid,"side":side,"qty":abs(nq),"entry":avg,"broker_avg_price":avg,"broker_net_qty":nq,"initial_sl":temp_sl,"sl":temp_sl,"sl_order_id":None,"peak":avg,"best_r":0.0,"max_profit_pct":0.0,"source":"BROKER_ORPHAN_ADOPTED","entry_time":bp.get("createTime","recovered")}
            local[sid]=p
            # Establish immediate conservative protection; strategy context can be rebuilt afterwards.
            try:
                px=fnum(bot.fetch_ltp_concurrent([sid]).get(sid),avg)
                sl=broker_valid_trigger(side,temp_sl,px,max(abs(px-temp_sl),0.05))
                if sl is None: raise RuntimeError("NO_VALID_ORPHAN_SL")
                resp=install_protective_sl(bot.dhan,sid,abs(nq),side,sl,px)
                if not isinstance(resp,dict) or not resp.get("orderId"):
                    raise RuntimeError("ORPHAN_SL_NOT_ACCEPTED")
                p["sl"]=sl; p["sl_order_id"]=resp.get("orderId")
            except Exception as orphan_sl_error:
                events.append({"type":"ORPHAN_PROTECTION_FAILED","sid":sid,"error":str(orphan_sl_error)})
                try: bot.emergency_exit(sid,abs(nq),side)
                except Exception: pass
                local.pop(sid,None)
                continue
            events.append({"type":"ORPHAN_ADOPTED","sid":sid,"qty":abs(nq),"side":side,"entry":avg,"sl":p["sl"]})
    # Local position missing at broker: mark closed, never keep ghost locally.
    for sid in list(local):
        if sid not in broker:
            events.append({"type":"LOCAL_POSITION_MISSING_AT_BROKER","sid":sid,"local_qty":local[sid].get("qty",0)})
            local.pop(sid,None)
    if deep and hasattr(bot.dhan,"get_orders"):
        try:
            orders=bot.dhan.get_orders() or []
            if isinstance(orders,dict): orders=orders.get("data",orders.get("orders",[])) or []
            prefixes=("V82-","V84-","V851-","V853-","V854-")
            for o in orders:
                cls=classify_broker_order(o,prefixes)
                if cls!="BOT": events.append({"type":"EXTERNAL_ORDER","classification":cls,"orderId":o.get("orderId"),"securityId":o.get("securityId"),"symbol":o.get("tradingSymbol"),"side":o.get("transactionType"),"qty":o.get("quantity"),"status":o.get("orderStatus"),"correlationId":o.get("correlationId"),"remarks":o.get("remarks",o.get("Remarks"))})
        except Exception as e: events.append({"type":"ORDER_AUDIT_ERROR","error":str(e)})
        try:
            trades=bot.dhan.get_trades() if hasattr(bot.dhan,"get_trades") else []
            for t in trades if isinstance(trades,list) else []:
                oid=str(t.get("orderId",t.get("order_id","")))
                events.append({"type":"TRADE_AUDIT","orderId":oid,"securityId":t.get("securityId"),"symbol":t.get("tradingSymbol",t.get("customSymbol")),"side":t.get("transactionType"),"qty":t.get("tradedQuantity",t.get("quantity")),"price":t.get("tradedPrice",t.get("price"))})
        except Exception as e:
            events.append({"type":"TRADE_AUDIT_ERROR","error":str(e)})
    if hasattr(bot,"_save_state"): bot._save_state()
    return {"broker_positions":len(broker),"local_positions":len(local),"events":events,"deep":deep}


def append_audit_event(event: Dict[str,Any], path: str="trade_logs/v854_safety_audit.jsonl") -> None:
    """Append one durable audit event; failures never crash the trader."""
    try:
        import json, os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path,"a",encoding="utf-8") as fh:
            fh.write(json.dumps(event,default=str,separators=(",",":"))+"\n")
    except Exception:
        pass


def validate_entry_audit(record: Dict[str,Any]) -> Tuple[bool,str]:
    required=("symbol","side","entry_price","fill_price","qty","raw_score","final_score","initial_sl","risk_per_share")
    missing=[k for k in required if k not in record]
    if missing: return False,"MISSING_FIELDS:"+",".join(missing)
    if fnum(record.get("qty"))<=0: return False,"INVALID_QTY"
    if fnum(record.get("fill_price"))<=0: return False,"INVALID_FILL_PRICE"
    if fnum(record.get("initial_sl"))<=0: return False,"INVALID_INITIAL_SL"
    expected=abs(fnum(record["fill_price"])-fnum(record["initial_sl"]))
    if abs(expected-fnum(record["risk_per_share"]))>max(0.01,expected*0.02): return False,"RISK_PER_SHARE_MISMATCH"
    # Prevent the observed score/expected-R field corruption.
    if "expected_r" in record and fnum(record["expected_r"])>20 and fnum(record["final_score"])>50 and abs(fnum(record["expected_r"])-fnum(record["final_score"]))<0.01:
        return False,"EXPECTED_R_CONTAINS_SCORE"
    return True,"OK"
