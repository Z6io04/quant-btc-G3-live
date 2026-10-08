import copy
import json
import unittest
import tempfile
from decimal import Decimal
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
import urllib.error
import urllib.request

from g3core.config import Attention
from g3core.okx import OKXClient, SafetyError, ProtocolError, UncertainRequest, _RequiredProxy, _NoRedirect
from g3core.market import market_snapshot, account_snapshot, funding_history

NOW = 1791331200000
CREDS = {'api_key': 'test-key', 'secret_key': 'test-secret', 'passphrase': 'test-pass'}


class Response:
    status = 200
    def __init__(self, value): self.value = json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return self.value[:size]


class APITransportTests(unittest.TestCase):
    def client(self, transport, allow=True):
        c = OKXClient(proxy='http://127.0.0.1:17890', credentials=CREDS, allow_orders=allow, transport=transport)
        c._server_anchor_ms = NOW
        c._last_sync_monotonic = __import__('time').monotonic()
        return c

    def test_no_demo_or_implicit_proxy(self):
        with self.assertRaises(SafetyError): OKXClient(mode='demo', proxy='http://127.0.0.1:17890')
        with self.assertRaises(SafetyError): OKXClient()
        for proxy in ('http://remote:17890', 'https://localhost:17890', 'http://user:secret@localhost:17890'):
            with self.assertRaises(SafetyError): OKXClient(proxy=proxy)

    def test_proxy_cannot_honor_no_proxy_or_redirect(self):
        req = urllib.request.Request('https://www.okx.com/api/v5/public/time')
        with patch('urllib.request.proxy_bypass', side_effect=AssertionError('must not bypass')):
            _RequiredProxy({}).proxy_open(req, 'http://127.0.0.1:17890', 'https')
        self.assertEqual(req.host, '127.0.0.1:17890')
        self.assertIsNone(_NoRedirect().redirect_request(req, None, 302, '', {}, 'https://other.invalid/'))

    def test_private_is_signed_live(self):
        captured = []
        def transport(req, timeout):
            captured.append(req)
            return Response({'code': '0', 'data': []})
        c = self.client(transport)
        c.read('/api/v5/account/config')
        headers = {k.lower(): v for k, v in captured[0].header_items()}
        self.assertEqual(headers['x-simulated-trading'], '0')
        self.assertEqual(headers['ok-access-key'], CREDS['api_key'])
        self.assertIn('ok-access-sign', headers)
        with self.assertRaises(SafetyError): c.read('/api/v5/account/config', private=False)

    def test_post_timeout_never_retried_or_leaks(self):
        calls = []
        def transport(req, timeout):
            calls.append(req)
            raise urllib.error.URLError('secret-message ' + CREDS['secret_key'])
        c = self.client(transport)
        body = {'instId': 'BTC-USDT', 'tdMode': 'cash', 'clOrdId': 'g3A1', 'side': 'buy', 'ordType': 'ioc', 'sz': '0.0001', 'px': '90000'}
        with self.assertRaises(UncertainRequest) as raised: c.write('/api/v5/trade/order', body)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('test-secret', str(raised.exception))
        self.assertNotIn('secret-message', str(raised.exception))

    def test_mutation_is_restricted(self):
        captured = []
        def transport(req, timeout):
            captured.append(req)
            body = json.loads(req.data)
            return Response({'code': '0', 'data': [{'sCode': '0', 'ordId': '12', 'clOrdId': body['clOrdId']}]})
        c = self.client(transport)
        good = {'instId': 'BTC-USD-SWAP', 'tdMode': 'cross', 'posSide': 'net', 'reduceOnly': True,
                'clOrdId': 'g3B1', 'side': 'buy', 'ordType': 'ioc', 'sz': '0.1', 'px': '90000'}
        c.write('/api/v5/trade/order', good)
        for change in ({'reduceOnly': False}, {'reduceOnly': 'true'}, {'tdMode': 'isolated'},
                       {'ordType': 'market'}, {'instId': 'BTC-USDT-SWAP'}, {'px': 'NaN'}, {'sz': '1e5'}, {'tgtCcy': 'base_ccy'}):
            with self.assertRaises(SafetyError): c.write('/api/v5/trade/order', dict(good, **change))
        with self.assertRaises(SafetyError): c.write('/api/v5/account/set-leverage', {})
        self.assertEqual(len(captured), 1)

    def test_order_ack_identity_mismatch_uncertain(self):
        c = self.client(lambda *a, **kw: Response({'code': '0', 'data': [{'sCode': '0', 'ordId': '12', 'clOrdId': 'other'}]}))
        body = {'instId': 'BTC-USDT', 'tdMode': 'cash', 'clOrdId': 'g3A1', 'side': 'buy', 'ordType': 'ioc', 'sz': '0.0001', 'px': '90000'}
        with self.assertRaises(UncertainRequest): c.write('/api/v5/trade/order', body)

    def test_pagination_complete_and_no_progress_fails(self):
        c = self.client(lambda *a, **kw: None)
        page = [{'ordId': str(n)} for n in range(300, 200, -1)]
        c.read = unittest.mock.Mock(side_effect=[page, [{'ordId': '200'}]])
        self.assertEqual(len(c.pending()), 101)
        self.assertEqual(c.read.call_args_list[1].args[1]['after'], '201')
        c.read = unittest.mock.Mock(side_effect=[page, page])
        with self.assertRaises(ProtocolError): c.pending()
        c.read = unittest.mock.Mock(return_value=page)
        with self.assertRaises(ProtocolError): c._paginate('/api/v5/trade/orders-pending', {}, 'ordId', max_pages=1)

    def test_archived_and_recent_fills_dedup(self):
        c = self.client(lambda *a, **kw: None)
        c.now = lambda: NOW
        r = {'billId':'12', 'instType':'SWAP','instId':'BTC-USD-SWAP','ts':str(NOW-1000),'fillTime':str(NOW-1100),
             'side':'sell','ordId':'1','clOrdId':'g3A1','tradeId':'2','feeCcy':'BTC',
             'fillPx':'90000','fillSz':'0.1','fee':'-0.00000005','fillPnl':'0'}
        c.read = unittest.mock.Mock(side_effect=[[r], [r]])
        self.assertEqual(c.fills('SWAP', 'BTC-USD-SWAP', begin=NOW-4*86400000), [r])
        self.assertIn('fills-history', c.read.call_args_list[0].args[0])
        c.read = unittest.mock.Mock(side_effect=[[r], [dict(r, fee='-0.01')]])
        with self.assertRaises(ProtocolError): c.fills('SWAP', begin=NOW-4*86400000)


