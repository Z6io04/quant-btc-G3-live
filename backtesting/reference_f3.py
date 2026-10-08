"""Pure F3 decisions for real execution; this module never invents a fill.

The BTC-USDT holding hedges an inverse BTC-USD-SWAP short. Contract USD
face, not the exchange leverage setting, determines the hedge. All reported
amounts are decimal strings suitable for durable JSON state and API sizing.
"""
from bisect import bisect_right
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
D = Decimal

# Economic rules frozen from config-F3-20u.json / config-F3-60u.json.
# Capital is intentionally absent: setup must supply the real-money budget.
RULES = {
    'entry_lookback_days': 14, 'expected_hold_days': 90, 'cost_cover': '1',
    'exit_lookback_days': 7, 'exit_projection_days': 14,
    'exit_switch_cost_multiple': '1', 'exit_bad_days': 3,
    'min_hold_hours': 168, 'cooldown_hours': 24, 'decision_hour_bjt': 9,
    'execution_latency_minutes': 1, 'funding_available_lag_minutes': 1,
    'second_leg_delay_minutes': 0, 'cash_reserve': '0',
    'allocation_mode': 'nearest_or_exact', 'max_entry_delta_fraction': '0.02',
    'treasury_trigger_fraction': '0.02', 'treasury_mode': 'neutral',
    'treasury_min_collateral_ratio': '0.75',
    'minimum_entry_collateral_ratio': '0.5', 'margin_exit_ratio': '0.25',
    'loss_limit_fraction': '0.03', 'halt_drawdown': '0.05',
    'residual_exit_fraction': '0.04', 'assumed_mmr': '0.004',
    'spot_fee': '0.001', 'swap_fee': '0.0005', 'adverse_bps': '3',
}


def decimal(value, label='number', positive=False, nonnegative=False):
    if isinstance(value, bool) or value is None:
        raise ValueError('Invalid ' + label)
    try:
        result = D(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Invalid ' + label) from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError('Invalid ' + label)
    return result


def amount(value):
    result = format(value, 'f')
    return result if '.' not in result else result.rstrip('0').rstrip('.') or '0'


def round_step(value, step, up=False):
    value, step = decimal(value), decimal(step, 'step', positive=True)
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def _fee(value, default):
    result = decimal(default if value is None else value, 'fee', nonnegative=True)
    if result >= 1:
        raise ValueError('Fee must be below one')
    return result


def _fees(account=None):
    fees = (account or {}).get('fees', {})
    return _fee(fees.get('spot'), RULES['spot_fee']), _fee(fees.get('swap'), RULES['swap_fee'])


def total_cost_rate(spot_fee=None, swap_fee=None, adverse_bps=None):
    spot = _fee(spot_fee, RULES['spot_fee'])
    swap = _fee(swap_fee, RULES['swap_fee'])
    slip = decimal(RULES['adverse_bps'] if adverse_bps is None else adverse_bps, 'slippage', nonnegative=True)
    if slip >= 10_000:
        raise ValueError('Slippage must be below 100%')
    return 2 * spot + 2 * swap + 4 * slip / D(10_000)


def _events(events):
    """Normalize settled rates only; predictions cannot enter this function."""
    if hasattr(events, 'items'):
        records = events.items()
    else:
        records = []
        for row in events:
            if isinstance(row, dict):
                if 'ts' in row and 'rate' in row:
                    # market.funding_history normalizes realizedRate to rate.
                    records.append((row['ts'], row['rate']))
                    continue
                if 'realizedRate' not in row:
                    raise ValueError('Funding record requires settled realizedRate')
                records.append((row['fundingTime'], row['realizedRate']))
            else:
                records.append((row[0], row[1]))
    normalized = {}
    for timestamp, rate in records:
        if isinstance(timestamp, bool):
            raise ValueError('Invalid funding timestamp')
        try:
            ts = int(timestamp)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid funding timestamp') from exc
        if ts < 0 or D(str(timestamp)) != ts:
            raise ValueError('Invalid funding timestamp')
        # A non-minute exchange settlement becomes knowable at the next minute.
        ts = (ts + MINUTE - 1) // MINUTE * MINUTE
        rate = decimal(rate, 'realized funding rate')
        if ts in normalized and normalized[ts] != rate:
            raise ValueError('Conflicting settled funding rates')
        normalized[ts] = rate
    return sorted(normalized.items())


def funding_frame(events, now_ms, days, lag_minutes=1):
    """Return a complete backward-looking window, or None for missing history.

    A preceding settlement must anchor the beginning, and gaps over eight
    hours (plus exchange timestamp rounding) invalidate the whole window.
    """
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError('now_ms must be a nonnegative integer timestamp')
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 1 for x in (days, lag_minutes)):
        raise ValueError('Lookback and information lag must be positive integers')
    cutoff = now_ms - lag_minutes * MINUTE
    rows = [(t, r) for t, r in _events(events) if t <= cutoff]
    if not rows or cutoff - rows[-1][0] > 8 * HOUR:
        return None
    times = [t for t, _ in rows]
    first = bisect_right(times, cutoff - days * DAY)
    if not first:
        return None
    covered = rows[first - 1:]
    if any(b[0] - a[0] > 8 * HOUR + MINUTE for a, b in zip(covered, covered[1:])):
        return None
    selected = rows[first:]
    if not selected:
        return None
    return {'daily_rate': amount(sum((r for _, r in selected), D(0)) / D(days)),
            'last_rate': amount(rows[-1][1]), 'last_settlement': rows[-1][0],
            'observations': len(selected), 'known_at': now_ms}


