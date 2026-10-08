import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import probe_nodes as p
import proxy_watch as w


class ProbeTests(unittest.TestCase):
    def settings(self, folder):
        return w.Settings(Path('/unused/mihomo'), folder / 'live.yaml',
                          folder / 'subscription.yaml', folder / 'production-state',
                          allowed_ips=('8.8.8.8',))

    def node(self):
        return {'name': 'private-name', 'type': 'vless', 'server': 'private-host',
                'port': 443, 'uuid': '00000000-0000-0000-0000-000000000001',
                'tls': True, 'ech-opts': {'enable': True, 'config': 'private-ech'}}

    def test_active_watcher_refuses_before_loading_settings(self):
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'active\n')), \
             patch.object(w.Settings, 'load') as load, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(p.main([]), 2)
            load.assert_not_called()
        self.assertIn('stop proxy-watch.service', out.getvalue())

    def test_inactive_missing_allowed_but_query_failure_refuses(self):
        for code, state in ((3, 'inactive'), (3, 'failed'), (4, 'unknown')):
            with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, state)):
                p.watcher_stopped()
        with patch.object(p.subprocess, 'run', side_effect=OSError('private-secret')):
            with self.assertRaisesRegex(w.WatchError, 'cannot verify'):
                p.watcher_stopped()

    def test_success_preserves_options_uses_temporary_state_and_no_ip_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, node = self.settings(root), self.node()
            settings.live_config.write_text('original')
            candidate_settings = []
            parent = self
            class Runtime:
                def __init__(self, staged):
                    candidate_settings.append(staged)
                    parent.assertEqual(staged.allowed_ips, ())
                    parent.assertNotEqual(staged.state_dir, settings.state_dir)
                @contextlib.contextmanager
                def candidate(self, actual):
                    parent.assertEqual(actual, node)
                    yield 17891
            with patch.object(w, 'candidates', return_value=([(4, node)], [])), \
                 patch.object(w, 'Runtime', Runtime), patch.object(w, 'probe') as check, \
                 patch.object(w, 'https_json', return_value={'ip': '1.1.1.1'}) as fetch, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p.scan(settings), 0)
            check.assert_called_once()
            fetch.assert_called_once()
            self.assertEqual(settings.live_config.read_text(), 'original')
            self.assertFalse(settings.state_dir.exists())
            self.assertFalse(candidate_settings[0].state_dir.exists())
            self.assertIn('"node": 5', out.getvalue())
            self.assertIn('1.1.1.1', out.getvalue())
            self.assertNotIn('private-', out.getvalue())

    def test_schema_rejection_redacts_node_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            node = self.node()
            node['dialer-proxy'] = 'top-secret'
            settings.subscription.write_text(w.yaml.safe_dump({'proxies': [node]}))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p.scan(settings), 1)
            self.assertIn('"nodes": [1]', out.getvalue())
            self.assertIn('node fields or indirect dependency', out.getvalue())
            self.assertNotIn('top-secret', out.getvalue())
            self.assertNotIn('private-', out.getvalue())

    def failure_scan(self, fetch=None, probe=None):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            cleaned = []
            class Runtime:
                def __init__(self, staged):
                    pass
                @contextlib.contextmanager
                def candidate(self, node):
                    try:
                        yield 17891
                    finally:
                        cleaned.append(True)
            with patch.object(w, 'candidates', return_value=([(0, self.node())], [])), \
                 patch.object(w, 'Runtime', Runtime), patch.object(w, 'probe', side_effect=probe), \
                 patch.object(w, 'https_json', **(fetch or {'return_value': {'ip': '127.0.0.1'}})), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    result = p.scan(settings)
                except KeyboardInterrupt:
                    result = 'interrupt'
            self.assertEqual(cleaned, [True])
            return result, out.getvalue()

    def test_private_ip_refused_and_arbitrary_exception_redacted(self):
        result, text = self.failure_scan()
        self.assertEqual(result, 1)
        self.assertIn('invalid public exit IP', text)
        result, text = self.failure_scan(fetch={'side_effect': RuntimeError('secret-url-password')})
        self.assertEqual(result, 1)
        self.assertIn('RuntimeError', text)
        self.assertNotIn('secret-url-password', text)

    def test_interrupt_still_cleans_candidate(self):
        result, text = self.failure_scan(probe=KeyboardInterrupt())
        self.assertEqual(result, 'interrupt')
        self.assertNotIn('healthy', text)

    def test_okx_failure_and_ip_failure_are_distinct(self):
        result, text = self.failure_scan(probe=w.WatchError('public probe HTTP status 403'))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(result, 1)
        self.assertEqual(rows[-2]['stage'], 'okx_public')
        self.assertFalse(rows[-2]['okx_public'])
        self.assertFalse(rows[-2]['ip_lookup'])
        self.assertEqual(rows[-2]['status'], 'failed')
        self.assertEqual(rows[-1]['okx_reachable_nodes'], 0)
        result, text = self.failure_scan(fetch={'side_effect': w.WatchError('public probe HTTP status 403')})
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(result, 1)
        self.assertEqual(rows[-2]['stage'], 'exit_ip')
        self.assertTrue(rows[-2]['okx_public'])
        self.assertFalse(rows[-2]['ip_lookup'])
        self.assertEqual(rows[-2]['status'], 'partial')
        self.assertEqual(rows[-1]['okx_reachable_nodes'], 1)
        self.assertEqual(rows[-1]['healthy_nodes'], 0)

    def test_nodes_parser_rejects_malformed_and_selection_uses_original_index(self):
        for value in ('', '1,', '0', '201', '1,1', '-2', '1;secret', '１'):
            with self.assertRaises(p.argparse.ArgumentTypeError):
                p.parse_nodes(value)
        self.assertEqual(p.parse_nodes('1, 3'), (1, 3))
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            reason = 'TLS with certificate verification is required'
            rejected = [{'index': i, 'reason': reason} for i in range(4)]
            with patch.object(w, 'candidates', return_value=([], rejected)), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p.scan(settings, (2, 4)), 1)
                with self.assertRaisesRegex(w.WatchError, 'does not exist'):
                    p.scan(settings, (5,))
            rows = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(rows[0]['nodes'], [2, 4])
            self.assertEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main()
