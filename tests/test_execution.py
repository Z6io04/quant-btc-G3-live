"""Offline execution fault tests; no credentials, network or real orders."""
import copy
import json
from decimal import Decimal as D
import tempfile
import unittest
import time

from g3core.execution import Executor, ExecutionError, SPOT, SWAP
from g3core.store import Store, AccountLock
from g3core.okx import OKXClient, APIError, SafetyError


class Fake:
    def __init__(self):
        self.clock = 1790000000000
        self.posts, self.orders, self.trades, self.bill_rows = [], {}, [], []
        self.fail_after_accept = False
        self.account = {'uid':'123','balances':{
            'USDT':{'cash':'100','available':'100','eq':'100'},
            'BTC':{'cash':'0','available':'0','eq':'0'}},
            'position':None,'positions':[],'pending':[],'algo_pending':[],
            'fees':{'spot':'0.001','swap':'0.0005'}}

    def now(self):
        return self.clock

    def write(self,path,body,exp_time=None):
        self.posts.append((path,copy.deepcopy(body)))
        if path.endswith('cancel-order'):
            self.orders[body['clOrdId']]['state'] = 'canceled'
            return []
        oid = str(len(self.orders)+1)
        self.orders[body['clOrdId']] = dict(body,ordId=oid,state='live',accFillSz='0')
        if self.fail_after_accept:
            raise TimeoutError('Do not persist this text')
        return [{'ordId':oid,'clOrdId':body['clOrdId'],'sCode':'0'}]

    def get_order(self,inst,client_id):
        return self.orders.get(client_id)

    def fills(self,*args,**kwargs):
        return copy.deepcopy(self.trades)

    def bills(self,**kwargs):
        return copy.deepcopy(self.bill_rows)

    def settle(self,size=None,fee='0',ccy=None,pnl='0'):
        row = list(self.orders.values())[-1]
        size = row['sz'] if size is None else size
        row.update(state='filled' if D(size)==D(row['sz']) else 'canceled',accFillSz=size)
        if D(size)==0:
            return
        ccy = ccy or ('BTC' if row['instId']==SWAP or row['side']=='buy' else 'USDT')
        fill = {'tradeId':str(len(self.trades)+1),'ordId':row['ordId'],'instId':row['instId'],
                'side':row['side'],'fillSz':size,'fillPx':'10000','fee':fee,'feeCcy':ccy,
                'fillPnl':pnl,'ts':str(self.clock)}
        self.trades.append(fill)
        balances = self.account['balances']
        btc,usdt = D(balances['BTC']['cash']),D(balances['USDT']['cash'])
        if row['instId']==SPOT:
            direction = 1 if row['side']=='buy' else -1
            btc += direction*D(size)
            usdt -= direction*D(size)*10000
        else:
            old = D((self.account['position'] or {}).get('contracts','0'))
            qty = old + (-D(size) if row['side']=='sell' else D(size))
            self.account['position'] = {'contracts':str(qty),'avgPx':'10000','upl':'0','margin':'0'} if qty else None
            btc += D(pnl)
        if ccy=='BTC':
            btc += D(fee)
        else:
            usdt += D(fee)
        for key,value in (('BTC',btc),('USDT',usdt)):
            balances[key].update(cash=str(value),available=str(value),eq=str(value))
        self.bill_rows.append({'billId':str(len(self.bill_rows)+1),'type':'2','subType':'1',
                              'ccy':ccy,'balChg':'0','ordId':row['ordId'],'ts':str(self.clock)})


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = Fake()
        self.store = Store(self.tmp.name)
        self.cfg = {'capital_usdt':100,'max_loss_usdt':20,'max_slippage_bps':10,'quote_age_seconds':5,'max_unhedged_seconds':60,'order_ttl_seconds':15}
        self.market = {'now':self.api.now(),'mark':'10000',
            'spot':{'bid':'10000','ask':'10000','ts':self.api.now()},
            'swap':{'bid':'10000','ask':'10000','ts':self.api.now()},
            'spec':{'spot_lot':'0.00001','spot_min':'0.0001','spot_tick':'0.1',
                    'swap_lot':'1','swap_min':'1','swap_tick':'0.1','contract_usd':'10','tier_max_contracts':'10000'}}
        self.exe = Executor(self.api,self.store,self.cfg)
        self.exe.initialize({'uid':'123','mode':'live','capital':100},self.api.account)
        self.plan = {'allowed':True,'gross_btc':'0.0091','contracts':'9','estimated_spot_cost':'91.1'}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tick(self):
        return self.exe.tick(self.market,copy.deepcopy(self.api.account))

    def entry(self):
        self.exe.begin_entry(self.plan,self.market,self.api.account)
        self.tick()

    def active(self):
        self.entry()
        self.api.settle(fee='-0.0000091')
        self.tick()
        self.tick()
        self.api.settle(fee='-0.0000045')
        self.tick()
        self.tick()
        self.assertEqual(self.store.state()['phase'],'active')

    def test_ack_is_not_fill(self):
        self.entry()
        self.assertEqual(self.store.state()['btc_balance'],'0')
        self.assertEqual(len(self.store.unresolved()),1)
        self.tick()
        self.assertEqual(len(self.api.posts),1)

    def test_uncertain_post_restarts_lookup_same_id_without_repeat(self):
        self.api.fail_after_accept = True
        self.entry()
        client_id = self.store.unresolved()[0]['id']
        self.assertLessEqual(len(client_id),32)
        self.assertEqual(self.store.unresolved()[0]['status'],'unknown')
        self.store.close()
        self.store = Store(self.tmp.name)
        self.exe = Executor(self.api,self.store,self.cfg)
        self.tick()
        self.assertEqual(len(self.api.posts),1)
        self.api.settle(fee='-0.0000091')
        self.tick()
        self.assertEqual(self.store.state()['phase'],'enter_swap')
        self.assertEqual(len([p for p in self.api.posts if p[1]['instId']==SPOT]),1)
        self.assertEqual(len(self.api.posts),2)

    def test_not_found_order_blocks_new_orders(self):
        self.entry()
        self.api.orders.clear()
        for _ in range(4):
            self.tick()
        self.assertEqual(len(self.api.posts),1)
        self.assertEqual(self.store.unresolved()[0]['status'],'unknown')

    def test_partial_spot_sizes_hedge_using_actual_net_receipts(self):
        self.entry()
        self.api.settle(size='0.005',fee='-0.000005')
        self.tick()
        self.tick()
        order = self.api.posts[-1][1]
        self.assertEqual(order['instId'],SWAP)
        self.assertEqual(order['sz'],'5')
        self.assertEqual(order['side'],'sell')
        self.assertFalse(order['reduceOnly'])

    def test_minimum_hedge_failure_unwinds_spot(self):
        self.entry()
        self.api.settle(size='0.0005',fee='-0.0000005')
        self.tick()
        self.tick()
        self.assertEqual(self.store.state()['op']['type'],'exit')
        self.tick()
        self.tick()
        self.assertEqual(self.api.posts[-1][1]['instId'],SPOT)
        self.assertEqual(self.api.posts[-1][1]['side'],'sell')

    def test_two_legs_real_fees_and_cash_reconcile(self):
        self.active()
        state = self.store.state()
        self.assertEqual(D(state['btc_balance']),D('0.0090864'))
        self.assertEqual(D(state['cash_usdt']),9)
        self.assertEqual(D(state['fees_by_ccy']['BTC']),D('-0.0000136'))
        self.assertEqual(state['contracts'],'9')
        self.assertEqual(len(self.store.records('fills')),2)
        self.tick()
        self.assertEqual(len(self.store.records('fills')),2)

    def test_missing_fill_details_prevent_synthetic_accounting(self):
        self.entry()
        self.api.settle()
        self.api.trades.clear()
        self.tick()
        self.assertEqual(self.store.state()['btc_balance'],'0')
        self.assertEqual(len(self.store.unresolved()),1)

    def test_partial_exit_reduces_remaining_contracts_only(self):
        self.active()
        self.exe.begin_exit('test')
        self.tick()
        body = self.api.posts[-1][1]
        self.assertTrue(body['reduceOnly'])
        self.assertEqual(body['sz'],'9')
        self.api.settle(size='4')
        self.tick()
        self.assertEqual(self.api.posts[-1][1]['sz'],'5')
        self.assertEqual(self.api.posts[-1][1]['instId'],SWAP)

    def test_funding_bills_are_deduplicated_actual_signed_btc(self):
        self.active()
        self.api.bill_rows.append({'billId':'fund1','type':'8','subType':'173','ccy':'BTC',
                                  'instId':SWAP,'balChg':'0.000001','ts':str(self.api.now())})
        for key in ('cash','available','eq'):
            self.api.account['balances']['BTC'][key] = str(D(self.api.account['balances']['BTC'][key])+D('0.000001'))
        self.tick()
        self.tick()
        self.assertEqual(self.store.state()['funding_btc'],'0.000001')
        self.assertEqual(self.store.state()['position']['funding_btc'],'0.000001')

    def test_unknown_deposit_bill_halts_without_adopting_balance(self):
        self.api.bill_rows = [{'billId':'external','type':'1','subType':'11','ccy':'USDT',
                              'balChg':'100','ts':str(self.api.now())}]
        state = self.tick()
        self.assertTrue(state['halted'])
        self.assertEqual(state['cash_usdt'],'100')
        self.assertFalse(self.api.posts)

    def test_pause_resume_durable_and_halt_not_cleared(self):
        self.store.control('pause')
        self.tick()
        self.assertTrue(self.store.state()['paused'])
        self.store.control('resume')
        self.tick()
        self.assertFalse(self.store.state()['paused'])
        self.exe._halt('manual inspection')
        self.store.control('resume')
        self.tick()
        self.assertTrue(self.store.state()['paused'])
        self.assertEqual(self.store.state()['halted'],'manual inspection')

    def test_flatten_control_cannot_open_and_closes_swap_first(self):
        self.active()
        self.store.control('flatten')
        self.tick()
        self.assertEqual(self.api.posts[-1][1]['instId'],SWAP)
        self.assertEqual(self.api.posts[-1][1]['side'],'buy')
        self.assertTrue(self.store.state()['paused'])

    def test_stale_quote_no_order(self):
        self.api.clock += 6000
        with self.assertRaises(ExecutionError):
            self.exe.begin_entry(self.plan,self.market,self.api.account)
        self.assertFalse(self.api.posts)

    def test_order_intent_and_state_saved_before_post(self):
        original = self.api.write
        def checking(path,body,exp_time=None):
            self.assertEqual(self.store.intent(body['clOrdId'])['status'],'sent')
            self.assertEqual(self.store.state()['op']['awaiting'],body['clOrdId'])
            return original(path,body,exp_time)
        self.api.write = checking
        self.entry()

    def test_ledger_identity_cannot_be_reset(self):
        with self.assertRaises(ValueError):
            self.exe.initialize({'uid':'other'},self.api.account)

    def test_definitely_rejected_hedge_unwinds_known_spot(self):
        self.entry()
        self.api.settle(fee='-0.0000091')
        original = self.api.write
        def rejected(path,body,exp_time=None):
            if body.get('instId') == SWAP:
                raise APIError('51008',path)
            return original(path,body,exp_time)
        self.api.write = rejected
        self.tick()
        self.assertFalse(self.store.unresolved())
        self.tick()
        self.assertEqual(self.store.state()['op']['type'],'exit')
        self.tick()
        self.tick()
        self.assertEqual(self.api.posts[-1][1]['side'],'sell')
        self.assertEqual(self.api.posts[-1][1]['instId'],SPOT)

    def test_local_pretransport_rejection_is_not_uncertain(self):
        def rejected(path,body,exp_time=None):
            raise SafetyError('ORDERS_DISABLED',path)
        self.api.write = rejected
        self.entry()
        self.assertFalse(self.store.unresolved())
        self.tick()
        self.assertEqual(self.store.state()['phase'],'idle')

    def test_generated_two_leg_bodies_pass_real_adapter_without_network(self):
        self.active()
        captured = []
        class Response:
            status = 200
            def __init__(self,value): self.value = json.dumps(value).encode()
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): return self.value[:size]
        def transport(request,timeout):
            body = json.loads(request.data)
            captured.append(body)
            return Response({'code':'0','data':[{'ordId':'123','clOrdId':body['clOrdId'],'sCode':'0'}]})
        client = OKXClient(proxy='http://127.0.0.1:17890', credentials={'api_key':'offline','secret_key':'offline','passphrase':'offline'},allow_orders=True,transport=transport)
        client._server_anchor_ms = self.api.now()
        client._last_sync_monotonic = time.monotonic()
        for path,body in self.api.posts:
            client.write(path,body,exp_time=client.now()+5000)
        self.assertEqual(len(captured),2)
        self.assertEqual({r['instId'] for r in captured},{SPOT,SWAP})

    def test_treasury_uses_entry_neutral_not_mark_and_never_sells_principal(self):
        self.active()
        state = self.store.state()
        state['position']['funding_btc'] = '0.001'
        self.store.set_state(state)
        self.market['mark'] = '20000'
        self.exe.begin_treasury('0.001')
        self.tick()
        # Actual BTC exceeds entry-price neutrality by only 0.0000864,
        # below the 0.0001 minimum; mark doubling cannot release principal.
        self.assertEqual(len(self.api.posts),2)
        self.assertEqual(self.store.state()['phase'],'active')

    def test_prepared_intent_from_crash_is_expired_without_post(self):
        self.exe.begin_entry(self.plan,self.market,self.api.account)
        state = self.store.state()
        request = {'instId':SPOT,'tdMode':'cash','side':'buy','ordType':'ioc','sz':'0.0091','px':'10000'}
        item = self.store.create_intent('crash',request,{},self.api.now())
        state['op']['awaiting'] = item['id']
        self.store.set_state(state)
        self.tick()
        self.assertFalse(self.api.posts)
        self.assertEqual(self.store.intent(item['id'])['status'],'expired')
        self.assertEqual(self.store.state()['phase'],'idle')

    def test_price_move_after_plan_cannot_exceed_budget(self):
        self.exe.begin_entry(self.plan,self.market,self.api.account)
        self.market['spot'].update(bid='12000',ask='12000')
        self.tick()
        self.assertFalse(self.api.posts)
        self.assertEqual(self.store.state()['phase'],'idle')

    def test_entry_funding_is_attributed_to_group(self):
        self.entry()
        self.api.settle(fee='-0.0000091')
        self.tick()
        self.tick()
        self.api.settle(fee='-0.0000045')
        self.api.bill_rows.append({'billId':'fund1','type':'8','subType':'173','ccy':'BTC',
                                  'instId':SWAP,'balChg':'0.000001','ts':str(self.api.now())})
        for key in ('cash','available','eq'):
            self.api.account['balances']['BTC'][key] = str(D(self.api.account['balances']['BTC'][key])+D('0.000001'))
        self.tick()
        self.tick()
        self.assertEqual(self.store.state()['position']['funding_btc'],'0.000001')

    def test_hedge_submission_immediately_follows_spot_reconciliation(self):
        self.entry()
        self.api.settle(fee='-0.0000091')
        self.tick()
        self.assertEqual(len(self.api.posts),2)
        self.assertEqual(self.api.posts[-1][1]['instId'],SWAP)

    def test_already_balanced_hedge_not_unwound_for_bookkeeping_delay(self):
        self.entry()
        self.api.settle(fee='-0.0000091')
        self.tick()
        self.api.settle(fee='-0.0000045')
        self.api.clock += 70000
        for kind in ('spot','swap'):
            self.market[kind]['ts'] = self.api.now()
        self.tick()
        self.assertEqual(self.store.state()['phase'],'active')
        self.assertEqual(len(self.api.posts),2)

    def test_late_funding_when_flat_keeps_actual_cash_ledger(self):
        self.api.bill_rows.append({'billId':'late','type':'8','subType':'173','ccy':'BTC',
                                  'instId':SWAP,'balChg':'0.000001','ts':str(self.api.now())})
        for key in ('cash','available','eq'):
            self.api.account['balances']['BTC'][key] = '0.000001'
        self.tick()
        self.assertEqual(self.store.state()['funding_btc'],'0.000001')

    def test_exit_sells_exactly_one_minimum_spot_lot(self):
        state = self.store.state()
        state['btc_balance'] = '0.0001'
        self.store.set_state(state)
        for key in ('cash','available','eq'):
            self.api.account['balances']['BTC'][key] = '0.0001'
        self.exe.begin_exit('dust_exit')
        self.tick()
        self.assertEqual(self.api.posts[-1][1]['sz'],'0.0001')
        self.assertEqual(self.api.posts[-1][1]['side'],'sell')

    def test_lock_excludes_duplicate_process(self):
        with AccountLock(self.tmp.name):
            with self.assertRaises(RuntimeError):
                with AccountLock(self.tmp.name):
                    pass

    def test_stop_before_send_preserves_operation_without_creating_intent(self):
        self.exe.begin_entry(self.plan,self.market,self.api.account)
        original_operation = copy.deepcopy(self.store.state()['op'])
        self.exe.stopping = lambda: True
        with self.assertRaises(ExecutionError):
            self.tick()
        self.assertFalse(self.api.posts)
        self.assertFalse(self.store.intents())
        self.assertEqual(self.store.state()['op'],original_operation)

    def test_stop_after_durable_sent_marker_expires_only_unsent_intent(self):
        calls = []
        def stopping():
            calls.append(True)
            if len(calls)==2:
                self.assertEqual(self.store.unresolved()[0]['status'],'sent')
                return True
            return False
        self.exe.stopping = stopping
        self.entry()
        self.assertFalse(self.api.posts)
        self.assertFalse(self.store.unresolved())
        item = self.store.intents()[0]
        self.assertEqual(item['status'],'expired')
        self.assertEqual(item['result']['code'],'STOP_REQUESTED')
        self.assertEqual(self.store.state()['op']['type'],'entry')
        self.assertIsNone(self.store.state()['op']['awaiting'])
        self.assertEqual(self.store.state()['op']['last_order']['filled'],'0')

    def test_stop_cannot_erase_an_old_unresolved_order(self):
        self.entry()
        original = copy.deepcopy(self.store.unresolved()[0])
        operation = copy.deepcopy(self.store.state()['op'])
        self.exe.stopping = lambda: True
        with self.assertRaises(ExecutionError):
            self.exe._send(SWAP,'sell','9',self.market)
        self.assertEqual(len(self.api.posts),1)
        self.assertEqual(self.store.unresolved(),[original])
        self.assertEqual(self.store.state()['op'],operation)

    def test_read_status_does_not_create_missing_ledger(self):
        self.assertEqual(Store.read_status(self.tmp.name+'/missing'),{})


if __name__ == '__main__':
    unittest.main()
