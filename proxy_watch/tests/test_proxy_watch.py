"""Offline only: synthetic credentials, mocked HTTPS, owned fake local process."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_watch as w

NODE = {"name": "synthetic-VLESS", "type": "vless", "server": "example.invalid", "port": 443,
        "uuid": "00000000-0000-4000-8000-000000000001", "tls": True, "udp": True,
        "client-fingerprint": "chrome", "reality-opts": {"public-key": "synthetic-not-a-real-key", "short-id": "1234"}}
NOW = 1800000000
OK = {"okx_ts": NOW * 1000, "exit_ip": "8.8.8.8"}


class FakeRuntime:
    def __init__(self):
        self.tested = []
        self.cleaned = 0
        self.restarts = 0
        self.validations = 0
        self.restart_failure = False
        self.on_validate = None

    @contextlib.contextmanager
    def candidate(self, node):
        self.tested.append(copy.deepcopy(node))
        try:
            yield 17891
        finally:
            self.cleaned += 1

    def validate(self, config, folder):
        self.validations += 1
        if self.on_validate:
            self.on_validate()

    def restart(self):
        self.restarts += 1
        if self.restart_failure and self.restarts == 1:
            raise w.WatchError("simulated restart failure")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s = w.Settings(self.root / "mihomo", self.root / "live.yaml", self.root / "subscription.yaml",
                            self.root / "state", allowed_ips=("8.8.8.8",))
        w.private_dir(self.s.state_dir)
        self.raw = w.config_bytes(NODE, 17890)
        self.s.live_config.write_bytes(self.raw)
        self.s.subscription.write_text(w.yaml.safe_dump({"proxies": [NODE]}))
        self.clock = NOW
        self.runtime = FakeRuntime()

    def watcher(self, health):
        return w.Watcher(self.s, self.runtime, health, now=lambda: self.clock, sleep=lambda _: None)

    @staticmethod
    def fail(port):
        raise w.WatchError("simulated outage")

    def test_three_failures_scan_and_persistent_cooldown(self):
        watcher = self.watcher(self.fail)
        self.assertEqual(watcher.tick(), "waiting_for_failures")
        self.assertEqual(watcher.tick(), "waiting_for_failures")
        self.assertEqual(watcher.tick(), "scan_exhausted")
        self.assertEqual(len(self.runtime.tested), 1)
        rebuilt = self.watcher(self.fail)
        self.assertEqual(rebuilt.tick(), "cooldown")
        self.assertEqual(len(self.runtime.tested), 1)
        self.clock += 3601
        self.assertEqual(rebuilt.tick(), "scan_exhausted")
        self.assertEqual(len(self.runtime.tested), 2)
        self.assertEqual(self.s.live_config.read_bytes(), self.raw)

    def test_cooldown_still_probes_current_and_resets_streak(self):
        watcher = self.watcher(self.fail)
        watcher.state.update(failures=3, next_scan_at=NOW + 3600)
        health = mock.Mock(return_value=OK)
        watcher.health = health
        self.assertEqual(watcher.tick(), "healthy")
        health.assert_called_once_with(17890)
        self.assertEqual(watcher.state["failures"], 0)
        self.assertEqual(watcher.state["next_scan_at"], 0)

    def test_no_allowed_ips_never_tests_or_switches(self):
        self.s.allowed_ips = ()
        watcher = self.watcher(self.fail)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "blocked_no_allowed_ips")
        self.assertEqual(self.runtime.tested, [])
        self.assertEqual(self.runtime.restarts, 0)

    def test_switch_requires_candidate_and_production_success(self):
        def health(port):
            if port == 17891 or self.runtime.restarts:
                return OK
            raise w.WatchError("current down")
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "switched")
        self.assertEqual(self.runtime.cleaned, 1)
        self.assertEqual(self.runtime.restarts, 1)
        self.assertEqual(self.runtime.tested[0], NODE)
        self.assertEqual(w.parse_yaml(self.s.live_config.read_bytes())["proxies"][0], NODE)
        backups = list(self.s.state_dir.glob("backup-*.yaml"))
        self.assertEqual(backups[0].read_bytes(), self.raw)
        self.assertFalse(watcher.journal_path.exists())
        self.assertEqual(watcher.state["next_scan_at"], 0)

    def test_production_failure_rolls_back_original_bytes(self):
        changed = copy.deepcopy(NODE)
        changed["name"] = "candidate"
        self.s.subscription.write_text(w.yaml.safe_dump({"proxies": [changed]}))
        def health(port):
            if port == 17891 or self.runtime.restarts == 2:
                return OK
            raise w.WatchError("unhealthy")
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "current_recovered")
        self.assertEqual(self.s.live_config.read_bytes(), self.raw)
        self.assertEqual(self.runtime.restarts, 2)
        self.assertFalse(watcher.journal_path.exists())

    def test_healthy_original_after_rollback_stops_remaining_candidates(self):
        second = copy.deepcopy(NODE)
        second["name"] = "second"
        self.s.subscription.write_text(w.yaml.safe_dump({"proxies": [NODE, second]}))
        def health(port):
            if port == 17891 or self.runtime.restarts == 2:
                return OK
            raise w.WatchError("initial/first production outage")
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "current_recovered")
        self.assertEqual(len(self.runtime.tested), 1)
        self.assertEqual(self.runtime.restarts, 2)
        self.assertEqual(watcher.state["next_scan_at"], 0)
        self.assertEqual(watcher.state["failures"], 0)
        self.assertEqual(self.s.live_config.read_bytes(), self.raw)

    def test_successful_switch_can_rescan_after_three_new_failures(self):
        def health(port):
            if port == 17891 or self.runtime.restarts:
                return OK
            raise w.WatchError("current down")
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "switched")
        watcher.health = self.fail
        self.clock += 300
        self.assertEqual(watcher.tick(), "waiting_for_failures")
        self.assertEqual(watcher.tick(), "waiting_for_failures")
        self.assertEqual(watcher.tick(), "scan_exhausted")
        self.assertEqual(len(self.runtime.tested), 2)

    def test_production_failure_rollback_then_tries_next_candidate(self):
        second = copy.deepcopy(NODE)
        second["name"] = "second"
        self.s.subscription.write_text(w.yaml.safe_dump({"proxies": [NODE, second]}))
        def health(port):
            if port == 17891 or self.runtime.restarts >= 3:
                return OK
            raise w.WatchError("first production node failed")
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "switched")
        self.assertEqual(len(self.runtime.tested), 2)
        self.assertEqual(self.runtime.restarts, 3)
        self.assertEqual(w.parse_yaml(self.s.live_config.read_bytes())["proxies"][0]["name"], "second")
        self.assertEqual(watcher.state["next_scan_at"], 0)

    def test_failed_rollback_restart_blocks_remaining_candidates(self):
        second = copy.deepcopy(NODE)
        second["name"] = "second"
        self.s.subscription.write_text(w.yaml.safe_dump({"proxies": [NODE, second]}))
        def health(port):
            if port == 17891:
                return OK
            raise w.WatchError("current/production down")
        def restart():
            self.runtime.restarts += 1
            if self.runtime.restarts == 2:
                raise w.WatchError("rollback restart failure")
        self.runtime.restart = restart
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "switch_blocked")
        self.assertEqual(len(self.runtime.tested), 1)
        self.assertTrue(watcher.journal_path.exists())

    def test_restart_failure_also_rolls_back(self):
        self.runtime.restart_failure = True
        watcher = self.watcher(lambda port: OK)
        self.assertFalse(watcher.apply(NODE, w.Snapshot.take(self.s.live_config)))
        self.assertEqual(self.runtime.restarts, 2)
        self.assertEqual(self.s.live_config.read_bytes(), self.raw)

    def test_production_startup_first_probe_fails_second_succeeds(self):
        health = mock.Mock(side_effect=[w.WatchError("listener not ready"), OK])
        watcher = self.watcher(health)
        self.assertTrue(watcher.apply(NODE, w.Snapshot.take(self.s.live_config)))
        self.assertEqual(health.call_count, 2)
        self.assertEqual(self.runtime.restarts, 1)

    def test_manual_change_during_validation_is_not_overwritten(self):
        watcher = self.watcher(lambda port: OK)
        original = w.Snapshot.take(self.s.live_config)
        edited = self.raw + b"# operator edit\n"
        self.runtime.on_validate = lambda: self.s.live_config.write_bytes(edited)
        with self.assertRaises(w.Conflict):
            watcher.apply(NODE, original)
        self.assertEqual(self.s.live_config.read_bytes(), edited)
        self.assertEqual(self.runtime.restarts, 0)

    def test_manual_change_during_restart_is_not_overwritten_by_rollback(self):
        watcher = self.watcher(self.fail)
        edited = self.raw + b"# manual change after restart\n"
        self.runtime.restart = lambda: self.s.live_config.write_bytes(edited)
        with self.assertRaises(w.Conflict):
            watcher.apply(NODE, w.Snapshot.take(self.s.live_config))
        self.assertEqual(self.s.live_config.read_bytes(), edited)
        self.assertTrue(watcher.journal_path.exists())

    def test_crash_journal_recovers_and_conflict_stops(self):
        watcher = self.watcher(lambda port: OK)
        backup = self.s.state_dir / "backup-test.yaml"
        backup.write_bytes(self.raw)
        node = dict(NODE, name="new")
        new = w.config_bytes(node, 17890)
        journal = {"backup": backup.name, "old_sha": w.digest(self.raw), "new_sha": w.digest(new)}
        w.write_json(watcher.journal_path, journal)
        self.s.live_config.write_bytes(new)
        watcher.recover()
        self.assertEqual(self.s.live_config.read_bytes(), self.raw)
        self.assertFalse(watcher.journal_path.exists())
        w.write_json(watcher.journal_path, journal)
        edited = new + b"# manual\n"
        self.s.live_config.write_bytes(edited)
        with self.assertRaises(w.Conflict):
            watcher.recover()
        self.assertEqual(self.s.live_config.read_bytes(), edited)

    def test_scan_ip_rejection_never_restarts(self):
        def health(port):
            raise w.WatchError("exit IP not in allowed_ips")
        watcher = self.watcher(health)
        watcher.state["failures"] = 2
        self.assertEqual(watcher.tick(), "scan_exhausted")
        self.assertEqual(self.runtime.restarts, 0)

    def test_current_probe_thread_continues_during_scan(self):
        self.s.interval_seconds = 0.02  # Test clock only; production loader enforces >=60.
        health = mock.Mock(return_value=OK)
        watcher = self.watcher(health)
        with watcher.current_monitor() as recovered:
            self.assertTrue(recovered.wait(1))
        health.assert_called_once_with(17890)
        self.assertFalse(any(t.name == "current-public-probe" for t in threading.enumerate()))

    def test_schema_preserves_supported_fields_and_reports_rejected(self):
        ws = copy.deepcopy(NODE)
        ws.update(network="ws", **{"ws-opts": {"path": "/path", "headers": {"Host": "example.invalid"}}})
        self.assertEqual(w.validate_node(ws), ws)
        for change in ({"dialer-proxy": "other"}, {"skip-cert-verify": True}, {"type": "trojan"},
                       {"unknown-new-option": True}, {"network": "xhttp"}, {"ws-opts": {"path": "/"}}):
            with self.subTest(change=change), self.assertRaises(w.WatchError):
                w.validate_node(dict(NODE, **change))
        indirect = copy.deepcopy(NODE)
        indirect["dialer-proxy"] = "other"
        self.s.subscription.write_text(w.yaml.safe_dump({"proxies": [NODE, indirect]}))
        good, rejected = w.candidates(self.s.subscription)
        self.assertEqual(len(good), 1)
        self.assertEqual(rejected[0]["index"], 1)

    def test_yaml_unsafe_tags_aliases_duplicates_rejected(self):
        for raw in (b"a: 1\na: 2", b"a: &alias {x: 1}\nb: *alias", b"a: !!python/object/apply:os.system ['id']"):
            with self.subTest(raw=raw), self.assertRaises(w.WatchError):
                w.parse_yaml(raw)

    def test_live_rejects_indirect_or_open_control_configs(self):
        for change in ({"allow-lan": True}, {"external-controller": "127.0.0.1:9090"},
                       {"listeners": []}, {"rules": ["MATCH,DIRECT"]}, {"mixed-port": 19000}):
            doc = w.config_for(NODE, 17890)
            doc.update(change)
            with self.subTest(change=change), self.assertRaises(w.WatchError):
                w.validate_live(w.yaml.safe_dump(doc).encode())

    def test_singleton_lock(self):
        lock = self.s.state_dir / "watch.lock"
        with w.locked(lock):
            with self.assertRaises(w.WatchError), w.locked(lock):
                pass

    def test_check_does_not_create_state_read_subscription_or_restart(self):
        self.s.state_dir.rmdir()
        self.s.subscription.unlink()
        with mock.patch.object(w.Settings, "load", return_value=self.s), mock.patch.object(w, "probe", return_value=OK), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(w.main(["check"]), 0)
        self.assertFalse(self.s.state_dir.exists())

    def test_owned_candidate_cleanup_and_occupied_port(self):
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            self.s.candidate_port = port_socket.getsockname()[1]
        self.s.mihomo_binary.write_text("#!" + sys.executable + "\n" +
            "import pathlib,re,socket,sys,time\n"
            "if '-t' in sys.argv: sys.exit(0)\n"
            "data=pathlib.Path(sys.argv[sys.argv.index('-f')+1]).read_text()\n"
            "port=int(re.search(r'mixed-port: (\\d+)',data).group(1))\n"
            "sock=socket.socket();sock.bind(('127.0.0.1',port));sock.listen()\n"
            "time.sleep(60)\n")
        self.s.mihomo_binary.chmod(0o700)
        runtime = w.Runtime(self.s)
        with self.assertRaisesRegex(w.WatchError, "simulated"), runtime.candidate(NODE):
            raise w.WatchError("simulated probe failure")
        w.port_free(self.s.candidate_port)
        self.assertEqual(list(self.s.state_dir.glob("candidate-*")), [])
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.s.candidate_port))
            occupied.listen()
            with self.assertRaisesRegex(w.WatchError, "occupied"), runtime.candidate(NODE):
                pass
            self.assertGreater(occupied.fileno(), 0)

    def test_public_time_and_ip_validation(self):
        good = {"code": "0", "data": [{"ts": str(NOW * 1000)}]}
        calls = []
        def fetch(url, port, timeout):
            calls.append((url, port))
            return good if url == w.OKX_TIME else {"ip": "8.8.8.8"}
        result = w.probe(self.s, 17891, fetch=fetch, now=lambda: NOW)
        self.assertEqual(result["exit_ip"], "8.8.8.8")
        self.assertEqual(calls, [(w.OKX_TIME, 17891), (self.s.ip_url, 17891)])
        for bad in ({"code": "50011", "data": [{"ts": str(NOW * 1000)}]},
                    {"code": "0", "data": [{"ts": str((NOW - 121) * 1000)}]},
                    {"code": 0, "data": [{"ts": str(NOW * 1000)}]}, {"code": "0", "data": []}):
            with self.subTest(bad=bad), self.assertRaises(w.WatchError):
                w.probe(self.s, fetch=lambda *_: bad, now=lambda: NOW)
        with self.assertRaisesRegex(w.WatchError, "allowed_ips"):
            w.probe(self.s, fetch=lambda url, *_: good if url == w.OKX_TIME else {"ip": "1.1.1.1"}, now=lambda: NOW)

    def test_https_explicit_tunnel_tls_and_no_proxy_bypass(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{"code":"0"}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "HTTPS_PROXY": "http://wrong.invalid:1"}), mock.patch.object(w.http.client, "HTTPSConnection", return_value=connection) as factory:
            self.assertEqual(w.https_json(w.OKX_TIME, 17890, 8), {"code": "0"})
        self.assertEqual(factory.call_args.args, ("127.0.0.1", 17890))
        self.assertEqual(factory.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(factory.call_args.kwargs["context"].check_hostname)
        connection.set_tunnel.assert_called_once_with("openapi.okx.com", 443)
        self.assertNotIn("Authorization", connection.request.call_args.kwargs["headers"])
        connection.close.assert_called_once()
        response.status = 302
        with mock.patch.object(w.http.client, "HTTPSConnection", return_value=connection), self.assertRaises(w.WatchError):
            w.https_json(w.OKX_TIME, 17890, 8)


if __name__ == "__main__":
    unittest.main()
