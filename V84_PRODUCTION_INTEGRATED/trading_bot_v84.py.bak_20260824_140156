#!/usr/bin/env python3
"""V8.4 production integration.

Preserves the proven V8.2 Dhan gateway/orchestration and replaces only the
entry decision, sizing, confirmation and profit-management layers.

LIVE IS EXPLICIT: V84_ENABLE_LIVE=1 is required in addition to V82_DRY_RUN=0.
"""
from __future__ import annotations
import json, os, time, logging
try:
    from v84_trade_logger import log_entry, log_exit, log_scan_cycle, cleanup_old_logs
    cleanup_old_logs()
except ImportError:
    log_entry=log_exit=log_scan_cycle=cleanup_old_logs=lambda *a,**k:None
try:
    from V85_1_PATCH import (install_gateway_patch, safe_place_hard_sl, safe_reconcile,
        allocate_batch, execute_entry_allocated, desired_trailing_stop, modify_pending_sl,
        V851Config, size_candidate)
    _V851_LOADED = True
except ImportError as _e:
    log.warning(f"V8.5.1 patch not loaded: {_e}")
    _V851_LOADED = False
try:
    from v853_profit_engine_patch import (V853EntryEngine, ExitEngine, Position as V853Position,
        Candidate as V853Candidate, EntryAction, ExitAction, recover_stop_after_dhan_rejection,
        structural_stop, rank_actionable)
    _V853_LOADED = True
    _v853_entry = V853EntryEngine()
    _v853_exit = ExitEngine()
    pass  # V8.5.3 loaded (log not yet available)
except ImportError as _e:
    log.warning(f"V8.5.3 strategy not loaded: {_e}")
    _V853_LOADED = False
from datetime import datetime
from pathlib import Path
from trading_bot_v82 import TradingBotV82, now_ist, ist_time, _event
from v84_strategy import final_decision
from v84.config import RISK, INTRADAY
from v84.indicators import vwap
from V854_UNIFIED_PATCH import (structural_stop as v854_structural_stop, broker_valid_trigger, trail_trigger as v854_trail_trigger, evaluate_profit_exit as v854_evaluate_profit_exit, reconcile_broker as v854_reconcile_broker, install_protective_sl as v854_install_protective_sl, modify_sl as v854_modify_sl, validate_entry_audit as v854_validate_entry_audit, append_audit_event as v854_append_audit, correlation_id as v854_correlation_id)

log=logging.getLogger("V84_PROD")
RISK_FILE=Path("v84_daily_risk.json")

