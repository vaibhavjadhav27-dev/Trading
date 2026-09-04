#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, py_compile, shutil
from datetime import datetime, timezone
from pathlib import Path

def backup(p):
    b=p.with_suffix(p.suffix+'.bak')
    if not b.exists(): shutil.copy2(p,b)
def replace_once(t,old,new,label):
    n=t.count(old)
    if n!=1: raise RuntimeError(f'PATCH ABORTED {label}: expected one anchor, found {n}')
    return t.replace(old,new,1)
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def patch_v84(p):
    backup(p); t=p.read_text()
    t=replace_once(t,"""        # V12 FIX 5: Startup compatibility check
        try:
            import inspect as _insp
            _pfe_params=set(_insp.signature(profit_fading_exit).parameters.keys())
            log.info(f"profit_fading_exit signature: {_pfe_params}")
        except Exception as _e5:
            log.warning(f"signature check failed: {_e5}")
""","""        # Production compatibility check: validate the actual imported dict interface.
        try:
            import inspect as _insp
            _pfe_params=list(_insp.signature(profit_fading_exit).parameters.keys())
            if _pfe_params != ["snapshot_dict"]:
                raise RuntimeError(f"profit_fading_exit contract mismatch: {_pfe_params}")
            _pfe_probe=profit_fading_exit({"symbol":"BOOT_PROBE","side":"LONG","price":100.0,"entry_price":100.0,"sl":99.0,"initial_sl":99.0,"peak":100.0,"best_r":0.0,"qty":1,"entry_type":"NORMAL","position_pct":1.0,"confirmed":True})
            if not isinstance(_pfe_probe,dict): raise RuntimeError("profit_fading_exit probe did not return dict")
            log.info("profit_fading_exit contract verified: %s",_pfe_params)
        except Exception as _e5:
            log.critical("STARTUP BLOCKED: profit_fading_exit compatibility failed: %s",_e5)
            raise
""","PFE startup contract")
    t=replace_once(t,"""            entry = float(p["entry"])
            side = p["side"]
            risk_per_share = max(abs(entry - float(p.get("initial_sl", p["sl"]))), 0.01)
""","""            entry=float(p.get("entry",0) or 0)
            if entry <= 0:
                log.error("INVALID_POSITION_ENTRY: sid=%s entry=%r; skipping unsafe exit evaluation",sid,p.get("entry"))
                _event("invalid_position_entry",{"sid":str(sid),"entry":p.get("entry")})
                continue
            side=str(p.get("side","")).upper()
            if side not in ("LONG","SHORT"):
                log.error("INVALID_POSITION_SIDE: sid=%s side=%r",sid,p.get("side")); continue
            try: _sl_val=float(p.get("initial_sl",p.get("sl",0)) or 0)
            except (TypeError,ValueError): _sl_val=0.0
            if _sl_val <= 0:
                log.error("INVALID_POSITION_SL: sid=%s initial_sl=%r sl=%r",sid,p.get("initial_sl"),p.get("sl")); continue
            risk_per_share=max(abs(entry-_sl_val),0.01)
""","position entry guard")
    t=replace_once(t,"""                        if not f:continue
                        d=final_decision(f)
                        # V10.1-R CANONICAL AUTHORITY: veto (anti-chase) + sizing (30%/100%)
""","""                        if not f: continue
                        d=None
                        try:
                            d=final_decision(f)
                        except Exception as _decision_e:
                            log.exception("FINAL_DECISION_FAILED: %s",_decision_e)
                            _event("v84_entry_evaluation_error",{"symbol":c.get("symbol"),"error":str(_decision_e)})
                            continue
                        if not isinstance(d,dict):
                            log.error("FINAL_DECISION_INVALID: %s returned %r",c.get("symbol"),type(d).__name__)
                            continue
                        # V10.1-R CANONICAL AUTHORITY: veto (anti-chase) + sizing (30%/100%)
""","candidate d initialization")
    t=replace_once(t,'                            if d.get("status")=="ENTER":ranked.append((d,c))\n','''                            if isinstance(d,dict) and d.get("status")=="ENTER":
                                ranked.append((d,c))
''','safe ranked append')
    t=replace_once(t,'        self._load_v84_risk()\n        log.info("V10.1 active (ATR-normalized, anti-chase, giveback exits)")\n','''        self._load_v84_risk()
        try:
            _src=Path(__file__).resolve(); _src_sha=__import__("hashlib").sha256(_src.read_bytes()).hexdigest()
            log.info("RELEASE_ID version=V84_HARDENED source=%s sha256=%s pid=%s",_src,_src_sha,os.getpid())
        except Exception as _release_e: log.warning("RELEASE_ID unavailable: %s",_release_e)
        log.info("V10.1 active (ATR-normalized, anti-chase, giveback exits)")
''','release identity')
    p.write_text(t)

