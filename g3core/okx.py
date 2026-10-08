"""G3 live-only, explicit-proxy OKX V5 adapter; no credentials are read from the environment.

Mutation results are acknowledgements, NOT fills. The execution layer must persist
its intent before calling ``write`` and reconcile any ``UncertainRequest`` with
orders, algos and positions before doing anything else. This adapter never retries
a POST. It exposes no withdrawal, transfer, leverage or account-mode mutation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


SPOT = "BTC-USDT"
SWAP = "BTC-USD-SWAP"
INSTRUMENTS = frozenset({SPOT, SWAP})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PUBLIC_READS = frozenset({
    "/api/v5/public/time", "/api/v5/public/instruments",
    "/api/v5/public/position-tiers", "/api/v5/public/funding-rate",
    "/api/v5/public/funding-rate-history", "/api/v5/public/mark-price",
    "/api/v5/market/ticker", "/api/v5/market/candles",
    "/api/v5/market/history-candles", "/api/v5/market/mark-price-candles",
    "/api/v5/market/history-mark-price-candles", "/api/v5/market/books",
})
PRIVATE_READS = frozenset({
    "/api/v5/account/instruments", "/api/v5/account/max-avail-size", "/api/v5/account/config", "/api/v5/account/balance",
    "/api/v5/account/positions", "/api/v5/account/leverage-info",
    "/api/v5/account/trade-fee", "/api/v5/account/bills",
    "/api/v5/account/bills-archive", "/api/v5/account/max-size",
    "/api/v5/trade/order", "/api/v5/trade/order-algo",
    "/api/v5/trade/orders-pending", "/api/v5/trade/orders-algo-pending",
    "/api/v5/trade/orders-history", "/api/v5/trade/orders-history-archive",
    "/api/v5/trade/orders-algo-history", "/api/v5/trade/fills",
    "/api/v5/trade/fills-history",
})
WRITES = frozenset({"/api/v5/trade/order", "/api/v5/trade/cancel-order"})
EXPIRING_WRITES = frozenset({"/api/v5/trade/order"})
OFFICIAL_HOSTS = frozenset({"openapi.okx.com", "www.okx.com", "us.okx.com", "eea.okx.com"})
CLIENT_ID = re.compile(r"[A-Za-z0-9]{1,32}\Z")


class OKXError(RuntimeError):
    """Base class. Text contains only our diagnostic code and an approved path."""

    def __init__(self, code, path=""):
        self.code = str(code) if re.fullmatch(r"[A-Z0-9_]{1,60}", str(code)) else "UNKNOWN"
        self.path = path if path in PUBLIC_READS | PRIVATE_READS | WRITES else ""
        super().__init__(f"{self.code} {self.path}".strip())


class APIError(OKXError):
    """A syntactically valid exchange rejection; does not include msg/sMsg."""


class NetworkError(OKXError):
    """Read request failed without a usable result."""


class ProtocolError(OKXError):
    """Read response does not have the expected structure."""


class UncertainRequest(OKXError):
    """A mutation may have been accepted. Do NOT repeat it blindly."""


class SafetyError(OKXError):
    """A request was rejected locally before transport."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, never forward signed headers to a redirected host.
        return None


class _RequiredProxy(urllib.request.ProxyHandler):
    """ProxyHandler without the implicit OS/environment proxy_bypass lookup.

    The stock handler still honors no_proxy even with explicit proxies. Trading
    traffic must never silently leave directly because the shell sets no_proxy=*.
    Only an already-validated unauthenticated HTTP loopback proxy is permitted.
    """

    def proxy_open(self, req, proxy, type):
        parsed = urllib.parse.urlsplit(proxy)
        req.set_proxy(parsed.netloc, "http")
        return None  # HTTPSHandler makes a CONNECT tunnel and verifies OKX TLS.


def _valid_client_id(value):
    if not isinstance(value, str) or CLIENT_ID.fullmatch(value) is None:
        raise SafetyError("INVALID_CLIENT_ID")
    return value


