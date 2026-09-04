import logging
log = logging.getLogger(__name__)

def calculate_safe_qty(price, sl_distance, balance, adv_shares=0, config=None, size_mult=1.0):
    """
    Smart Position Sizing with:
    - Risk-based qty (2% of available balance / SL distance)
    - Notional cap (50% of capital)
    - ADT liquidity gate (hard reject if shares > 1% ADV)
    - Cost-ratio guard (min 3x cost cushion)
    - Minimum notional floor (Rs.15000)
    """
    if sl_distance is None or sl_distance <= 0:
        return {'qty': 0, 'reason': 'no_sl', 'notional': 0, 'effective_risk': 0}
    if not config:
        import config as cfg
        config = cfg
    
    # Cash buffer
    cash_buffer = getattr(config, "CASH_BUFFER_PCT", 5) / 100
    available = balance * (1 - cash_buffer)
    
    # Risk-based qty: 2% of available / SL distance
    risk_pct = getattr(config, "RISK_PER_TRADE_PCT", 2) / 100
    risk_amount = available * risk_pct
    if sl_distance <= 0:
        log.warning("SL distance <= 0, cannot size position")
        return {"qty": 0, "reason": "invalid_sl"}
    
    risk_qty = int(risk_amount / sl_distance)
    
    # Notional cap: 50% of available capital
    max_capital_pct = getattr(config, "MAX_POSITION_PCT", 100) / 100 * size_mult
    notional_cap = available * max_capital_pct
    notional_qty = int(notional_cap / price) if price > 0 else 0
    
    # Take minimum of risk-based and notional-capped
    qty = min(risk_qty, notional_qty)
    
    # ═══ ADT LIQUIDITY GATE (HARD REJECT) ═══
    adv_max_pct = getattr(config, "ADV_MAX_PCT", 1.0) / 100
    if adv_shares is not None and adv_shares > 0 and qty > (adv_shares * adv_max_pct):
        capped_qty = int(adv_shares * adv_max_pct)
        if capped_qty < 1:
            log.warning(f"ADT gate: qty {qty} > 1% ADV ({adv_shares}). REJECT.")
            return {"qty": 0, "reason": "adt_liquidity_reject"}
        log.info(f"ADT cap: {qty} -> {capped_qty} (1% of ADV {adv_shares})")
        qty = capped_qty
    
    # ═══ COST-RATIO GUARD (Min 3x cost cushion) ═══
    try:
        from dhan_charges import dhan_charges_mis
        est_charges = dhan_charges_mis(qty, price, price * 1.01)
    except Exception:
        est_charges = 25.0
    target_gross = qty * sl_distance  # Expected R at 1R
    if target_gross < est_charges * 3:
        # Need at least 3x charges at 1R target
        min_qty_for_cost = int((est_charges * 3) / sl_distance) + 1
        if min_qty_for_cost > notional_qty:
            log.warning(f"Cost-ratio guard: need {min_qty_for_cost} shares for 3x cost but notional cap is {notional_qty}. REJECT.")
            return {"qty": 0, "reason": "cost_ratio_reject"}
        log.info(f"Cost-ratio guard: bumping qty {qty} -> {min_qty_for_cost} for 3x cost cushion")
        qty = max(qty, min_qty_for_cost)
    
    # ═══ MINIMUM NOTIONAL FLOOR (Rs.15000) ═══
    min_notional = 15000
    if qty * price < min_notional:
        floor_qty = int(min_notional / price) + 1
        if floor_qty * price > notional_cap:
            log.warning(f"Notional floor: need Rs.{floor_qty * price:.0f} but cap is Rs.{notional_cap:.0f}. REJECT.")
            return {"qty": 0, "reason": "notional_floor_reject"}
        log.info(f"Notional floor: bumping qty {qty} -> {floor_qty} for Rs.15k minimum")
        qty = max(qty, floor_qty)
    
    # ═══ HARD EFFECTIVE-RISK CEILING (slippage-aware) ═══
    slip = getattr(config, "SLIPPAGE_PCT", 0.2) / 100 * price
    max_risk = getattr(config, "MAX_EFFECTIVE_RISK_PCT", 3.0) / 100 * balance
    eff_risk_per_share = sl_distance + slip
    if eff_risk_per_share > 0:
        risk_capped_qty = int(max_risk / eff_risk_per_share)
        if qty > risk_capped_qty:
            log.info(f"Effective-risk ceiling: {qty} -> {risk_capped_qty} "
                     f"(cap {getattr(config,'MAX_EFFECTIVE_RISK_PCT',3.0)}% incl slippage)")
            qty = risk_capped_qty

    # Final sanity check
    qty = max(1, qty)
    effective_sl = sl_distance
    effective_risk = qty * sl_distance
    
    log.info(f"Sizing: qty={qty}, risk=Rs.{effective_risk:.0f}, notional=Rs.{qty*price:.0f}")
    
    return {
        "qty": qty,
        "effective_sl": sl_distance,
        "slippage_added": 0.5,
        "effective_risk": effective_risk,
        "notional": qty * price,
        "risk_pct_of_balance": (effective_risk / balance * 100),
        "reason": "sized"
    }
