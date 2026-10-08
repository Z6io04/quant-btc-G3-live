import contextlib
import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location('g3_mihomo_' + name, ROOT / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


configure = module('configure')
activate = module('activate_initial')
installer = module('install')
download = module('download_subscription')
w = configure.w
VLESS = {'name': 'private-node-name', 'type': 'vless', 'server': 'example.invalid',
         'port': 443, 'uuid': 'a0b1c2d3-1234-4567-89ab-123456789abc', 'tls': True,
         'network': 'ws', 'ws-opts': {'path': '/private-path'},
         'ech-opts': {'enable': True, 'config': 'ECH-CONFIG'}}
ANYTLS = {'name': 'private-anytls', 'type': 'anytls', 'server': 'example.invalid',
          'port': 443, 'password': 'DO-NOT-PRINT-PASSWORD', 'sni': 'example.invalid',
          'ech-opts': {'enable': True, 'query-server-name': 'example.invalid'}}


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.private = self.folder / 'private'
        self.subscription = self.folder / 'subscription.yaml'
        self.subscription.write_bytes(w.yaml.safe_dump({'proxies': [VLESS, ANYTLS]}).encode())
        self.binary = self.folder / 'mihomo'

    def test_supported_fields_preserved_and_native_validation_required(self):
        for number, original in [(1, VLESS), (2, ANYTLS)]:
            private = self.folder / ('private' + str(number))
            with mock.patch.object(w.Runtime, 'validate') as validate, contextlib.redirect_stdout(io.StringIO()) as output:
                candidate = configure.prepare(self.subscription, number, self.binary, private)
            validate.assert_called_once()
            doc = w.parse_yaml(candidate.read_bytes())
            self.assertEqual(doc['proxies'], [original])
            self.assertEqual(doc['mixed-port'], 17890)
            self.assertEqual(doc['bind-address'], '127.0.0.1')
            self.assertFalse((private / 'config.yaml').exists())
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(original['server'], output.getvalue())
            self.assertNotIn(original['name'], output.getvalue())
            self.assertNotIn(ANYTLS['password'], output.getvalue())

    def test_validator_failure_leaves_no_candidate_or_live_file(self):
        with mock.patch.object(w.Runtime, 'validate', side_effect=w.WatchError('Mihomo full-config validation failed')):
            with self.assertRaises(w.WatchError):
                configure.prepare(self.subscription, 2, self.binary, self.private)
        self.assertFalse((self.private / 'config.candidate.yaml').exists())
        self.assertFalse((self.private / 'config.yaml').exists())

    def test_native_validator_command_executes_before_output_is_installed(self):
        self.binary.write_text('#!/bin/sh\n[ "$1" = "-t" ] || exit 1\n[ "$2" = "-d" ] || exit 1\n[ "$4" = "-f" ] || exit 1\n[ -f "$5" ]\n')
        self.binary.chmod(0o700)
        with contextlib.redirect_stdout(io.StringIO()):
            candidate = configure.prepare(self.subscription, 2, self.binary, self.private)
        self.assertTrue(candidate.exists())
        self.binary.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaises(w.WatchError):
            configure.prepare(self.subscription, 2, self.binary, self.folder / 'rejected')
        self.assertFalse((self.folder / 'rejected' / 'config.candidate.yaml').exists())

    def test_existing_live_bytes_preserved_and_existing_candidate_refused(self):
        self.private.mkdir(mode=0o700)
        live = self.private / 'config.yaml'
        live.write_bytes(b'production bytes stay intact')
        with mock.patch.object(w.Runtime, 'validate'), contextlib.redirect_stdout(io.StringIO()):
            candidate = configure.prepare(self.subscription, 1, self.binary, self.private)
            before = candidate.read_bytes()
            with self.assertRaises(w.WatchError):
                configure.prepare(self.subscription, 2, self.binary, self.private)
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(live.read_bytes(), b'production bytes stay intact')

    def test_list_uses_original_numbers_without_secret_values(self):
        self.subscription.write_bytes(w.yaml.safe_dump({'proxies': [{'type': 'ss'}, ANYTLS]}).encode())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            good = configure.inventory(self.subscription)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['node'] for row in rows], [1, 2])
        self.assertEqual(rows[0]['status'], 'unsupported')
        self.assertEqual(good[0][0], 1)
        self.assertNotIn(ANYTLS['password'], output.getvalue())
        self.assertNotIn(ANYTLS['server'], output.getvalue())

    def test_tls_bypass_is_not_rewritten_to_pass(self):
        bad = dict(VLESS, **{'skip-cert-verify': True})
        self.subscription.write_bytes(w.yaml.safe_dump({'proxies': [bad]}).encode())
        with self.assertRaises(w.WatchError):
            configure.prepare(self.subscription, 1, self.binary, self.private)
        self.assertFalse(self.private.exists())

    def test_initial_activation_validates_and_refuses_existing_production(self):
        with mock.patch.object(w.Runtime, 'validate') as validate, contextlib.redirect_stdout(io.StringIO()):
            candidate = configure.prepare(self.subscription, 2, self.binary, self.private)
            activate.activate(self.binary, self.private)
            self.assertEqual((self.private / 'config.yaml').read_bytes(), candidate.read_bytes())
            self.assertEqual(validate.call_count, 2)
            with self.assertRaises(w.WatchError):
                activate.activate(self.binary, self.private)
            self.assertEqual(validate.call_count, 2)

    def test_failed_initial_validation_preserves_absence(self):
        self.private.mkdir(mode=0o700)
        (self.private / 'config.candidate.yaml').write_bytes(w.config_bytes(ANYTLS, 17890))
        with mock.patch.object(w.Runtime, 'validate', side_effect=w.WatchError('validation failed')):
            with self.assertRaises(w.WatchError):
                activate.activate(self.binary, self.private)
        self.assertFalse((self.private / 'config.yaml').exists())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = Path(self.tmp.name) / 'bin' / 'mihomo'
        self.raw = b'\x7fELF\x02\x01' + bytes(12) + b'\x3e\x00' + bytes(100)
        self.archive = gzip.compress(self.raw)
        tag = 'v1.2.3'
        name = 'mihomo-linux-amd64-compatible-' + tag + '.gz'
        self.release = {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': [{
            'name': name, 'digest': 'sha256:' + hashlib.sha256(self.archive).hexdigest(),
            'browser_download_url': installer.RELEASES + tag + '/' + name}]}

    def run_install(self, archive=None):
        with mock.patch.object(installer.platform, 'system', return_value='Linux'), \
                mock.patch.object(installer.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(installer, 'fetch', side_effect=[json.dumps(self.release).encode(), self.archive if archive is None else archive]), \
                contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.destination)

    def test_official_hash_architecture_and_receipt(self):
        self.run_install()
        self.assertEqual(self.destination.read_bytes(), self.raw)
        receipt = json.loads(self.destination.with_name('mihomo.release.json').read_text())
        self.assertEqual(receipt['tag'], 'v1.2.3')
        self.assertEqual(receipt['binary_sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)

    def test_corrupt_archive_rejected_before_install(self):
        with self.assertRaisesRegex(installer.InstallError, 'SHA256 mismatch'):
            self.run_install(b'tampered')
        self.assertFalse(self.destination.exists())

    def test_missing_trusted_digest_never_falls_back_to_self_hash(self):
        self.release['assets'][0]['digest'] = None
        with self.assertRaisesRegex(installer.InstallError, 'digest missing'):
            self.run_install()
        self.assertFalse(self.destination.exists())

    def test_unofficial_asset_and_prerelease_rejected(self):
        for field, value in [('browser_download_url', 'https://example.invalid/mihomo.gz'), ('digest', 'sha1:' + '0' * 40)]:
            changed = copy.deepcopy(self.release)
            changed['assets'][0][field] = value
            with self.assertRaises(installer.InstallError):
                installer.select_asset(changed)
        changed = copy.deepcopy(self.release)
        changed['prerelease'] = True
        with self.assertRaises(installer.InstallError):
            installer.select_asset(changed)

    def test_wrong_elf_architecture_rejected(self):
        with self.assertRaises(installer.InstallError):
            installer.validate_elf(self.raw[:18] + b'\xb7\x00' + self.raw[20:])

    def test_existing_binary_and_mac_host_preserved(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b'current-production')
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertEqual(self.destination.read_bytes(), b'current-production')
        with mock.patch.object(installer.platform, 'system', return_value='Darwin'):
            with self.assertRaisesRegex(installer.InstallError, 'not a Mac installer'):
                installer.install(self.destination)


class DownloadTests(unittest.TestCase):
    def test_existing_private_file_is_preserved_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'subscription.yaml'
            target.write_bytes(b'private')
            with mock.patch.object(download.getpass, 'getpass') as prompt, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(download.main(['--output', str(target)]), 2)
            prompt.assert_not_called()
            self.assertEqual(target.read_bytes(), b'private')

    def test_error_never_prints_subscription_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'subscription.yaml'
            with mock.patch.object(download.getpass, 'getpass', return_value='https://example.invalid/?token=PRIVATE-TOKEN'), \
                    mock.patch.object(download.urllib.request, 'build_opener', side_effect=ValueError('PRIVATE-TOKEN')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(download.main(['--output', str(target)]), 2)
            self.assertNotIn('PRIVATE-TOKEN', output.getvalue())
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
