#!/usr/bin/env python3
"""
mcx_native_orb_v2.py - MCX Evening Session: Native ORB 3-Layer Architecture
Replaces: mcx_shadow_trader.py (US-Mirror approach)
"""
import sys, json, time, logging, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple

try:
    import requests
    import yfinance as yf
except ImportError as e:
    print(f"Missing: {e}. pip install yfinance requests")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/yf_cache")
os.makedirs("/tmp/yf_cache", exist_ok=True)

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
MCX_LOG_DIR = Path("trade_logs/mcx")
MCX_LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = Path("mcx_native_state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "mcx_native.log")])
log = logging.getLogger("mcx_native")

LIVE_MODE = False
ORB_START_HOUR, ORB_START_MIN = 19, 0
ORB_END_HOUR, ORB_END_MIN = 19, 15
SESSION_END_HOUR, SESSION_END_MIN = 23, 0
ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MIN = 22, 15
SL_PCT = 0.005
T1_PCT = 0.010
T2_PCT = 0.020
MAX_CAPITAL_PCT = 0.20
DAILY_LOSS_LIMIT_PCT = 3.0
POLL_SECONDS = 30
ORB_BUFFER_PCT = 0.001

CONTRACTS = {
    "CRUDEOILM": {"us_ticker": "CL=F", "mcx_symbol": "CRUDEOILM", "lot_size": 10, "margin_approx": 5000, "active": True},
    "NATGASMINI": {"us_ticker": "NG=F", "mcx_symbol": "NATURALGAS", "lot_size": 250, "margin_approx": 3000, "active": True},
    "GOLDPETAL": {"us_ticker": "GC=F", "mcx_symbol": "GOLDPETAL", "lot_size": 1, "margin_approx": 200, "active": True},
}
FALLBACK_SIDS = {
    "CRUDEOILM": ("560978", "CRUDEOILM-19Aug2026-FUT"),
    "NATGASMINI": ("561497", "NATGASMINI-26Aug2026-FUT"),
    "GOLDPETAL": ("562056", "GOLDPETAL-31Aug2026-FUT"),
}

@dataclass
class MCX_ORB:
    contract: str; security_id: str; orb_start: str; orb_end: str
    high: float = 0.0; low: float = 0.0; vwap: float = 0.0; frozen: bool = False

@dataclass
class MCX_Signal:
    contract: str; side: str; entry_price: float; stop: float; t1: float; t2: float
    orb_high: float = 0.0; orb_low: float = 0.0; mcx_momentum: float = 0.0
    mcx_rvol: float = 0.0; mcx_vwap: float = 0.0; us_direction: str = ""
    us_confirms: bool = False; expected_move_pct: float = 0.0; risk_pct: float = 0.0
    expected_r: float = 0.0; timestamp: str = ""; score: float = 0.0

@dataclass
class ShadowPosition:
    contract: str; side: str; entry: float; qty: int; sl: float; t1: float; t2: float
    peak: float = 0.0; entry_time: str = ""; status: str = "OPEN"

class MCXDhanClient:
    def __init__(self):
        from secrets_manager import get_dhan_token, get_dhan_client_id
        self.token = get_dhan_token()
        self.client_id = get_dhan_client_id()
        self.base_url = "https://api.dhan.co/v2"
        self.session = requests.Session()
        self.session.headers.update({"access-token": self.token, "client-id": self.client_id,
            "Content-Type": "application/json", "Accept": "application/json"})
        self._last_request = 0.0
        log.info(f"MCX DhanClient initialized (client_id={self.client_id[:6]}...)")

    def _throttle(self, min_gap=0.3):
        elapsed = time.time() - self._last_request
        if elapsed < min_gap: time.sleep(min_gap - elapsed)
        self._last_request = time.time()

    def get_mcx_candles(self, security_id, interval="1"):
        self._throttle()
        try:
            payload = {"securityId": str(security_id), "exchangeSegment": "MCX_COMM",
                "instrument": "FUTCOM", "interval": interval,
                "fromDate": datetime.now(IST).strftime("%Y-%m-%d"),
                "toDate": datetime.now(IST).strftime("%Y-%m-%d")}
            r = self.session.post(f"{self.base_url}/charts/intraday", json=payload, timeout=12)
            if r.status_code == 200: return r.json()
            log.warning(f"MCX candles SID={security_id}: HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"MCX candles SID={security_id}: {e}")
        return None

    def get_mcx_ltp(self, security_id):
        data = self.get_mcx_candles(security_id)
        if data and "close" in data and data["close"]: return float(data["close"][-1])
        return 0.0

    def get_mcx_orb(self, security_id, lookback_minutes=15):
        data = self.get_mcx_candles(security_id)
        if not data or "high" not in data: return 0.0, 0.0, 0.0
        highs, lows, closes = data.get("high",[]), data.get("low",[]), data.get("close",[])
        if len(closes) < 2: return 0.0, 0.0, 0.0
        n = min(lookback_minutes, len(highs))
        return float(max(highs[:n])), float(min(lows[:n])), float(closes[-1])

    def get_mcx_momentum(self, security_id):
        data = self.get_mcx_candles(security_id, "5")
        if not data or "close" not in data or len(data["close"]) < 4: return 0.0, 0.0
        closes = [float(c) for c in data["close"]]
        r5 = (closes[-1]-closes[-2])/closes[-2]*100 if closes[-2] else 0
        r15 = (closes[-1]-closes[-4])/closes[-4]*100 if len(closes)>=4 and closes[-4] else 0
        return r5, r15

    def get_mcx_rvol(self, security_id):
        data = self.get_mcx_candles(security_id)
        if not data or "volume" not in data: return 0.0
        volumes = [int(v) for v in data.get("volume",[]) if v]
        return sum(volumes)/10000 if volumes else 0.0

    def get_balance(self):
        try:
            r = self.session.get(f"{self.base_url}/fundlimit", timeout=10)
            if r.status_code == 200:
                d = r.json()
                return float(d.get("availabelBalance", d.get("data",{}).get("availabelBalance",0) if isinstance(d.get("data"),dict) else 0))
        except Exception as e: log.warning(f"Balance: {e}")
        return 0.0

    def place_order(self, security_id, qty, side, price=0):
        if not LIVE_MODE:
            log.info(f"[SHADOW] Would place {side} {qty} SID={security_id}")
            return "SHADOW_ORDER"
        payload = {"dhanClientId": self.client_id, "transactionType": "BUY" if side=="LONG" else "SELL",
            "exchangeSegment": "MCX_COMM", "productType": "INTRADAY", "orderType": "MARKET",
            "validity": "DAY", "securityId": str(security_id), "quantity": int(qty),
            "price": 0, "triggerPrice": 0, "disclosedQuantity": 0, "afterMarketOrder": False}
        try:
            r = self.session.post(f"{self.base_url}/orders", json=payload, timeout=12)
            return r.json().get("orderId") if r.status_code in (200,201) else None
        except Exception as e: log.error(f"MCX order: {e}"); return None

    def place_hard_sl(self, security_id, qty, side, trigger):
        if not LIVE_MODE:
            log.info(f"[SHADOW] Would place SL {side} {qty} @ {trigger}")
            return "SHADOW_SL"
        txn = "SELL" if side=="LONG" else "BUY"
        payload = {"dhanClientId": self.client_id, "transactionType": txn,
            "exchangeSegment": "MCX_COMM", "productType": "INTRADAY",
            "orderType": "STOP_LOSS_MARKET", "validity": "DAY",
            "securityId": str(security_id), "quantity": int(qty),
            "price": 0, "triggerPrice": float(trigger), "disclosedQuantity": 0}
        try:
            r = self.session.post(f"{self.base_url}/orders", json=payload, timeout=12)
            return r.json().get("orderId") if r.status_code in (200,201) else None
        except Exception as e: log.error(f"MCX SL: {e}"); return None

def get_us_direction(ticker):
    try:
        data = yf.download(ticker, period="1d", interval="5m", progress=False)
        if data is None or len(data) < 3: return "NEUTRAL"
        closes = data["Close"].values
        if len(closes) >= 3:
            r = (closes[-1]-closes[0])/closes[0]*100
            if r > 0.15: return "BULLISH"
            elif r < -0.15: return "BEARISH"
    except Exception as e: log.warning(f"US direction {ticker}: {e}")
    return "NEUTRAL"

def check_mcx_breakout(orb, current_price, momentum_5m, rvol, us_direction):
    if not orb.frozen or orb.high<=0 or orb.low<=0 or current_price<=0: return None
    orb_range = orb.high - orb.low
    if orb_range <= 0: return None
    side = None
    if current_price > orb.high*(1+ORB_BUFFER_PCT): side = "LONG"
    elif current_price < orb.low*(1-ORB_BUFFER_PCT): side = "SHORT"
    if not side: return None
    if side=="LONG" and momentum_5m < 0: return None
    if side=="SHORT" and momentum_5m > 0: return None
    if rvol < 0.5: return None
    us_confirms = (side=="LONG" and us_direction in ("BULLISH","NEUTRAL")) or (side=="SHORT" and us_direction in ("BEARISH","NEUTRAL"))
    if side=="LONG":
        stop=round(current_price*(1-SL_PCT),2); t1=round(current_price*(1+T1_PCT),2); t2=round(current_price*(1+T2_PCT),2)
    else:
        stop=round(current_price*(1+SL_PCT),2); t1=round(current_price*(1-T1_PCT),2); t2=round(current_price*(1-T2_PCT),2)
    risk_pct=SL_PCT*100; expected_move_pct=T1_PCT*100; expected_r=expected_move_pct/risk_pct if risk_pct>0 else 0
    score = 0.0
    breakout_strength = abs(current_price-(orb.high if side=="LONG" else orb.low))/orb_range
    score += min(breakout_strength*30, 30)
    score += min(abs(momentum_5m)*10, 20)
    score += min(rvol*10, 15)
    score += 15 if us_confirms else 5
    score += min(expected_r*5, 20)
    return MCX_Signal(contract=orb.contract, side=side, entry_price=current_price,
        stop=stop, t1=t1, t2=t2, orb_high=orb.high, orb_low=orb.low,
        mcx_momentum=momentum_5m, mcx_rvol=rvol, mcx_vwap=orb.vwap,
        us_direction=us_direction, us_confirms=us_confirms,
        expected_move_pct=expected_move_pct, risk_pct=risk_pct, expected_r=expected_r,
        timestamp=datetime.now(IST).isoformat(timespec="seconds"), score=round(score,1))

def calculate_lots(balance, entry_price, stop_price, lot_size, margin_per_lot):
    risk_per_lot = abs(entry_price-stop_price)*lot_size
    if risk_per_lot <= 0: return 0
    max_risk = balance*0.015
    lots_by_risk = int(max_risk/risk_per_lot)
    lots_by_margin = int(balance*MAX_CAPITAL_PCT/margin_per_lot) if margin_per_lot>0 else 0
    return max(1, min(lots_by_risk, lots_by_margin))

def log_mcx_orb(orbs):
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    path = MCX_LOG_DIR / f"orb_{date_str}.jsonl"
    with open(path, "a") as f:
        for name, orb in orbs.items():
            f.write(json.dumps(asdict(orb), default=str)+"\n")
    log.info("MCX ORB logged to %s", path)

def log_mcx_signal(signal, action):
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    path = MCX_LOG_DIR / f"signals_{date_str}.jsonl"
    record = asdict(signal); record["action"] = action
    with open(path, "a") as f: f.write(json.dumps(record, default=str)+"\n")

def log_mcx_exit(position, exit_price, reason):
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    path = MCX_LOG_DIR / f"exits_{date_str}.jsonl"
    pnl_pct = ((exit_price-position.entry)/position.entry*100) if position.side=="LONG" else ((position.entry-exit_price)/position.entry*100)
    record = {"timestamp": datetime.now(IST).isoformat(timespec="seconds"),
        "contract": position.contract, "side": position.side,
        "entry": position.entry, "exit": exit_price, "qty": position.qty,
        "pnl_pct": round(pnl_pct,4), "peak": position.peak, "reason": reason}
    with open(path, "a") as f: f.write(json.dumps(record, default=str)+"\n")
    log.info("EXIT %s %s pnl=%.3f%% reason=%s", position.side, position.contract, pnl_pct, reason)

def validate_contract(security_id, dhan):
    data = dhan.get_mcx_candles(security_id)
    if data and "close" in data and data["close"]: return True
    log.warning(f"Contract SID={security_id} may be expired")
    return False

def now_ist(): return datetime.now(IST)
def ist_time(h, m): return now_ist().replace(hour=h, minute=m, second=0, microsecond=0)

def run_mcx_session():
    log.info("="*60)
    log.info("MCX NATIVE ORB SESSION - %s", "LIVE" if LIVE_MODE else "SHADOW")
    log.info("="*60)
    try: dhan = MCXDhanClient()
    except Exception as e: log.error(f"DhanClient failed: {e}"); return
    balance = dhan.get_balance()
    if balance <= 0: log.error("No balance"); return
    log.info(f"Balance: Rs.{balance:,.2f}")
    contract_ids = {}
    for key, cfg in CONTRACTS.items():
        if not cfg["active"]: continue
        fb = FALLBACK_SIDS.get(key)
        if fb:
            sid, name = fb
            if validate_contract(sid, dhan):
                contract_ids[key] = (sid, name)
                log.info(f"  {key}: {name} (SID={sid}) VALID")
            else: log.error(f"  {key}: SID={sid} EXPIRED")
    if not contract_ids: log.error("No valid contracts"); return
    orb_start = ist_time(ORB_START_HOUR, ORB_START_MIN)
    orb_end = ist_time(ORB_END_HOUR, ORB_END_MIN)
    session_end = ist_time(SESSION_END_HOUR, SESSION_END_MIN)
    if now_ist() < orb_start:
        wait = (orb_start-now_ist()).total_seconds()
        log.info(f"Waiting {wait:.0f}s for ORB window...")
        time.sleep(max(0, wait))
    log.info("Recording MCX native 15-min ORB...")
    orbs = {}
    while now_ist() < orb_end:
        for key, (sid, name) in contract_ids.items():
            h, l, cur = dhan.get_mcx_orb(sid, 15)
            if h > 0 and l > 0:
                if key not in orbs:
                    orbs[key] = MCX_ORB(contract=key, security_id=sid, orb_start=orb_start.isoformat(), orb_end=orb_end.isoformat())
                orbs[key].high = max(orbs[key].high, h)
                orbs[key].low = min(orbs[key].low, l) if orbs[key].low > 0 else l
        time.sleep(15)
    for key, orb in orbs.items():
        orb.frozen = True
        log.info(f"  {key}: ORB H={orb.high:.2f} L={orb.low:.2f} range={orb.high-orb.low:.2f}")
    log_mcx_orb(orbs)
    if not orbs: log.error("No ORBs recorded"); return
    us_directions = {}
    for key, cfg in CONTRACTS.items():
        if key in contract_ids:
            us_dir = get_us_direction(cfg["us_ticker"])
            us_directions[key] = us_dir
            log.info(f"  {key} US: {us_dir}")
    log.info("="*60)
    log.info("Monitoring for native MCX ORB breakouts...")
    shadow_positions = []
    session_pnl = 0.0
    while now_ist() < session_end:
        for pos in shadow_positions:
            if pos.status != "OPEN": continue
            sid = orbs[pos.contract].security_id
            ltp = dhan.get_mcx_ltp(sid)
            if ltp <= 0: continue
            if pos.side=="LONG": pos.peak = max(pos.peak, ltp)
            else: pos.peak = min(pos.peak, ltp) if pos.peak>0 else ltp
            reason = None
            if pos.side=="LONG":
                if ltp<=pos.sl: reason="HARD_SL"
                elif ltp>=pos.t2: reason="T2_HIT"
                elif ltp>=pos.t1: reason="T1_HIT"
            else:
                if ltp>=pos.sl: reason="HARD_SL"
                elif ltp<=pos.t2: reason="T2_HIT"
                elif ltp<=pos.t1: reason="T1_HIT"
            if reason:
                pos.status = reason
                pnl = ((ltp-pos.entry)/pos.entry*100) if pos.side=="LONG" else ((pos.entry-ltp)/pos.entry*100)
                session_pnl += pnl
                log_mcx_exit(pos, ltp, reason)
        open_count = sum(1 for p in shadow_positions if p.status=="OPEN")
        _now = now_ist()
        past_cutoff = _now.hour>ENTRY_CUTOFF_HOUR or (_now.hour==ENTRY_CUTOFF_HOUR and _now.minute>=ENTRY_CUTOFF_MIN)
        if not past_cutoff and session_pnl>-(DAILY_LOSS_LIMIT_PCT) and open_count==0:
            for key, orb in orbs.items():
                if not orb.frozen: continue
                sid = orb.security_id
                ltp = dhan.get_mcx_ltp(sid)
                if ltp <= 0: continue
                mom5, mom15 = dhan.get_mcx_momentum(sid)
                rvol = dhan.get_mcx_rvol(sid)
                us_dir = us_directions.get(key, "NEUTRAL")
                signal = check_mcx_breakout(orb, ltp, mom5, rvol, us_dir)
                if signal and signal.score >= 50:
                    log.info(f"SIGNAL: {signal.side} {key} @ Rs.{ltp:.2f} score={signal.score} R={signal.expected_r:.1f}")
                    log_mcx_signal(signal, "ENTRY")
                    confirmed = True
                    for chk in range(3):
                        time.sleep(10)
                        chk_ltp = dhan.get_mcx_ltp(sid)
                        if chk_ltp <= 0: confirmed=False; break
                        if signal.side=="LONG" and chk_ltp<orb.high: confirmed=False; break
                        if signal.side=="SHORT" and chk_ltp>orb.low: confirmed=False; break
                    if confirmed:
                        cfg = CONTRACTS[key]
                        lots = calculate_lots(balance, signal.entry_price, signal.stop, cfg["lot_size"], cfg["margin_approx"])
                        log.info(f"ENTRY: {signal.side} {key} {lots} lots @ Rs.{signal.entry_price:.2f} SL={signal.stop:.2f}")
                        dhan.place_order(orb.security_id, lots*cfg["lot_size"], signal.side)
                        if LIVE_MODE: dhan.place_hard_sl(orb.security_id, lots*cfg["lot_size"], signal.side, signal.stop)
                        shadow_positions.append(ShadowPosition(contract=key, side=signal.side,
                            entry=signal.entry_price, qty=lots, sl=signal.stop, t1=signal.t1, t2=signal.t2,
                            peak=signal.entry_price, entry_time=now_ist().isoformat(timespec="seconds")))
                    else: log_mcx_signal(signal, "FALSE_BREAKOUT")
        time.sleep(POLL_SECONDS)
    for pos in shadow_positions:
        if pos.status == "OPEN":
            sid = orbs[pos.contract].security_id
            ltp = dhan.get_mcx_ltp(sid)
            if ltp<=0: ltp=pos.entry
            pos.status = "EOD"
            pnl = ((ltp-pos.entry)/pos.entry*100) if pos.side=="LONG" else ((pos.entry-ltp)/pos.entry*100)
            session_pnl += pnl
            log_mcx_exit(pos, ltp, "MANDATORY_EOD")
    log.info("="*60)
    log.info(f"SESSION COMPLETE: signals={len(shadow_positions)} pnl={session_pnl:+.3f}%%")
    log.info("="*60)
    summary = {"date": now_ist().strftime("%Y-%m-%d"), "session_pnl_pct": round(session_pnl,4),
        "total_signals": len(shadow_positions), "positions": [asdict(p) for p in shadow_positions],
        "orbs": {k: asdict(v) for k, v in orbs.items()}, "us_directions": us_directions}
    with open(STATE_PATH, "w") as f: json.dump(summary, f, indent=2, default=str)

if __name__ == "__main__":
    if "--close" in sys.argv: log.info("MCX --close: session ended"); sys.exit(0)
    run_mcx_session()