def entry_signal(events, now_ms, spot_fee=None, swap_fee=None, adverse_bps=None):
    frame = funding_frame(events, now_ms, RULES['entry_lookback_days'], RULES['funding_available_lag_minutes'])
    cost = total_cost_rate(spot_fee, swap_fee, adverse_bps)
    reason = None
    if frame is None:
        reason = 'funding_history_incomplete'
    elif D(frame['last_rate']) <= 0:
        reason = 'latest_funding_not_positive'
    elif D(frame['daily_rate']) * RULES['expected_hold_days'] < D(RULES['cost_cover']) * cost:
        reason = 'funding_does_not_cover_cost'
    return {'allowed': reason is None, 'reason': reason, 'frame': frame, 'cost_rate': amount(cost)}


def is_decision_time(now_ms, last_daily_check=None):
    """Only the fresh 09:00 Beijing minute is eligible; no historical replay."""
    minute = now_ms // MINUTE * MINUTE
    if (minute + 8 * HOUR) % DAY != RULES['decision_hour_bjt'] * HOUR:
        return False
    return last_daily_check is None or int(last_daily_check) < minute


def _price(raw, tick, buy):
    raw = decimal(raw, 'price', positive=True)
    slip = D(RULES['adverse_bps']) / D(10_000)
    result = round_step(raw * (1 + slip if buy else 1 - slip), tick, up=buy)
    if result <= 0:
        raise ValueError('Invalid estimated executable price')
    return result


def _balance(account, currency, field):
    return decimal(account.get('balances', {}).get(currency, {}).get(field, '0'), currency + '.' + field)


def _spec(market):
    source = market['spec']
    names = ('spot_lot', 'spot_min', 'spot_tick', 'swap_lot', 'swap_min',
             'swap_tick', 'contract_usd', 'tier_max_contracts')
    spec = {key: decimal(source[key], key, positive=True) for key in names}
    spec['mmr'] = decimal(source['mmr'], 'mmr', positive=True)
    spec['spot_max'] = decimal(source.get('spot_max','1000000'), 'spot_max', positive=True)
    if spec['mmr'] >= D(RULES['margin_exit_ratio']):
        raise ValueError('Exchange maintenance margin exceeds strategy exit guard')
    spec['usd_step'] = spec['contract_usd'] * spec['swap_lot']
    spec['usd_min'] = spec['contract_usd'] * spec['swap_min']
    return spec