SPOT_SPEC = {'instType':'SPOT','instId':'BTC-USDT','state':'live','baseCcy':'BTC','quoteCcy':'USDT',
             'lotSz':'0.00000001','minSz':'0.00001','tickSz':'0.1','maxLmtSz':'100','groupId':'12'}
SWAP_SPEC = {'instType':'SWAP','instId':'BTC-USD-SWAP','state':'live','ctType':'inverse','ctValCcy':'USD',
             'settleCcy':'BTC','instFamily':'BTC-USD','ctVal':'100','ctMult':'1','lotSz':'0.1','minSz':'0.1',
             'tickSz':'0.1','maxLmtSz':'1000','groupId':'1'}


class FixtureAPI:
    def __init__(self):
        self.overrides = {}
        self.pending_rows = []
        self.config = {'uid':'123','acctLv':'2','posMode':'net_mode','autoLoan':False,'perm':'read_only,trade'}
        self.details = [{'ccy':'USDT','cashBal':'10','eq':'10','availBal':'10','availEq':'10','liab':''},
                        {'ccy':'BTC','cashBal':'0.001','eq':'0.0009','availBal':'0.0008','availEq':'0.0003','liab':'','imr':'0.0005','mmr':'0.00001'}]
    def now(self): return NOW
    def pending(self): return self.pending_rows
    def pending_algos(self): return []
    def read(self, path, params=None, private=None):
        if path in self.overrides: return copy.deepcopy(self.overrides[path])
        params = params or {}
        if path.endswith('/instruments'): return [copy.deepcopy(SPOT_SPEC if params['instType']=='SPOT' else SWAP_SPEC)]
        if path.endswith('/position-tiers'): return [{'tier':'1','maxLever':'100','mmr':'0.004','maxSz':'5000'}]
        if path.endswith('/funding-rate'): return [{'instId':'BTC-USD-SWAP','fundingRate':'0.0001','fundingTime':str(NOW+3600000),'nextFundingTime':str(NOW+9*3600000),'ts':str(NOW)}]
        if path.endswith('/mark-price'): return [{'instId':'BTC-USD-SWAP','markPx':'90000','ts':str(NOW)}]
        if path.endswith('/ticker'): return [{'instId':params['instId'],'bidPx':'89999.9','askPx':'90000','ts':str(NOW)}]
        if path.endswith('/config'): return [copy.deepcopy(self.config)]
        if path.endswith('/leverage-info'): return [{'instId':'BTC-USD-SWAP','mgnMode':'cross','lever':'2'}]
        if path.endswith('/positions'): return []
        if path.endswith('/balance'): return [{'details':copy.deepcopy(self.details)}]
        if path.endswith('/max-avail-size'): return [{'instId':'BTC-USDT','availSell':'0.0002','availBuy':'8'}]
        if path.endswith('/trade-fee'):
            return [{'instType':params['instType'],'taker':'-0.9','feeGroup':[{'groupId':'12' if params['instType']=='SPOT' else '1','taker':'-0.001' if params['instType']=='SPOT' else '-0.0005'}]}]
        raise AssertionError(path)


