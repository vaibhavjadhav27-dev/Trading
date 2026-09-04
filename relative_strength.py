import json, os, logging
log = logging.getLogger(__name__)

RS_FILE = 'stock_metrics.json'

def load_rs_scores():
    try:
        if os.path.exists(RS_FILE):
            with open(RS_FILE, 'r') as f:
                data = json.load(f)
            scores = {}
            for sid, metrics in data.items():
                if isinstance(metrics, dict):
                    scores[metrics.get('ticker', sid)] = metrics.get('rs_score', 0)
            log.info(f'RS scores loaded: {len(scores)} stocks')
            return scores
    except Exception as e:
        log.warning(f'RS load failed: {e}')
    return {}

def get_rs_bonus(ticker, rs_scores):
    if not rs_scores or not ticker:
        return 0
    rs = rs_scores.get(ticker, 0)
    if rs >= 2.0:  # 2x Nifty return
        return 15
    elif rs >= 1.5:
        return 10
    elif rs >= 1.0:
        return 5
    return 0

def get_clv_bonus(sid, clv_scores):
    if not clv_scores or not sid:
        return 0
    clv = clv_scores.get(sid, 0)
    if clv >= 0.7:  # Close near high
        return 10
    elif clv >= 0.4:
        return 5
    return 0