def plan_entry(cfg, market, account):
    """Estimate a hedge within the explicit budget; orders use actual fills.

    The estimate includes adverse execution, base-coin spot fee rounding,
    and inverse short fees. Exchange leverage never changes target USD face.
    """
    spec = _spec(market)
    capital = decimal(cfg.get('capital_usdt'), 'capital_usdt', positive=True)
    cash = min(capital, _balance(account, 'USDT', 'cash'), _balance(account, 'USDT', 'available'))
    if cash <= 0:
        return {'allowed': False, 'reason': 'insufficient_available_usdt'}
    if account.get('position') and decimal(account['position'].get('contracts', '0')) != 0:
        return {'allowed': False, 'reason': 'position_exists'}
    sf, pf = _fees(account)
    sp = _price(market['spot']['ask'], spec['spot_tick'], True)
    pp = _price(market['swap']['bid'], spec['swap_tick'], False)
    # Reserve quote-currency fees as well as estimating a possible base-coin
    # fee below. Only the actual charged fee reaches the execution ledger;
    # this small unused USDT allowance prevents an otherwise full-budget buy
    # from exceeding the budget if the account charges its fee in USDT.
    budget = cash * (1 - D(RULES['cash_reserve'])) / (1 + sf)

    def purchase(gross):
        fee = round_step(gross * sf, spec['spot_lot'], up=True)
        return gross - fee, fee

    def candidate(exact=False):
        if exact:
            n = round_step(budget / sp * (1 - sf) * pp / (1 + pf), spec['usd_step'])
            while n >= spec['usd_min']:
                need = n / pp * (1 + pf)
                gross = round_step(need / (1 - sf), spec['spot_lot'], up=True)
                net, fee = purchase(gross)
                if net < need:
                    gross += spec['spot_lot']
                    net, fee = purchase(gross)
                if gross * sp <= budget:
                    break
                n = round_step(n - spec['usd_step'], spec['usd_step'])
            if n < spec['usd_min']:
                return None, 'minimum_lot'
        else:
            gross = round_step(budget / sp, spec['spot_lot'])
            net, fee = purchase(gross)
            raw = net * pp / (1 + pf)
            n = round_step(raw + spec['usd_step'] / 2, spec['usd_step'])
        if n < spec['usd_min']:
            return None, 'minimum_lot'
        if gross > spec['spot_max']:
            return None, 'spot_order_limit'
        if gross < spec['spot_min'] or gross * sp > budget:
            return None, 'spot_budget'
        coins = net - n / pp * pf
        delta = (coins - n / pp) * sp
        if abs(delta) > cash * D(RULES['max_entry_delta_fraction']):
            return None, 'entry_delta_limit'
        if coins * pp / n < D(RULES['minimum_entry_collateral_ratio']):
            return None, 'entry_margin'
        contracts = n / spec['contract_usd']
        if contracts > spec['tier_max_contracts']:
            return None, 'maintenance_tier_limit'
        return {'allowed': True, 'reason': None,
                'gross_btc': amount(gross), 'contracts': amount(contracts),
                'notional_usd': amount(n), 'estimated_spot_cost': amount(gross * sp),
                'estimated_net_btc': amount(net), 'estimated_spot_fee_btc': amount(fee),
                'estimated_swap_entry_fee_btc': amount(n / pp * pf),
                'estimated_delta_usdt': amount(delta), 'target_collateral_btc': amount(coins),
                'estimated_spot_entry': amount(sp), 'estimated_swap_entry': amount(pp),
                'allocation_method': 'exact_fallback' if exact else 'nearest',
                'budget_usdt': amount(cash), 'quote_fee_reserve_usdt': amount(cash - budget),
                'spot_fee_rate': amount(sf), 'swap_fee_rate': amount(pf)}, None

    plan, reason = candidate()
    if reason == 'entry_delta_limit':
        plan, reason = candidate(exact=True)
    return plan or {'allowed': False, 'reason': reason,
                    'minimum_notional_usd': amount(spec['usd_min']), 'budget_usdt': amount(cash)}


def valuation(market, account):
    """Conservative two-leg liquidation estimate, not realized account profit."""
    spec = _spec(market)
    sf, pf = _fees(account)
    cash = _balance(account, 'USDT', 'cash')
    coins = _balance(account, 'BTC', 'cash')
    p = account.get('position') or {}
    signed = decimal(p.get('contracts', '0'))
    if signed > 0:
        raise ValueError('Unexpected long inverse position')
    if signed:
        n = -signed * spec['contract_usd']
        entry = decimal(p['avgPx'], 'swap average entry', positive=True)
        cover = _price(market['swap']['ask'], spec['swap_tick'], True)
        coins += n * (1 / cover - 1 / entry) - n / cover * pf
    if coins >= 0:
        value = coins * _price(market['spot']['bid'], spec['spot_tick'], False) * (1 - sf)
    else:
        value = coins * _price(market['spot']['ask'], spec['spot_tick'], True) * (1 + sf)
    return amount(cash + value)