def _digits(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]+", value))


class OKXClient:
    """REST adapter scoped to BTC-USDT spot and BTC-USD-SWAP execution.

    ``transport`` is an optional callable(Request, timeout=seconds) returning a
    context manager with read(size) and status, for offline tests. Production uses
    urllib and the required loopback HTTP proxy. No API call occurs on construction.
    ``allow_orders`` defaults to False. A higher-level explicit
    activation/approval gate must set it only for an authorized run.
    """

    def __init__(self, mode="live", origin="https://openapi.okx.com",
                 proxy=None, credentials=None,
                 allow_orders=False, transport=None, timeout=10):
        if mode != "live":
            raise SafetyError("INVALID_MODE")
        try:
            parsed = urllib.parse.urlsplit(origin)
            origin_host = parsed.hostname
        except (ValueError, TypeError, AttributeError):
            raise SafetyError("INVALID_ORIGIN") from None
        if (parsed.scheme != "https" or origin_host not in OFFICIAL_HOSTS
                or parsed.netloc != origin_host or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise SafetyError("INVALID_ORIGIN")
        try:
            p = urllib.parse.urlsplit(proxy)
            proxy_port = p.port
        except (ValueError, TypeError, AttributeError):
            raise SafetyError("INVALID_PROXY") from None
        if (p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not proxy_port or p.username is not None or p.password is not None
                or p.path not in {"", "/"} or p.query or p.fragment):
            raise SafetyError("INVALID_PROXY")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise SafetyError("INVALID_TIMEOUT")
        if not isinstance(allow_orders, bool):
            raise SafetyError("INVALID_ORDER_PERMISSION")
        self.mode = mode
        self.origin = origin.rstrip("/")
        self.proxy = proxy.rstrip("/")
        self.allow_orders = allow_orders
        self.timeout = timeout
        self._credentials = None
        if credentials is not None:
            if (not isinstance(credentials, dict)
                    or set(credentials) != {"api_key", "secret_key", "passphrase"}):
                raise SafetyError("INVALID_CREDENTIALS")
            for val in credentials.values():
                if (not isinstance(val, str) or not val or len(val) > 4096
                        or any(ord(ch) < 32 or ord(ch) > 126 for ch in val)):
                    raise SafetyError("INVALID_CREDENTIALS")
            self._credentials = dict(credentials)
        self._server_anchor_ms = None
        self._last_sync_monotonic = None
        self._last_request_monotonic = None
        if transport is None:
            opener = urllib.request.build_opener(
                _RequiredProxy({"http": self.proxy, "https": self.proxy}),
                _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            self._transport = opener.open
        elif callable(transport):
            self._transport = transport
        else:
            raise SafetyError("INVALID_TRANSPORT")

    def now(self):
        """Server time advanced monotonically; local wall time only before sync."""
        if self._server_anchor_ms is not None and self._last_sync_monotonic is not None:
            return self._server_anchor_ms + round((time.monotonic() - self._last_sync_monotonic) * 1000)
        return int(time.time() * 1000)

    def sync_time(self):
        """Read public time, reject slow/invalid samples, update clock offset."""
        start_mono = time.monotonic()
        rows = self.read("/api/v5/public/time")
        end_mono = time.monotonic()
        elapsed = end_mono - start_mono
        if elapsed > min(self.timeout, 10):
            raise NetworkError("CLOCK_SAMPLE_TOO_SLOW", "/api/v5/public/time")
        if len(rows) != 1 or not isinstance(rows[0], dict) or not _digits(rows[0].get("ts")):
            raise ProtocolError("INVALID_CLOCK", "/api/v5/public/time")
        stamp = int(rows[0]["ts"])
        if not 1_577_836_800_000 <= stamp <= 4_102_444_800_000:
            raise ProtocolError("INVALID_CLOCK", "/api/v5/public/time")
        # The server sampled somewhere during the round trip. Estimate its time
        # at receipt with half the trip, then ignore subsequent wall-clock jumps.
        self._server_anchor_ms = stamp + round(elapsed * 500)
        self._last_sync_monotonic = end_mono
        return stamp

    def _headers(self, method, request_path, body, private, exp_time):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "BTC-G3-Execution/1", "x-simulated-trading": "0"}
        if private:
            if self._credentials is None:
                raise SafetyError("MISSING_CREDENTIALS", request_path.split("?", 1)[0])
            timestamp = datetime.fromtimestamp(self.now() / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            signing = (timestamp + method + request_path).encode("utf-8") + body
            sign = base64.b64encode(hmac.new(self._credentials["secret_key"].encode("utf-8"), signing, hashlib.sha256).digest()).decode("ascii")
            headers.update({"OK-ACCESS-KEY": self._credentials["api_key"],
                            "OK-ACCESS-PASSPHRASE": self._credentials["passphrase"],
                            "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-SIGN": sign})
        if exp_time is not None:
            headers["expTime"] = str(exp_time)
        return headers

    def _request(self, method, path, params=None, body=None, private=False, exp_time=None):
        mutation = method == "POST"
        encoded_body = b""
        if mutation:
            try:
                encoded_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise SafetyError("INVALID_JSON_BODY", path) from None
        if params is not None and not isinstance(params, dict):
            raise SafetyError("INVALID_PARAMETERS", path)
        try:
            query = urllib.parse.urlencode(params or {})
        except (TypeError, ValueError):
            raise SafetyError("INVALID_PARAMETERS", path) from None
        request_path = path + (("?" + query) if query else "")
        attempts = 1 if mutation else 3
        for attempt in range(attempts):
            # A low global per-client rate also keeps paginated account reads below
            # their 5 requests / 2 seconds endpoint limits.
            if self._last_request_monotonic is not None:
                delay = 0.42 - (time.monotonic() - self._last_request_monotonic)
                if delay > 0:
                    time.sleep(delay)
            headers = self._headers(method, request_path, encoded_body, private, exp_time)
            req = urllib.request.Request(self.origin + request_path,
                                         data=encoded_body if mutation else None,
                                         headers=headers, method=method)
            self._last_request_monotonic = time.monotonic()
            try:
                with self._transport(req, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if status != 200:
                    if mutation:
                        raise UncertainRequest("HTTP_RESPONSE", path)
                    if isinstance(status, int) and (status in {408, 425, 429} or status >= 500) and attempt + 1 < attempts:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise NetworkError("HTTP_RESPONSE", path)
            except urllib.error.HTTPError as exc:
                http_status = exc.code
                if exc.fp is not None:
                    try:
                        exc.close()
                    except OSError:
                        pass
                # Even a 4xx HTML proxy response need not be an exchange rejection.
                if mutation:
                    raise UncertainRequest("HTTP_" + str(http_status), path) from None
                if http_status in {408, 425, 429} or http_status >= 500:
                    if attempt + 1 < attempts:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                raise NetworkError("HTTP_" + str(http_status), path) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError,
                    OSError, http.client.HTTPException):
                if mutation:
                    raise UncertainRequest("TRANSPORT_UNKNOWN", path) from None
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise NetworkError("TRANSPORT_FAILED", path) from None
            error_type = UncertainRequest if mutation else ProtocolError
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise error_type("INVALID_RESPONSE_SIZE", path)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                raise error_type("INVALID_JSON_RESPONSE", path) from None
            if not isinstance(payload, dict) or not _digits(payload.get("code")):
                raise error_type("INVALID_RESPONSE", path)
            code = payload["code"]
            rows = payload.get("data")
            if code != "0":
                if mutation and code in {"50000", "50001", "50002", "50004", "50013", "50026"}:
                    raise UncertainRequest(code, path)
                if not mutation and code in {"50000", "50001", "50004", "50011", "50013", "50026"} and attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if mutation and code in {"1", "2"}:
                    self._check_mutation_rows(rows, path)
                    # An accepted item combined with a failure envelope is not a
                    # confirmed rejection. Reconcile instead of creating a retry.
                    raise UncertainRequest("INCONSISTENT_MUTATION_ACK", path)
                raise APIError(code, path)
            if not isinstance(rows, list):
                raise error_type("INVALID_DATA", path)
            if mutation:
                self._check_mutation_rows(rows, path)
                if (len(rows) != 1 or not _digits(rows[0].get("ordId"))
                        or ("clOrdId" in body and rows[0].get("clOrdId") != body["clOrdId"])):
                    raise UncertainRequest("MUTATION_ID_MISMATCH", path)
            return rows
        raise NetworkError("READ_RETRIES_EXHAUSTED", path)

    @staticmethod
    def _check_mutation_rows(rows, path):
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, dict) or not _digits(row.get("sCode")) for row in rows)):
            raise UncertainRequest("INVALID_MUTATION_ACK", path)
        codes = [row["sCode"] for row in rows]
        if all(code == "0" for code in codes):
            return
        if "0" in codes or any(code in {"50000", "50001", "50002", "50004", "50013", "50026"} for code in codes):
            raise UncertainRequest("PARTIAL_OR_UNKNOWN_RESULT", path)
        raise APIError(codes[0], path)

    def read(self, path, params=None, private=None):
        """GET only from allowlisted paths; private authentication inferred by path."""
        if private is None:
            private = path in PRIVATE_READS
        if not isinstance(private, bool) or path not in (PRIVATE_READS if private else PUBLIC_READS):
            raise SafetyError("READ_PATH_NOT_ALLOWED")
        return self._request("GET", path, params=params, private=private)

    def write(self, path, body, exp_time=None):
        """One bounded IOC/cancel attempt. Ack is never treated as execution."""
        if path not in WRITES:
            raise SafetyError("WRITE_PATH_NOT_ALLOWED")
        if not self.allow_orders:
            raise SafetyError("ORDERS_DISABLED", path)
        if self._credentials is None:
            raise SafetyError("MISSING_CREDENTIALS", path)
        if self._last_sync_monotonic is None or time.monotonic() - self._last_sync_monotonic > 300:
            raise SafetyError("CLOCK_SYNC_REQUIRED", path)
        if not isinstance(body, dict) or body.get("instId") not in INSTRUMENTS:
            raise SafetyError("INSTRUMENT_NOT_ALLOWED", path)
        if path == "/api/v5/trade/order":
            allowed = {"instId", "tdMode", "clOrdId", "side", "ordType", "sz", "px", "posSide", "reduceOnly"}
            if set(body) - allowed:
                raise SafetyError("ORDER_FIELD_NOT_ALLOWED", path)
            _valid_client_id(body.get("clOrdId"))
            if body.get("ordType") != "ioc" or body.get("side") not in {"buy", "sell"}:
                raise SafetyError("ORDER_TYPE_NOT_ALLOWED", path)
            for key in ("sz", "px"):
                _decimal(body.get(key), positive=True)
            if body["instId"] == SPOT:
                # OKX limit/IOC spot size is BTC base units; tgtCcy is market-only.
                if body.get("tdMode") != "cash" or "posSide" in body or "reduceOnly" in body:
                    raise SafetyError("POSITION_MODE_NOT_ALLOWED", path)
            elif (body.get("tdMode") != "cross" or body.get("posSide") != "net"
                  or not isinstance(body.get("reduceOnly"), bool)
                  or body["reduceOnly"] != (body["side"] == "buy")):
                # This carry strategy opens short only; every swap buy reduces.
                raise SafetyError("POSITION_MODE_NOT_ALLOWED", path)
            if exp_time is None:
                exp_time = self.now() + 5000
            if (isinstance(exp_time, bool) or not isinstance(exp_time, int)
                    or not self.now() < exp_time <= self.now() + 10000):
                raise SafetyError("INVALID_DEADLINE", path)
        else:
            if set(body) - {"instId", "clOrdId", "ordId"} or ("clOrdId" in body) == ("ordId" in body):
                raise SafetyError("INVALID_CANCEL_BODY", path)
            if "clOrdId" in body:
                _valid_client_id(body["clOrdId"])
            elif not _digits(body["ordId"]):
                raise SafetyError("INVALID_ORDER_ID", path)
            if exp_time is not None:
                raise SafetyError("DEADLINE_NOT_SUPPORTED", path)
        return self._request("POST", path, body=body, private=True, exp_time=exp_time)

    def get_order(self, inst_id, cl_ord_id):
        path = "/api/v5/trade/order"
        if inst_id not in INSTRUMENTS:
            raise SafetyError("INSTRUMENT_NOT_ALLOWED", path)
        try:
            rows = self.read(path, {"instId": inst_id, "clOrdId": _valid_client_id(cl_ord_id)})
        except APIError as exc:
            if exc.code == "51603":
                return None
            raise
        if not rows:
            return None
        if (len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get("clOrdId") != cl_ord_id or rows[0].get("instId") != inst_id):
            raise ProtocolError("ORDER_ID_MISMATCH", path)
        return rows[0]

    order = get_order

    def _paginate(self, path, params, id_key, max_pages=100):
        """Return complete requested slice or fail; never silently cap pages."""
        found = {}
        cursor = params.get("after")
        if cursor is not None and not _digits(str(cursor)):
            raise SafetyError("INVALID_PAGINATION_CURSOR", path)
        for _ in range(max_pages):
            query = dict(params, limit="100")
            if cursor is not None:
                query["after"] = str(cursor)
            rows = self.read(path, query)
            if not isinstance(rows, list) or len(rows) > 100:
                raise ProtocolError("INVALID_PAGE_SIZE", path)
            previous = cursor
            for row in rows:
                if not isinstance(row, dict) or not _digits(row.get(id_key)):
                    raise ProtocolError("INVALID_PAGINATION_ID", path)
                identifier = row[id_key]
                if previous is not None and int(identifier) >= int(previous):
                    raise ProtocolError("PAGINATION_NO_PROGRESS", path)
                previous = identifier
                if identifier in found and found[identifier] != row:
                    raise ProtocolError("PAGINATION_DUPLICATE_CONFLICT", path)
                found[identifier] = row
            if len(rows) < 100:
                return list(found.values())
            cursor = rows[-1][id_key]
        raise ProtocolError("PAGINATION_LIMIT_REACHED", path)

    def pending(self):
        # No instrument filter: unexpected manual/other-instrument orders matter.
        return self._paginate("/api/v5/trade/orders-pending", {}, "ordId")

    pending_orders = pending

    def pending_algos(self):
        found = {}
        for kind in ("conditional", "oco", "trigger", "move_order_stop", "chase", "twap", "smart_iceberg", "iceberg"):
            rows = self._paginate("/api/v5/trade/orders-algo-pending", {"ordType": kind}, "algoId")
            for row in rows:
                key = row["algoId"]
                if key in found and found[key] != row:
                    raise ProtocolError("ALGO_DUPLICATE_CONFLICT", "/api/v5/trade/orders-algo-pending")
                found[key] = row
        return list(found.values())

    def _window(self, begin, end):
        now = self.now()
        end = now if end is None else end
        begin = end - 2 * 86400000 if begin is None else begin
        if (isinstance(begin, bool) or isinstance(end, bool) or not isinstance(begin, int)
                or not isinstance(end, int) or not 0 <= begin <= end <= now):
            raise SafetyError("INVALID_HISTORY_WINDOW")
        if now - begin > 88 * 86400000:
            raise SafetyError("HISTORY_TOO_OLD")
        return begin, end

    def fills(self, inst_type, inst_id=None, after=None, begin=None, end=None):
        """Complete fill slice by billId, including archived + recent overlap.

        InstType deliberately accepts all relevant account types, so the caller can
        audit foreign activity. Raw rows are retained, including ADL/liquidation.
        """
        if inst_type not in {"SPOT", "MARGIN", "SWAP", "FUTURES", "OPTION"}:
            raise SafetyError("INVALID_FILL_SCOPE")
        begin, end = self._window(begin, end)
        recent_begin = max(begin, self.now() - 2 * 86400000)
        sources = [("/api/v5/trade/fills", recent_begin)] if recent_begin <= end else []
        if recent_begin > begin:
            sources.insert(0, ("/api/v5/trade/fills-history", begin))
        found = {}
        for path, source_begin in sources:
            params = {"instType": inst_type, "begin": str(source_begin), "end": str(end)}
            if inst_id is not None:
                params["instId"] = inst_id
            if after is not None:
                params["after"] = str(after)
            for row in self._paginate(path, params, "billId"):
                if (row.get("instType") != inst_type or (inst_id is not None and row.get("instId") != inst_id)
                        or not _digits(row.get("ts")) or not source_begin <= int(row["ts"]) <= end
                        or not _digits(row.get("fillTime")) or not 0 < int(row["fillTime"]) <= end
                        or row.get("side") not in {"buy", "sell"}
                        or not isinstance(row.get("instId"), str) or not row["instId"]
                        or not isinstance(row.get("ordId"), str)
                        or not isinstance(row.get("clOrdId"), str)
                        or not isinstance(row.get("tradeId"), str)
                        or not isinstance(row.get("feeCcy"), str) or not row["feeCcy"]):
                    raise ProtocolError("FILL_RECORD_MISMATCH", path)
                for key in ("fillSz", "fillPx", "fee", "fillPnl"):
                    try:
                        _decimal(row.get(key), positive=key in {"fillSz", "fillPx"})
                    except SafetyError:
                        raise ProtocolError("INVALID_FILL_NUMBER", path) from None
                key = row["billId"]
                if key in found and found[key] != row:
                    raise ProtocolError("FILL_DUPLICATE_CONFLICT", path)
                found[key] = row
        return sorted(found.values(), key=lambda row: (int(row["fillTime"]), int(row["billId"])))

    def bills(self, begin=None, end=None):
        """Complete account-wide money records; funding is not inferred from rates."""
        begin, end = self._window(begin, end)
        recent_begin = max(begin, self.now() - 6 * 86400000)
        sources = [("/api/v5/account/bills", recent_begin)] if recent_begin <= end else []
        if recent_begin > begin:
            sources.insert(0, ("/api/v5/account/bills-archive", begin))
        found = {}
        for path, source_begin in sources:
            params = {"begin": str(source_begin), "end": str(end)}
            for row in self._paginate(path, params, "billId"):
                if (not _digits(row.get("ts")) or not source_begin <= int(row["ts"]) <= end
                        or not isinstance(row.get("ccy"), str) or not row["ccy"]
                        or not _digits(row.get("type")) or not _digits(row.get("subType"))):
                    raise ProtocolError("BILL_RECORD_MISMATCH", path)
                try:
                    _decimal(row.get("balChg"))
                except SafetyError:
                    raise ProtocolError("INVALID_BILL_NUMBER", path) from None
                key = row["billId"]
                if key in found and found[key] != row:
                    raise ProtocolError("BILL_DUPLICATE_CONFLICT", path)
                found[key] = row
        return sorted(found.values(), key=lambda row: (int(row["ts"]), int(row["billId"])))


def _decimal(value, positive=False):
    if not isinstance(value, str) or len(value) > 100 or re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value) is None:
        raise SafetyError("INVALID_DECIMAL")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise SafetyError("INVALID_DECIMAL") from None
    if not number.is_finite() or (positive and number <= 0):
        raise SafetyError("INVALID_DECIMAL")
    return number
