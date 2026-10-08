"""Offline migration checks; every API/systemd result is synthetic."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('retire_f3', ROOT / 'tools/retire_f3_binding.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

STOPPED = 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\n'


class API:
    def sync_time(self):
        return 1

    def write(self, *args, **kwargs):
        raise AssertionError('migration must never send orders')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'btc-G3-live'; self.root.mkdir()
        self.install('retire_f3.sys.platform', 'linux')
        self.install('retire_f3.os.geteuid', return_value=1000)
        self.install('retire_f3.Path.home', return_value=self.home)
        self.install('retire_f3.config.ROOT', self.root)
        self.install('socket.socket', side_effect=AssertionError('no network'))
        self.cfg = dict(m.config.DEFAULT)
        m.config.atomic_json(self.root / 'config.json', self.cfg)
        self.install('retire_f3.config.credentials', return_value={'api_key':'fixture','secret_key':'fixture','passphrase':'fixture'})
        self.client = self.install('retire_f3.runtime.client', return_value=API())
        self.service = self.install('retire_f3.subprocess.run', return_value=subprocess.CompletedProcess([], 0, STOPPED, ''))
        self.actual = {'uid':'123', 'position':None, 'positions':[], 'pending':[], 'algo_pending':[],
                       'balances':{'BTC':{'cash':'0','eq':'0','available':'0'},
                                   'USDT':{'cash':'30','available':'30'}}}
        self.account = self.install('retire_f3.account_snapshot', side_effect=lambda *a:copy.deepcopy(self.actual))
        self.key = m.config.account_key('123')
        self.paths = [self.home / '.local/state' / name / 'accounts' / (self.key + '.json')
                      for name in ('btc-F3-live','btc-v53-live')]
        self.base = {'schema':1,'uid':'123','directory':str(self.home/'btc-F3-live/var/live'),
                     'strategy':'F3-live-1','binding_id':'a'*32}
        self.write_records()

    def install(self, name, *args, **kwargs):
        p = patch(name, *args, **kwargs); result = p.start(); self.addCleanup(p.stop); return result

    def write_records(self):
        for path, value in zip(self.paths, [self.base,dict(self.base,account=self.key,mode='live',phase='active')]):
            m.config.atomic_json(path,value,overwrite=path.exists())
        self.original = [p.read_bytes() for p in self.paths]

    def tree(self):
        return {str(p.relative_to(self.home)):(p.read_bytes(),p.stat().st_mode & 0o777)
                for p in self.home.rglob('*') if p.is_file()}

    def unchanged(self, fn):
        before = [p.read_bytes() for p in self.paths]
        with self.assertRaises(Exception):fn()
        self.assertEqual([p.read_bytes() for p in self.paths],before)

    def test_default_check_writes_nothing_and_uses_readonly_client(self):
        before = self.tree()
        result = m.retire()
        self.assertEqual(result['mode'],'check')
        self.assertEqual(self.tree(),before)
        self.assertEqual(self.client.call_args.kwargs,{'orders':False})
        self.assertGreaterEqual(self.account.call_count,2)
        seen = {(r.args[0][1], r.args[0][4]) for r in self.service.call_args_list}
        self.assertEqual(seen,{(scope,unit) for scope in ('--user','--system')
                             for unit in ('btc-F3-live.service','btc-G3-live.service')})

    def test_apply_archives_both_exact_bytes_and_preserves_other_files(self):
        other = self.paths[0].parent/'unrelated.json'; m.config.atomic_json(other,{'keep':True})
        old = self.home/'btc-F3-live/var/live';old.mkdir(parents=True)
        ledger=old/'state.sqlite3';ledger.write_bytes(b'old ledger must remain')
        result = m.retire(True)
        self.assertTrue(all(not p.exists() for p in self.paths))
        folder=Path(result['backup'])
        for name,raw in zip(('F3.json','shared-F3.json'),self.original):
            self.assertEqual((folder/name).read_bytes(),raw)
            self.assertEqual((folder/name).stat().st_mode & 0o777,0o600)
        self.assertEqual(folder.stat().st_mode & 0o777,0o700)
        self.assertEqual(json.loads((folder/'audit.json').read_text())['phase'],'retired')
        self.assertEqual(ledger.read_bytes(),b'old ledger must remain')
        self.assertTrue(other.exists())
        self.assertFalse((m.config.data_dir()/'state.sqlite3').exists())
        self.assertEqual(result['orders_sent'],0)

    def test_wrong_strategy_directory_hash_binding_or_schema_refused(self):
        for change in ({'strategy':'v53'},{'directory':str(self.home/'other')}, {'uid':'456'},
                       {'binding_id':'b'*32},{'account':'wrong'},{'schema':True}, {'extra':True}):
            self.write_records()
            shared=json.loads(self.paths[1].read_text());shared.update(change)
            m.config.atomic_json(self.paths[1],shared,overwrite=True)
            self.unchanged(lambda:m.retire(True))

    def test_missing_registration_never_modifies_survivor(self):
        self.paths[1].unlink(); raw=self.paths[0].read_bytes()
        with self.assertRaises(Exception):m.retire(True)
        self.assertEqual(self.paths[0].read_bytes(),raw)
        self.assertFalse(self.paths[1].exists())

    def test_missing_zero_position_orders_and_balance_are_not_assumed_safe(self):
        clean=copy.deepcopy(self.actual)
        mutations = [lambda a:a.pop('position'),lambda a:a.update(position={'contracts':'0'}),
                     lambda a:a.update(positions=[{}]),lambda a:a.pop('pending'),
                     lambda a:a.update(pending=[{}]),lambda a:a.update(algo_pending=[{}]),
                     lambda a:a['balances']['BTC'].pop('cash'),
                     lambda a:a['balances']['BTC'].update(cash='0.00000001'),
                     lambda a:a['balances']['BTC'].update(eq='NaN')]
        for mutate in mutations:
            self.actual=copy.deepcopy(clean);mutate(self.actual)
            self.unchanged(lambda:m.retire(True))

    def test_private_uid_change_during_final_check_refuses(self):
        changed=copy.deepcopy(self.actual);changed['uid']='456'
        self.account.side_effect=[copy.deepcopy(self.actual),copy.deepcopy(self.actual),changed]
        self.unchanged(lambda:m.retire(True))

    def test_any_user_or_system_service_active_or_unclear_refuses(self):
        for scope in ('--user','--system'):
            for unit in ('btc-F3-live.service','btc-G3-live.service'):
                for status in ('active','activating','deactivating','reloading'):
                    self.service.side_effect=lambda cmd,**kw:subprocess.CompletedProcess(cmd,0,
                        STOPPED.replace('ActiveState=inactive','ActiveState='+status)
                        if cmd[1]==scope and cmd[4]==unit else STOPPED,'')
                    self.unchanged(lambda:m.retire(True))
        self.service.side_effect=None
        for output in (STOPPED.replace('MainPID=0','MainPID=42'),'',STOPPED.replace('LoadState=loaded','LoadState=error')):
            self.service.return_value=subprocess.CompletedProcess([],1,output,'')
            self.unchanged(lambda:m.retire(True))

    def test_armed_g3_refused_before_credentials_or_api(self):
        from g3core.store import Store
        store=Store(m.config.data_dir());store.put('state',{'armed':True});store.close()
        self.unchanged(lambda:m.retire(True))
        self.client.assert_not_called()
        self.account.assert_not_called()

    def test_existing_g3_binding_refused(self):
        m.config.atomic_json(m.config.binding_path('123'),{'strategy':'G3-live-1'})
        self.unchanged(lambda:m.retire(True))

    def test_shared_account_lock_blocks_manual_process(self):
        with m.runtime.account_guard('123'):
            self.unchanged(lambda:m.retire(True))

    def test_partial_backup_failure_never_removes_originals(self):
        original=m.exclusive_bytes
        def fail(path,raw):
            if path.name=='shared-F3.json':raise OSError('injected backup failure')
            return original(path,raw)
        with patch.object(m,'exclusive_bytes',side_effect=fail):
            self.unchanged(lambda:m.retire(True))

    def test_second_unlink_failure_restores_first_from_original_bytes(self):
        unlink=Path.unlink
        def fail(path,*args,**kwargs):
            if path==self.paths[1]:raise OSError('injected second removal failure')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail):self.unchanged(lambda:m.retire(True))
        audit=next((self.root/'private/f3-binding-retirements').glob('*/audit.json'))
        self.assertEqual(json.loads(audit.read_text())['phase'],'rolled_back')

    def test_same_bytes_new_inode_before_removal_are_rejected(self):
        original=m.flat_account; count=0
        def mutate(*args,**kwargs):
            nonlocal count
            result=original(*args,**kwargs);count+=1
            if count==3:
                p=self.paths[0];raw=p.read_bytes();p.unlink();m.exclusive_bytes(p,raw)
            return result
        with patch.object(m,'flat_account',side_effect=mutate):self.unchanged(lambda:m.retire(True))

    def test_restore_failure_keeps_both_archives_and_requires_manual_recovery(self):
        unlink=Path.unlink; original=m.exclusive_bytes
        def fail_unlink(path,*args,**kwargs):
            if path==self.paths[1]:raise OSError('injected second removal failure')
            return unlink(path,*args,**kwargs)
        def fail_restore(path,raw):
            if path==self.paths[0]:raise OSError('injected restore failure')
            return original(path,raw)
        with patch.object(Path,'unlink',fail_unlink),patch.object(m,'exclusive_bytes',side_effect=fail_restore):
            with self.assertRaises(m.Refuse):m.retire(True)
        audit=next((self.root/'private/f3-binding-retirements').glob('*/audit.json'))
        self.assertEqual(json.loads(audit.read_text())['phase'],'manual_recovery_required')
        for name,raw in zip(('F3.json','shared-F3.json'),self.original):
            self.assertEqual((audit.parent/name).read_bytes(),raw)
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(self.paths[1].read_bytes(),self.original[1])

    def test_cli_never_prints_raw_exception_or_uid(self):
        stream=io.StringIO()
        with patch.object(m,'retire',side_effect=RuntimeError('secret-key UID 123')), \
             redirect_stderr(stream),redirect_stdout(stream):
            self.assertEqual(m.main([]),1)
        self.assertNotIn('secret-key',stream.getvalue())
        self.assertNotIn('123',stream.getvalue())


if __name__=='__main__':unittest.main()