class TradingBotV84(TradingBotV82):
    def __init__(self,dry_run=False):
        super().__init__(dry_run=dry_run)
        if os.getenv("V84_ENABLE_LIVE","0")!="1":
            self.dry_run=True
        self._load_v84_risk()
        log.info("V8.4 production integration loaded; V8.2 Dhan/orchestration retained")

    def _load_v84_risk(self):
        today=now_ist().date().isoformat()
        try:
            x=json.loads(RISK_FILE.read_text())
            self.v84_risk=x if x.get("date")==today else {"date":today,"realized_pnl":0.0,"consecutive_losses":0}
        except Exception:
            self.v84_risk={"date":today,"realized_pnl":0.0,"consecutive_losses":0}
        RISK_FILE.write_text(json.dumps(self.v84_risk,indent=2))

    def _save_v84_risk(self):
        RISK_FILE.write_text(json.dumps(self.v84_risk,indent=2,default=str))

    def _daily_allowed(self):
        bal=max(float(self.dhan.get_balance() or 0),1.0)
        pnl=float(self.v84_risk.get("realized_pnl",0.0) or 0)
        pnl_pct=pnl/bal*100
        if pnl_pct<=-RISK.daily_hard_stop_pct:return False,"DAILY_HARD_STOP"
        if int(self.v84_risk.get("consecutive_losses",0))>=RISK.max_consecutive_losses:return False,"CONSECUTIVE_LOSS_LOCK"
        if pnl_pct<=-RISK.daily_soft_stop_pct:return False,"DAILY_SOFT_STOP"
        return True,"OK"

    def _build_entry_features(self,c):
        f=dict(c)
        live=self.fetch_ltp_concurrent([c["security_id"]]).get(str(c["security_id"]),c.get("ltp",0))
        f["ltp"]=float(live or c.get("ltp",0))
        f["df"]=self._df(c["security_id"])
        if f["df"] is None:return None
        from v82_strategy import rvol,momentum
        f["rvol"]=rvol(f["df"],c.get("avg_daily_volume",c.get("adv_20d",0)) or 0)
        f["momentum_5m"],f["momentum_15m"],f["momentum_30m"],f["accel"]=momentum(f["df"])
        f["nifty_data"]=dict(getattr(self,"nifty_data",{}) or {})
        f["avg_daily_volume"]=c.get("avg_daily_volume",c.get("adv_20d",0)) or 0
        return f

    def _open_risk_pct(self,balance):
        total=0.0
        for p in self.active_positions.values():
            try: total += abs(float(p["entry"])-float(p["sl"]))*int(p["qty"])
            except Exception: pass
        return total/max(balance,1.0)*100

    def _risk_quantity(self,c,d,price):
        balance=float(self.dhan.get_balance() or 0)
        stop=float(d["stop"])
        dist=abs(price-stop)
        if balance<=0 or dist<=0:return 0,0,0,"INVALID_RISK"
        allowed,why=self._daily_allowed()
        if not allowed:return 0,0,0,why
        open_risk=self._open_risk_pct(balance)
        remaining=max(0,RISK.max_open_risk_pct-open_risk)
        risk_pct=min(RISK.risk_per_trade_pct,remaining)
        if risk_pct<=0:return 0,0,0,"OPEN_RISK_CAP"
        budget=balance*(1-RISK.cash_reserve_pct/100)*risk_pct/100
        per_share=dist+price*RISK.slippage_pct/100
        qty=int(budget/per_share)
        max_qty=int(balance*(RISK.max_single_notional_pct/100)/max(price,.01))
        qty=min(qty,max_qty)
        if qty<1:return 0,0,0,"RISK_BUDGET_TOO_SMALL"
        txn="BUY" if d["side"]=="LONG" else "SELL"
        margin=self.dhan.calculate_margin(c["security_id"],qty,txn,price) or {}
        avail=float(margin.get("availableBalance",margin.get("availabelBalance",balance)) or balance)
        req=float(margin.get("totalMargin",0) or 0)
        if req>avail and req>0:
            qty=max(0,int(qty*avail/req*.98))
            if qty<1:return 0,0,0,"MARGIN_REJECT"
            margin=self.dhan.calculate_margin(c["security_id"],qty,txn,price) or margin
            avail=float(margin.get("availableBalance",avail) or avail);req=float(margin.get("totalMargin",req) or req)
        return (qty if req<=avail else 0),risk_pct,req,"OK" if req<=avail else "MARGIN_REJECT"

    def _confirm_entry(self,c,d):
        """Three 10-second confirmations + structure check; rejects transient breach."""
        if self.dry_run:return True
        sid=str(c["security_id"]); side=d["side"]; ref=float(d["entry_price"])
        for i in range(3):
            if i: time.sleep(10)
            px=float(self.fetch_ltp_concurrent([sid]).get(sid,0) or 0)
            if px<=0:return False
            if side=="LONG" and px < ref:return False
            if side=="SHORT" and px > ref:return False
        # VWAP as quality MODIFIER (not absolute gate) — per developer spec
        # Strong setup+momentum can override mild VWAP misalignment
        try:
            df=self._df(sid)
            if df is not None and len(df)>=3:
                from v82_strategy import vwap as _vwap_calc
                _vw=_vwap_calc(df); _last=float(df["close"].iloc[-1])
                _score=float(d.get("final_score",0))
                # Only REJECT if VWAP strongly against AND score < 75 (not exceptional)
                if side=="LONG" and _last < _vw * 0.995 and _score < 75:
                    log.info(f"CONFIRM REJECT {c.get('symbol','?')}: LONG below VWAP ({_last:.2f}<{_vw:.2f}) score={_score:.0f}<75")
                    return False
                if side=="SHORT" and _last > _vw * 1.005 and _score < 75:
                    log.info(f"CONFIRM REJECT {c.get('symbol','?')}: SHORT above VWAP ({_last:.2f}>{_vw:.2f}) score={_score:.0f}<75")
                    return False
                # Log warning for mild misalignment (doesn't reject)
                if side=="LONG" and _last < _vw * 0.998:
                    log.info(f"VWAP WARNING {c.get('symbol','?')}: LONG mildly below VWAP, score={_score:.0f} allows entry")
                if side=="SHORT" and _last > _vw * 1.002:
                    log.info(f"VWAP WARNING {c.get('symbol','?')}: SHORT mildly above VWAP, score={_score:.0f} allows entry")
        except Exception:pass
        return True

    def execute_entry(self,c,d):
        sid=str(c["security_id"]);side=d["side"]
        if sid in self.active_positions:return False,"already_active"
        if len(self.active_positions)>=RISK.max_intraday_positions:return False,"POSITION_CAP"
        # MIN VOLUME GATE: 50K shares minimum (today's cumulative or 20d average)
        _df_vol = c.get("df")
        _today_vol = int(_df_vol["volume"].sum()) if _df_vol is not None and "volume" in _df_vol.columns else 0
        _avg_vol = int(c.get("avg_daily_volume", c.get("adv_20d", 0)) or 0)
        if _today_vol < 50000 and _avg_vol < 50000:
            log.info("SKIP %s: low volume (today=%dK, avg=%dK) < 50K min", c.get("symbol","?"), _today_vol//1000, _avg_vol//1000)
            return False, "LOW_VOLUME"
        if not self._confirm_entry(c,d):return False,"ENTRY_CONFIRMATION_FAILED"
        price=float(self.fetch_ltp_concurrent([sid]).get(sid,d["entry_price"]) or d["entry_price"])
        stop=float(d["stop"])
        if (side=="LONG" and stop>=price) or (side=="SHORT" and stop<=price):return False,"INVALID_STOP_SIDE"
        qty,risk_pct,margin_req,status=self._risk_quantity(c,d,price)
        if qty<1:return False,status
        # PATCH #1: Log sizing decision audit trail
        _sizing_audit={"balance":float(self.dhan.get_balance() or 0),"entry_price":price,"stop_price":float(d["stop"]),"risk_per_share":abs(price-float(d["stop"])),"risk_pct":risk_pct,"margin_required":margin_req,"final_qty":qty,"notional":round(qty*price,2),"score":float(d.get("final_score",0)),"status":status}
        log.info(f"SIZING: {c.get('symbol','?')} qty={qty} | bal={_sizing_audit['balance']:.0f} risk/sh={_sizing_audit['risk_per_share']:.2f} notional={_sizing_audit['notional']:.0f} score={_sizing_audit['score']:.1f}")
        txn="BUY" if side=="LONG" else "SELL"
        resp=self.dhan.place_order(sid,qty,0,txn,"MARKET");oid=(resp or {}).get("orderId") if isinstance(resp,dict) else None
        if not oid:return False,"dhan_order_not_accepted"
        fill=self.dhan.verify_fill(oid)
        if fill["status"]=="PARTIAL":
            try:self.dhan.cancel_order(oid)
            except Exception:pass
        if fill["status"] not in ("FILLED","PARTIAL") or fill["qty"]<=0:return False,f"entry_{fill['status']}"
        fq=int(fill["qty"]);fp=float(fill["price"] or price)
        nq=self.dhan.verify_position(sid,side)
        if (side=="LONG" and nq<=0) or (side=="SHORT" and nq>=0):
            self.emergency_exit(sid,fq,side);return False,"broker_position_mismatch"
        # Keep the strategy-derived stop, but never allow a stop on the wrong side of fill.
        if (side=="LONG" and stop>=fp) or (side=="SHORT" and stop<=fp):
            _atr=float(c.get("atr",0) or 0)
            if _atr<=0:
                _atr=float(c.get("atr_pct",0) or 0)/100.0*fp
            if _atr<=0:
                _atr=max(fp*0.005,0.05)
            try:
                stop=v854_structural_stop(side,fp,_atr,float(c.get("support",0) or 0) or None,float(c.get("resistance",0) or 0) or None,float(d.get("entry_price",0) or 0) or None)
            except Exception:
                stop=fp*(0.9925 if side=="LONG" else 1.0075)
            stop=broker_valid_trigger(side,stop,fp,_atr) or 0.0
            if stop<=0:
                self.emergency_exit(sid,fq,side); return False,"NO_VALID_STRUCTURAL_STOP"
        sl=self.dhan.place_hard_sl(sid,fq,side,round(stop,2));sl_oid=(sl or {}).get("orderId") if isinstance(sl,dict) else None
        if not sl_oid:
            self.emergency_exit(sid,fq,side);return False,"hard_sl_not_accepted"
        st=(self.dhan.get_order_status(sl_oid) or {}).get("orderStatus","").upper()
        if st not in ("PENDING","TRANSIT","TRADED","PART_TRADED"):
            self.emergency_exit(sid,fq,side);return False,f"hard_sl_{st or 'UNKNOWN'}"
        self.active_positions[sid]={"symbol":c.get("symbol","?"),"security_id":sid,"side":side,"qty":fq,"entry":fp,"initial_sl":stop,"sl":stop,"sl_order_id":sl_oid,"peak":fp,"score":float(d["final_score"]),"mode":d["setup_type"],"target":float(d["target"]),"risk_pct":risk_pct,"best_r":0.0,"weak_count":0,"max_profit_pct":0.0,"entry_time":now_ist().isoformat(),"source":"V854_BOT","rs":float(c.get("rs",d.get("rs",0)) or 0),"atr":float(c.get("atr",c.get("atr_pct",0)) or 0),"support":float(c.get("support",0) or 0),"resistance":float(c.get("resistance",0) or 0),"trigger":float(d.get("entry_price",0) or 0)}
        self._save_state();_event("v84_entry",self.active_positions[sid])
        _v854_audit={"event":"ENTRY","symbol":c.get("symbol","?"),"security_id":sid,"side":side,"entry_price":fp,"fill_price":fp,"qty":fq,"raw_score":float(c.get("raw_score",d.get("final_score",0)) or 0),"final_score":float(d.get("final_score",0)),"initial_sl":stop,"risk_per_share":abs(fp-stop),"expected_move_pct":float(d.get("expected_move_pct",0) or 0),"setup_type":d.get("setup_type",""),"entry_time":now_ist().isoformat(),"order_id":oid,"sl_order_id":sl_oid}
        _ok,_why=v854_validate_entry_audit(_v854_audit)
        _v854_audit["validation"]=_why; v854_append_audit(_v854_audit)
        if not _ok: log.error("V854 ENTRY AUDIT INVALID %s: %s",c.get("symbol","?"),_why)
        try:import trade_journal;trade_journal.log_trade({"symbol":c.get("symbol"),"side":side,"entry":fp,"qty":fq,"score":float(d["final_score"]),"mode":d["setup_type"],"entry_time":now_ist().isoformat()})
        except Exception:pass
        try:log_entry(c,d,{"price":fp,"qty":fq,"order_id":oid},{"total_score":float(d.get("final_score",0)),"market":float(d.get("market_score",0)),"sector":float(d.get("sector_score",0)),"rs":float(d.get("rs_score",0)),"momentum":float(d.get("momentum_score",0)),"rvol":float(d.get("rvol_score",0)),"vwap_trend":float(d.get("vwap_score",0)),"setup_quality":float(d.get("setup_quality",0)),"entry_quality":float(d.get("entry_quality",0))},{"nifty":self._nifty_ltp,"banknifty":self._banknifty_ltp,"regime":self._regime,"direction":self._market_direction,"vix":0,"leaders":self._leaders,"laggards":self._laggards})
        except Exception:pass
        return True,"ENTRY_OK"

    def close_position(self,sid,reason,px=None):
        p=self.active_positions.get(str(sid))
        if not p:return False
        entry=float(p["entry"]);side=p["side"];exit_px=float(px or entry)
        pnl=((exit_px-entry)/entry*100) if side=="LONG" else ((entry-exit_px)/entry*100)
        ok=super().close_position(sid,reason,px)
        if ok:
            try:
                _dur=0
                if p.get("entry_time"):
                    from datetime import datetime,timezone,timedelta
                    _et=datetime.fromisoformat(str(p["entry_time"]))
                    _dur=int((datetime.now(timezone(timedelta(hours=5,minutes=30)))-_et).total_seconds()//60)
            except Exception:_dur=0
            try:log_exit(p.get("symbol","?"),sid,side,entry,exit_px,int(p.get("qty",0)),reason,float(p.get("peak",0)),_dur,str(p.get("entry_time","")))
            except Exception:pass
            self.v84_risk["realized_pnl"]=float(self.v84_risk.get("realized_pnl",0))+((exit_px-entry)*p["qty"] if side=="LONG" else (entry-exit_px)*p["qty"])
            self.v84_risk["consecutive_losses"]=int(self.v84_risk.get("consecutive_losses",0))+1 if pnl<0 else 0
            self._save_v84_risk()
        return ok

    def monitor_positions(self):
        """V8.5.4 authoritative position manager.

        Order: obtain market context -> update MFE -> evaluate thesis ->
        protect broker-side SL -> software backup -> persist.  Dhan remains
        authoritative for actual quantity/position state.
        """
        if not self.active_positions:
            return
        prices=self.fetch_ltp_concurrent(list(self.active_positions.keys()))
        for sid,p in list(self.active_positions.items()):
            px=float(prices.get(sid,0) or 0)
            if px<=0: continue
            side=str(p.get("side","LONG")).upper()
            entry=float(p.get("entry",px)); initial_sl=float(p.get("initial_sl",p.get("sl",0)))
            # Fetch OHLC BEFORE V8.5.3/V8.5.4 evaluation. This fixes the old
            # df-before-assignment bug that was silently swallowed by except pass.
            df=self._df(sid)
            features={"momentum_5m":0.0,"momentum_15m":0.0,"vwap_reversal":False,
                      "rs":0.0,"structure_break":False,"volume_climax":False,
                      "price_progress_stalling":False,"setup_invalidated":False}
            if df is not None and len(df)>=3:
                try:
                    closes=[float(x) for x in df["close"].tail(6)]
                    features["momentum_5m"]=(closes[-1]-closes[-2])/closes[-2]*100 if closes[-2] else 0.0
                    if len(closes)>=4: features["momentum_15m"]=(closes[-1]-closes[-4])/closes[-4]*100 if closes[-4] else 0.0
                    vw=float(vwap(df)); features["vwap"]=vw
                    features["vwap_reversal"]=(px<vw if side=="LONG" else px>vw)
                    c1,c2=closes[-2],closes[-1]
                    vwap_break=(c1<vw and c2<vw) if side=="LONG" else (c1>vw and c2>vw)
                    support=float(p.get("support",0) or 0); resistance=float(p.get("resistance",0) or 0)
                    structure_break=vwap_break
                    if side=="LONG" and support>0 and px<support: structure_break=True
                    if side=="SHORT" and resistance>0 and px>resistance: structure_break=True
                    features["structure_break"]=structure_break
                    features["price_progress_stalling"]=abs(features["momentum_5m"])<0.15
                    if "volume" in df.columns and len(df)>=6:
                        vols=[float(x) for x in df["volume"].tail(6)]
                        avgv=sum(vols[:-1])/max(1,len(vols[:-1])); features["volume_climax"]=vols[-1]>=2.0*avgv if avgv>0 else False
                except Exception as e:
                    log.warning("V854 feature build failed %s: %s", p.get("symbol",sid), e)
            # Relative strength can be retained from the entry snapshot; if absent,
            # do not invent a reversal signal.
            features["rs"]=float(p.get("rs",0) or 0)
            p["broker_net_qty"]=p.get("qty",0)
            # Software stop is a BACKUP only. The broker-side SL is the primary control.
            if initial_sl<=0:
                initial_sl=entry*(0.9925 if side=="LONG" else 1.0075)
                p["initial_sl"]=initial_sl; p["sl"]=initial_sl
            result=v854_evaluate_profit_exit(p,px,features)
            _momentum_ok = (features["momentum_5m"] >= 0) if side=="LONG" else (features["momentum_5m"] <= 0)
            new_trigger=v854_trail_trigger(p,px,momentum_ok=_momentum_ok,structure_ok=not features["structure_break"])
            old_sl=float(p.get("sl",initial_sl) or initial_sl)
            # Move broker SL monotonically from ORIGINAL risk. Never loosen it.
            should_modify=(new_trigger is not None and ((side=="LONG" and new_trigger>old_sl) or (side=="SHORT" and new_trigger<old_sl)))
            if should_modify and p.get("sl_order_id"):
                mod=v854_modify_sl(self.dhan,p["sl_order_id"],int(p.get("qty",0)),side,new_trigger,px)
                if mod:
                    p["sl"]=new_trigger
                    log.info("V854 PROFIT LOCK %s %s sl=%.2f bestR=%.2f",p.get("symbol",sid),side,new_trigger,float(p.get("best_r",0)))
            elif should_modify and not p.get("sl_order_id"):
                repl=v854_install_protective_sl(self.dhan,sid,int(p.get("qty",0)),side,new_trigger,px)
                if repl and isinstance(repl,dict) and repl.get("orderId"):
                    p["sl_order_id"]=repl.get("orderId"); p["sl"]=new_trigger
                else:
                    log.error("V854 PROTECTION_FAILED %s",p.get("symbol",sid))
                    # Do not silently continue as if protected. Use existing emergency safety policy.
                    self.emergency_exit(sid,int(p.get("qty",0)),side); continue
            # Confirmed strategy exit comes after protection update, not before it.
            if result.get("action")=="EXIT":
                self.close_position(sid,"V854_"+str(result.get("reason","REVERSAL")),px); continue
            # Software backup if the live price has actually crossed the current broker stop.
            sl=float(p.get("sl",initial_sl) or initial_sl)
            if (side=="LONG" and px<=sl) or (side=="SHORT" and px>=sl):
                self.close_position(sid,"V854_SOFTWARE_SL_BACKUP",px); continue
            v854_append_audit({"event":"POSITION_SNAPSHOT","time":now_ist().isoformat(),"symbol":p.get("symbol",sid),"security_id":sid,"side":side,"qty":int(p.get("qty",0)),"entry":entry,"ltp":px,"sl":sl,"peak":p.get("peak"),"best_r":p.get("best_r"),"current_r":result.get("current_r"),"reversal_score":result.get("reversal_score"),"next_action":result.get("action"),"reason":result.get("reason")})
        self._save_state()

    def run(self):
        enforce=os.getenv("V82_ENFORCE_STATIC_IP","1")=="1"
        ok,msg=self.dhan.preflight(enforce_static_ip=enforce)
        if not ok and not self.dry_run:raise RuntimeError("V8.4 PREFLIGHT FAILED: "+msg)
        log.info("V8.4 PREFLIGHT: %s",msg)
        log.info("ENGINE_VERSION=8.5.4 | PATCHES=V851+V853+V854_UNIFIED")
        if _V851_LOADED:
            install_gateway_patch(self.dhan)
            log.info("V8.5.1 gateway patch installed (SL-M fix + tick rounding)")
        # Initialize market state attributes (PATCH #6)
        self._nifty_ltp=0.0; self._banknifty_ltp=0.0
        self._regime="NORMAL"; self._market_direction="NEUTRAL"
        self._leaders=[]; self._laggards=[]
        try:
            _v854_recon=v854_reconcile_broker(self, deep=True)
            log.info("V854 startup reconciliation: %s", _v854_recon)
            v854_append_audit({"event":"RECONCILIATION","time":now_ist().isoformat(),**_v854_recon})
        except Exception as _e:
            log.exception("V854 startup reconciliation failed: %s", _e)
        _v854_next_recon=time.monotonic()+60
        _v854_next_audit=time.monotonic()+900
        while now_ist()<ist_time(9,30):time.sleep(5)
        self.check_market_quality();self.snapshot_market();self.select_candidates()
        next_scan=time.monotonic();next_snapshot=time.monotonic()
        while now_ist()<ist_time(15,5):
            self._load_v84_risk()
            if time.monotonic()>=next_snapshot:self.snapshot_market();next_snapshot=time.monotonic()+self.snapshot_minutes*60
            self.monitor_positions()
            if time.monotonic() >= _v854_next_recon:
                try:
                    _v854_recon=v854_reconcile_broker(self, deep=False)
                    log.info("V854 position reconciliation: %s", _v854_recon)
                    v854_append_audit({"event":"RECONCILIATION","time":now_ist().isoformat(),**_v854_recon})
                except Exception as _e:
                    log.exception("V854 reconciliation failed: %s", _e)
                _v854_next_recon=time.monotonic()+60
            if time.monotonic() >= _v854_next_audit:
                try:
                    _v854_audit=v854_reconcile_broker(self, deep=True)
                    log.info("V854 15m deep broker audit: %s", _v854_audit)
                    v854_append_audit({"event":"DEEP_BROKER_AUDIT","time":now_ist().isoformat(),**_v854_audit})
                except Exception as _e:
                    log.exception("V854 deep audit failed: %s", _e)
                _v854_next_audit=time.monotonic()+900
            now=now_ist();hh,mm=map(int,self.entry_cutoff.split(":"))
            if now<ist_time(hh,mm) and time.monotonic()>=next_scan:
                self.check_market_quality();self.select_candidates()
                allowed,why=self._daily_allowed()
                if allowed:
                    ranked=[]
                    for c in list(self.candidates):
                        if str(c["security_id"]) in self.active_positions:continue
                        f=self._build_entry_features(c)
                        if not f:continue
                        d=final_decision(f);_event("v84_entry_evaluation",{"symbol":c.get("symbol"),**{k:d.get(k) for k in ("side","final_score","edge","setup_type","status","reason")}})
                        if d.get("status")=="ENTER":ranked.append((d,c))
                    ranked.sort(key=lambda x:(x[0]["final_score"],x[0]["edge"],x[0]["expected_move_pct"]),reverse=True)
                    # One stock at a time per scan; existing positions still may be monitored.
                    # V8.5.3: No new entries after 14:30 IST
                    _now_ist = now_ist()
                    if _now_ist.hour >= 14 and _now_ist.minute >= 30:
                        if ranked: log.info(f'ENTRY CUTOFF 14:30: {len(ranked)} candidates skipped (market close approaching)')
                        ranked = []
                    if ranked:
                        try:log_scan_cycle([(d["final_score"],{**c,"side":d.get("side",""),"setup_type":d.get("setup_type",""),"vwap":float(c.get("vwap",0) or 0),"atr_pct":float(c.get("atr_pct",0) or 0)}) for d,c in ranked],{"nifty":self._nifty_ltp,"regime":self._regime,"direction":self._market_direction},top_n=10)
                        except Exception:pass
                        if _V851_LOADED:
                            # V8.5.3: Filter candidates through strategy engine first
                            if _V853_LOADED:
                                _v853_filtered = []
                                for _d, _c in ranked:
                                    try:
                                        _cand = V853Candidate(
                                            symbol=_c.get("symbol","?"), side=_d.get("side","LONG"),
                                            ltp=float(_c.get("ltp",0)), atr=float(_c.get("atr_pct",0.5))/100*float(_c.get("ltp",1)),
                                            raw_score=float(_d.get("final_score",0)), rs=float(_c.get("rs",0)),
                                            rvol=float(_c.get("rvol",0)), vwap=float(_c.get("vwap",0)) if _c.get("vwap") else None,
                                            momentum_5m=float(_c.get("momentum_5m",0)), momentum_15m=float(_c.get("momentum_15m",0)),
                                            trigger=float(_d.get("entry_price",0)) if _d.get("entry_price") else None,
                                            setup_type=_d.get("setup_type",""), regime=self._regime,
                                            expected_move_pct=float(_d.get("expected_move_pct",0.6)) if _d.get("expected_move_pct") else 0.6
                                        )
                                        _decision = _v853_entry.evaluate(_cand)
                                        if _decision.action == EntryAction.ENTER_NOW:
                                            _v853_filtered.append((_d, _c))
                                            log.info(f"V853 ENTER: {_c.get('symbol','?')} phase={_decision.phase.value} q={_decision.quality_score:.0f} t={_decision.timing_score:.0f} f={_decision.final_score:.0f}")
                                        else:
                                            log.info(f"V853 SKIP: {_c.get('symbol','?')} action={_decision.action.value} reason={_decision.reason}")
                                    except Exception as _e:
                                        log.warning(f"V853 eval error {_c.get('symbol','?')}: {_e}")
                                        _v853_filtered.append((_d, _c))  # fallback: include on error
                                ranked = _v853_filtered
                            # V8.5.1: Batch allocate ALL qualified, then execute
                            allocated = allocate_batch(self, ranked)
                            for _d, _c, _sizing in allocated:
                                _ok, _reason = execute_entry_allocated(self, _c, _d, _sizing)
                                if _ok:
                                    log.info(f"V851 ENTRY OK: {_c.get('symbol','?')} qty={_sizing.get('qty',0)} score={_d.get('final_score',0):.1f} net_edge={_sizing.get('expected_net_edge_pct',0):.2f}%")
                                    try:log_entry(_c,_d,{"price":_sizing.get("entry",0),"qty":_sizing.get("qty",0),"order_id":"V851"},_sizing,{"nifty":self._nifty_ltp,"regime":self._regime,"direction":self._market_direction})
                                    except Exception:pass
                                else:
                                    log.info(f"V851 ENTRY FAIL: {_c.get('symbol','?')} reason={_reason}")
                        else:
                            # Fallback: old single-entry path
                            for _d,_c in ranked:
                                _ok,_reason=self.execute_entry(_c,_d)
                                if _ok:log.info(f"ENTRY OK: {_c.get('symbol','?')}")
                                elif _reason in ("POSITION_CAP","OPEN_RISK_CAP"):break
                else:log.info("V8.4 ENTRY LOCK: %s",why)
                next_scan=time.monotonic()+self.rescan_minutes*60
            time.sleep(2)
        for sid in list(self.active_positions):
            p=self.active_positions[sid];px=self.fetch_ltp_concurrent([sid]).get(sid,p["entry"]);self.close_position(sid,"MANDATORY_EOD",px)
        self._run_supporting_modules();log.info("V8.4 EOD complete")

if __name__=="__main__":
    TradingBotV84(dry_run=os.getenv("V82_DRY_RUN","0")=="1").run()
