"""Manual, deposit-only capital reconciliation. Never transfer funds or send orders.

OKX official bill mapping (reviewed 2026-10-07): type 1 = Transfer,
subType 11 = Transfer in, subType 12 = Transfer out. Only 1/11 is accepted.
https://www.okx.com/docs-v5/#trading-account-rest-api-get-bill-types
The complete account bill slice comes from OKXClient.bills (all currencies/types).
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import os
import sqlite3
import subprocess
import sys

from . import config, runtime
from .config import Attention, require
from .market import account_snapshot, market_snapshot
from .store import AccountLock, Store

DAY = 86400000
BILL_FIELDS = ('billId','type','subType','ccy','balChg','sz','fee','pnl','instId','ordId','ts')


def number(value):
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        raise Attention('资金核对数值缺失或不合法；没有同步。') from None
    require(result.is_finite(), '资金核对数值不合法；没有同步。')
    return result


def text(value):
    return format(number(value), 'f')


def service_stopped():
    """An unavailable user manager is not evidence that trading has stopped."""
    try:
        result = subprocess.run(
            ['systemctl','--user','show','--no-pager',config.UNIT,
             '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],
            text=True, capture_output=True, timeout=10)
        fields = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        good = (result.returncode in (0,4) and
                set(fields) == {'LoadState','ActiveState','SubState','MainPID'} and
                fields['LoadState'] in ('loaded','not-found') and
                fields['ActiveState'] in ('inactive','failed') and
                fields['SubState'] in ('dead','failed') and fields['MainPID'] == '0')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        good = False
    require(good, '先在空仓时停止G3服务，再转入USDT；无法确认服务停止时不允许同步资金。')


def existing_ledger(directory):
    """Store may create schemas; reject uninitialized or unknown files before it opens."""
    path = config.plain_file(directory/'state.sqlite3')
    require(path.stat().st_size > 0, '空账本尚未启用；资金同步不会初始化账户。')
    for suffix in ('-wal','-shm','-journal'):
        config.plain_file(directory/('state.sqlite3'+suffix),missing=True)
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        require(db.execute('PRAGMA user_version').fetchone()[0] == 1, '账本版本未知；不能同步资金。')
        objects = db.execute("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
        require(set(objects) == {('table',n) for n in ('meta','events','intents','records','controls')},
                '账本结构未知；不能同步资金。')
        require({r[0] for r in db.execute('SELECT key FROM meta')} == {'identity','state'},
                '启用身份或运行账本缺失、格式未知；不能同步资金。')
    finally:
        db.close()


def eligible_state(store, cfg):
    state = store.state()
    require(state.get('armed') is True, 'G3尚未启用；首次资金应在setup-config填写。')
    require(not state.get('halted'), '已有风控停止，资金同步不能清除风控或覆盖故障。')
    require(state.get('phase') == 'idle' and not state.get('position') and not state.get('op') and
            not state.get('flatten_requested'), '必须结束两腿交易且没有执行中的操作，才能同步资金。')
    require(state.get('reconciliation_mismatch_since') is None, '原账本已有余额不符；不能以注资掩盖未核对差额。')
    require(not store.unresolved() and not store.controls(), '存在未完成订单核对或控制请求；不能同步资金。')
    require(number(state.get('contracts')) == 0 and number(state.get('btc_balance')) == 0 and
            number(state.get('baseline_btc')) == 0 and number(state.get('dust_btc',0)) == 0,
            'BTC现货或合约仍有余额（含零碎BTC）；本次不支持带仓同步。')
    require(number(state.get('capital_usdt')) > 0 and number(state.get('baseline_usdt')) >= 0 and
            number(state.get('cash_usdt')) >= 0 and number(state.get('last_equity')) > 0 and
            number(state.get('peak_equity')) >= number(state.get('last_equity')),
            '原资金或净值记录不合法；没有同步。')
    require(type(state.get('initialized_at')) is int and type(state.get('bill_cursor')) is int and
            0 < state['initialized_at'] <= state['bill_cursor'], '原账单时间记录不合法。')
    config.effective_config(cfg,state)
    capital = number(state['capital_usdt'])
    equity = capital+number(state['cash_usdt'])-number(state['baseline_usdt'])
    peak = number(state['peak_equity'])
    require(equity == number(state['last_equity']), '原空仓净值与现金账本不一致；需先完成原有对账。')
    require(capital-equity < number(cfg['max_loss_usdt']) and (peak-equity)/peak < Decimal('0.05')
            and number(state.get('max_drawdown','0')) < Decimal('0.05'),
            '原资金已达到亏损或回撤停止条件，不能用新增资金稀释风险后恢复。')
    return state


def normalize_bill(raw):
    require(isinstance(raw,dict), '账单响应格式不正确。')
    row = {k:str(raw.get(k,'')) for k in BILL_FIELDS}
    require(row['billId'].isdigit() and row['ts'].isdigit(), '账单编号或时间不合法。')
    return row


def flat_account(account, identity):
    require(account['uid'] == identity['uid'], 'API账户与原G3账本不一致；不能同步其他账户。')
    require(not account.get('position') and not account.get('positions') and
            not account.get('pending') and not account.get('algo_pending'),
            '欧易仍有持仓或普通/算法委托，不能同步资金。')
    balances = account['balances']
    for ccy, balance in balances.items():
        require(number(balance.get('liab',0)) == 0, '账户有负债，不能同步资金。')
        require(number(balance['cash']) >= 0, '账户有负现金余额，不能同步资金。')
        if ccy != 'USDT':
            require(number(balance['cash']) == 0 and number(balance['eq']) == 0 and
                    number(balance['available']) == 0, '账户存在BTC或其他币种资产；本次只支持USDT空仓注资。')
    usdt = balances['USDT']
    require(number(usdt['cash']) == number(usdt['eq']), '空仓USDT现金与权益不一致，不能同步。')
    return usdt


def deposit_plan(api, store, cfg, identity, state):
    """No ledger mutation. All unseen bills must be positive USDT transfer-ins."""
    api.sync_time()
    end = api.now()
    require(type(end) is int and 0 <= end-state['bill_cursor'] <= 6*DAY,
            '距上次账单核对超过6天或时钟异常；需先人工核对，不能直接同步资金。')
    begin = max(state['initialized_at'], state['bill_cursor']-DAY)
    rows = api.bills(begin=begin,end=end)
    require(isinstance(rows,list), '没有取得完整账单列表。')
    known = {r['billId']:r for r in store.records('bills')}
    seen, incoming = {}, []
    for raw in rows:
        row = normalize_bill(raw)
        require(begin <= int(row['ts']) <= end, '账单不在要求的完整核对区间。')
        if row['billId'] in seen:
            require(seen[row['billId']] == row, '同一账单ID存在冲突。')
            continue
        seen[row['billId']] = row
        if row['billId'] in known:
            require(known[row['billId']] == row, '原已登记账单发生变化；没有同步。')
            continue
        require(row['type'] == '1' and row['subType'] == '11' and row['ccy'] == 'USDT' and
                row['instId'] == '' and row['ordId'] == '' and number(row['balChg']) > 0 and
                number(row['fee'] or '0') == 0 and number(row['pnl'] or '0') == 0,
                '发现未登记的非USDT转入账单（如交易、提现、转出或利息）；没有同步。')
        incoming.append(row)
    incoming.sort(key=lambda r:(int(r['ts']),int(r['billId'])))
    amount = sum((number(r['balChg']) for r in incoming),Decimal(0))
    account = account_snapshot(api,cfg)
    usdt = flat_account(account,identity)
    market = market_snapshot(api,cfg)
    require(market['spec'] == identity['spec'], '交易规格与启用时不同；不能通过同步资金升级交易规格。')
    actual, expected = number(usdt['cash']), number(state['cash_usdt'])+amount
    require(actual == expected, '实际USDT余额变化与新增转入账单之和不完全一致；等待账单完整后重试。')
    capital = number(state['capital_usdt'])+amount
    # Capital is the cumulative assigned principal, not current profit. Available
    # must cover the remaining current strategy equity, including actual losses.
    required = number(state['last_equity'])+amount
    require(number(usdt['available']) >= required, 'USDT可用余额不足以覆盖同步后的策略净值。')
    require(capital > 0 and required > 0, '同步后的资金记录不合法。')
    return {'rows':incoming,'added':text(amount),'capital':text(capital),
            'cash':text(actual),'available':text(usdt['available']),'equity':text(required),'end':end}


def sync_capital(cfg, confirm=input):
    """Called by launch.py sync-capital. confirm receives a prompt and returns text.

    Stop while flat BEFORE making a transfer. The command only recognizes manual
    USDT deposits with exact matching incoming exchange bills. It does not change
    config/identity, loss limits, pause/halt flags, historical PnL or risk records.
    """
    require(sys.platform.startswith('linux') and os.geteuid()!=0,
            '请在Debian服务器上使用普通用户运行，不要sudo。')
    service_stopped()
    directory = config.data_dir()
    existing_ledger(directory)
    with AccountLock(directory):
        existing_ledger(directory)
        store = Store(directory)
        try:
            saved = store.identity()
            require(isinstance(saved,dict) and isinstance(saved.get('uid'),str), '缺少原启用身份；不能自动重建账户。')
            with runtime.account_guard(saved['uid']):
                service_stopped()
                api = runtime.client(cfg,config.credentials(),orders=False)
                runner = runtime.Runner(api,store,cfg)
                identity = runner.verify_identity()
                state = eligible_state(store,cfg)
                plan = deposit_plan(api,store,cfg,identity,state)
                if number(plan['added']) == 0:
                    return {'passed':True,'added_usdt':'0','capital_usdt':state['capital_usdt'],
                            'orders_sent':0,'changed':False,'note':'没有新的可确认USDT转入；账本和服务均未修改。'}
                prompt = ('已核对USDT转入 %s U，共%s笔；G3本金 %s → %s U；累计亏损限额仍为%s U。\n'
                          '这只把已到账资金计入G3，不划转、不下单、不启动服务。确认请输入 SYNC G3：' %
                          (plan['added'],len(plan['rows']),state['capital_usdt'],plan['capital'],cfg['max_loss_usdt']))
                require(confirm(prompt).strip() == 'SYNC G3', '未确认同步，账本未修改。')
                service_stopped()
                require(runner.verify_identity() == identity, '确认期间账户身份或文件已改变；没有同步。')
                require(eligible_state(store,cfg) == state, '确认期间本地账本已改变；没有同步。')
                fresh = deposit_plan(api,store,cfg,identity,state)
                require(all(fresh[k] == plan[k] for k in ('rows','added','capital','cash','available','equity')),
                        '确认期间账单、余额或可用余额发生变化；请重新检查。')
                service_stopped()
                # One SQLite commit: interruption cannot register bills without
                # their capital change, nor credit the same bill twice on retry.
                with store.transaction():
                    require(store.identity() == identity and eligible_state(store,cfg) == state,
                            '提交前原账本或控制请求已变化；没有同步。')
                    for row in fresh['rows']:
                        require(store.record('bills',row['billId'],row), '转入账单已登记；不重复增加本金。')
                    updated = deepcopy(state)
                    added = number(fresh['added'])
                    for key in ('capital_usdt','cash_usdt','baseline_usdt','peak_equity','last_equity'):
                        updated[key] = text(number(updated[key])+added)
                    updated['capital_added_usdt'] = text(number(updated.get('capital_added_usdt','0'))+added)
                    updated['bill_cursor'] = fresh['end']
                    updated['last_capital_sync_at'] = fresh['end']
                    store.set_state(updated)
                    store.event('capital_added',{'amount_usdt':fresh['added'],
                                'capital_usdt':updated['capital_usdt'],
                                'bill_ids':[r['billId'] for r in fresh['rows']],
                                'orders_sent':0,'max_loss_usdt_unchanged':cfg['max_loss_usdt']},fresh['end'])
                return {'passed':True,'changed':True,'added_usdt':fresh['added'],
                        'capital_usdt':updated['capital_usdt'],'max_loss_usdt':cfg['max_loss_usdt'],
                        'orders_sent':0,'note':'本金已同步；历史盈亏和风控记录保留，服务尚未启动。'}
        finally:
            store.close()
