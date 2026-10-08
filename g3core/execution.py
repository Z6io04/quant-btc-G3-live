"""Actual-fill, crash-recoverable two-leg execution for a dedicated OKX account.

Every economic POST has a durable unique ID before transmission. An uncertain
response is only looked up by that ID; it is never interpreted as permission to
repeat the order. IOC acknowledgments are not fills. No candle-based fills.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import time
import uuid

from .okx import APIError, SafetyError

SPOT = 'BTC-USDT'
SWAP = 'BTC-USD-SWAP'
TERMINAL = ('filled', 'canceled', 'mmp_canceled')
ZERO = Decimal('0')


class ExecutionError(RuntimeError):
    safe_message = True


def dec(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ExecutionError('交易数据包含非有限数字。')
    return number


def text(value):
    return format(dec(value), 'f')


def floor(value, step):
    return (dec(value) / dec(step)).to_integral_value(rounding=ROUND_DOWN) * dec(step)


def initial_state(now, cfg, account):
    budget = dec(cfg['capital_usdt'])
    balance = account['balances']
    return {'schema':1, 'strategy':'G3-live-1', 'initialized_at':now, 'armed':True, 'paused':False,
            'halted':None, 'phase':'idle', 'position':None, 'op':None,
            'baseline_usdt':text(balance.get('USDT', {}).get('cash', '0')),
            'baseline_btc':text(balance.get('BTC', {}).get('cash', '0')),
            'cash_usdt':text(balance.get('USDT', {}).get('cash', '0')),
            'btc_balance':text(balance.get('BTC', {}).get('cash', '0')),
            'contracts':'0', 'capital_usdt':text(budget), 'capital_added_usdt':'0', 'fees_by_ccy':{},
            'realized_pnl_btc':'0', 'funding_btc':'0', 'closed_trades':0,
            'peak_equity':text(budget), 'last_equity':text(budget),
            'max_drawdown':'0', 'cooldown_until':0, 'last_reconciled_at':now,
            'bill_cursor':now, 'reconciliation_mismatch_since':None,
            'last_decision':'等待启动后的下一个完整交易信号。',
            'dust_btc':'0', 'flatten_requested':False}


class Executor:
    def __init__(self, api, store, cfg, spec=None):
        self.api, self.store, self.base_cfg, self.spec = api, store, dict(cfg), spec
        self.stopping = lambda: False

    @property
    def cfg(self):
        from .config import effective_config
        return effective_config(self.base_cfg, self.store.state() if self.store is not None else {})

    def initialize(self, identity, account, now=None):
        now = self.api.now() if now is None else now
        if self.store.identity() is not None:
            self.store.initialize(identity, {})
            return False
        budget = dec(self.cfg.get('capital_usdt'))
        loss = dec(self.cfg.get('max_loss_usdt'))
        if budget <= 0 or loss <= 0 or loss > budget:
            raise ExecutionError('首次启用前必须配置有效本金和最大停止损失金额。')
        if account.get('position') or account.get('positions') or account.get('pending') or account.get('algo_pending'):
            raise ExecutionError('首次启用必须使用无持仓、无委托的独立子账户。')
        balances = account['balances']
        if dec(balances.get('BTC', {}).get('cash', '0')) != ZERO:
            raise ExecutionError('首次启用的 BTC 余额必须为 0。')
        if dec(balances.get('USDT', {}).get('available', '0')) < budget:
            raise ExecutionError('可用 USDT 少于配置本金。')
        return self.store.initialize(identity, initial_state(now, self.cfg, account))

    def _halt(self, reason):
        state = self.store.state()
        state.update(halted=reason, paused=True, last_decision=reason)
        self.store.set_state(state)
        self.store.event('halt', {'reason':reason}, self.api.now())

    def _controls(self):
        for command in self.store.controls():
            with self.store.transaction():
                state = self.store.state()
                if command['action'] == 'pause':
                    state['paused'] = True
                    state['last_decision'] = '用户暂停新开仓；持仓风险检查继续。'
                elif command['action'] == 'resume':
                    if state.get('halted'):
                        state['last_decision'] = '存在风控停止或对账故障，resume 不会清除故障。'
                    else:
                        state['paused'] = False
                        state['last_decision'] = '已恢复新开仓条件检查。'
                else:
                    state['paused'] = True
                    state['flatten_requested'] = True
                    state['last_decision'] = '用户请求按实际持仓依次平掉两腿。'
                self.store.set_state(state)
                self.store.apply_control(command['id'])

    def begin_entry(self, plan, market, account):
        state = self.store.state()
        if not plan.get('allowed', True):
            raise ExecutionError(plan.get('reason', '入场计划未通过。'))
        if (not state.get('armed') or state.get('paused') or state.get('halted')
                or state.get('position') or state.get('op') or self.store.unresolved()
                or state.get('flatten_requested')):
            raise ExecutionError('当前账户状态不允许新开仓。')
        self.spec = market['spec']
        self._fresh(market)
        if not self.reconcile(account, market):
            raise ExecutionError('新开仓前两腿实际余额尚未核对通过。')
        state = self.store.state()
        if state.get('halted') or state.get('flatten_requested'):
            raise ExecutionError('账户风险检查要求停止新增交易。')
        contracts, gross = dec(plan['contracts']), dec(plan['gross_btc'])
        if contracts < dec(self.spec['swap_min']) or gross < dec(self.spec['spot_min']):
            raise ExecutionError('预算不足以满足交易所最小现货及合约数量。')
        if contracts != floor(contracts, self.spec['swap_lot']) or gross != floor(gross, self.spec['spot_lot']):
            raise ExecutionError('计划数量不符合交易所步长。')
        cost = gross * self._limit(market, SPOT, 'buy') * (1 + abs(dec(account['fees']['spot'])))
        if cost > min(dec(self.cfg['capital_usdt']),dec(state['last_equity'])) or cost > dec(account['balances']['USDT']['available']):
            raise ExecutionError('限价和实际手续费预留超过本金或可用余额。')
        now = self.api.now()
        state['op'] = {'id':uuid.uuid4().hex, 'type':'entry', 'stage':'spot', 'started_at':now,
                       'plan':plan, 'attempt':0, 'awaiting':None, 'reason':'minute_signal',
                       'entry_equity':state['last_equity']}
        state['phase'] = 'enter_spot'
        self.store.set_state(state)
        self.store.event('entry_planned', {'plan':plan}, now)

    def begin_exit(self, reason='risk_exit'):
        state = self.store.state()
        if state.get('op'):
            state['flatten_requested'] = True
            self.store.set_state(state)
            return
        state['op'] = {'id':uuid.uuid4().hex, 'type':'exit', 'stage':'swap',
                       'started_at':self.api.now(), 'attempt':0, 'awaiting':None, 'reason':reason}
        state['phase'] = 'exit_swap'
        self.store.set_state(state)

    def begin_treasury(self, amount):
        state = self.store.state()
        if state.get('op') or not state.get('position') or state.get('halted') or self.store.unresolved():
            raise ExecutionError('当前不能执行资金费兑换。')
        if dec(amount) <= 0:
            raise ExecutionError('兑换金额必须为正。')
        state['op'] = {'id':uuid.uuid4().hex, 'type':'treasury', 'stage':'spot',
                       'amount':text(amount), 'started_at':self.api.now(), 'attempt':0, 'awaiting':None}
        state['phase'] = 'treasury'
        self.store.set_state(state)

    def _fresh(self, market):
        now = self.api.now()
        for name in ('spot','swap'):
            if not -1000 <= now-int(market[name]['ts']) <= self.cfg.get('quote_age_seconds',5)*1000:
                raise ExecutionError('盘口过期；本轮不发送订单。')
            if dec(market[name]['bid']) <= 0 or dec(market[name]['ask']) < dec(market[name]['bid']):
                raise ExecutionError('盘口价格无效。')

    def _limit(self, market, instrument, side):
        key = 'spot' if instrument == SPOT else 'swap'
        step = dec(market['spec'][key+'_tick'])
        slip = dec(self.cfg.get('max_slippage_bps',3)) / 10000
        raw = dec(market[key]['ask' if side == 'buy' else 'bid']) * (1+slip if side == 'buy' else 1-slip)
        # Round toward the book, never outside the configured slippage limit.
        return (raw/step).to_integral_value(rounding=ROUND_DOWN if side=='buy' else ROUND_UP)*step

    def _send(self, instrument, side, size, market, reduce=False):
        if self.stopping():
            raise ExecutionError('服务正在停止，本轮不创建或发送新的订单。')
        self._fresh(market)
        state = self.store.state()
        op = state['op']
        if self.store.unresolved() or op.get('awaiting'):
            raise ExecutionError('尚有未核对订单，不能发送另一笔订单。')
        size = dec(size)
        if size <= 0:
            raise ExecutionError('订单数量必须为正。')
        spec = market['spec']
        kind = 'spot' if instrument == SPOT else 'swap'
        maximum = dec(spec.get('spot_max','1000000') if instrument == SPOT else spec['tier_max_contracts'])
        if size != floor(size,spec[kind+'_lot']) or size < dec(spec[kind+'_min']) or size > maximum:
            raise ExecutionError('实际订单数量不符合交易所步长、最小量或单笔上限。')
        body = {'instId':instrument, 'tdMode':'cash' if instrument == SPOT else 'cross',
                'side':side, 'ordType':'ioc', 'sz':text(size), 'px':text(self._limit(market,instrument,side))}
        if instrument == SWAP:
            body.update(posSide='net', reduceOnly=bool(reduce))
        now = self.api.now()
        action = op['id'] + ':' + str(op['attempt'])
        with self.store.transaction():
            item = self.store.create_intent(action, body, {'op':op['id'], 'stage':op['stage'], 'kind':op['type']}, now)
            op['awaiting'] = item['id']
            op['attempt'] += 1
            self.store.set_state(state)
        # Durable sent marker BEFORE mutation. Crash here requires lookup, not retry.
        item['status'] = 'sent'
        self.store.update_intent(item)
        expiry = min(now+5000, int(market['spot' if instrument == SPOT else 'swap']['ts']) + self.cfg.get('quote_age_seconds',5)*1000)
        try:
            if self.stopping():
                raise SafetyError('STOP_REQUESTED', '/api/v5/trade/order')
            rows = self.api.write('/api/v5/trade/order', item['request'], exp_time=expiry)
        except (APIError, SafetyError) as exc:
            # These exception types guarantee authoritative rejection or local
            # refusal before transport; unlike a timeout, zero execution is known.
            with self.store.transaction():
                item['status'] = 'expired'
                item['result'] = {'reason':'definite_rejection','code':exc.code}
                self.store.update_intent(item)
                state = self.store.state()
                state['op']['awaiting'] = None
                state['op']['last_order'] = {'filled':'0','cost':'0','instId':instrument,'side':side}
                self.store.set_state(state)
            return
        except Exception:
            item['status'] = 'unknown'
            item['result'] = {'reason':'acknowledgement_unavailable'}
        else:
            item['status'] = 'acknowledged'
            item['result'] = {'ack':[{k:r[k] for k in ('ordId','clOrdId','sCode') if k in r} for r in rows]}
            # Even an explicit HTTP/API rejection is treated conservatively if
            # no authoritative order record is available; never blindly repeat.
        self.store.update_intent(item)

    def recover(self):
        """Only reconcile existing IDs. No new economic orders in recovery."""
        for item in self.store.unresolved():
            if item['status'] == 'prepared':
                with self.store.transaction():
                    item['status'] = 'expired'
                    self.store.update_intent(item)
                    state = self.store.state()
                    if state.get('op') and state['op'].get('awaiting') == item['id']:
                        state['op']['awaiting'] = None
                        state['op']['last_order'] = {'filled':'0','side':item['request']['side'],'instId':item['request']['instId']}
                        self.store.set_state(state)
                continue
            row = self.api.get_order(item['request']['instId'], item['id'])
            if row is None:
                item['status'] = 'unknown'
                self.store.update_intent(item)
                continue
            request = item['request']
            for key in ('clOrdId','instId','side','ordType'):
                if str(row.get(key)) != str(request[key]):
                    self._halt('交易所订单与本地持久化请求不一致。')
                    raise ExecutionError('Order identity mismatch')
            if row.get('tdMode') != request['tdMode']:
                self._halt('交易所订单保证金模式与本地请求不一致。')
                raise ExecutionError('Order margin mode mismatch')
            if request['instId'] == SWAP and (row.get('posSide') != 'net' or str(row.get('reduceOnly')).lower() != str(request['reduceOnly']).lower()):
                self._halt('交易所合约订单方向或只减仓标记不一致。')
                raise ExecutionError('Order reduction mode mismatch')
            if dec(row.get('sz','0')) != dec(request['sz']):
                self._halt('交易所订单数量与本地请求不一致。')
                raise ExecutionError('Order size mismatch')
            if not ZERO <= dec(row.get('accFillSz','0')) <= dec(request['sz']):
                raise ExecutionError('Order cumulative fill outside request bounds')
            item['exchange_order_id'] = str(row['ordId'])
            self.store.update_intent(item)
            if row.get('state') not in TERMINAL:
                if self.api.now()-item['created_at'] > self.cfg.get('order_ttl_seconds',15)*1000 and not item.get('cancel_sent'):
                    item['cancel_sent'] = True
                    self.store.update_intent(item)
                    try:
                        self.api.write('/api/v5/trade/cancel-order', {'instId':request['instId'],'clOrdId':item['id']})
                    except Exception:
                        pass  # Cancellation acknowledgment is not a terminal order.
                continue
            fills = self.api.fills('SPOT' if request['instId']==SPOT else 'SWAP',request['instId'],begin=item['created_at']-1000,end=self.api.now())
            fills = [r for r in fills if str(r.get('ordId')) == str(row['ordId'])]
            if sum((dec(r['fillSz']) for r in fills), ZERO) != dec(row.get('accFillSz','0')):
                continue  # Fills endpoint may lag order endpoint. Never invent a fill.
            self._apply(item,row,fills)

    def _apply(self, item, row, fills):
        with self.store.transaction():
            state = self.store.state()
            btc, usdt, contracts = dec(state['btc_balance']), dec(state['cash_usdt']), dec(state['contracts'])
            total, total_cost = ZERO, ZERO
            for raw in fills:
                fill = {k:str(raw.get(k,'')) for k in ('billId','tradeId','ordId','instId','side','fillSz','fillPx','fee','feeCcy','fillPnl','fillTime','ts')}
                if not fill['tradeId'] or fill['instId'] != item['request']['instId'] or fill['side'] != item['request']['side']:
                    raise ExecutionError('成交明细身份不一致。')
                fee = dec(fill['fee'] or '0')
                ccy = fill['feeCcy']
                if ccy not in ('BTC','USDT'):
                    raise ExecutionError('实际手续费币种不受支持；需要人工核对。')
                quantity, price = dec(fill['fillSz']), dec(fill['fillPx'])
                if quantity <= 0 or price <= 0:
                    raise ExecutionError('实际成交数量或价格无效。')
                total += quantity
                total_cost += quantity*price
                if not self.store.record('fills',fill['instId']+':'+fill['ordId']+':'+fill['tradeId'],fill):
                    raise ExecutionError('成交已应用但请求未完成；账本损坏。')
                if fill['instId'] == SPOT:
                    direction = 1 if fill['side']=='buy' else -1
                    btc += direction*quantity
                    usdt -= direction*quantity*price
                else:
                    contracts += quantity if fill['side']=='sell' else -quantity
                    pnl = dec(fill['fillPnl'] or '0')
                    btc += pnl
                    state['realized_pnl_btc'] = text(dec(state['realized_pnl_btc'])+pnl)
                if ccy == 'BTC':
                    btc += fee
                else:
                    usdt += fee
                state['fees_by_ccy'][ccy] = text(dec(state['fees_by_ccy'].get(ccy,'0'))+fee)
            if contracts < 0 or btc < Decimal('-0.0000000001'):
                raise ExecutionError('成交导致意外多头或负 BTC 余额。')
            state.update(btc_balance=text(btc),cash_usdt=text(usdt),contracts=text(contracts))
            op = state.get('op')
            if not op or op.get('awaiting') != item['id']:
                raise ExecutionError('订单与执行状态无法关联。')
            op['awaiting'] = None
            op['last_order'] = {'filled':text(total),'cost':text(total_cost),'instId':row['instId'],'side':row['side']}
            if row['instId']==SPOT and row['side']=='buy' and total:
                fill_times = [int(r.get('fillTime') or r.get('ts') or self.api.now()) for r in fills]
                op.setdefault('unhedged_since',min(fill_times))
            if row['instId'] == SWAP and row['side']=='sell' and total:
                # Inverse average entry is harmonic in price, not arithmetic.
                denominator = sum((dec(r['fillSz'])/dec(r['fillPx']) for r in fills),ZERO)
                op['swap_entry'] = text(total/denominator)
            if state.get('position') and op['type']=='treasury':
                state['position']['treasury_sold_btc'] = text(dec(state['position'].get('treasury_sold_btc','0'))+total)
            item.update(status='applied',result={'ordId':str(row['ordId']),'state':row['state'],'filled':text(total)},updated_at=self.api.now())
            self.store.update_intent(item)
            self.store.set_state(state)

    def sync_bills(self):
        state = self.store.state()
        now = self.api.now()
        if now-state['bill_cursor'] > 6*86400000:
            self._halt('离线超过账单安全回溯窗口，须人工核对后恢复。')
            return False
        rows = self.api.bills(begin=max(state['initialized_at'],state['bill_cursor']-86400000),end=now)
        own_orders = {str(i['result'].get('ordId')) for i in self.store.intents() if i['status']=='applied'}
        for item in self.store.intents():
            own_orders.update(str(r.get('ordId')) for r in item['result'].get('ack',[]))
            if item.get('exchange_order_id'):
                own_orders.add(item['exchange_order_id'])
        with self.store.transaction():
            state = self.store.state()
            for raw in rows:
                bill = {k:str(raw.get(k,'')) for k in ('billId','type','subType','ccy','balChg','sz','fee','pnl','instId','ordId','ts')}
                if not bill['billId'] or not bill['ts']:
                    raise ExecutionError('账单缺少唯一编号或时间。')
                if int(bill['ts']) < state['initialized_at']:
                    continue
                if not self.store.record('bills',bill['billId'],bill):
                    continue
                if bill['type']=='8' and bill['subType'] in ('173','174') and bill['ccy']=='BTC' and bill['instId']==SWAP:
                    change = dec(bill['balChg'])
                    state['btc_balance'] = text(dec(state['btc_balance'])+change)
                    state['funding_btc'] = text(dec(state['funding_btc'])+change)
                    if state.get('position'):
                        state['position']['funding_btc'] = text(dec(state['position'].get('funding_btc','0'))+change)
                    elif (state.get('op') or {}).get('type') == 'entry' and dec(state['contracts']) > 0:
                        state['op']['funding_btc'] = text(dec(state['op'].get('funding_btc','0'))+change)
                elif bill['type']=='2' and bill['ordId'] in own_orders:
                    pass  # Physical balance deltas and fees already come from actual fills.
                else:
                    state.update(halted='发现非本策略的账单或未知余额变动，停止自动交易。',paused=True)
            state['bill_cursor'] = now
            self.store.set_state(state)
        return not self.store.state().get('halted')

    def reconcile(self, account, market):
        state = self.store.state()
        known = {i['id'] for i in self.store.unresolved()}
        if any(str(p.get('clOrdId','')) not in known for p in account.get('pending',[])) or account.get('algo_pending'):
            self._halt('发现未登记的委托；停止自动交易。')
            return False
        actual_btc = dec(account['balances'].get('BTC',{}).get('cash','0'))
        actual_usdt = dec(account['balances'].get('USDT',{}).get('cash','0'))
        actual_contracts = -dec((account.get('position') or {}).get('contracts','0'))
        ok = (abs(actual_btc-dec(state['btc_balance'])) <= Decimal('0.000000001')
              and abs(actual_usdt-dec(state['cash_usdt'])) <= Decimal('0.00001')
              and actual_contracts == dec(state['contracts']))
        now = self.api.now()
        if not ok:
            state['reconciliation_mismatch_since'] = state.get('reconciliation_mismatch_since') or now
            state['last_decision'] = '余额或仓位与逐笔账本暂不一致；等待成交/账单同步，禁止新订单。'
            if now-state['reconciliation_mismatch_since'] > 60000:
                state.update(halted='实际余额/仓位持续无法与账本核对，需人工检查。',paused=True)
            self.store.set_state(state)
            return False
        mark = dec(market['mark'])
        upl = dec((account.get('position') or {}).get('upl','0'))
        equity = dec(self.cfg['capital_usdt']) + actual_usdt-dec(state['baseline_usdt']) + (actual_btc-dec(state['baseline_btc'])+upl)*mark
        peak = max(dec(state['peak_equity']),equity)
        drawdown = (peak-equity)/peak if peak>0 else ZERO
        state.update(last_reconciled_at=now,reconciliation_mismatch_since=None,
                     last_equity=text(equity),peak_equity=text(peak),
                     max_drawdown=text(max(dec(state['max_drawdown']),drawdown)))
        if state.get('position'):
            state['position'].update(coins_btc=text(actual_btc), contracts=text(actual_contracts))
        if dec(self.cfg['capital_usdt'])-equity >= dec(self.cfg['max_loss_usdt']):
            state.update(halted='达到配置的账户停止损失金额，请求退出。',paused=True,flatten_requested=True)
        if drawdown >= Decimal('0.05'):
            state.update(halted='G3 账户回撤达到 5%，停止新增交易并请求退出。',paused=True,flatten_requested=True)
        self.store.set_state(state)
        return True

    def tick(self, market, account, now=None, prepared=False):
        self.spec = market['spec']
        self._controls()
        if not prepared:
            self.recover()
            self.sync_bills()
        if self.store.unresolved():
            state = self.store.state()
            state['last_decision'] = '有已发出但尚未核对的订单；只查询原订单，不重复下单。'
            self.store.set_state(state)
            return state
        if not self.reconcile(account,market):
            return self.store.state()
        state = self.store.state()
        if state.get('halted') and not state.get('flatten_requested'):
            return state
        if state.get('flatten_requested') and not state.get('op'):
            self.begin_exit('manual_or_risk_flatten')
        state = self.store.state()
        if state.get('op'):
            self._advance(market,account)
        return self.store.state()

    def _stage(self, stage, phase):
        state = self.store.state()
        state['op']['stage'] = stage
        state['op'].pop('last_order',None)
        state['phase'] = phase
        self.store.set_state(state)

    def _advance(self, market, account):
        state = self.store.state()
        op = state['op']
        if op.get('awaiting'):
            return
        btc, contracts = dec(state['btc_balance']),dec(state['contracts'])
        spec = market['spec']
        if op['type']=='entry':
            actual_entry = dec((account.get('position') or {}).get('avgPx','0'))
            hedge_verified = (contracts > 0 and actual_entry > 0 and
                abs(btc-contracts*dec(spec['contract_usd'])/actual_entry)*dec(market['spot']['ask']) <= dec(op['entry_equity'])*Decimal('0.02'))
            timeout = (self.api.now()-op.get('unhedged_since',op['started_at']) > self.cfg.get('max_unhedged_seconds',60)*1000 and not hedge_verified)
            if (state.get('flatten_requested') or state.get('paused') or state.get('halted')
                    or timeout):
                state['op'] = None
                self.store.set_state(state)
                self.begin_exit('incomplete_entry_unwind')
                return
            if op['stage']=='spot':
                if 'last_order' not in op:
                    cost = dec(op['plan']['gross_btc'])*self._limit(market,SPOT,'buy')*(1+abs(dec(account['fees']['spot'])))
                    if cost > min(dec(self.cfg['capital_usdt']),dec(state['last_equity']),dec(account['balances']['USDT']['available'])):
                        return self._finish_flat('当前限价和手续费预留超过本金；本次入场取消。')
                    return self._send(SPOT,'buy',op['plan']['gross_btc'],market)
                if btc < dec(spec['spot_min']):
                    return self._finish_flat('现货未成交或余额不足最小数量。')
                self._stage('swap','enter_swap')
                return self._advance(market,account)
            if op['stage']=='swap':
                if 'last_order' not in op:
                    # Account for actual spot receipts and current inverse fee.
                    px = self._limit(market,SWAP,'sell')
                    possible = btc*px/(dec(spec['contract_usd'])*(1+abs(dec(account['fees']['swap']))))
                    step = dec(spec['swap_lot'])
                    # G3 permits the nearest hedge above or below neutrality if
                    # residual remains <=2%; flooring here silently changed G3.
                    nearest = floor(possible+step/2,step)
                    qty = min(dec(op['plan']['contracts']),nearest,floor(spec['tier_max_contracts'],step))
                    face = dec(spec['contract_usd'])
                    coins_after_fee = btc-qty*face/px*abs(dec(account['fees']['swap']))
                    residual = abs(coins_after_fee-qty*face/px)*dec(market['spot']['ask'])
                    if qty and (residual > dec(op['entry_equity'])*Decimal('0.02') or coins_after_fee*px/(qty*face)<Decimal('0.5')):
                        # A partial spot fill can require the exact/floor hedge;
                        # any BTC excess is sold after actual swap reconciliation.
                        qty = min(dec(op['plan']['contracts']),floor(possible,step),floor(spec['tier_max_contracts'],step))
                    if qty < dec(spec['swap_min']):
                        state['op'] = None
                        self.store.set_state(state)
                        self.begin_exit('spot_fill_cannot_fund_minimum_hedge')
                        return
                    return self._send(SWAP,'sell',qty,market)
                if not contracts:
                    state['op'] = None
                    self.store.set_state(state)
                    self.begin_exit('hedge_not_filled')
                    return
                self._stage('trim','entry_trim')
                return self._advance(market,account)
            actual_entry = dec((account.get('position') or {}).get('avgPx','0'))
            if actual_entry <= 0:
                raise ExecutionError('实际币本位合约入场价尚不可核对。')
            neutral = contracts*dec(spec['contract_usd'])/actual_entry
            excess = max(ZERO,btc-neutral)
            residual = abs(btc-neutral)*dec(market['spot']['ask'])
            if residual > dec(op['entry_equity'])*Decimal('0.02'):
                qty = floor(min(excess,dec(account['balances']['BTC']['available'])),spec['spot_lot'])
                if 'last_order' not in op and qty >= dec(spec['spot_min']):
                    return self._send(SPOT,'sell',qty,market)
                state['op'] = None
                self.store.set_state(state)
                self.begin_exit('hedge_residual_above_two_percent')
                return
            entry = (account.get('position') or {}).get('avgPx') or op.get('swap_entry')
            if not entry or dec(entry)<=0:
                raise ExecutionError('实际合约入场价格尚不可核对。')
            state['position'] = {'entered_at':self.api.now(),'opened_at':self.api.now(),
                'capital_at_entry':op['entry_equity'],'entry_equity':op['entry_equity'],
                'contracts':text(contracts),'notional_usd':text(contracts*dec(spec['contract_usd'])),
                'swap_entry':text(entry),'entry_swap_px':text(entry),'coins_btc':text(btc),
                'entry_btc':text(btc),'funding_btc':op.get('funding_btc','0'),'treasury_sold_btc':'0',
                'bad_days':0,'last_daily_check':0}
            state.update(phase='active',op=None,last_decision='两腿实际成交和账户余额已核对；持有资金费对冲组合。')
            self.store.set_state(state)
            return
        if op['type']=='treasury':
            if 'last_order' in op:
                state.update(phase='active',op=None,last_decision='资金费兑换已按实际成交登记。')
                self.store.set_state(state)
                return
            position = state['position']
            earned = max(ZERO,dec(position['funding_btc'])-dec(position['treasury_sold_btc']))
            actual_entry = dec((account.get('position') or {}).get('avgPx','0'))
            if actual_entry <= 0:
                raise ExecutionError('实际币本位合约入场价尚不可核对。')
            neutral = contracts*dec(spec['contract_usd'])/actual_entry
            collateral = dec(account['balances']['BTC'].get('eq',btc))
            minimum_collateral = contracts*dec(spec['contract_usd'])/dec(market['mark'])*Decimal('0.75')
            max_sale = min(dec(op['amount']),earned,max(ZERO,btc-neutral),max(ZERO,collateral-minimum_collateral),dec(account['balances']['BTC']['available']))
            # Fee may be charged in BTC on a sell for some account configurations.
            qty = floor(max_sale/(1+abs(dec(account['fees']['spot']))),spec['spot_lot'])
            if qty < dec(spec['spot_min']):
                state.update(phase='active',op=None,last_decision='资金费零头或抵押余额不足，暂不兑换。')
                self.store.set_state(state)
                return
            return self._send(SPOT,'sell',qty,market)
        # Exit: close inverse short first, then sell only released BTC.
        if op['stage']=='swap':
            if contracts:
                if op['attempt'] >= 8:
                    self._halt('多次限价平仓仍有合约剩余；保留账本，需人工处理。')
                    state = self.store.state()
                    state['flatten_requested'] = False
                    self.store.set_state(state)
                    return
                op.pop('last_order',None)
                self.store.set_state(state)
                return self._send(SWAP,'buy',contracts,market,reduce=True)
            self._stage('spot','exit_spot')
            return self._advance(market,account)
        # max-avail-size already bounds the exchange-available sell quantity.
        # A synthetic base-fee haircut here can strand exactly one minimum lot.
        qty = floor(min(btc,dec(account['balances'].get('BTC',{}).get('available','0'))),spec['spot_lot'])
        if qty >= dec(spec['spot_min']):
            if op['attempt'] >= 12:
                self._halt('多次现货退出仍有余额；保留账本，需人工处理。')
                state = self.store.state()
                state['flatten_requested'] = False
                self.store.set_state(state)
                return
            op.pop('last_order',None)
            self.store.set_state(state)
            return self._send(SPOT,'sell',qty,market)
        if btc >= dec(spec['spot_min']):
            state['last_decision'] = '合约已平，等待抵押 BTC 解冻后卖出。'
            self.store.set_state(state)
            return
        self._finish_flat('两腿已退出；低于现货最小数量的 BTC 零头仍登记。')

    def _finish_flat(self, reason):
        state = self.store.state()
        if dec(state['contracts']) != 0:
            raise ExecutionError('仍有合约持仓，不能登记为空仓。')
        had_position = state.get('position') is not None
        state.update(op=None,position=None,phase='idle',dust_btc=state['btc_balance'],
                     flatten_requested=False,last_decision=reason)
        if had_position:
            state['closed_trades'] += 1
            state['cooldown_until'] = self.api.now()+86400000
        self.store.set_state(state)