def evaluate_position(cfg, market, account, position, events, now_ms, *, peak_equity_usdt=None):
    """Evaluate guards and neutral treasury from reconciled exchange balances.

    ``position`` is the executor's durable group (opened_at, entry_equity,
    funding_btc, treasury_sold_btc, bad_days, last_daily_check). This returns
    requested actions; submitting, reconciling, and recording them is external.
    """
    spec = _spec(market)
    sf, pf = _fees(account)
    equity = D(valuation(market, account))
    capital = decimal(cfg.get('capital_usdt'), 'capital_usdt', positive=True)
    peak = max(capital, equity, decimal(peak_equity_usdt if peak_equity_usdt is not None else capital, 'peak', positive=True))
    dd = (peak - equity) / peak
    reason = None
    metrics = {'drawdown': amount(dd), 'peak_equity_usdt': amount(peak)}
    max_loss = cfg.get('max_loss_usdt')
    if max_loss is not None and capital - equity >= decimal(max_loss, 'max_loss_usdt', positive=True):
        reason = 'maximum_loss_guard'
    actual = account.get('position') or {}
    signed = decimal(actual.get('contracts', '0'))
    if signed > 0:
        raise ValueError('Unexpected long inverse position')
    result = {'exit_reason': reason, 'treasury_btc': '0', 'bad_days': int((position or {}).get('bad_days', 0)),
              'last_daily_check': (position or {}).get('last_daily_check'),
              'equity_usdt': amount(equity), 'halt': dd >= D(RULES['halt_drawdown']) or reason is not None,
              'metrics': metrics}
    if not position:
        return result
    entry_equity = decimal(position['entry_equity'], 'entry_equity', positive=True)
    coins = _balance(account, 'BTC', 'cash')
    if signed:
        n = -signed * spec['contract_usd']
        entry = decimal(actual['avgPx'], 'average entry', positive=True)
        mark = decimal(market['mark'], 'mark', positive=True)
        spot = decimal(market['spot']['bid'], 'spot bid', positive=True)
        collateral = _balance(account, 'BTC', 'eq')
        ratio = collateral * mark / n
        delta_coins = coins - n / entry
        delta = delta_coins * spot
        fraction = abs(delta) / max(equity, D('1e-12'))
        metrics.update(collateral_ratio=amount(ratio), residual_delta_usdt=amount(delta),
                       residual_fraction=amount(fraction), notional_usd=amount(n))
        if reason is None and ratio <= max(spec['mmr'], D(RULES['assumed_mmr'])) + pf:
            reason = 'potential_liquidation'
        elif reason is None and ratio <= D(RULES['margin_exit_ratio']):
            reason = 'margin_guard'
        if reason is None and fraction > D(RULES['residual_exit_fraction']):
            reason = 'residual_exposure_guard'
        if reason is None and equity - entry_equity <= -D(RULES['loss_limit_fraction']) * entry_equity:
            reason = 'combined_loss_guard'
        if reason is None and result['halt']:
            reason = 'account_halt'
        if is_decision_time(now_ms, result['last_daily_check']) and now_ms - int(position['opened_at']) >= RULES['min_hold_hours'] * HOUR:
            result['last_daily_check'] = now_ms // MINUTE * MINUTE
            frame = funding_frame(events, now_ms, RULES['exit_lookback_days'])
            result['funding_frame'] = frame
            if frame is not None:
                adverse = -D(frame['daily_rate']) * RULES['exit_projection_days'] >= D(RULES['exit_switch_cost_multiple']) * total_cost_rate(sf, pf)
                result['bad_days'] = result['bad_days'] + 1 if adverse else 0
                if reason is None and result['bad_days'] >= RULES['exit_bad_days']:
                    reason = 'funding_deterioration'
        if reason is None and delta > D(RULES['treasury_trigger_fraction']) * equity:
            earned = decimal(position.get('funding_btc', '0')) - decimal(position.get('treasury_sold_btc', '0'))
            excess_margin = collateral - D(RULES['treasury_min_collateral_ratio']) * n / mark
            available = _balance(account, 'BTC', 'available')
            sell = round_step(max(D(0), min(earned, excess_margin, delta_coins, available)), spec['spot_lot'])
            if sell >= spec['spot_min']:
                result['treasury_btc'] = amount(sell)
    else:
        # A group with a missing hedge must be handled by execution recovery.
        reason = reason or 'hedge_missing'
    result['exit_reason'] = reason
    return result
