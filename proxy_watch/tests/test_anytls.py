"""Offline direct-AnyTLS schema tests. All credentials below are synthetic."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_watch as w

NODE = {"name": "synthetic-anytls", "type": "anytls", "server": "node.example.invalid", "port": 443,
        "password": "synthetic-secret-never-log", "sni": "cert.example.invalid", "udp": True,
        "client-fingerprint": "chrome", "alpn": ["h2", "http/1.1"], "skip-cert-verify": False}
VLESS = {"name": "synthetic-vless", "type": "vless", "server": "vless.example.invalid", "port": 443,
         "uuid": "00000000-0000-4000-8000-000000000001", "tls": True}

class AnyTLSTests(unittest.TestCase):
    def test_all_values_survive_candidate_and_live_roundtrip(self):
        node = copy.deepcopy(NODE)
        node.update({"idle-session-check-interval": 30, "idle-session-timeout": 30,
                     "min-idle-session": 0, "client-metadata": "test", "fingerprint": "synthetic",
                     "name-cert-verify": "cert.example.invalid", "ech-opts": {"enable": False}})
        for original in (node, VLESS):
            accepted = w.validate_node(original)
            for port in (17890, 17891):
                self.assertEqual(w.parse_yaml(w.config_bytes(accepted, port))["proxies"][0], original)
            self.assertEqual(w.validate_live(w.config_bytes(accepted, 17890)), original)
        accepted = w.validate_node(node)
        accepted["alpn"].append("changed")
        self.assertEqual(node["alpn"], ["h2", "http/1.1"])

    def test_password_required_and_types_exact(self):
        for change in ({"password": ""}, {"password": None}, {"password": 42},
                       {"port": True}, {"udp": 1}, {"alpn": "h2"},
                       {"min-idle-session": True}, {"idle-session-timeout": -1},
                       {"idle-session-check-interval": "30"}):
            with self.subTest(change=change), self.assertRaises(w.WatchError):
                w.validate_node(dict(NODE, **change))
        missing = dict(NODE)
        del missing["password"]
        with self.assertRaisesRegex(w.WatchError, "missing AnyTLS password"):
            w.validate_node(missing)

    def test_certificate_verification_remains_required(self):
        for original in (NODE, VLESS):
            with self.assertRaisesRegex(w.WatchError, "certificate verification"):
                w.validate_node(dict(original, **{"skip-cert-verify": True}))
            with self.assertRaises(w.WatchError):
                w.validate_node(dict(original, **{"skip-cert-verify": "false"}))
        native_tls = dict(NODE)
        del native_tls["skip-cert-verify"]
        self.assertEqual(w.validate_node(native_tls), native_tls)
        with self.assertRaisesRegex(w.WatchError, "certificate verification"):
            w.validate_node(dict(VLESS, tls=False))

    def test_protocols_do_not_accept_each_others_fields_or_dependencies(self):
        for change in ({"tls": True}, {"uuid": VLESS["uuid"]}, {"flow": "x"},
                       {"network": "ws"}, {"reality-opts": {"public-key": "x"}},
                       {"dialer-proxy": "secret-dependency"}, {"shadow-tls-opts": {}},
                       {"unrecognized-secret-field": "private"}):
            with self.subTest(change=change), self.assertRaisesRegex(w.WatchError, "unsupported AnyTLS"):
                w.validate_node(dict(NODE, **change))
        for field in ("password", "sni"):
            with self.assertRaisesRegex(w.WatchError, "unsupported VLESS"):
                w.validate_node(dict(VLESS, **{field: "private"}))
        with self.assertRaisesRegex(w.WatchError, "unsupported protocol"):
            w.validate_node(dict(NODE, type="trojan"))

    def test_mixed_candidates_are_filtered_without_secret_reasons(self):
        bad = copy.deepcopy(dict(NODE, name="private-rejected-name", **{"skip-cert-verify": True}))
        unknown = copy.deepcopy(dict(NODE, name="private-unsupported-name", **{"private-key-name": "private-key-value"}))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "subscription.yaml"
            path.write_text(w.yaml.safe_dump({"proxies": [VLESS, bad, NODE, unknown]}))
            good, rejected = w.candidates(path)
        self.assertEqual(good, [(0, VLESS), (2, NODE)])
        self.assertEqual([item["index"] for item in rejected], [1, 3])
        reasons = json.dumps(rejected)
        for secret in (NODE["password"], NODE["server"], NODE["sni"], bad["name"], unknown["name"], "private-key-value", "private-key-name"):
            self.assertNotIn(secret, reasons)
        self.assertIn("certificate verification", reasons)

    def test_native_mihomo_validation_still_required_and_failure_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            s = w.Settings(root / "mihomo", root / "live.yaml", root / "subscription.yaml", root / "state")
            w.private_dir(s.state_dir)
            config = root / "candidate.yaml"
            config.write_bytes(w.config_bytes(w.validate_node(NODE), 17891))
            with mock.patch.object(w.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
                with self.assertRaisesRegex(w.WatchError, "full-config validation failed"):
                    w.Runtime(s).validate(config, root)
                self.assertEqual(run.call_args.args[0], [str(s.mihomo_binary), "-t", "-d", str(root), "-f", str(config)])
                self.assertEqual(run.call_args.kwargs["stdout"], w.subprocess.DEVNULL)

if __name__ == "__main__":
    unittest.main()
