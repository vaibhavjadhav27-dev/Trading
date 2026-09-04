"""Canonical DhanHQ v2 gateway for the integrated V8.2 engine.

All live broker I/O goes through this module.  It deliberately uses Dhan REST
for order/margin/position state and polling for fills; no legacy Dhan client is
used by the V8.2 orchestrator.
"""
from __future__ import annotations
import os, time, uuid, requests
from threading import Lock
from dhan_request_coordinator import global_dhan_coordinator


class BucketLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / max(float(per_second), 0.1)
        self.lock = Lock(); self.last = 0.0
    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.interval - (now - self.last)
            if delay > 0: time.sleep(delay)
            self.last = time.monotonic()


class DhanV82Gateway:
    LIVE = "https://api.dhan.co/v2"
    SANDBOX = "https://sandbox.dhan.co/v2"

    def __init__(self, client_id=None, access_token=None, session=None, dry_run=False):
        self.client_id = client_id or os.getenv("DHAN_CLIENT_ID", "")
        self.token = access_token or os.getenv("DHAN_ACCESS_TOKEN", "")
        self.dry_run = bool(dry_run or os.getenv("V82_DRY_RUN", "0") == "1")
        self.base_url = os.getenv("DHAN_BASE_URL", self.SANDBOX if os.getenv("V82_SANDBOX", "0") == "1" else self.LIVE).rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": self.token,
            "client-id": self.client_id,
        })
        # Dhan documented limits: orders 10/s, data 5/s, quotes 1/s, non-trading 20/s.
        self.order_rl = BucketLimiter(10)
        self.data_rl = BucketLimiter(5)
        self.quote_rl = BucketLimiter(1)
        self.nontrade_rl = BucketLimiter(20)

    def _request(self, method, endpoint, payload=None, kind="data", retries=2):
        if self.dry_run:
            return self._dry_response(method, endpoint, payload)
        limiter = {"order": self.order_rl, "quote": self.quote_rl, "data": self.data_rl, "nontrade": self.nontrade_rl}.get(kind, self.data_rl)
        url = self.base_url + endpoint
        last = None
        for attempt in range(retries + 1):
            global_dhan_coordinator().wait(kind)
            limiter.wait()
            try:
                if method == "GET":
                    resp = self.session.get(url, timeout=12)
                elif method == "DELETE":
                    resp = self.session.delete(url, timeout=12)
                elif method == "PUT":
                    resp = self.session.put(url, json=payload or {}, timeout=12)
                else:
                    resp = self.session.post(url, json=payload or {}, timeout=12)
                last = resp
                if resp.status_code in (200, 201, 202):
                    try: return resp.json()
                    except Exception: return {"http_status": resp.status_code}
                if resp.status_code == 401:
                    raise RuntimeError("Dhan authentication expired/invalid token")
                if resp.status_code == 429:
                    global_dhan_coordinator().penalize_429(15.0)
                    time.sleep(min(15.0, 1.5 * (attempt + 1))); continue
                if 500 <= resp.status_code < 600:
                    time.sleep(min(5.0, 1.0 * (attempt + 1))); continue
                raise RuntimeError(f"Dhan HTTP {resp.status_code}: {resp.text[:500]}")
            except requests.RequestException as exc:
                if attempt >= retries: raise
                time.sleep(min(5.0, 1.0 * (attempt + 1)))
        raise RuntimeError(f"Dhan request failed: {endpoint}; last={getattr(last, 'status_code', None)}")

    def _dry_response(self, method, endpoint, payload):
        if endpoint == "/fundlimit": return {"availabelBalance": 100000.0, "availableBalance": 100000.0}
        if endpoint == "/profile": return {"dhanClientId": self.client_id, "tokenValidity": "DRY", "activeSegment": "Equity, Derivative, Commodity"}
        if endpoint == "/ip/getIP": return {"primaryIP": "DRY"}
        if endpoint == "/marketfeed/ltp":
            ids=(payload or {}).get("NSE_EQ", []); return {"data":{"NSE_EQ":{str(x):{"last_price":100.0} for x in ids}}}
        if endpoint == "/marketfeed/ohlc":
            ids=(payload or {}).get("NSE_EQ", []); return {"data":{"NSE_EQ":{str(x):{"last_price":100.0,"ohlc":{"open":99.5,"close":99.5,"high":100.5,"low":99.0}} for x in ids}}}
        if endpoint == "/margincalculator":
            p=payload or {}; q=int(p.get("quantity",0)); px=float(p.get("price",0) or 0)
            return {"totalMargin":px*q,"availableBalance":100000.0,"insufficientBalance":0,"leverage":"1.00"}
        if endpoint == "/orders":
            q=int((payload or {}).get("quantity",0)); op=str((payload or {}).get("orderType","MARKET")); oid="DRY-"+uuid.uuid4().hex[:12]
            if op=="STOP_LOSS_MARKET": return {"orderId":oid,"orderStatus":"PENDING","filledQty":0,"averageTradedPrice":0}
            return {"orderId":oid,"orderStatus":"TRADED","filledQty":q,"averageTradedPrice":float((payload or {}).get("price",100) or 100)}
        if endpoint.startswith("/orders/"): return {"orderStatus":"TRADED","filledQty":1,"averageTradedPrice":100.0}
        if endpoint == "/positions": return []
        return {}

    def fund_limit(self): return self._request("GET", "/fundlimit", kind="nontrade")
    def get_balance(self):
        d=self.fund_limit() or {}
        if isinstance(d,dict) and isinstance(d.get("data"),dict): d=d["data"]
        return float(d.get("availableBalance", d.get("availabelBalance", 0)) or 0)

    def profile(self): return self._request("GET", "/profile", kind="nontrade")
    def get_static_ip(self): return self._request("GET", "/ip/getIP", kind="nontrade")

    def market_ltp(self, security_ids, exchange="NSE_EQ"):
        INDEX_SIDS = {"13", "25"}
        ids=[str(x) for x in security_ids]; out={}
        import time as _t
        for sid in ids:
            try:
                seg = "IDX_I" if sid in INDEX_SIDS else "NSE_EQ"
                d=self.get_ohlc_intraday(sid, seg, "1")
                if isinstance(d, dict) and d.get("close"):
                    out[sid]=float(d["close"][-1])
                _t.sleep(0.15)
            except Exception:
                pass
        return out

    def market_ohlc(self, security_ids, exchange="NSE_EQ"):
        INDEX_SIDS = {"13", "25"}
        ids=[str(x) for x in security_ids if str(x) not in INDEX_SIDS]; out={}
        import time as _t, logging
        for sid in ids[:180]:
            try:
                _t.sleep(0.25)
                d=self.get_ohlc_intraday(sid, "NSE_EQ", "5")
                if isinstance(d, dict) and d.get("close"):
                    closes=d["close"]; opens=d["open"]; highs=d["high"]; lows=d["low"]
                    if closes:
                        out[sid]={"ltp":float(closes[-1]),"open":float(opens[0]) if opens else 0,"close":float(closes[-1]),"high":float(max(float(x) for x in highs)) if highs else 0,"low":float(min(float(x) for x in lows)) if lows else 0}
            except Exception:
                pass
        logging.getLogger("V82_FINAL").info(f"market_ohlc: fetched {len(out)}/{len(ids)} via intraday charts")
        return out

    def get_ohlc_intraday(self, security_id, exchange="NSE_EQ", interval="5"):
        today=time.strftime("%Y-%m-%d")
        instrument="INDEX" if exchange=="IDX_I" else "EQUITY"
        return self._request("POST", "/charts/intraday", {"securityId":str(security_id),"exchangeSegment":exchange,"instrument":instrument,"interval":str(interval),"fromDate":today,"toDate":today}, kind="data")

    def get_historical_daily(self, security_id, exchange="NSE_EQ", days=70):
        from datetime import datetime,timedelta
        to=datetime.now().strftime("%Y-%m-%d"); fr=(datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d")
        return self._request("POST", "/charts/historical", {"securityId":str(security_id),"exchangeSegment":exchange,"instrument":"INDEX" if exchange=="IDX_I" else "EQUITY","expiryCode":0,"fromDate":fr,"toDate":to}, kind="data")

    def calculate_margin(self, security_id, qty, transaction_type, price):
        return self._request("POST", "/margincalculator", {"dhanClientId":self.client_id,"exchangeSegment":"NSE_EQ","transactionType":transaction_type,"securityId":str(security_id),"quantity":int(qty),"productType":"INTRADAY","orderType":"MARKET","price":float(price or 0)}, kind="order")

    def place_order(self, security_id, qty, price=0, transaction_type="BUY", order_type="MARKET", trigger_price=0, correlation_id=None):
        is_slm=str(order_type).upper()=="STOP_LOSS_MARKET"
        payload={"dhanClientId":self.client_id,"correlationId":correlation_id or ("V82-"+uuid.uuid4().hex[:20]),"transactionType":transaction_type,"exchangeSegment":"NSE_EQ","productType":"INTRADAY","orderType":order_type,"validity":"DAY","securityId":str(security_id),"quantity":int(qty),"disclosedQuantity":0,"price":0.0 if is_slm else float(price or 0),"triggerPrice":float(trigger_price or 0),"afterMarketOrder":False}
        return self._request("POST", "/orders", payload, kind="order")

    def get_order_status(self, order_id):
        r=self._request("GET", f"/orders/{order_id}", kind="order")
        if isinstance(r,list):
            return next((o for o in r if str(o.get("orderId",""))==str(order_id)),r[0] if r else {})
        if isinstance(r,dict) and "data" in r:
            d=r["data"]
            if isinstance(d,list):
                return next((o for o in d if str(o.get("orderId",""))==str(order_id)),d[0] if d else {})
            return d
        return r or {}
    def get_trades_for_order(self, order_id): return self._request("GET", f"/trades/{order_id}", kind="order")
    def get_orders(self):
        r=self._request("GET", "/orders", kind="order") or []
        if isinstance(r,dict): return r.get("data",r.get("orders",[])) or []
        return r
    def get_trades(self):
        r=self._request("GET", "/trades", kind="order") or []
        if isinstance(r,dict): return r.get("data",r.get("trades",[])) or []
        return r
    def get_order_by_correlation(self, correlation_id):
        r=self._request("GET", f"/orders/external/{correlation_id}", kind="order") or {}
        if isinstance(r,dict) and "data" in r and isinstance(r["data"],dict): return r["data"]
        return r
    def get_positions(self): return self._request("GET", "/positions", kind="nontrade")
    def cancel_order(self, order_id): return self._request("DELETE", f"/orders/{order_id}", kind="order")

    def verify_fill(self, order_id, timeout=20):
        end=time.time()+timeout; last={}
        while time.time()<end:
            d=self.get_order_status(order_id) or {}
            if isinstance(d,list):
                d=next((o for o in d if str(o.get("orderId",""))==str(order_id)),d[0] if d else {})
            last=d
            st=str(d.get("orderStatus",d.get("status",""))).upper()
            filled=int(float(d.get("filledQty",d.get("tradedQty",0)) or 0)); remaining=int(float(d.get("remainingQuantity",0) or 0))
            price=float(d.get("averageTradedPrice",d.get("avgTradedPrice",0)) or 0)
            if st in ("TRADED","COMPLETE"):
                return {"status":"FILLED","qty":filled or int(d.get("quantity",0) or 0),"remaining":0,"price":price,"raw":d}
            if st=="PART_TRADED" and filled>0:
                return {"status":"PARTIAL","qty":filled,"remaining":remaining,"price":price,"raw":d}
            if st in ("REJECTED","CANCELLED","EXPIRED"):
                return {"status":st,"qty":filled,"remaining":remaining,"price":price,"raw":d}
            time.sleep(.5)
        return {"status":"TIMEOUT","qty":0,"remaining":0,"price":0,"raw":last}

    def verify_position(self, security_id, side=None):
        ps=self.get_positions() or []; ps=ps.get("data",[]) if isinstance(ps,dict) else ps
        for p in ps if isinstance(ps,list) else []:
            if str(p.get("securityId",p.get("security_id","")))==str(security_id):
                nq=int(float(p.get("netQty",p.get("net_qty",0)) or 0))
                if side=="LONG" and nq<=0:return 0
                if side=="SHORT" and nq>=0:return 0
                return nq
        return 0

    def place_hard_sl(self, security_id, qty, side, trigger):
        txn="SELL" if side=="LONG" else "BUY"
        for attempt in range(2):
            try:
                resp=self.place_order(security_id,qty,0,txn,"STOP_LOSS_MARKET",trigger_price=trigger)
                if resp and isinstance(resp,dict) and resp.get("orderId"):
                    return resp
                import logging; logging.getLogger("v82_gw").warning(f"Hard SL attempt {attempt+1}: no orderId: {str(resp)[:200]}")
            except Exception as e:
                import logging; logging.getLogger("v82_gw").warning(f"Hard SL attempt {attempt+1} failed: {e}")
            if attempt==0:
                import time; time.sleep(1)
                # Adjust trigger slightly (widen by 0.1%) in case price moved
                trigger=round(trigger*(0.999 if side=="LONG" else 1.001),2)
        import logging; logging.getLogger("v82_gw").error(f"HARD SL FAILED after 2 attempts SID={security_id} side={side} trigger={trigger}")
        return None

    def preflight(self, enforce_static_ip=False):
        if not self.client_id or not self.token:return False,"missing DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN"
        try:
            p=self.profile(); self.fund_limit()
            if enforce_static_ip:
                ip=self.get_static_ip(); primary=str((ip or {}).get("primaryIP","") or "")
                public=os.getenv("V82_PUBLIC_IP","")
                if public and primary and public!=primary:return False,f"static IP mismatch: EC2={public} DhanPrimary={primary}"
                if not primary and not self.dry_run:return False,"Dhan primary static IP not configured"
            return True,"Dhan profile/funds/static-IP preflight OK"
        except Exception as e:return False,str(e)
