"""Offline ECH compatibility regressions; only synthetic configuration values."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_watch as w


NODE = {
    "name": "synthetic-ECH-node", "type": "vless", "server": "node.example.invalid",
    "port": 443, "uuid": "00000000-0000-4000-8000-000000000002", "tls": True,
    "ech-opts": {"enable": True, "config": "synthetic-ECH-value",
                 "query-server-name": "ech.example.invalid"},
}


class ECHCompatibilityTests(unittest.TestCase):
    def test_tcp_and_ws_candidate_and_live_preserve_all_ech_fields(self):
        for network in ("tcp", "ws"):
            with self.subTest(network=network):
                node = copy.deepcopy(NODE)
                node["network"] = network
                if network == "ws":
                    node["ws-opts"] = {"path": "/synthetic", "headers": {"Host": "ws.example.invalid"}}
                accepted = w.validate_node(node)
                for port in (17891, 17890):
                    doc = w.parse_yaml(w.config_bytes(accepted, port))
                    self.assertEqual(doc["proxies"][0], node)
                    self.assertEqual(doc["mixed-port"], port)
                self.assertEqual(w.validate_live(w.config_bytes(accepted, 17890)), node)
                accepted["ech-opts"]["config"] = "changed-only-in-copy"
                self.assertEqual(node["ech-opts"]["config"], "synthetic-ECH-value")

    def test_disabled_ech_preserves_explicit_false_and_settings(self):
        node = copy.deepcopy(NODE)
        node["ech-opts"]["enable"] = False
        result = w.validate_live(w.config_bytes(w.validate_node(node), 17890))
        self.assertIs(result["ech-opts"]["enable"], False)
        self.assertEqual(result, node)

    def test_optional_or_empty_ech_config_is_not_filled_or_dropped(self):
        for options in ({"enable": True}, {"enable": True, "config": ""}, {}):
            with self.subTest(options=options):
                node = copy.deepcopy(NODE)
                node["ech-opts"] = options
                self.assertEqual(w.validate_node(node)["ech-opts"], options)
                self.assertEqual(w.validate_live(w.config_bytes(node, 17890))["ech-opts"], options)

    def test_invalid_ech_types_are_rejected(self):
        for options in (None, [], "enabled", {"enable": "true"}, {"enable": 1},
                        {"config": None}, {"config": ["synthetic"]},
                        {"query-server-name": 42}, {"query-server-name": False}):
            with self.subTest(options=options):
                node = copy.deepcopy(NODE)
                node["ech-opts"] = options
                with self.assertRaises(w.WatchError):
                    w.validate_node(node)

    def test_unknown_fields_and_transport_mismatch_still_rejected(self):
        for change in ({"future-field": "synthetic"},
                       {"ech-opts": {"enable": True, "future-field": "synthetic"}},
                       {"network": "ws", "grpc-opts": {"grpc-service-name": "synthetic"}},
                       {"network": "grpc", "ws-opts": {"path": "/synthetic"}}):
            with self.subTest(change=change):
                node = copy.deepcopy(NODE)
                node.update(change)
                with self.assertRaises(w.WatchError):
                    w.validate_node(node)

    def test_ech_does_not_relax_tls_certificate_checks(self):
        for change in ({"tls": False}, {"skip-cert-verify": True}):
            with self.subTest(change=change):
                node = copy.deepcopy(NODE)
                node.update(change)
                with self.assertRaisesRegex(w.WatchError, "certificate verification"):
                    w.validate_node(node)

    def test_subscription_filter_preserves_valid_node_without_leaking_rejected_values(self):
        rejected_node = copy.deepcopy(NODE)
        rejected_node["name"] = "rejected-private-name"
        rejected_node["ech-opts"]["query-server-name"] = ["private-invalid-value"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subscription.yaml"
            path.write_text(w.yaml.safe_dump({"proxies": [rejected_node, NODE]}))
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                good, rejected = w.candidates(path)
            self.assertEqual(good, [(1, NODE)])
            self.assertEqual(rejected, [{"index": 0, "reason": "invalid transport option type"}])
            public_output = capture.getvalue() + json.dumps(rejected)
            for value in (rejected_node["name"], NODE["uuid"], NODE["server"],
                          NODE["ech-opts"]["config"], "private-invalid-value"):
                self.assertNotIn(value, public_output)

    def test_install_validates_and_preserves_ech_in_production_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = w.Settings(root / "mihomo", root / "live.yaml", root / "subscription.yaml",
                           root / "state", allowed_ips=("8.8.8.8",))
            w.private_dir(s.state_dir)
            old_node = copy.deepcopy(NODE)
            old_node.pop("ech-opts")
            old_raw = w.config_bytes(old_node, 17890)
            s.live_config.write_bytes(old_raw)
            runtime = mock.Mock()
            validated = []
            runtime.validate.side_effect = lambda path, folder: validated.append(w.validate_live(path.read_bytes()))
            health = mock.Mock(return_value={"okx_ts": 1800000000000, "exit_ip": "8.8.8.8"})
            watcher = w.Watcher(s, runtime=runtime, health=health,
                                now=lambda: 1800000000, sleep=lambda _: None)
            self.assertTrue(watcher.apply(w.validate_node(NODE), w.Snapshot.take(s.live_config)))
            self.assertEqual(validated, [NODE])
            self.assertEqual(w.validate_live(s.live_config.read_bytes()), NODE)
            runtime.restart.assert_called_once_with()
            self.assertEqual(next(s.state_dir.glob("backup-*.yaml")).read_bytes(), old_raw)


if __name__ == "__main__":
    unittest.main()
