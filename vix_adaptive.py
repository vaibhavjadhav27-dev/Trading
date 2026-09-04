"""VIX-Adaptive Filter Module"""
import config

def clamp(value, min_val, max_val):
    return max(min_val, min(max_val, value))

def get_vix_scale(nifty_orb_pct):
    if nifty_orb_pct <= 0:
        return 1.0
    scale = nifty_orb_pct / config.NIFTY_ORB_MEDIAN
    return clamp(scale, config.VIX_SCALE_MIN, config.VIX_SCALE_MAX)

def get_adaptive_filters(nifty_orb_pct):
    scale = get_vix_scale(nifty_orb_pct)
    return {
        'orb_min': config.ORB_MIN_RANGE_PCT * scale,
        'orb_max': config.ORB_MAX_RANGE_PCT * scale,
        'gap_min': config.GAP_MIN_BASE * scale,
        'gap_max': min(config.GAP_MAX_TIER1, 5.0 * scale),
        'vix_scale': scale,
        'regime_note': _get_regime_note(scale)
    }

def _get_regime_note(scale):
    if scale < 0.85:
        return "CALM (tight filters)"
    elif scale < 1.2:
        return "NORMAL"
    elif scale < 1.5:
        return "VOLATILE (wider filters)"
    else:
        return "HIGH_VOL (very wide, be selective)"
