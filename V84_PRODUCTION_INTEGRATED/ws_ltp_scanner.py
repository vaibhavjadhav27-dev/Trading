"""
ws_ltp_scanner.py - WebSocket LTP Scanner (Synchronous, Thread-Safe)
Uses dhanhq MarketFeed synchronous API (run_forever + get_data)
"""
import time
import threading
import logging
from dhanhq import MarketFeed, DhanContext

log = logging.getLogger('TradingBot')


def get_bulk_ltp(client_id, token, security_ids, timeout=25):
    """
    Get LTP for multiple stocks via WebSocket (synchronous).
    Args:
        client_id: Dhan client ID
        token: Dhan access token
        security_ids: list of (ticker, security_id) tuples
        timeout: max seconds to collect
    Returns:
        dict: {security_id_str: ltp_float}
    """
    ctx = DhanContext(client_id, token)
    instruments = [(1, str(int(sid)), 15) for _, sid in security_ids]  # NSE=1, mode=15(Full)

    all_ltp = {}
    batch_size = 100  # SDK limit

    for i in range(0, len(instruments), batch_size):
        batch = instruments[i:i+batch_size]
        batch_ltp = _fetch_batch_sync(ctx, batch, timeout=min(timeout, 12))
        all_ltp.update(batch_ltp)

    _n = len(all_ltp)
    _tot = len(security_ids)
    _b = (len(instruments) + batch_size - 1) // batch_size
    if _n == 0:
        log.warning(f'get_bulk_ltp DIAG: 0/{_tot} LTPs over {_b} batch(es) - WS returned nothing')
    else:
        log.info(f'get_bulk_ltp DIAG: {_n}/{_tot} LTPs over {_b} batch(es)')
    return all_ltp


def _fetch_batch_sync(ctx, instruments, timeout=12):
    """Fetch one batch using synchronous run_forever + get_data."""
    ltp_map = {}
    feed = None

    try:
        feed = MarketFeed(dhan_context=ctx, instruments=instruments, version='v2')

        # Run WebSocket in background thread
        ws_thread = threading.Thread(target=feed.run_forever, daemon=True)
        ws_thread.start()

        # Collect ticks for timeout seconds
        start = time.time()
        seen_sids = set()
        target_count = len(instruments)

        while time.time() - start < timeout:
            try:
                data = feed.get_data()
                if data and 'security_id' in data and 'LTP' in data:
                    sid = str(data['security_id'])
                    ltp = float(data['LTP'])
                    if ltp > 0:
                        ltp_map[sid] = ltp
                        seen_sids.add(sid)
                        # Early exit if we got all stocks
                        if len(seen_sids) >= target_count * 0.9:
                            break
            except Exception:
                pass
            time.sleep(0.01)  # 10ms poll

        # Disconnect
        try:
            feed.disconnect()
        except Exception:
            pass

    except Exception as e:
        log.warning(f'WS batch error: {e}')

    return ltp_map
