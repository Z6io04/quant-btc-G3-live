"""Distinct public probe stages/codes; never expose HTTP bodies."""
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_watch as w


class ProbeErrorTests(unittest.TestCase):
    def test_empty_url_path_uses_root_before_query(self):
        for url, expected in [("https://api.ipify.org?format=json", "/?format=json"),
                              ("https://api4.ipify.org?format=json", "/?format=json"),
                              ("https://api64.ipify.org?format=json", "/?format=json"),
                              ("https://api.ipify.org", "/"),
                              (w.OKX_TIME, "/api/v5/public/time")]:
            response = mock.Mock(status=200)
            response.read.return_value = b'{"ip":"1.1.1.1"}'
            connection = mock.Mock()
            connection.getresponse.return_value = response
            with mock.patch.object(w.http.client, "HTTPSConnection", return_value=connection):
                w.https_json(url, 17891, 5)
            self.assertEqual(connection.request.call_args.args, ("GET", expected))

    def test_http_codes_and_body_limit_are_separate_and_redacted(self):
        for status, raw, expected in [(403, b"private-response", "HTTP status 403"),
                                       (429, b"private-response", "HTTP status 429"),
                                       (200, b"x" * 16385, "response too large")]:
            response = mock.Mock(status=status)
            response.read.return_value = raw
            connection = mock.Mock()
            connection.getresponse.return_value = response
            with mock.patch.object(w.http.client, "HTTPSConnection", return_value=connection):
                with self.assertRaises(w.WatchError) as caught:
                    w.https_json(w.OKX_TIME, 17891, 5)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("private-response", str(caught.exception))
            connection.close.assert_called_once()

    def test_timeout_and_tls_are_reported_without_exception_text(self):
        for error, expected in [(TimeoutError("private"), "timed out"),
                                (ssl.SSLCertVerificationError("private"), "certificate verification"),
                                (ssl.SSLError("private"), "TLS handshake")]:
            connection = mock.Mock()
            connection.request.side_effect = error
            with mock.patch.object(w.http.client, "HTTPSConnection", return_value=connection):
                with self.assertRaises(w.WatchError) as caught:
                    w.https_json(w.OKX_TIME, 17891, 5)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("private", str(caught.exception))

    def test_watcher_ip_gate_failure_is_not_reported_as_okx_failure(self):
        settings = w.Settings(Path("binary"), Path("live"), Path("sub"), Path("state"), allowed_ips=("1.1.1.1",))
        good = {"code": "0", "data": [{"ts": "1800000000000"}]}
        def fetch(url, *_):
            if url == w.OKX_TIME:
                return good
            raise w.WatchError("public probe HTTP status 403")
        with self.assertRaisesRegex(w.WatchError, "exit IP check: public probe HTTP status 403"):
            w.probe(settings, fetch=fetch, now=lambda: 1800000000)
        with self.assertRaisesRegex(w.WatchError, "OKX public check: public probe timed out"):
            w.probe(settings, fetch=mock.Mock(side_effect=w.WatchError("public probe timed out")))
