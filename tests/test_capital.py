"""Offline capital-deposit reconciliation tests: no network, no real account/order."""
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from g3core import capital, config, runtime
from g3core.execution import Executor
from g3core.store import AccountLock, Store

NOW = 1791340000000
CREDS = {'api_key':'offline-key','secret_key':'offline-secret','passphrase':'offline-pass'}
SPEC = {'contract_usd':'100','swap_min':'0.1','swap_lot':'0.1','spot_min':'0.00001'}
STOPPED = 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\n'


def account(cash='30', uid='123'):
    return {'uid':uid, 'position':None, 'positions':[], 'pending':[], 'algo_pending':[],
            'balances':{'USDT':{'cash':cash,'eq':cash,'available':cash,'liab':'0'},
                        'BTC':{'cash':'0','eq':'0','available':'0','liab':'0'}}}


def incoming(amount='5', bill_id='1', ts=NOW-1000, **changes):
    row = {'billId':bill_id,'type':'1','subType':'11','ccy':'USDT','balChg':amount,
           'sz':amount,'fee':'','pnl':'','instId':'','ordId':'','ts':str(ts)}
    row.update(changes)
    return row


class ReadOnlyAPI:
    def __init__(self): self.clock=NOW; self.rows=[incoming()]; self.calls=[]
    def now(self): return self.clock
    def sync_time(self): self.calls.append('sync_time'); return self.clock
    def bills(self, **kwargs): self.calls.append(('bills',kwargs)); return deepcopy(self.rows)
    def write(self, *args, **kwargs): raise AssertionError('capital sync must never POST')


class CapitalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.root=self.home/'btc-G3-live'; self.root.mkdir()
        (self.root/'g3core').mkdir()
        (self.root/'launch.py').write_text('# fixed offline fixture\n')
        (self.root/'g3core/__init__.py').write_text('# fixed offline fixture\n')
        self.patch('g3core.config.ROOT',self.root)
        self.patch('pathlib.Path.home',return_value=self.home)
        self.patch('g3core.capital.sys.platform','linux')
        self.patch('g3core.capital.os.geteuid',return_value=1000)
        self.patch('socket.socket',side_effect=AssertionError('offline test cannot use network'))
        self.service=self.patch('g3core.capital.subprocess.run',return_value=subprocess.CompletedProcess([],0,STOPPED,''))
        self.cfg=dict(config.DEFAULT,capital_usdt=25,max_loss_usdt=5)
        config.atomic_json(self.root/'config.json',self.cfg)
        config.atomic_json(self.root/'private/credentials.json',CREDS)
        self.api=ReadOnlyAPI()
        self.factory=self.patch('g3core.runtime.client',return_value=self.api)
        self.actual=account()
        self.snapshot=self.patch('g3core.capital.account_snapshot',side_effect=lambda *args:deepcopy(self.actual))
        self.market=self.patch('g3core.capital.market_snapshot',return_value={'spec':deepcopy(SPEC)})
        self.store=Store(config.data_dir()); self.addCleanup(self.store.close)
        binding=config.bind_account('123',config.data_dir())
        self.identity=runtime.identity(self.cfg,CREDS,'123',SPEC)
        self.identity['binding_id']=binding['binding_id']
        Executor(self.api,self.store,self.cfg).initialize(self.identity,account('25'),NOW-60000)
        self.store.event('armed',{'orders_sent':0},NOW-60000)

    def patch(self,*args,**kwargs):
        p=patch(*args,**kwargs); value=p.start(); self.addCleanup(p.stop); return value

    def state(self,**changes):
        value=self.store.state(); value.update(changes); self.store.set_state(value); return value

    def sync(self,confirm=lambda prompt:'SYNC G3'):
        return capital.sync_capital(self.cfg,confirm=confirm)

    def unchanged(self,fn):
        before=deepcopy(self.store.state())
        bills=deepcopy(self.store.records('bills'))
        with self.assertRaises((config.Attention,RuntimeError,ValueError)):
            fn()
        self.assertEqual(before,self.store.state())
        self.assertEqual(bills,self.store.records('bills'))

    def test_deposit_updates_capital_and_matching_bills_without_order_or_file_change(self):
        before_cfg=(self.root/'config.json').read_bytes()
        before_creds=(self.root/'private/credentials.json').read_bytes()
        result=self.sync()
        self.assertEqual('5',result['added_usdt'])
        self.assertEqual('30',result['capital_usdt'])
        self.assertEqual(0,result['orders_sent'])
        state=self.store.state()
        for key in ('capital_usdt','cash_usdt','baseline_usdt','peak_equity','last_equity'):
            self.assertEqual('30',state[key])
        self.assertEqual('5',state['capital_added_usdt'])
        self.assertEqual(NOW,state['bill_cursor'])
        self.assertEqual(before_cfg,(self.root/'config.json').read_bytes())
        self.assertEqual(before_creds,(self.root/'private/credentials.json').read_bytes())
        self.assertEqual(self.identity,self.store.identity())
        self.assertEqual([incoming()],self.store.records('bills'))
        self.factory.assert_called_once_with(self.cfg,CREDS,orders=False)

    def test_repeated_sync_is_noop_and_does_not_prompt_or_credit_twice(self):
        self.sync()
        before=self.store.state()
        result=self.sync(lambda prompt:self.fail('no-op should not prompt'))
        self.assertFalse(result['changed'])
        self.assertEqual('0',result['added_usdt'])
        self.assertEqual(before,self.store.state())
        self.assertEqual(1,len(self.store.records('bills')))

    def test_multiple_deposits_and_identical_page_overlap_count_once(self):
        self.api.rows=[incoming('2','1'),incoming('3','2'),incoming('2','1')]
        self.sync()
        self.assertEqual('30',self.store.state()['capital_usdt'])
        self.assertEqual(2,len(self.store.records('bills')))

    def test_profit_loss_pause_cooldown_and_historical_risk_remain(self):
        self.state(cash_usdt='24',last_equity='24',max_drawdown='0.04',paused=True,
                   closed_trades=2,cooldown_until=NOW+capital.DAY,realized_pnl_btc='-0.00001',
                   fees_by_ccy={'BTC':'-0.000002'},funding_btc='0.000001')
        self.actual=account('29')
        before=self.store.state()
        self.sync()
        after=self.store.state()
        self.assertEqual('29',after['last_equity'])
        self.assertEqual('30',after['peak_equity'])
        for key in ('paused','halted','max_drawdown','closed_trades','cooldown_until',
                    'realized_pnl_btc','fees_by_ccy','funding_btc'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(5,self.cfg['max_loss_usdt'])

    def test_exact_confirmation_required(self):
        for answer in ('yes','sync g3','','SYNC G3 NOW'):
            with self.subTest(answer=answer): self.unchanged(lambda:self.sync(lambda _:answer))

    def test_api_account_uid_must_match_original_binding(self):
        self.actual['uid']='456'
        self.unchanged(self.sync)

    def test_api_credentials_change_refused_before_private_reads(self):
        config.atomic_json(self.root/'private/credentials.json',dict(CREDS,api_key='new-offline-key'),overwrite=True)
        self.unchanged(self.sync)
        self.snapshot.assert_not_called()

    def test_changed_source_and_shared_binding_refused(self):
        (self.root/'launch.py').write_text('# changed fixture\n')
        self.unchanged(self.sync)
        self.snapshot.assert_not_called()

    def test_changed_shared_binding_refused(self):
        path=self.home/'.local/state/btc-v53-live/accounts'/(config.account_key('123')+'.json')
        config.atomic_json(path,{'strategy':'v53'},overwrite=True)
        self.unchanged(self.sync)
        self.snapshot.assert_not_called()

    def test_spec_change_refused(self):
        self.market.return_value={'spec':dict(SPEC,swap_min='1')}
        self.unchanged(self.sync)

    def test_stopped_state_requires_known_dead_pid_zero(self):
        variants=[('ActiveState=active\nSubState=running\nMainPID=55\nLoadState=loaded\n',0),
                  ('LoadState=loaded\nActiveState=activating\nSubState=start\nMainPID=0\n',0),
                  ('LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=1\n',0),
                  ('',1),('LoadState=error\nActiveState=inactive\nSubState=dead\nMainPID=0\n',0)]
        for output,rc in variants:
            self.service.return_value=subprocess.CompletedProcess([],rc,output,'')
            with self.subTest(output=output): self.unchanged(self.sync)
        self.factory.assert_not_called()

    def test_missing_service_is_allowed_if_manager_confirms_inactive(self):
        self.service.return_value=subprocess.CompletedProcess([],4,STOPPED.replace('loaded','not-found'),'')
        self.assertTrue(self.sync()['changed'])

    def test_local_and_shared_uid_locks_prevent_concurrent_use(self):
        with AccountLock(config.data_dir()): self.unchanged(self.sync)
        with runtime.account_guard('123'): self.unchanged(self.sync)
        self.factory.assert_not_called()

    def test_linux_nonroot_gate_precedes_service_and_credentials(self):
        with patch('g3core.capital.sys.platform','darwin'): self.unchanged(self.sync)
        with patch('g3core.capital.os.geteuid',return_value=0): self.unchanged(self.sync)
        self.service.assert_not_called()
        self.factory.assert_not_called()

    def test_missing_ledger_is_not_initialized(self):
        with patch('g3core.config.data_dir',return_value=self.root/'no-ledger'):
            with self.assertRaises(FileNotFoundError): self.sync()
        self.assertFalse((self.root/'no-ledger').exists())
        self.factory.assert_not_called()

    def test_empty_and_unknown_ledger_not_initialized_or_migrated(self):
        empty=self.root/'empty'; empty.mkdir(); (empty/'state.sqlite3').touch()
        with patch('g3core.config.data_dir',return_value=empty):
            with self.assertRaises(config.Attention): self.sync()
        self.assertEqual(0,(empty/'state.sqlite3').stat().st_size)
        self.store.db.execute('PRAGMA user_version=999')
        self.unchanged(self.sync)
        self.assertEqual(999,self.store.db.execute('PRAGMA user_version').fetchone()[0])
        self.factory.assert_not_called()

    def test_any_live_position_order_debt_btc_or_other_asset_refused(self):
        variants=[dict(position={'contracts':'-1'}),dict(positions=[{'pos':'1'}]),
                  dict(pending=[{'ordId':'123'}]),dict(algo_pending=[{'algoId':'123'}])]
        for change in variants:
            self.actual=account(); self.actual.update(change)
            with self.subTest(change=change): self.unchanged(self.sync)
        for ccy,key,value in (('BTC','cash','0.000001'),('BTC','eq','0.000001'),('USDT','liab','1')):
            self.actual=account(); self.actual['balances'][ccy][key]=value
            with self.subTest(ccy=ccy,key=key): self.unchanged(self.sync)
        self.actual=account(); self.actual['balances']['ETH']={'cash':'1','eq':'1','available':'1'}
        self.unchanged(self.sync)

    def test_unresolved_or_pending_controls_refused(self):
        self.store.control('pause'); self.unchanged(self.sync)
        self.store.db.execute('DELETE FROM controls')
        self.store.create_intent('pending',{}, {},NOW); self.unchanged(self.sync)

    def test_local_halt_position_operation_or_dust_refused(self):
        original=self.store.state()
        variants=[{'halted':'already stopped'},{'position':{'x':1}},{'op':{'x':1}},
                  {'contracts':'1'},{'btc_balance':'0.00000001'},{'dust_btc':'0.00000001'},
                  {'reconciliation_mismatch_since':NOW-1000},{'flatten_requested':True}]
        for change in variants:
            self.store.set_state(dict(original,**change))
            with self.subTest(change=change): self.unchanged(self.sync)

    def test_original_absolute_or_drawdown_stop_cannot_be_diluted_by_deposit(self):
        self.state(cash_usdt='23.75',last_equity='23.75')  # exactly 5% DD
        self.actual=account('28.75'); self.unchanged(self.sync)
        self.state(cash_usdt='20',last_equity='20',peak_equity='20') # loss 5U
        self.actual=account('25'); self.unchanged(self.sync)
        self.state(cash_usdt='25',last_equity='25',peak_equity='25',max_drawdown='0.05')
        self.actual=account('30'); self.unchanged(self.sync)

    def test_mismatched_old_equity_and_unverified_extra_capital_refused(self):
        self.state(last_equity='24.9'); self.unchanged(self.sync)
        self.state(last_equity='25',capital_usdt='26'); self.unchanged(self.sync)

    def test_unknown_types_outflow_fee_pnl_currency_order_and_negative_bill_refused(self):
        bad=[{'type':'2'},{'subType':'12'},{'subType':'999'},{'fee':'-0.1'},
             {'pnl':'0.1'},{'ccy':'BTC'},{'ordId':'123'},{'instId':'BTC-USDT'},
             {'balChg':'-5'},{'balChg':'0'},{'balChg':'NaN'}]
        for change in bad:
            self.api.rows=[incoming(**change)]
            with self.subTest(change=change): self.unchanged(self.sync)

    def test_missing_or_stale_bill_window_refused(self):
        self.api.rows=[]; self.unchanged(self.sync)
        self.api.rows=[incoming()]
        self.api.clock=NOW+6*capital.DAY; self.unchanged(self.sync)
        self.api.clock=NOW-100000; self.unchanged(self.sync)

    def test_bill_range_conflict_and_known_record_change_refused(self):
        self.api.rows=[incoming(ts=NOW+1)]; self.unchanged(self.sync)
        self.api.rows=[incoming(),incoming(amount='6')]; self.unchanged(self.sync)
        self.store.record('bills','1',incoming(amount='4'))
        self.api.rows=[incoming()]; self.unchanged(self.sync)

    def test_balance_delta_must_match_exactly_and_available_cover_equity(self):
        for cash in ('29.999999','30.000001','29','31'):
            self.actual=account(cash)
            with self.subTest(cash=cash): self.unchanged(self.sync)
        self.actual=account('30'); self.actual['balances']['USDT']['available']='29'
        self.unchanged(self.sync)

    def test_extra_unassigned_cash_remains_unassigned(self):
        self.state(cash_usdt='100',baseline_usdt='100')
        self.actual=account('105'); self.sync()
        state=self.store.state()
        self.assertEqual('30',state['capital_usdt'])
        self.assertEqual('105',state['baseline_usdt'])
        self.assertEqual('30',state['last_equity'])

    def test_confirmation_changes_balances_or_control_requests_refuses(self):
        def mutate(_): self.actual=account('31'); return 'SYNC G3'
        self.unchanged(lambda:self.sync(mutate))
        self.actual=account()
        def control(_): self.store.control('resume'); return 'SYNC G3'
        self.unchanged(lambda:self.sync(control))
        self.assertEqual(1,len(self.store.controls()))

    def test_service_reactivated_during_confirmation_refuses(self):
        def start(_):
            self.service.return_value=subprocess.CompletedProcess([],0,STOPPED.replace('inactive','active'),'')
            return 'SYNC G3'
        self.unchanged(lambda:self.sync(start))

    def test_transaction_failure_rolls_back_bills_state_and_event(self):
        original_event=Store.event
        def fail(store,kind,*args):
            if kind=='capital_added': raise OSError('simulated disk problem')
            return original_event(store,kind,*args)
        before=self.store.state(); events=self.store.db.execute('SELECT * FROM events').fetchall()
        with patch.object(Store,'event',fail):
            with self.assertRaises(OSError): self.sync()
        self.assertEqual(before,self.store.state())
        self.assertEqual([],self.store.records('bills'))
        self.assertEqual(events,self.store.db.execute('SELECT * FROM events').fetchall())
        self.assertTrue(self.sync()['changed'])

    def test_runtime_bill_replay_accepts_same_normalized_deposit_without_halt(self):
        self.sync()
        Executor(self.api,self.store,self.cfg).sync_bills()
        self.assertIsNone(self.store.state()['halted'])
        self.assertEqual('30',self.store.state()['capital_usdt'])


if __name__=='__main__': unittest.main()
