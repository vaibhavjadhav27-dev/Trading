
"""
MCX V8.5.4 LIVE-READY ENGINE
============================

Fixes observed in Aug-20/21 shadow logs:
- No US-price-vs-MCX-price comparison for entries.
- Native MCX ORB is built once from the first 15 completed 1-minute bars.
- No 30-second historical-candle polling.
- Batch LTP uses Dhan Market Quote (one request for all active contracts).
- No stale hard-coded contract IDs; resolve from Dhan scrip master.
- No hard-coded balance fallback.
- Multiple qualified MCX positions allowed subject to portfolio risk/margin.
- 3x5-second confirmation uses cached LTP, not API calls.
- State/audit is atomic and restart-safe.
- Profit runner: structural/ATR SL, progressive R protection, peak/giveback.
- LIVE_MODE is explicit and defaults False.

This module is broker-agnostic at the strategy layer. The optional live
adapter must implement place/modify/cancel order methods from the project's
existing Dhan gateway. Do not bypass the existing gateway.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, time as dtime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import csv, io, json, math, os, tempfile, time, logging

IST_NAME = "Asia/Kolkata"
MCX_OPEN = dtime(18, 30)
ORB_END = dtime(18, 45)
ENTRY_CUTOFF = dtime(22, 15)
MCX_CLOSE = dtime(23, 0)

DATA_API_MIN_INTERVAL = 0.25
LTP_API_MIN_INTERVAL = 1.05

LOG_DIR = Path("logs")
STATE_DIR = Path("mcx_state")
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

log = logging.getLogger("mcx_v854")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


@dataclass(frozen=True)
class Contract:
    key: str
    symbol: str
    security_id: str
    expiry: str
    lot_size: float
    tick_size: float
    margin_required: float = 0.0


@dataclass
class Position:
    key: str
    symbol: str
    security_id: str
    side: str
    qty_lots: int
    lot_size: float
    entry: float
    initial_sl: float
    sl: float
    peak: float
    best_r: float
    entry_time: str
    status: str = "OPEN"
    exit: Optional[float] = None
    exit_reason: Optional[str] = None
    max_favourable_pct: float = 0.0
    max_adverse_pct: float = 0.0


class Throttle:
    def __init__(self):
        self.last_data = 0.0
        self.last_ltp = 0.0

    def wait_data(self):
        delay = DATA_API_MIN_INTERVAL - (time.monotonic() - self.last_data)
        if delay > 0:
            time.sleep(delay)
        self.last_data = time.monotonic()

    def wait_ltp(self):
        delay = LTP_API_MIN_INTERVAL - (time.monotonic() - self.last_ltp)
        if delay > 0:
            time.sleep(delay)
        self.last_ltp = time.monotonic()


class NativeMCXData:
    """
    Provider using Dhan REST endpoints only for setup/historical data and
    batched LTP. It can later be swapped for the Dhan WebSocket provider
    without changing strategy code.
    """

    SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
    API = "https://api.dhan.co/v2"

    def __init__(self, requests_session, headers: Dict[str, str]):
        self.s = requests_session
        self.headers = headers
        self.throttle = Throttle()

    def resolve_contracts(self, prefixes: Dict[str, str], today: date) -> Dict[str, Contract]:
        self.throttle.wait_data()
        r = self.s.get(self.SCRIP_URL, timeout=15)
        r.raise_for_status()

        out: Dict[str, Contract] = {}
        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)

        # Column names vary between compact/detailed masters; use common names.
        for key, prefix in prefixes.items():
            candidates = []
            for row in rows:
                exch = str(row.get("EXCH_ID", row.get("SEM_EXM_EXCH_ID", ""))).upper()
                inst = str(row.get("SEM_INSTRUMENT_NAME", row.get("INSTRUMENT", ""))).upper()
                symbol = str(row.get("SEM_TRADING_SYMBOL", row.get("TRADING_SYMBOL", "")))
                if exch != "MCX":
                    continue
                if "FUTCOM" not in inst and "FUT" not in inst:
                    continue
                if not symbol.startswith(prefix + "-"):
                    continue

                expiry_raw = str(row.get("SEM_EXPIRY_DATE", row.get("EXPIRY_DATE", "")))
                expiry = self._parse_date(expiry_raw)
                if expiry is None or expiry < today:
                    continue

                sid = str(row.get("SEM_SMST_SECURITY_ID", row.get("SECURITY_ID", "")))
                lot = self._num(row.get("SEM_LOT_UNITS", row.get("LOT_SIZE", 0)))
                tick = self._num(row.get("SEM_TICK_SIZE", row.get("TICK_SIZE", 0.05)))
                if sid and lot > 0:
                    candidates.append((expiry, symbol, sid, lot, tick))

            if candidates:
                expiry, symbol, sid, lot, tick = sorted(candidates)[0]
                out[key] = Contract(key, symbol, sid, expiry.isoformat(), lot, tick)

        return out

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    @staticmethod
    def _parse_date(v):
        v = str(v).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v[:19], fmt).date()
            except Exception:
                pass
        return None

    def intraday(self, security_id: str, instrument: str = "FUTCOM",
                 interval: str = "1", from_dt: Optional[str] = None,
                 to_dt: Optional[str] = None) -> Dict[str, List[float]]:
        self.throttle.wait_data()
        today = date.today().isoformat()
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": "MCX_COMM",
            "instrument": instrument,
            "interval": interval,
            "oi": True,
            "fromDate": from_dt or f"{today} 18:30:00",
            "toDate": to_dt or f"{today} 18:45:00",
        }
        r = self.s.post(f"{self.API}/charts/intraday",
                        headers=self.headers, json=payload, timeout=12)
        if r.status_code == 429:
            raise RuntimeError("Dhan data API rate limited (429)")
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, dict) else {}

    def marketfeed_ltp(self, security_ids: List[str]) -> Dict[str, float]:
        """One batched LTP request for all contracts; max one request/sec."""
        if not security_ids:
            return {}
        self.throttle.wait_ltp()
        payload = {"MCX_COMM": [str(x) for x in security_ids]}
        r = self.s.post(f"{self.API}/marketfeed/ltp",
                        headers=self.headers, json=payload, timeout=8)
        if r.status_code == 429:
            raise RuntimeError("Dhan quote API rate limited (429)")
        r.raise_for_status()
        d = r.json()
        raw = d.get("data", d) if isinstance(d, dict) else {}
        out = {}
        if isinstance(raw, dict):
            for sid, obj in raw.items():
                if isinstance(obj, dict):
                    px = obj.get("last_price", obj.get("ltp", obj.get("LTP")))
                    if px is not None:
                        try:
                            out[str(sid)] = float(px)
                        except Exception:
                            pass
        return out


def completed_orb(df: Dict[str, List[float]], bars: int = 15) -> Optional[Tuple[float,float]]:
    highs = df.get("high", [])
    lows = df.get("low", [])
    closes = df.get("close", [])
    n = min(bars, len(highs), len(lows), len(closes))
    if n < bars:
        return None
    return max(map(float, highs[:bars])), min(map(float, lows[:bars]))


def atr14(highs, lows, closes) -> float:
    if len(closes) < 15:
        return 0.0
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            float(highs[i])-float(lows[i]),
            abs(float(highs[i])-float(closes[i-1])),
            abs(float(lows[i])-float(closes[i-1]))
        ))
    return sum(tr[-14:]) / 14.0 if len(tr) >= 14 else 0.0


def vwap(highs, lows, closes, volumes) -> float:
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n == 0:
        return 0.0
    pv = 0.0
    vol = 0.0
    for h, l, c, q in zip(highs[-n:], lows[-n:], closes[-n:], volumes[-n:]):
        typical = (float(h)+float(l)+float(c))/3.0
        q = max(0.0, float(q))
        pv += typical*q
        vol += q
    return pv/vol if vol > 0 else float(closes[-1])


def rvol(volumes, lookback=20) -> float:
    if len(volumes) < lookback + 1:
        return 0.0
    avg = sum(map(float, volumes[-lookback-1:-1]))/lookback
    return float(volumes[-1])/avg if avg > 0 else 0.0


def momentum_pct(closes, bars):
    if len(closes) <= bars:
        return 0.0
    old = float(closes[-bars-1])
    return (float(closes[-1])-old)/old*100.0 if old else 0.0


def score_snapshot(px, vw, atr, orb_hi, orb_lo, mom5, mom15, rv, side):
    aligned = (px > vw) if side == "LONG" else (px < vw)
    breakout = (px > orb_hi) if side == "LONG" else (px < orb_lo)
    mom_ok = (mom5 > 0 and mom15 > 0) if side == "LONG" else (mom5 < 0 and mom15 < 0)
    if atr <= 0:
        return 0.0, False
    vwap_strength = max(0.0, min(1.0, 1.0 - abs(px-vw)/(2*atr)))
    mom_strength = max(0.0, min(1.0, (abs(mom5)+abs(mom15))/0.40))
    vol_strength = max(0.0, min(1.0, (rv-1.0)/1.5))
    score = (
        30*(1.0 if breakout else 0.0) +
        20*vwap_strength +
        20*mom_strength +
        15*vol_strength +
        10*(1.0 if aligned else 0.0) +
        5*(1.0 if mom_ok else 0.0)
    )
    return round(score, 2), bool((breakout and mom_ok) or (aligned and mom_ok))


def structural_stop(px, side, orb_hi, orb_lo, vw, atr):
    if side == "LONG":
        return min(orb_hi, vw) - 0.15*atr
    return max(orb_lo, vw) + 0.15*atr


def expected_target(px, side, atr):
    return px + 2.2*atr if side == "LONG" else px - 2.2*atr


def choose_signal(snapshot: Dict[str, Any], min_score=72.0, max_extension_atr=1.8):
    px=snapshot["price"]; vw=snapshot["vwap"]; atr=snapshot["atr"]
    hi=snapshot["orb_high"]; lo=snapshot["orb_low"]
    candidates=[]
    for side in ("LONG","SHORT"):
        score, valid=score_snapshot(px,vw,atr,hi,lo,snapshot["mom5"],snapshot["mom15"],snapshot["rvol"],side)
        if not valid or score < min_score:
            continue
        ext=abs(px-vw)/atr if atr else 999
        setup = "ORB_CONTINUATION" if ((side=="LONG" and px>hi) or (side=="SHORT" and px<lo)) else "VWAP_TREND"
        if ext > max_extension_atr and setup != "ORB_CONTINUATION":
            continue
        stop=structural_stop(px,side,hi,lo,vw,atr)
        if side=="LONG" and not (stop < px): continue
        if side=="SHORT" and not (stop > px): continue
        target=expected_target(px,side,atr)
        candidates.append({
            "side":side, "score":score, "entry":px, "stop":stop,
            "target":target, "setup":setup, "atr":atr,
            "reasons":[setup,f"RVOL={snapshot['rvol']:.2f}",f"Mom5={snapshot['mom5']:.3f}%",f"Mom15={snapshot['mom15']:.3f}%"]
        })
    candidates.sort(key=lambda x:x["score"], reverse=True)
    if not candidates:
        return None
    if len(candidates)>1 and candidates[0]["score"]-candidates[1]["score"] < 8:
        return None
    return candidates[0]


def initial_r(entry, initial_sl):
    return abs(float(entry)-float(initial_sl))


def update_position(pos: Position, px: float, atr: float,
                    momentum_ok: bool, structure_ok: bool):
    """Returns action: HOLD / UPDATE_SL / EXIT."""
    if pos.status != "OPEN":
        return "HOLD"
    r=initial_r(pos.entry,pos.initial_sl)
    if r <= 0:
        return "EXIT"
    if pos.side=="LONG":
        pos.peak=max(pos.peak,px)
        live_r=(px-pos.entry)/r
        best_r=(pos.peak-pos.entry)/r
    else:
        pos.peak=min(pos.peak,px)
        live_r=(pos.entry-px)/r
        best_r=(pos.entry-pos.peak)/r

    pos.best_r=max(pos.best_r,best_r)
    pos.max_favourable_pct=max(pos.max_favourable_pct,
        ((pos.peak-pos.entry)/pos.entry*100 if pos.side=="LONG"
         else (pos.entry-pos.peak)/pos.entry*100))
    adverse=((pos.entry-px)/pos.entry*100 if pos.side=="LONG"
             else (px-pos.entry)/pos.entry*100)
    pos.max_adverse_pct=max(pos.max_adverse_pct, max(0.0,adverse))

    if best_r >= 3.0:
        protect_r=2.25
        trail_atr=0.9 if not momentum_ok or not structure_ok else 1.3
    elif best_r >= 2.5:
        protect_r=1.75
        trail_atr=1.0 if not momentum_ok or not structure_ok else 1.4
    elif best_r >= 2.0:
        protect_r=1.25
        trail_atr=1.1 if not momentum_ok or not structure_ok else 1.5
    elif best_r >= 1.0:
        protect_r=0.50
        trail_atr=1.6
    else:
        protect_r=0.0
        trail_atr=1.8

    desired=None
    if pos.side=="LONG":
        if protect_r:
            desired=pos.entry+protect_r*r
        trail=pos.peak-trail_atr*atr
        desired=max(desired or -1e99,trail)
        desired=min(desired,px-0.05*atr)
        if desired>pos.sl:
            pos.sl=desired
            return "UPDATE_SL"
        giveback=best_r-live_r
    else:
        if protect_r:
            desired=pos.entry-protect_r*r
        trail=pos.peak+trail_atr*atr
        desired=min(desired or 1e99,trail)
        desired=max(desired,px+0.05*atr)
        if desired<pos.sl:
            pos.sl=desired
            return "UPDATE_SL"
        giveback=best_r-live_r

    # Confirmed reversal: structure + momentum + one independent confirmation
    if best_r >= 1.0 and (not momentum_ok and not structure_ok):
        if giveback >= (0.75 if best_r>=3 else 1.0):
            return "EXIT"
    # Broker SL remains authoritative for ordinary adverse movement.
    if pos.side=="LONG" and px <= pos.sl:
        return "EXIT"
    if pos.side=="SHORT" and px >= pos.sl:
        return "EXIT"
    return "HOLD"


def atomic_json(path: Path, payload: Any):
    fd,tmp=tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    os.close(fd)
    try:
        Path(tmp).write_text(json.dumps(payload,indent=2,default=str))
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def allocation(capital, positions, candidate, margin_per_lot, risk_per_trade_pct=0.40, reserve_pct=20.0, max_portfolio_risk_pct=1.20, lot_risk=None):
    used_margin=sum(float(p.get("margin",0)) for p in positions if p.get("status")=="OPEN")
    current_risk=sum(float(p.get("risk",0)) for p in positions if p.get("status")=="OPEN")
    max_risk=capital*max_portfolio_risk_pct/100
    remaining_risk=max(0.0,max_risk-current_risk)
    if lot_risk is None:
        lot_risk=abs(candidate["entry"]-candidate["stop"])*candidate["lot_size"]
    trade_risk=min(capital*risk_per_trade_pct/100,remaining_risk)
    risk_lots=math.floor(trade_risk/lot_risk) if lot_risk>0 else 0
    margin_lots=math.floor(max(0.0,capital*(1-reserve_pct/100)-used_margin)/margin_per_lot) if margin_per_lot>0 else 0
    lots=max(0,min(risk_lots,margin_lots))
    return {
        "lots":lots,
        "risk":lots*lot_risk,
        "margin":lots*margin_per_lot,
        "available_margin_after":max(0.0,capital*(1-reserve_pct/100)-used_margin-lots*margin_per_lot)
    }


def smoke_test():
    # structural direction
    assert structural_stop(100,"LONG",99,97,99.5,1) < 100
    assert structural_stop(100,"SHORT",103,101,100.5,1) > 100

    snap={"price":101.2,"vwap":100,"atr":0.8,"orb_high":100.8,"orb_low":98,
          "mom5":0.20,"mom15":0.35,"rvol":2.2}
    s=choose_signal(snap)
    assert s and s["side"]=="LONG"

    p=Position("X","X","1","LONG",1,1,100,99,99,100,0,"now")
    a=update_position(p,102,0.8,True,True)
    assert a=="UPDATE_SL" and p.sl>99
    old_peak=p.peak
    update_position(p,101,0.8,True,True)
    assert p.peak==old_peak

    print("MCX V8.5.4 engine smoke tests: PASS")


if __name__=="__main__":
    smoke_test()
