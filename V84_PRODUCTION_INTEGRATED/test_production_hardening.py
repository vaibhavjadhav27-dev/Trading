#!/usr/bin/env python3
from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parent
def check(name,cond):
    if not cond: raise AssertionError('FAIL '+name)
    print('PASS '+name)
def main():
    v84=(ROOT/'trading_bot_v84.py').read_text(); mcx=(ROOT/'mcx_v12_engine.py').read_text(); gw=(ROOT/'v82_dhan_gateway.py').read_text(); native=(ROOT/'mcx_v854_engine.py').read_text()
    for s in (v84,mcx,gw,native): ast.parse(s)
    check('candidate_scope_guard','d=None' in v84 and 'isinstance(d,dict) and d.get("status")=="ENTER"' in v84)
    check('invalid_entry_guard','if entry <= 0:' in v84 and 'INVALID_POSITION_ENTRY' in v84)
    check('PFE_dict_contract','profit_fading_exit contract verified' in v84 and 'profit_fading_exit(_pfe_dict)' in v84)
    check('IST_timezone','IST = timezone(timedelta(hours=5, minutes=30))' in mcx and 'return datetime.now(IST)' in mcx)
    check('shared_NSE_coordination','global_dhan_coordinator().wait(kind)' in gw and 'penalize_429(15.0)' in gw)
    check('shared_MCX_coordination','global_dhan_coordinator().wait("quote")' in native and 'global_dhan_coordinator().wait("data")' in native)
    print('ALL PRODUCTION HARDENING SMOKE TESTS PASSED')
if __name__=='__main__': main()
