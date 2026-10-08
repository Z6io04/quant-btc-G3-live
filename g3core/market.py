"""Validated live public/account data for BTC spot + inverse perpetual carry.

Amounts remain decimal strings. BTC cash is distinct from BTC equity including
unrealized PnL. Available BTC is additionally bounded by the exchange's spot sell
availability, so cross collateral is never assumed to be freely sellable.
"""
from decimal import Decimal, InvalidOperation
import re

from .config import Attention
from .okx import SPOT, SWAP

DAY = 86400000


def number(value, *, positive=False, nonnegative=False):
    if not isinstance(value, str) or len(value) > 100 or re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value) is None:
        raise Attention('欧易数值字段缺失或不合法；本轮不提交订单。')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise Attention('欧易数值字段不合法。') from None
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise Attention('欧易数值字段超出允许范围。')
    return result


def text(value):
    return format(value, 'f')


def one(rows, label):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise Attention('无法唯一确认欧易' + label + '。')
    return rows[0]


def timestamp(value):
    if not isinstance(value, str) or not value.isdigit() or not 1577836800000 <= int(value) <= 4102444800000:
        raise Attention('欧易时间字段缺失或不合法。')
    return int(value)


def _instrument(api, inst_type, inst_id):
    r = one(api.read('/api/v5/public/instruments', {'instType': inst_type, 'instId': inst_id}), '交易规格')
    expected = {'instType': inst_type, 'instId': inst_id, 'state': 'live'}
    expected.update({'baseCcy': 'BTC', 'quoteCcy': 'USDT'} if inst_type == 'SPOT' else
                    {'ctType': 'inverse', 'ctValCcy': 'USD', 'settleCcy': 'BTC', 'instFamily': 'BTC-USD'})
    if any(r.get(key) != value for key, value in expected.items()):
        raise Attention('本版仅支持可交易的BTC-USDT现货与BTC-USD-SWAP币本位永续。')
    for key in ('lotSz', 'minSz', 'tickSz', 'maxLmtSz'):
        number(r.get(key), positive=True)
    if number(r['minSz']) % number(r['lotSz']) != 0:
        raise Attention('欧易最小交易量与步长不兼容。')
    if not isinstance(r.get('groupId'), str) or not r['groupId'] or r['groupId'].strip() != r['groupId']:
        raise Attention('交易规格缺少有效手续费分组。')
    return r


def _quote(api, inst_id):
    r = one(api.read('/api/v5/market/ticker', {'instId': inst_id}), '买卖报价')
    if r.get('instId') != inst_id:
        raise Attention('欧易报价对应的产品不符。')
    bid, ask = number(r.get('bidPx'), positive=True), number(r.get('askPx'), positive=True)
    if bid > ask:
        raise Attention('欧易买卖报价交叉；等待有效报价。')
    return {'bid': format(bid, 'f'), 'ask': format(ask, 'f'), 'ts': timestamp(r.get('ts'))}


def market_snapshot(api, cfg):
    spot, swap = _instrument(api, 'SPOT', SPOT), _instrument(api, 'SWAP', SWAP)
    tiers = api.read('/api/v5/public/position-tiers', {'instType': 'SWAP', 'tdMode': 'cross', 'instFamily': 'BTC-USD'})
    if not isinstance(tiers, list) or any(not isinstance(r, dict) for r in tiers):
        raise Attention('欧易保证金档位字段不合法。')
    tier = one([r for r in tiers if r.get('tier') == '1'], '第一档保证金')
    if number(tier.get('maxLever'), positive=True) < Decimal(str(cfg['leverage'])):
        raise Attention('欧易第一档不支持G3配置的杠杆。')
    mmr = number(tier.get('mmr'), positive=True)
    if mmr >= 1:
        raise Attention('欧易维持保证金率不合法。')
    face = number(swap.get('ctVal'), positive=True) * number(swap.get('ctMult') or '1', positive=True)
    limit = min(number(tier.get('maxSz'), positive=True), number(swap['maxLmtSz'], positive=True))
    spec = {'spot_lot': spot['lotSz'], 'spot_min': spot['minSz'], 'spot_tick': spot['tickSz'],
            'swap_lot': swap['lotSz'], 'swap_min': swap['minSz'], 'swap_tick': swap['tickSz'],
            'contract_usd': format(face, 'f'), 'mmr': format(mmr, 'f'), 'tier_max_contracts': format(limit, 'f'),
            'spot_max': spot['maxLmtSz'], 'spot_fee_group': spot['groupId'], 'swap_fee_group': swap['groupId']}
    fund = one(api.read('/api/v5/public/funding-rate', {'instId': SWAP}), '资金费率')
    if fund.get('instId') != SWAP:
        raise Attention('资金费率产品不符。')
    funding = {'rate': text(number(fund.get('fundingRate'))), 'time': timestamp(fund.get('fundingTime')),
               'next_time': timestamp(fund.get('nextFundingTime')), 'ts': timestamp(fund.get('ts'))}
    if funding['next_time'] <= funding['time']:
        raise Attention('资金费率结算时刻顺序不合法。')
    if fund.get('nextFundingRate') not in (None, ''):
        funding['next_rate'] = text(number(fund['nextFundingRate']))
    mark = one(api.read('/api/v5/public/mark-price', {'instType': 'SWAP', 'instId': SWAP}), '标记价格')
    if mark.get('instId') != SWAP:
        raise Attention('标记价格产品不符。')
    mark_price, mark_ts = number(mark.get('markPx'), positive=True), timestamp(mark.get('ts'))
    spot_quote, swap_quote = _quote(api, SPOT), _quote(api, SWAP)
    now = api.now()
    age = Decimal(str(cfg.get('quote_age_seconds', 5))) * 1000
    if any(not -1000 <= now - ts <= age for ts in (spot_quote['ts'], swap_quote['ts'], mark_ts)):
        raise Attention('欧易报价或标记价格过期；本轮不提交订单。')
    return {'now': now, 'spot': spot_quote, 'swap': swap_quote, 'mark': text(mark_price),
            'mark_ts': mark_ts, 'spec': spec, 'funding': funding}