class MarketAccountTests(unittest.TestCase):
    cfg = {'leverage': 2, 'quote_age_seconds': 5}
    def test_inverse_minimum_decimal_and_quote_identity(self):
        api = FixtureAPI()
        market = market_snapshot(api, self.cfg)
        self.assertEqual(market['spec']['contract_usd'], '100')
        self.assertEqual(market['spec']['swap_min'], '0.1')
        self.assertEqual(market['spec']['tier_max_contracts'], '1000')
        api.overrides['/api/v5/market/ticker'] = [{'instId':'WRONG','bidPx':'1','askPx':'2','ts':str(NOW)}]
        with self.assertRaises(Attention): market_snapshot(api, self.cfg)

    def test_stale_mark_and_linear_contract_rejected(self):
        api = FixtureAPI()
        api.overrides['/api/v5/public/mark-price'] = [{'instId':'BTC-USD-SWAP','markPx':'90000','ts':str(NOW-6000)}]
        with self.assertRaises(Attention): market_snapshot(api, self.cfg)
        api = FixtureAPI()
        api.overrides['/api/v5/public/instruments'] = [dict(SWAP_SPEC, ctType='linear')]
        with self.assertRaises(Attention): market_snapshot(api, self.cfg)

    def test_cash_and_equity_separate_and_collateral_unsellable(self):
        api = FixtureAPI()
        account = account_snapshot(api, self.cfg)
        self.assertEqual(account['balances']['BTC']['cash'], '0.001')
        self.assertEqual(account['balances']['BTC']['eq'], '0.0009')
        self.assertEqual(account['balances']['BTC']['available'], '0.0002')
        self.assertEqual(account['balances']['USDT']['available'], '8')
        self.assertEqual(account['fees'], {'spot':'0.001','swap':'0.0005'})

    def test_fee_group_required_no_legacy_fallback(self):
        api = FixtureAPI()
        api.overrides['/api/v5/account/trade-fee'] = [{'instType':'SPOT','taker':'-0.001','feeGroup':[]}]
        with self.assertRaises(Attention): account_snapshot(api, self.cfg)

    def test_account_modes_liabilities_foreign_assets_rejected(self):
        for change in ({'autoLoan':True}, {'autoLoan':'false'}, {'acctLv':'3'}, {'perm':'read_only,trade,withdraw'}):
            api=FixtureAPI(); api.config.update(change)
            with self.assertRaises(Attention): account_snapshot(api, self.cfg)
        api=FixtureAPI(); api.details[0]['liab']='1'
        with self.assertRaises(Attention): account_snapshot(api, self.cfg)
        api=FixtureAPI(); api.details.append({'ccy':'ETH','cashBal':'1','eq':'1','availBal':'1'})
        with self.assertRaises(Attention): account_snapshot(api, self.cfg)

    def test_omitted_zero_currency_and_pending_audit_visible(self):
        api=FixtureAPI(); api.details=api.details[:1]; api.pending_rows=[{'ordId':'42','instId':'ETH-USDT'}]
        result=account_snapshot(api,self.cfg)
        self.assertEqual(result['balances']['BTC']['cash'],'0')
        self.assertEqual(result['pending'],api.pending_rows)

    def test_zero_positions_and_tiny_btc_do_not_break_snapshot(self):
        api=FixtureAPI()
        api.details[1].update(cashBal='0.00000001', eq='-0.00000001', availBal='0.00000001')
        api.overrides['/api/v5/account/positions']=[{'pos':'0','instId':'OLD-SWAP'}]
        result=account_snapshot(api,self.cfg)
        self.assertEqual(result['positions'], [])
        self.assertEqual(result['balances']['BTC']['available'], '0.00000001')
        self.assertEqual(result['balances']['BTC']['eq'], '-0.00000001')

    def test_funding_requires_realized_rate(self):
        api=FixtureAPI()
        row={'instId':'BTC-USD-SWAP','fundingTime':str(NOW-1000),'realizedRate':'0.0001','fundingRate':'0.0009'}
        api.overrides['/api/v5/public/funding-rate-history']=[row]
        self.assertEqual(funding_history(api,NOW-10000),[{'ts':NOW-1000,'rate':'0.0001'}])
        row['realizedRate']=''
        with self.assertRaises(Attention): funding_history(api,NOW-10000)


