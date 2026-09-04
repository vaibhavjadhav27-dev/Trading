#!/usr/bin/env python3
from __future__ import annotations
import fcntl, json, os, time
from contextlib import contextmanager
from pathlib import Path

class DhanRequestCoordinator:
    def __init__(self, state_file=None):
        self.state_file = Path(state_file or os.getenv('DHAN_COORDINATOR_STATE','/home/ubuntu/trading-bot/run/dhan_request_coordinator.json'))
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.min_interval = {
            'quote': float(os.getenv('DHAN_GLOBAL_QUOTE_INTERVAL','1.10')),
            'data': float(os.getenv('DHAN_GLOBAL_DATA_INTERVAL','0.25')),
            'nontrade': float(os.getenv('DHAN_GLOBAL_NONTRADE_INTERVAL','0.05')),
            'order': 0.0,
        }
    @contextmanager
    def _locked_state(self):
        self.state_file.touch(exist_ok=True)
        with open(self.state_file,'r+',encoding='utf-8') as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0); raw=fh.read().strip(); state=json.loads(raw) if raw else {}
            except Exception:
                state={}
            try:
                yield fh,state
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    @staticmethod
    def _save(fh,state):
        fh.seek(0); fh.truncate(); json.dump(state,fh,separators=(',',':')); fh.flush(); os.fsync(fh.fileno())
    def wait(self,kind='data'):
        kind=kind if kind in self.min_interval else 'data'; interval=self.min_interval[kind]
        while True:
            with self._locked_state() as (fh,state):
                now=time.time(); blocked=float(state.get('blocked_until',0) or 0); last=float(state.get('last_'+kind,0) or 0)
                target=max(blocked,last+interval); delay=target-now
                if delay<=0:
                    state['last_'+kind]=now; self._save(fh,state); return
            time.sleep(min(max(delay,0.01),1.0))
    def penalize_429(self,seconds=15.0):
        with self._locked_state() as (fh,state):
            until=time.time()+max(float(seconds),1.0)
            state['blocked_until']=max(float(state.get('blocked_until',0) or 0),until)
            state['last_429']=time.time(); self._save(fh,state)
_GLOBAL=None
def global_dhan_coordinator():
    global _GLOBAL
    if _GLOBAL is None: _GLOBAL=DhanRequestCoordinator()
    return _GLOBAL