def _fee(api, inst_type, inst_id, instrument=None):
    instrument = instrument or _instrument(api, inst_type, inst_id)
    params = {'instType': inst_type}
    params['instId' if inst_type == 'SPOT' else 'instFamily'] = inst_id if inst_type == 'SPOT' else 'BTC-USD'
    r = one(api.read('/api/v5/account/trade-fee', params, private=True), '手续费')
    if r.get('instType') != inst_type or not isinstance(r.get('feeGroup'), list) or not r['feeGroup']:
        raise Attention('欧易手续费分组缺失；不能使用旧字段猜测费率。')
    groups = {}
    for group in r['feeGroup']:
        if not isinstance(group, dict) or not isinstance(group.get('groupId'), str) or not group['groupId'] or group['groupId'] in groups:
            raise Attention('欧易手续费分组不合法或重复。')
        groups[group['groupId']] = group
    if instrument['groupId'] not in groups:
        raise Attention('欧易手续费分组未匹配本交易产品。')
    rate = number(groups[instrument['groupId']].get('taker'))
    if abs(rate) >= 1:
        raise Attention('欧易手续费率超出允许范围。')
    return text(max(Decimal(0), -rate))  # positive OKX rates are rebates: do not budget rebates


def account_snapshot(api, cfg, check_settings=True):
    """Read-only account audit; never change settings or move money.

    This does not assert that pending orders belong to us: the execution journal
    must compare returned pending/algo_pending and bills against its own history.
    """
    account = one(api.read('/api/v5/account/config', private=True), '账户配置')
    uid = account.get('uid')
    if not isinstance(uid, str) or not uid.isdigit():
        raise Attention('欧易账户UID缺失。')
    if account.get('acctLv') != '2' or account.get('posMode') != 'net_mode':
        raise Attention('G3须使用合约模式（原单币种保证金）和买卖/净持仓模式，请在欧易手动设置。')
    if account.get('autoLoan') is not False:
        raise Attention('请在欧易关闭自动借币后重新检查。')
    permissions = {p.strip().lower() for p in str(account.get('perm', '')).split(',')}
    if 'trade' not in permissions or 'withdraw' in permissions:
        raise Attention('G3密钥须具有读取和交易权限，不能带提币权限。')
    lever = one(api.read('/api/v5/account/leverage-info', {'instId': SWAP, 'mgnMode': 'cross'}, private=True), '币本位全仓杠杆')
    leverage = number(lever.get('lever'), positive=True)
    if (lever.get('instId') != SWAP or lever.get('mgnMode') != 'cross'
            or leverage != Decimal(str(cfg['leverage']))):
        raise Attention('请在欧易将BTC-USD-SWAP币本位永续设为全仓2倍，然后重新检查。')
    rows = api.read('/api/v5/account/positions', private=True)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise Attention('欧易持仓响应不合法。')
    active = [r for r in rows if number(r.get('pos')) != 0]
    if len(active) > 1:
        raise Attention('G3独立子账户存在多笔仓位，请人工核对。')
    position = None
    for r in active:
        if (r.get('instId') != SWAP or r.get('mgnMode') != 'cross' or r.get('posSide') != 'net'
                or r.get('ccy') != 'BTC' or number(r.get('lever'), positive=True) != leverage):
            raise Attention('G3独立子账户存在其他产品、持仓模式或杠杆的仓位。')
        position = {'contracts': text(number(r['pos'])), 'avgPx': text(number(r.get('avgPx'), positive=True)),
                    'upl': text(number(r.get('upl'))), 'margin': text(number(r.get('margin') or '0')), 'raw': r}
    balance = one(api.read('/api/v5/account/balance', private=True), '账户余额')
    details = balance.get('details')
    if not isinstance(details, list):
        raise Attention('欧易币种余额字段缺失。')
    balances = {}
    for r in details:
        if not isinstance(r, dict) or not isinstance(r.get('ccy'), str) or not r['ccy'] or r['ccy'] in balances:
            raise Attention('欧易币种余额重复或不合法。')
        ccy = r['ccy']
        cash, eq, available = number(r.get('cashBal')), number(r.get('eq')), number(r.get('availBal'))
        # Liability fields are inapplicable/empty in futures acctLv=2. Present
        # nonzero values are still rejected, as is any negative cash balance.
        liability = max(number(r.get(key) or '0', nonnegative=True) for key in ('liab', 'crossLiab', 'isoLiab', 'interest'))
        if liability != 0 or cash < 0:
            raise Attention('账户有借币、利息或负现金余额，G3不支持借贷交易。')
        if ccy not in {'BTC', 'USDT'} and (cash != 0 or eq != 0 or available != 0):
            raise Attention('G3独立子账户存在BTC和USDT以外的资产。')
        balances[ccy] = {'cash': text(cash), 'eq': text(eq), 'available': text(min(cash, available)),
                         'available_equity': text(number(r.get('availEq') or '0')), 'liab': text(liability),
                         'imr': text(number(r.get('imr') or '0')), 'mmr': text(number(r.get('mmr') or '0')),
                         'raw': r}
    # OKX omits a currency when both cash and equity are zero.
    for ccy in ('BTC', 'USDT'):
        balances.setdefault(ccy, {'cash': '0', 'eq': '0', 'available': '0', 'available_equity': '0',
                                  'liab': '0', 'imr': '0', 'mmr': '0', 'raw': {}})
    available = one(api.read('/api/v5/account/max-avail-size', {'instId': SPOT, 'tdMode': 'cash'}, private=True), '现货可用资金')
    if available.get('instId') != SPOT:
        raise Attention('欧易现货可用资金产品不符。')
    balances['BTC']['available'] = text(min(number(balances['BTC']['available']), number(available.get('availSell'), nonnegative=True)))
    balances['USDT']['available'] = text(min(number(balances['USDT']['available']), number(available.get('availBuy'), nonnegative=True)))
    fees = {'spot': _fee(api, 'SPOT', SPOT), 'swap': _fee(api, 'SWAP', SWAP)}
    pending = api.pending()
    algo_pending = api.pending_algos()
    return {'now': api.now(), 'uid': uid, 'config': account, 'balances': balances, 'position': position,
            'positions': active, 'all_positions': rows, 'pending': pending, 'algo_pending': algo_pending,
            'leverage': text(leverage), 'fees': fees}


