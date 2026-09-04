#!/usr/bin/env python3
"""Non-trading preflight for V8.4 integration on the existing EC2 bot tree."""
from __future__ import annotations
import importlib, os, sys
from pathlib import Path

REQUIRED = [
    'trading_bot_v82','v82_dhan_gateway','v82_strategy','secrets_manager',
    'trade_journal','fno_ban_check','sector_rotation','dual_scorer','trade_policy',
    'v84_strategy','v84.scoring','v84.risk','v84.swing','v84.mcx','trading_bot_v84'
]
FILES = ['trading_bot_v82.py','v82_dhan_gateway.py','watchlist.csv','prev_close_cache.json']

def main():
    root=Path(__file__).resolve().parent
    os.chdir(root);sys.path.insert(0,str(root))
    missing=[x for x in FILES if not Path(x).exists()]
    if missing: raise SystemExit('MISSING_FILES:'+','.join(missing))
    bad=[]
    for name in REQUIRED:
        try: importlib.import_module(name)
        except Exception as e: bad.append((name,repr(e)))
    if bad:
        for x in bad: print('IMPORT_FAIL',*x)
        raise SystemExit(2)
    print('V84_PREFLIGHT_PASS')
    print('LIVE_GUARD=',os.getenv('V84_ENABLE_LIVE','0'))
    print('DHAN_TOKEN_PRESENT=',bool(os.getenv('DHAN_ACCESS_TOKEN')))
    print('DHAN_CLIENT_PRESENT=',bool(os.getenv('DHAN_CLIENT_ID')))
    print('NO_ORDERS_PLACED=TRUE')

if __name__=='__main__':main()