class ExchangeTransport:
    """Tiny exchange fixture at the HTTP boundary, not an OKXClient substitute."""
    def __init__(self):
        self.clock=NOW
        self.posts=[]
        self.orders={}
        self.trades=[]
        self.account={'uid':'123','balances':{
            'USDT':{'cash':'100','eq':'100','available':'100'},
            'BTC':{'cash':'0','eq':'0','available':'0'}},
            'position':None,'positions':[],'pending':[],'algo_pending':[],
            'fees':{'spot':'0.001','swap':'0.0005'}}

    def __call__(self, req, timeout):
        path=urlsplit(req.full_url).path
        query={k:v[0] for k,v in parse_qs(urlsplit(req.full_url).query).items()}
        if req.method=='POST':
            body=json.loads(req.data)
            self.posts.append((path,body))
            if path.endswith('/cancel-order'):
                row=self.orders[body['clOrdId']]
                row['state']='canceled'
            else:
                row=dict(body,ordId=str(len(self.orders)+1),state='live',accFillSz='0')
                self.orders[body['clOrdId']]=row
            data=[{'sCode':'0','ordId':row['ordId'],'clOrdId':row['clOrdId']}]
        elif path.endswith('/order'):
            data=[self.orders[query['clOrdId']]]
        elif path.endswith('/fills') or path.endswith('/fills-history'):
            data=[r for r in reversed(self.trades) if r['instType']==query['instType'] and r['instId']==query['instId']]
        elif path.endswith('/bills') or path.endswith('/bills-archive'):
            data=[]
        else:
            raise AssertionError('Unexpected HTTP path '+path)
        return Response({'code':'0','data':data})

    def settle(self):
        row=list(self.orders.values())[-1]
        row.update(state='filled',accFillSz=row['sz'])
        quantity=Decimal(row['sz'])
        is_spot=row['instId']=='BTC-USDT'
        direction=1 if row['side']=='buy' else -1
        btc=Decimal(self.account['balances']['BTC']['cash'])
        usdt=Decimal(self.account['balances']['USDT']['cash'])
        if is_spot:
            btc+=direction*quantity
            usdt-=direction*quantity*10000
        else:
            old=Decimal((self.account['position'] or {}).get('contracts','0'))
            pos=old+direction*quantity
            self.account['position']={'contracts':format(pos,'f'),'avgPx':'10000','upl':'0','margin':'0'} if pos else None
        for ccy,value in [('BTC',btc),('USDT',usdt)]:
            self.account['balances'][ccy]={k:format(value,'f') for k in ('cash','eq','available')}
        n=str(len(self.trades)+100)
        self.trades.append({'billId':n,'tradeId':n,'ordId':row['ordId'],'clOrdId':row['clOrdId'],
            'instType':'SPOT' if is_spot else 'SWAP','instId':row['instId'],
            'side':row['side'],'fillSz':row['sz'],'fillPx':'10000','fee':'0',
            'feeCcy':'USDT' if is_spot and row['side']=='sell' else 'BTC','fillPnl':'0',
            'ts':str(self.clock),'fillTime':str(self.clock)})


class ExecutorHTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        from g3core.execution import Executor
        from g3core.store import Store
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.socket=patch('socket.socket',side_effect=AssertionError('network forbidden'))
        self.socket.start(); self.addCleanup(self.socket.stop)
        self.sleep=patch('g3core.okx.time.sleep')
        self.sleep.start(); self.addCleanup(self.sleep.stop)
        self.exchange=ExchangeTransport()
        self.api=OKXClient(proxy='http://127.0.0.1:17890',credentials=CREDS,allow_orders=True,transport=self.exchange)
        self.api.now=lambda:self.exchange.clock
        self.api._last_sync_monotonic=__import__('time').monotonic()
        self.store=Store(self.temp.name); self.addCleanup(self.store.close)
        self.cfg={'capital_usdt':100,'max_loss_usdt':20,'quote_age_seconds':5,'max_slippage_bps':10,
                  'max_unhedged_seconds':60,'order_ttl_seconds':15}
        self.market={'now':NOW,'mark':'10000','spot':{'bid':'10000','ask':'10000','ts':NOW},
            'swap':{'bid':'10000','ask':'10000','ts':NOW},
            'spec':{'spot_lot':'0.00001','spot_min':'0.0001','spot_tick':'0.1','swap_lot':'1',
                    'swap_min':'1','swap_tick':'0.1','contract_usd':'10','tier_max_contracts':'10000'}}
        self.executor=Executor(self.api,self.store,self.cfg)
        self.executor.initialize({'uid':'123'},self.exchange.account)
        self.plan={'allowed':True,'gross_btc':'0.0091','contracts':'9','estimated_spot_cost':'91.1'}

    def tick(self):
        self.market['now']=self.exchange.clock
        for key in ('spot','swap'): self.market[key]['ts']=self.exchange.clock
        return self.executor.tick(self.market,copy.deepcopy(self.exchange.account))

    def start(self):
        self.executor.begin_entry(self.plan,self.market,self.exchange.account)
        self.tick()
        self.assertEqual(self.store.unresolved()[0]['status'],'acknowledged')

    def test_executor_spot_swap_reduce_and_spot_exit_cross_real_adapter(self):
        self.start()
        self.exchange.settle(); self.tick(); self.tick()
        self.assertEqual(self.exchange.posts[-1][1]['instId'],'BTC-USD-SWAP')
        self.assertFalse(self.exchange.posts[-1][1]['reduceOnly'])
        self.assertEqual(self.store.unresolved()[0]['status'],'acknowledged')
        self.exchange.settle(); self.tick(); self.tick()
        self.assertEqual(self.store.state()['phase'],'active')
        self.executor.begin_exit('offline_integration'); self.tick()
        self.assertTrue(self.exchange.posts[-1][1]['reduceOnly'])
        self.assertEqual(self.exchange.posts[-1][1]['side'],'buy')
        self.assertEqual(self.store.unresolved()[0]['status'],'acknowledged')
        self.exchange.settle(); self.tick(); self.tick()
        self.assertEqual(self.exchange.posts[-1][1]['instId'],'BTC-USDT')
        self.assertEqual(self.exchange.posts[-1][1]['side'],'sell')
        self.assertEqual(self.store.unresolved()[0]['status'],'acknowledged')
        self.assertEqual(len(self.exchange.posts),4)
        for path,body in self.exchange.posts:
            self.assertEqual(body['ordType'],'ioc')
            self.assertNotIn('tgtCcy',body)
            self.assertNotIn('ccy',body)
        persisted=json.dumps(self.store.intents())
        self.assertNotIn(CREDS['secret_key'],persisted)
        self.assertNotIn(CREDS['api_key'],persisted)

    def test_executor_timeout_cancel_crosses_real_adapter_once(self):
        self.start()
        self.exchange.clock+=16000
        self.tick()
        self.assertEqual(self.exchange.posts[-1][0],'/api/v5/trade/cancel-order')
        self.assertEqual(len(self.exchange.posts),2)
        self.tick(); self.tick()
        self.assertEqual(len(self.exchange.posts),2)
        self.assertEqual(self.store.state()['phase'],'idle')


if __name__ == '__main__': unittest.main()