def funding_history(api, begin, end=None):
    """Settled inverse funding only; actual account income comes from bills()."""
    end = api.now() if end is None else end
    if type(begin) is not int or type(end) is not int or not 0 <= begin <= end <= api.now() or end - begin > 88 * DAY:
        raise Attention('资金费率历史范围不合法或超过保留期限。')
    result = {}
    cursor = end + 1
    for _ in range(100):
        rows = api.read('/api/v5/public/funding-rate-history', {'instId': SWAP, 'after': str(cursor), 'limit': '100'})
        if not isinstance(rows, list) or len(rows) > 100:
            raise Attention('资金费率历史分页不合法。')
        if not rows:
            return [result[key] for key in sorted(result)]
        previous = cursor
        for r in rows:
            if not isinstance(r, dict) or r.get('instId') != SWAP:
                raise Attention('资金费率历史产品不符。')
            ts = timestamp(r.get('fundingTime'))
            if ts >= previous:
                raise Attention('资金费率历史分页未前进。')
            previous = ts
            # Empty realizedRate is not substituted with a predicted fundingRate.
            rate = text(number(r.get('realizedRate')))
            if begin <= ts <= end:
                value = {'ts': ts, 'rate': rate}
                if ts in result and result[ts] != value:
                    raise Attention('资金费率历史存在冲突。')
                result[ts] = value
        if previous < begin or len(rows) < 100:
            return [result[key] for key in sorted(result)]
        cursor = previous
    raise Attention('资金费率历史分页超过上限，不能把部分历史视为完整。')
