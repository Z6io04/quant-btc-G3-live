"""Offline user-facing lifecycle and service-installation regression tests."""
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import launch
from g3core import config, runtime, status as reporting
from g3core.store import Store

ROOT = Path(__file__).resolve().parents[1]
CREDS = {'api_key': 'offline-key', 'secret_key': 'offline-secret', 'passphrase': 'offline-pass'}


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'var/live'
        self.cfg = dict(config.DEFAULT, capital_usdt=20, max_loss_usdt=1)
        config.atomic_json(self.root / 'config.json', self.cfg)
        for target, value in (('g3core.config.ROOT', self.root),
                              ('g3core.config.data_dir', lambda: self.data),
                              ('g3core.status.data_dir', lambda: self.data)):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('socket.socket', side_effect=AssertionError('Offline test may not use network'))
        p.start()
        self.addCleanup(p.stop)

    def initialize(self, **overrides):
        state = {'armed': True, 'paused': False, 'halted': None, 'op': None,
                 'position': None, 'capital_usdt': '20', 'last_equity': '20',
                 'cash_usdt': '20', 'btc_balance': '0', 'contracts': '0'}
        state.update(overrides)
        with_store = Store(self.data)
        with_store.initialize({'uid': '123', 'test': 'offline'}, state)
        with_store.close()

    def test_budget_has_no_fixed_twenty_or_twenty_five_cap(self):
        path = self.root / 'config.json'
        for capital in (25.01,26,100,10000):
            config.atomic_json(path,dict(self.cfg,capital_usdt=capital),overwrite=True)
            self.assertEqual(config.load_config(path)['capital_usdt'],capital)

    def test_nonpositive_and_nonfinite_budgets_rejected(self):
        path=self.root/'config.json'
        for value in (0,-1,True,'100'):
            config.atomic_json(path,dict(self.cfg,capital_usdt=value),overwrite=True)
            with self.assertRaises(config.Attention):config.load_config(path)
        path.write_text(json.dumps(dict(self.cfg,capital_usdt=float('inf'))))
        with self.assertRaises(config.Attention):config.load_config(path)

    def test_initial_budget_and_loss_require_user_input(self):
        with patch('launch.interactive'), patch('builtins.input',side_effect=['','0.8']), redirect_stdout(io.StringIO()):
            with self.assertRaises(config.Attention):launch.setup_config()
        with patch('launch.interactive'), patch('builtins.input',side_effect=['125','0.8']), redirect_stdout(io.StringIO()):
            launch.setup_config()
        current=config.load_config(self.root/'config.json')
        self.assertEqual(current['capital_usdt'],125)
        self.assertEqual(current['max_loss_usdt'],0.8)

    def test_status_reports_grown_ledger_budget_and_separates_deposits(self):
        self.initialize(capital_usdt='70',capital_added_usdt='50',last_equity='70.5')
        output=io.StringIO()
        with patch('g3core.status.service',return_value='inactive'),redirect_stdout(output):
            reporting.display(reporting.status(self.cfg,probe=False))
        self.assertIn('预算：70 U',output.getvalue())
        self.assertIn('累计核对追加：50 U',output.getvalue())
        self.assertIn('不含入金）：0.5 U',output.getvalue())

    def test_status_local_does_not_load_credentials_or_probe_network(self):
        output = io.StringIO()
        with patch('g3core.config.credentials', side_effect=AssertionError('No private read')), \
             patch('g3core.status.network_probe', side_effect=AssertionError('No network')), \
             patch('g3core.status.service', return_value='inactive'), redirect_stdout(output):
            self.assertEqual(launch.main(['status', '--local']), 0)
        self.assertIn('尚未启用', output.getvalue())
        self.assertNotIn(CREDS['secret_key'], output.getvalue())

    def test_empty_sqlite_is_not_presented_as_armed_or_reconciled(self):
        Store(self.data).close()
        output = io.StringIO()
        with patch('g3core.status.service', return_value='inactive'), redirect_stdout(output):
            reporting.display(reporting.status(self.cfg, probe=False))
        self.assertIn('尚未启用', output.getvalue())
        self.assertNotIn('逐笔余额/持仓核对：通过', output.getvalue())
        self.assertNotIn('新开仓：允许', output.getvalue())

    def test_empty_database_does_not_prevent_config_setup(self):
        Store(self.data).close()
        with patch('launch.interactive'), patch('builtins.input', side_effect=['20', '0.5']), redirect_stdout(io.StringIO()):
            launch.setup_config()
        saved = json.loads((self.root / 'config.json').read_text())
        self.assertEqual(saved['capital_usdt'], 20)
        self.assertEqual(saved['max_loss_usdt'], 0.5)

    def test_armed_database_still_blocks_reconfiguration(self):
        self.initialize()
        before = (self.root / 'config.json').read_bytes()
        with patch('launch.interactive'), patch('builtins.input', side_effect=AssertionError('No prompt permitted')):
            with self.assertRaises(config.Attention):
                launch.setup_config()
        self.assertEqual((self.root / 'config.json').read_bytes(), before)

    def test_ledger_records_alone_prevent_treating_database_as_empty(self):
        s = Store(self.data)
        s.record('fills', 'one', {'fillSz': '0.0001'})
        s.close()
        with self.assertRaises(config.Attention):
            config.require_uninitialized()

    def test_arm_failed_preflight_cannot_create_store_or_account_binding(self):
        account = {'uid': '123', 'position': None, 'pending': [], 'algo_pending': [],
                   'balances': {'BTC': {'cash': '0'}, 'USDT': {'cash': '20', 'available': '20'}}}
        failures = [dict(account, position={'contracts': '-1'}),
                    dict(account, pending=[{'ordId': '123'}]),
                    dict(account, balances={'BTC': {'cash': '0.0001'}, 'USDT': {'cash': '20', 'available': '20'}}),
                    dict(account, balances={'BTC': {'cash': '0'}, 'USDT': {'cash': '19', 'available': '19'}})]
        for failed in failures:
            with self.subTest(account=failed), patch('g3core.runtime.sys.platform', 'linux'), \
                 patch('g3core.runtime.os.getuid', return_value=1000), \
                 patch('g3core.runtime.config.credentials', return_value=CREDS), \
                 patch('g3core.runtime.client', return_value=object()), \
                 patch('g3core.runtime.private_preflight', return_value=(failed, {'spec': {}})), \
                 patch('g3core.runtime.Store', side_effect=AssertionError('Failed preflight must not create ledger')), \
                 patch('g3core.runtime.config.bind_account', side_effect=AssertionError('Failed preflight must not bind')):
                with self.assertRaises(config.Attention):
                    runtime.arm(self.cfg, 'ARM G3 REAL MONEY')
        with patch('g3core.runtime.sys.platform', 'linux'), \
             patch('g3core.runtime.os.getuid', return_value=1000), \
             patch('g3core.runtime.config.credentials', return_value=CREDS), \
             patch('g3core.runtime.client', return_value=object()), \
             patch('g3core.runtime.private_preflight', return_value=(account, {'spec': {}})), \
             patch('g3core.runtime.plan_entry', return_value={'allowed': False, 'reason': 'minimum_lot'}), \
             patch('g3core.runtime.Store', side_effect=AssertionError('Failed sizing must not create ledger')):
            with self.assertRaises(config.Attention):
                runtime.arm(self.cfg, 'ARM G3 REAL MONEY')
        self.assertFalse((self.data / 'state.sqlite3').exists())

    def test_resume_only_queues_control_and_cannot_clear_halt(self):
        self.initialize(paused=True, halted='人工核对')
        with redirect_stdout(io.StringIO()), patch('g3core.runtime.run', side_effect=AssertionError('No service start')):
            self.assertEqual(launch.main(['resume']), 0)
        s = Store(self.data)
        try:
            self.assertEqual(s.state()['halted'], '人工核对')
            self.assertTrue(s.state()['paused'])
            self.assertEqual([r['action'] for r in s.controls()], ['resume'])
        finally:
            s.close()

    def test_unarmed_database_cannot_receive_resume(self):
        Store(self.data).close()
        with self.assertRaises(config.Attention):
            launch.main(['resume'])
        s = Store(self.data)
        try:
            self.assertEqual(s.controls(), [])
        finally:
            s.close()

    def test_flatten_requires_confirmation_before_queueing(self):
        self.initialize()
        with patch('launch.interactive'), patch('builtins.input', return_value='no'):
            with self.assertRaises(config.Attention):
                launch.main(['flatten'])
        s = Store(self.data)
        try:
            self.assertEqual(s.controls(), [])
        finally:
            s.close()

    def test_partial_execution_and_stale_status_are_explicit(self):
        self.initialize(op={'type': 'entry', 'stage': 'swap'}, btc_balance='0.0002',
                        health={'phase': 'enter_swap', 'checked_at': 1, 'message': '等待核对'},
                        last_reconciled_at=1)
        result = reporting.status(self.cfg, probe=False)
        result['service'] = 'inactive'
        output = io.StringIO()
        with redirect_stdout(output):
            reporting.display(result)
        self.assertIn('以下可能是旧记录', output.getvalue())
        self.assertIn('可能暂时只有一腿', output.getvalue())
        self.assertIn('没有登记完整组合', output.getvalue())

    def test_status_json_contains_no_credentials(self):
        self.initialize()
        output = io.StringIO()
        with patch('g3core.config.credentials', side_effect=AssertionError('No private read')), \
             patch('g3core.status.service', return_value='inactive'), redirect_stdout(output):
            launch.main(['status', '--local', '--json'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['mode'], 'live')
        self.assertIsNone(result['network'])
        self.assertNotIn('credentials', result)
        self.assertNotIn('secret_key', output.getvalue())


class InstallerTests(unittest.TestCase):
    def test_installer_only_reloads_user_service_and_never_starts_it(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / 'btc-G3-live'
            (project / 'deploy').mkdir(parents=True)
            for name in ('install-service.sh', 'btc-G3-live.service'):
                shutil.copyfile(ROOT / 'deploy' / name, project / 'deploy' / name)
            fake_bin = home / 'bin'
            fake_bin.mkdir()
            for name, body in {
                'id': '#!/bin/sh\nprintf "1000\\n"\n',
                'python3': '#!/bin/sh\nexit 0\n',
                'systemctl': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$G3_TEST_LOG"\ncase "$*" in *is-active*) exit 3;; esac\nexit 0\n',
            }.items():
                path = fake_bin / name
                path.write_text(body)
                path.chmod(0o700)
            log = home / 'calls.txt'
            env = dict(os.environ, HOME=str(home), PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', ''), G3_TEST_LOG=str(log))
            result = subprocess.run(['bash', str(project / 'deploy/install-service.sh')], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(calls, ['--user is-active --quiet btc-G3-live.service', '--user daemon-reload'])
            self.assertEqual((home / '.config/systemd/user/btc-G3-live.service').read_bytes(),
                             (project / 'deploy/btc-G3-live.service').read_bytes())
            self.assertIn('没有启动、启用或下单', result.stdout)

    def test_unit_uses_only_g3_user_home_and_no_environment_file(self):
        unit = (ROOT / 'deploy/btc-G3-live.service').read_text()
        self.assertIn('WorkingDirectory=%h/btc-G3-live', unit)
        self.assertIn('ExecStart=/usr/bin/python3 -u %h/btc-G3-live/launch.py run', unit)
        self.assertNotIn('EnvironmentFile=', unit)
        self.assertNotIn('btc-v53-live', unit)


if __name__ == '__main__':
    unittest.main()
