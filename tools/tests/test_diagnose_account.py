"""Synthetic, offline error disclosure and request-scope regression tests."""
import io
import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnose_account as d
from g3core.okx import OKXClient


class Response:
    status = 200
    def __init__(self, payload): self.raw = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return self.raw[:limit]


class DiagnosticTests(unittest.TestCase):
    def test_reports_valid_ipv4_ipv6_only_for_whitelist_error(self):
        msg = "Your IP 2001:db8::1234 (192.0.2.1) is not whitelisted. token=SECRET"
        result = d.sanitized_error(401, json.dumps({"code": "50110", "msg": msg}).encode())
        self.assertEqual(result["reported_source_ips"], ["2001:db8::1234", "192.0.2.1"])
        self.assertNotIn("SECRET", json.dumps(result))
        for code in ("50105", "50111", "50113"):
            other = d.sanitized_error(401, json.dumps({"code": code, "msg": msg}).encode())
            self.assertEqual(other["reported_source_ips"], [])

    def test_untrusted_body_and_error_code_are_not_printed(self):
        for raw in (b"<html>SECRET</html>", b"[]", b'"SECRET"',
                    b'{"code":"SECRET","msg":"SECRET"}',
                    b'{"code":50110}', b"SECRET" * d.MAX_ERROR_BYTES):
            result = d.sanitized_error(401, raw)
            self.assertIsNone(result["exchange_code"])
            self.assertNotIn("SECRET", json.dumps(result))

    def factory(self, fail, requests):
        def create(cfg, credentials, orders):
            self.assertFalse(orders)
            def transport(req, timeout):
                requests.append(req)
                self.assertEqual(req.get_method(), "GET")
                if req.full_url.endswith("/public/time"):
                    return Response({"code": "0", "data": [{"ts": "1791453600000"}]})
                self.assertTrue(req.full_url.endswith("/account/config"))
                if fail:
                    raw = b'{"code":"50110","msg":"IP 2001:db8::1234 SECRET"}'
                    raise HTTPError(req.full_url, 401, "PRIVATE", {}, io.BytesIO(raw))
                return Response({"code": "0", "data": [{"uid": "PRIVATE-UID"}]})
            return OKXClient(origin=cfg["origin"], proxy=cfg["proxy"],
                             credentials=credentials, allow_orders=orders, transport=transport)
        return create

    def test_private_read_signed_no_account_contents_returned(self):
        cfg = {"origin": "https://openapi.okx.com", "proxy": "http://127.0.0.1:17890"}
        creds = {"api_key": "synthetic-key", "secret_key": "synthetic-secret", "passphrase": "synthetic-pass"}
        requests = []
        result = d.diagnose(cfg, creds, factory=self.factory(False, requests))
        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1].get_header("Ok-access-key"), "synthetic-key")
        for secret in (*creds.values(), "PRIVATE-UID"):
            self.assertNotIn(secret, json.dumps(result))

    def test_real_adapter_401_preserves_numeric_metadata_only(self):
        cfg = {"origin": "https://openapi.okx.com", "proxy": "http://127.0.0.1:17890"}
        creds = {"api_key": "synthetic-key", "secret_key": "synthetic-secret", "passphrase": "synthetic-pass"}
        requests = []
        result = d.diagnose(cfg, creds, factory=self.factory(True, requests))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["exchange_code"], "50110")
        self.assertEqual(result["orders_sent"], 0)
        self.assertNotIn("SECRET", json.dumps(result))


if __name__ == "__main__": unittest.main()