def patch_mcx_v12(p):
    backup(p); t=p.read_text()
    t=replace_once(t,"""_IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + _IST_OFFSET
""","""IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> datetime:
    return datetime.now(IST)
""","IST timezone")
    block='''                # V12 FIX 1: Response logging for diagnostics
                if prices:
                    _sample = list(prices.items())[:2]
                    log.info(f"MCX V12 LTP OK: {len(prices)} prices parsed. Sample: {_sample}")
                else:
                    log.warning(f"MCX V12 LTP EMPTY: API returned data but 0 prices parsed. raw_keys={list(raw.get('data',{}).get('MCX_COMM',{}).keys())[:3] if isinstance(raw,dict) else 'not-dict'}")
'''
    first=t.find(block)
    if first>=0:
        pos=t.find(block,first+len(block))
        while pos>=0:
            t=t[:pos]+t[pos+len(block):]; pos=t.find(block,first+len(block))
    p.write_text(t)

def patch_mcx_native(p):
    backup(p); t=p.read_text()
    if 'from dhan_request_coordinator import global_dhan_coordinator' not in t:
        t=replace_once(t,'from __future__ import annotations\n','from __future__ import annotations\nfrom dhan_request_coordinator import global_dhan_coordinator\n','MCX coordinator import')
    t=replace_once(t,'        self.throttle.wait_data()\n        today = date.today().isoformat()\n','        global_dhan_coordinator().wait("data")\n        self.throttle.wait_data()\n        today = date.today().isoformat()\n','MCX data coordination')
    t=replace_once(t,'        self.throttle.wait_ltp()\n        payload = {"MCX_COMM": [int(x) for x in security_ids]}\n','        global_dhan_coordinator().wait("quote")\n        self.throttle.wait_ltp()\n        payload = {"MCX_COMM": [int(x) for x in security_ids]}\n','MCX quote coordination')
    old='''        if r.status_code == 429:
            if _V12_MCX:
'''; new='''        if r.status_code == 429:
            global_dhan_coordinator().penalize_429(15.0)
            if _V12_MCX:
'''
    if t.count(old) != 2: raise RuntimeError(f'PATCH ABORTED MCX 429 anchors: expected 2, found {t.count(old)}')
    t=t.replace(old,new,2)
    p.write_text(t)

def patch_gateway(p):
    backup(p); t=p.read_text()
    if 'from dhan_request_coordinator import global_dhan_coordinator' not in t:
        t=replace_once(t,'from threading import Lock\n','from threading import Lock\nfrom dhan_request_coordinator import global_dhan_coordinator\n','NSE coordinator import')
    t=replace_once(t,'        for attempt in range(retries + 1):\n            limiter.wait()\n            try:\n','        for attempt in range(retries + 1):\n            global_dhan_coordinator().wait(kind)\n            limiter.wait()\n            try:\n','NSE global coordination')
    t=replace_once(t,'                if resp.status_code == 429:\n                    time.sleep(min(5.0, 1.5 * (attempt + 1))); continue\n','                if resp.status_code == 429:\n                    global_dhan_coordinator().penalize_429(15.0)\n                    time.sleep(min(15.0, 1.5 * (attempt + 1))); continue\n','NSE 429 penalty')
    p.write_text(t)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source-dir',default='.'); args=ap.parse_args(); root=Path(args.source_dir).resolve()
    files=[root/'trading_bot_v84.py',root/'mcx_v12_engine.py',root/'mcx_v854_engine.py',root/'v82_dhan_gateway.py']
    missing=[str(x) for x in files if not x.exists()]
    if missing: raise SystemExit('Missing canonical files: '+', '.join(missing))
    here=Path(__file__).resolve().parent
    _coord_src=here/'dhan_request_coordinator.py'; _coord_dst=root/'dhan_request_coordinator.py'
    if _coord_src.resolve()!=_coord_dst.resolve(): shutil.copy2(_coord_src,_coord_dst)
    patch_v84(files[0]); patch_mcx_v12(files[1]); patch_mcx_native(files[2]); patch_gateway(files[3])
    for x in files+[root/'dhan_request_coordinator.py']: py_compile.compile(str(x),doraise=True)
    manifest={'patched_at_utc':datetime.now(timezone.utc).isoformat(),'files':{x.name:sha(x) for x in files+[root/'dhan_request_coordinator.py']}}
    (root/'PRODUCTION_RELEASE_MANIFEST.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(manifest,indent=2)); print('PATCH SUCCESS: backups created as *.bak; syntax compilation passed.')
if __name__=='__main__': main()
