#!/usr/bin/env python3
"""Optional, local-only Mihomo watcher. No exchange keys or order endpoints."""
import argparse
import contextlib
import copy
import dataclasses
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

import yaml

OKX_TIME = "https://openapi.okx.com/api/v5/public/time"
MAX_FILE = 4 * 1024 * 1024


class WatchError(Exception):
    """Messages contain categories, never subscription credentials or HTTP bodies."""


class Conflict(WatchError):
    pass


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise WatchError("YAML aliases are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise WatchError("YAML keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def read_bytes(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise WatchError("not a bounded regular file")
        return handle.read(MAX_FILE + 1), info


def parse_yaml(raw):
    if len(raw) > MAX_FILE:
        raise WatchError("YAML file too large")
    try:
        doc = yaml.load(raw, Loader=StrictLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        raise WatchError("invalid or unsupported YAML") from exc
    if not isinstance(doc, dict):
        raise WatchError("YAML root must be a mapping")
    return doc


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WatchError("state directory must be owned by this user with mode 0700")


def atomic_write(path, raw):
    if path.is_symlink():
        raise WatchError("refusing symlink write")
    fd, name = tempfile.mkstemp(prefix=".watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, sort_keys=True) + "\n").encode())


@dataclasses.dataclass(frozen=True)
class Snapshot:
    raw: bytes
    signature: tuple

    @classmethod
    def take(cls, path):
        raw, info = read_bytes(path)
        return cls(raw, (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, digest(raw)))

    def require_current(self, path):
        if self.signature != Snapshot.take(path).signature:
            raise Conflict("live config changed; refusing overwrite")


@dataclasses.dataclass
class Settings:
    mihomo_binary: Path
    live_config: Path
    subscription: Path
    state_dir: Path
    allowed_ips: tuple = ()
    service: str = "mihomo-proxy.service"
    interval_seconds: int = 60
    failure_threshold: int = 3
    full_scan_cooldown_seconds: int = 3600
    probe_timeout_seconds: int = 8
    candidate_startup_seconds: int = 8
    candidate_port: int = 17891
    max_time_skew_seconds: int = 120
    okx_time_url: str = OKX_TIME
    ip_url: str = "https://api.ipify.org?format=json"

    @classmethod
    def load(cls, path):
        doc = parse_yaml(read_bytes(path)[0])
        keys = {field.name for field in dataclasses.fields(cls)}
        if set(doc) - keys:
            raise WatchError("unknown watcher settings")
        required = {"mihomo_binary", "live_config", "subscription", "state_dir"}
        if not required <= set(doc):
            raise WatchError("missing watcher paths")
        for key in required:
            if not isinstance(doc[key], str) or not doc[key]:
                raise WatchError("watcher paths must be nonempty strings")
            doc[key] = Path(doc[key]).expanduser().absolute()
        allowed = doc.get("allowed_ips", [])
        if not isinstance(allowed, list):
            raise WatchError("allowed_ips must be an explicit list of public IPs")
        try:
            ips = [ipaddress.ip_address(ip) for ip in allowed if isinstance(ip, str)]
            if len(ips) != len(allowed) or any(not ip.is_global for ip in ips):
                raise ValueError()
        except ValueError as exc:
            raise WatchError("allowed_ips requires exact public IPs; no CIDR") from exc
        doc["allowed_ips"] = tuple(str(ip) for ip in ips)
        result = cls(**doc)
        bounds = {"interval_seconds": (60, 3600), "failure_threshold": (3, 20),
                  "full_scan_cooldown_seconds": (3600, 86400),
                  "probe_timeout_seconds": (1, 10), "candidate_startup_seconds": (1, 10),
                  "candidate_port": (1024, 65535), "max_time_skew_seconds": (10, 300)}
        for key, (low, high) in bounds.items():
            value = getattr(result, key)
            if type(value) is not int or not low <= value <= high:
                raise WatchError("invalid setting: " + key)
        if result.candidate_port == 17890 or result.service != "mihomo-proxy.service":
            raise WatchError("fixed live port/service must not be changed")
        if result.live_config in (result.subscription, result.mihomo_binary) or result.subscription == result.mihomo_binary:
            raise WatchError("configuration and binary paths must differ")
        if result.ip_url not in ("https://api.ipify.org?format=json", "https://api4.ipify.org?format=json", "https://api64.ipify.org?format=json"):
            raise WatchError("unsupported public HTTPS IP endpoint")
        if result.okx_time_url not in (OKX_TIME, "https://www.okx.com/api/v5/public/time"):
            raise WatchError("unsupported OKX time endpoint")
        return result


# Bounded direct VLESS and AnyTLS schemas. Unknown fields fail closed rather
# than being silently removed; every accepted node value is copied intact.
STRING_FIELDS = {"name", "type", "server", "uuid", "servername", "flow", "network",
                 "client-fingerprint", "fingerprint", "name-cert-verify", "packet-encoding", "encryption"}
BOOL_FIELDS = {"udp", "tls", "skip-cert-verify"}
ANYTLS_STRING_FIELDS = {"name", "type", "server", "password", "sni",
                        "client-fingerprint", "fingerprint", "name-cert-verify", "client-metadata"}
ANYTLS_BOOL_FIELDS = {"udp", "skip-cert-verify"}
ANYTLS_INT_FIELDS = {"idle-session-check-interval", "idle-session-timeout", "min-idle-session"}
OPTION_FIELDS = {
    "ech-opts": {"enable": bool, "config": str, "query-server-name": str},
    "reality-opts": {"public-key": str, "short-id": str},
    "ws-opts": {"path": str, "headers": dict, "max-early-data": int, "early-data-header-name": str},
    "grpc-opts": {"grpc-service-name": str},
    "h2-opts": {"host": list, "path": str},
    "http-opts": {"method": str, "path": list, "headers": dict},
    "smux": {"enabled": bool, "protocol": str, "max-connections": int, "min-streams": int,
             "max-streams": int, "statistic": bool, "only-tcp": bool, "padding": bool},
}


def string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_node(node):
    if not isinstance(node, dict):
        raise WatchError("node must be a mapping")
    protocol = node.get("type")
    if protocol not in ("vless", "anytls"):
        raise WatchError("unsupported protocol; only direct VLESS and AnyTLS are supported")
    # AnyTLS has native TLS and no explicit tls:true field. Its password/SNI
    # must not accidentally be accepted by VLESS, or vice versa.
    strings = STRING_FIELDS if protocol == "vless" else ANYTLS_STRING_FIELDS
    booleans = BOOL_FIELDS if protocol == "vless" else ANYTLS_BOOL_FIELDS
    integers = set() if protocol == "vless" else ANYTLS_INT_FIELDS
    options = OPTION_FIELDS if protocol == "vless" else {"ech-opts": OPTION_FIELDS["ech-opts"]}
    allowed = strings | booleans | integers | set(options) | {"port", "alpn"}
    if set(node) - allowed:
        raise WatchError("unsupported " + ("VLESS" if protocol == "vless" else "AnyTLS") +
                         " node fields or indirect dependency")
    for key in strings & set(node):
        if not isinstance(node[key], str) or any(ord(char) < 32 for char in node[key]):
            raise WatchError("invalid node string field")
    if not {"name", "type", "server", "port"} <= set(node):
        raise WatchError("missing node fields")
    if not node["name"] or "," in node["name"] or node["name"].upper() in {"DIRECT", "REJECT", "GLOBAL"}:
        raise WatchError("unsupported node name")
    if not node["server"] or any(char.isspace() or char in "/?#@" for char in node["server"]):
        raise WatchError("invalid node server")
    if type(node["port"]) is not int or not 1 <= node["port"] <= 65535:
        raise WatchError("invalid node port")
    for key in booleans & set(node):
        if type(node[key]) is not bool:
            raise WatchError("invalid node boolean")
    if node.get("skip-cert-verify", False):
        raise WatchError("TLS certificate verification is required; skip-cert-verify:true is refused")
    if protocol == "vless":
        if "uuid" not in node:
            raise WatchError("missing VLESS uuid")
        try:
            uuid.UUID(node["uuid"])
        except (ValueError, AttributeError) as exc:
            raise WatchError("invalid VLESS uuid") from exc
        if node.get("tls") is not True:
            raise WatchError("TLS with certificate verification is required")
    else:
        if not node.get("password"):
            raise WatchError("missing AnyTLS password")
        for key in integers & set(node):
            if type(node[key]) is not int or not 0 <= node[key] <= 2147483647:
                raise WatchError("invalid AnyTLS session integer")
    network = node.get("network", "tcp")
    if network not in {"tcp", "ws", "grpc", "h2", "http"}:
        raise WatchError("unsupported transport")
    if "alpn" in node and not string_list(node["alpn"]):
        raise WatchError("invalid ALPN list")
    for key, fields in options.items():
        if key not in node:
            continue
        opts = node[key]
        if not isinstance(opts, dict) or set(opts) - set(fields):
            raise WatchError("unsupported transport option")
        if key.endswith("-opts") and key not in {"reality-opts", "ech-opts"} and key != network + "-opts":
            raise WatchError("transport options do not match network")
        for opt, kind in fields.items():
            if opt not in opts:
                continue
            value = opts[opt]
            if type(value) is not kind:
                raise WatchError("invalid transport option type")
            if kind is int and value < 0:
                raise WatchError("negative transport option")
            if kind is list and not string_list(value):
                raise WatchError("invalid transport list")
            if kind is dict:
                if not all(isinstance(k, str) and (isinstance(v, str) or (key == "http-opts" and string_list(v))) for k, v in value.items()):
                    raise WatchError("invalid transport headers")
        if key == "reality-opts" and not opts.get("public-key"):
            raise WatchError("Reality public key is required")
    return copy.deepcopy(node)


def candidates(path):
    doc = parse_yaml(read_bytes(path)[0])
    nodes = doc.get("proxies")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 200:
        raise WatchError("subscription requires 1..200 inline proxies")
    good, rejected, names = [], [], set()
    for index, node in enumerate(nodes):
        try:
            accepted = validate_node(node)
            if accepted["name"] in names:
                raise WatchError("duplicate node name")
            names.add(accepted["name"])
            good.append((index, accepted))
        except WatchError as exc:
            rejected.append({"index": index, "reason": str(exc)})
    return good, rejected


def config_for(node, port):
    return {"mixed-port": port, "bind-address": "127.0.0.1", "allow-lan": False,
            "mode": "rule", "log-level": "warning", "ipv6": False,
            "external-controller": "", "dns": {"enable": False}, "tun": {"enable": False},
            "profile": {"store-selected": False, "store-fake-ip": False},
            "proxies": [copy.deepcopy(node)], "rules": ["MATCH," + node["name"]]}


def config_bytes(node, port):
    return yaml.safe_dump(config_for(node, port), allow_unicode=True, sort_keys=False).encode()


def validate_live(raw):
    doc = parse_yaml(raw)
    allowed = {"port", "mixed-port", "bind-address", "allow-lan", "mode", "log-level", "ipv6", "external-controller", "external-controller-tls", "external-controller-unix", "dns", "tun", "profile", "proxies", "rules"}
    if set(doc) - allowed:
        raise WatchError("live config contains unsupported global fields")
    if doc.get("allow-lan", False) is not False or doc.get("bind-address", "127.0.0.1") not in {"127.0.0.1", "*"}:
        raise WatchError("live config must be loopback-only")
    if doc.get("mode", "rule") != "rule" or any(doc.get(key) for key in ("external-controller", "external-controller-tls", "external-controller-unix")):
        raise WatchError("live config requires rule mode and disabled controllers")
    if (doc.get("mixed-port") == 17890) + (doc.get("port") == 17890) != 1:
        raise WatchError("live config must expose exactly one fixed port 17890")
    if any(doc.get(key, 0) not in (0, 17890) for key in ("mixed-port", "port")):
        raise WatchError("extra live listener is unsupported")
    for key in ("dns", "tun"):
        if key in doc and (not isinstance(doc[key], dict) or doc[key] != {"enable": False}):
            raise WatchError("custom DNS/TUN configuration is unsupported")
    if "profile" in doc and (not isinstance(doc["profile"], dict) or set(doc["profile"]) - {"store-selected", "store-fake-ip"} or any(type(value) is not bool for value in doc["profile"].values())):
        raise WatchError("unsupported live profile")
    if not isinstance(doc.get("proxies"), list) or len(doc["proxies"]) != 1:
        raise WatchError("live config must contain exactly one direct VLESS or AnyTLS node")
    node = validate_node(doc["proxies"][0])
    if doc.get("rules") != ["MATCH," + node["name"]]:
        raise WatchError("live rules must route all requests through the single node")
    return node


def https_json(url, port, timeout):
    """Always CONNECT via loopback; environmental NO_PROXY cannot bypass this."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise WatchError("only public HTTPS probes are supported")
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, context=ssl.create_default_context())
    conn.set_tunnel(parsed.hostname, 443)
    try:
        conn.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                     headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "btc-G3-proxy-watch/1"})
        response = conn.getresponse()
        raw = response.read(16385)
        if response.status != 200:
            raise WatchError("public probe HTTP status " + str(int(response.status)))
        if len(raw) > 16384:
            raise WatchError("public probe response too large")
        return json.loads(raw)
    except ssl.SSLCertVerificationError as exc:
        raise WatchError("public probe TLS certificate verification failure") from exc
    except ssl.SSLError as exc:
        raise WatchError("public probe TLS handshake failure") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise WatchError("public probe timed out") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise WatchError("public probe transport/JSON failure") from exc
    finally:
        conn.close()


def probe(settings, port=17890, fetch=https_json, now=time.time):
    try:
        payload = fetch(settings.okx_time_url, port, settings.probe_timeout_seconds)
    except WatchError as exc:
        raise WatchError("OKX public check: " + str(exc)) from None
    try:
        stamp = payload["data"][0]["ts"]
        if payload["code"] != "0" or not isinstance(stamp, str) or not stamp.isdigit():
            raise ValueError()
        milliseconds = int(stamp)
        if abs(milliseconds / 1000 - now()) > settings.max_time_skew_seconds:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise WatchError("OKX business code/timestamp failure") from exc
    ip = None
    if settings.allowed_ips:
        try:
            payload = fetch(settings.ip_url, port, settings.probe_timeout_seconds)
        except WatchError as exc:
            raise WatchError("exit IP check: " + str(exc)) from None
        try:
            ip = str(ipaddress.ip_address(payload["ip"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise WatchError("invalid public exit IP response") from exc
        if ip not in settings.allowed_ips:
            raise WatchError("exit IP not in allowed_ips")
    return {"okx_ts": milliseconds, "exit_ip": ip, "ip_gate_configured": bool(settings.allowed_ips)}


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise WatchError("candidate port is occupied; no process was stopped") from exc


class Runtime:
    def __init__(self, settings):
        self.settings = settings

    def validate(self, config, workdir):
        try:
            result = subprocess.run([str(self.settings.mihomo_binary), "-t", "-d", str(workdir), "-f", str(config)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            if result.returncode:
                raise WatchError("Mihomo full-config validation failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WatchError("Mihomo full-config validator unavailable") from exc

    @contextlib.contextmanager
    def candidate(self, node):
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=self.settings.state_dir) as tmp:
            folder = Path(tmp)
            config = folder / "config.yaml"
            atomic_write(config, config_bytes(node, self.settings.candidate_port))
            self.validate(config, folder)
            port_free(self.settings.candidate_port)
            proc = None
            with open(folder / "mihomo.log", "wb") as output:
                os.chmod(folder / "mihomo.log", 0o600)
                try:
                    proc = subprocess.Popen([str(self.settings.mihomo_binary), "-d", str(folder), "-f", str(config)],
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=output)
                    deadline = time.monotonic() + self.settings.candidate_startup_seconds
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            raise WatchError("candidate process exited")
                        try:
                            with socket.create_connection(("127.0.0.1", self.settings.candidate_port), timeout=0.2):
                                break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise WatchError("candidate listener startup timeout")
                    yield self.settings.candidate_port
                finally:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=3)

    def restart(self):
        try:
            subprocess.run(["systemctl", "--user", "restart", self.settings.service],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WatchError("user service restart failed") from exc


class Watcher:
    def __init__(self, settings, runtime=None, health=None, now=time.time, sleep=time.sleep):
        self.s = settings
        self.runtime = runtime or Runtime(settings)
        self.health = health or (lambda port: probe(settings, port))
        self.now = now
        self.sleep = sleep
        self.last_rollback_healthy = False
        self.state_path = settings.state_dir / "state.json"
        self.journal_path = settings.state_dir / "transaction.json"
        self.state = {"version": 1, "failures": 0, "next_scan_at": 0}
        if self.state_path.exists():
            try:
                state = json.loads(read_bytes(self.state_path)[0])
                if state["version"] != 1 or type(state["failures"]) is not int or not 0 <= state["failures"] <= settings.failure_threshold or type(state["next_scan_at"]) not in (int, float) or not 0 <= state["next_scan_at"] < 1e12:
                    raise ValueError()
                self.state = state
            except (KeyError, TypeError, ValueError) as exc:
                raise WatchError("invalid persistent state; manual review required") from exc

    def event(self, name, **data):
        entry = {"at": int(self.now()), "event": name, **data}
        path = self.s.state_dir / "events.jsonl"
        # Bounded local log; no webhook, email, or chat notification.
        if path.exists() and path.stat().st_size > 1024 * 1024:
            os.replace(path, self.s.state_dir / "events.previous.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def save(self):
        write_json(self.state_path, self.state)

    def healthy(self, port=17890):
        try:
            return self.health(port)
        except WatchError:
            return None

    def verify_live(self):
        # Type=simple restart completion precedes listener readiness. Three
        # bounded real probes allow transient startup failure without rollback.
        for attempt in range(3):
            if self.healthy() is not None:
                return True
            if attempt < 2:
                self.sleep(2)
        return False

    @contextlib.contextmanager
    def current_monitor(self, timing=None):
        """Continue the public current-route probe while candidates are tested."""
        stop, recovered = threading.Event(), threading.Event()
        timing = timing if timing is not None else {"next": time.monotonic() + self.s.interval_seconds}

        def monitor():
            while not stop.wait(max(0, timing["next"] - time.monotonic())):
                timing["next"] = time.monotonic() + self.s.interval_seconds
                if self.healthy() is not None:
                    recovered.set()
                    return

        worker = threading.Thread(target=monitor, name="current-public-probe", daemon=True)
        worker.start()
        try:
            yield recovered
        finally:
            stop.set()
            # HTTP operations have bounded timeouts; stop before changing 17890.
            worker.join()

    def clear_journal(self):
        self.journal_path.unlink()
        fd = os.open(self.s.state_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def recover(self):
        """Authorized run/once resumes an interrupted transaction conservatively."""
        if not self.journal_path.exists():
            return
        try:
            journal = json.loads(read_bytes(self.journal_path)[0])
            name = journal["backup"]
            if not isinstance(name, str) or Path(name).name != name or not name.startswith("backup-"):
                raise ValueError()
            backup = read_bytes(self.s.state_dir / name)[0]
            if digest(backup) != journal["old_sha"]:
                raise ValueError()
            current = Snapshot.take(self.s.live_config)
            if digest(current.raw) not in {journal["old_sha"], journal["new_sha"]}:
                raise Conflict("pending transaction conflicts with manual config; review required")
            current.require_current(self.s.live_config)
            atomic_write(self.s.live_config, backup)
            self.runtime.restart()
            self.clear_journal()
            self.event("crash_rollback", healthy=self.verify_live())
        except (KeyError, TypeError, ValueError) as exc:
            raise WatchError("invalid transaction journal; manual review required") from exc

    def apply(self, node, original):
        self.last_rollback_healthy = False
        original.require_current(self.s.live_config)
        raw = config_bytes(node, 17890)
        # Validate the complete production config too, not just the staged port.
        with tempfile.TemporaryDirectory(prefix="validate-", dir=self.s.state_dir) as tmp:
            config = Path(tmp) / "config.yaml"
            atomic_write(config, raw)
            self.runtime.validate(config, Path(tmp))
        original.require_current(self.s.live_config)
        backup_name = "backup-" + str(time.time_ns()) + ".yaml"
        atomic_write(self.s.state_dir / backup_name, original.raw)
        journal = {"version": 1, "backup": backup_name, "old_sha": digest(original.raw), "new_sha": digest(raw)}
        write_json(self.journal_path, journal)
        # Guard changes made during validation/backup/journaling.
        try:
            original.require_current(self.s.live_config)
        except Conflict:
            self.clear_journal()
            raise
        atomic_write(self.s.live_config, raw)
        installed = Snapshot.take(self.s.live_config)
        if installed.raw != raw:
            raise Conflict("live config changed during installation; review required")
        try:
            self.runtime.restart()
            if not self.verify_live():
                raise WatchError("production probe failed after restart")
            installed.require_current(self.s.live_config)
            self.clear_journal()
            self.event("switch_verified", node_sha=digest(yaml.safe_dump(node).encode())[:16])
            return True
        except WatchError:
            # Never overwrite an uncooperative manual edit with an old backup.
            installed.require_current(self.s.live_config)
            atomic_write(self.s.live_config, original.raw)
            try:
                self.runtime.restart()
            except WatchError:
                self.event("rollback_restart_failed", manual_review_required=True)
                raise
            rollback_ok = self.verify_live()
            self.last_rollback_healthy = rollback_ok
            self.clear_journal()
            self.event("rollback", healthy=rollback_ok)
            return False

    def tick(self):
        if self.healthy() is not None:
            self.state["failures"] = 0
            self.state["next_scan_at"] = 0
            self.state["last_healthy_at"] = int(self.now())
            self.save()
            return "healthy"
        self.state["failures"] = min(self.s.failure_threshold, self.state["failures"] + 1)
        self.save()
        if self.state["failures"] < self.s.failure_threshold:
            return "waiting_for_failures"
        if not self.s.allowed_ips:
            return "blocked_no_allowed_ips"
        if self.journal_path.exists():
            return "blocked_pending_transaction"
        if self.now() < self.state["next_scan_at"]:
            return "cooldown"
        # Persist before any scan: termination/restarts cannot cause a scan storm.
        self.state["next_scan_at"] = self.now() + self.s.full_scan_cooldown_seconds
        self.state["last_scan_at"] = int(self.now())
        self.save()
        original = Snapshot.take(self.s.live_config)
        validate_live(original.raw)
        nodes, rejected = candidates(self.s.subscription)
        self.event("scan_start", candidates=len(nodes), rejected=rejected)
        timing = {"next": time.monotonic() + self.s.interval_seconds}
        for index, node in nodes:
            passed = False
            with self.current_monitor(timing) as recovered:
                try:
                    with self.runtime.candidate(node) as port:
                        self.health(port)
                    passed = True
                except WatchError as exc:
                    self.event("candidate_failed", index=index, reason=str(exc))
            if recovered.is_set():
                self.state["failures"] = 0
                self.state["next_scan_at"] = 0
                self.save()
                self.event("scan_aborted_current_recovered")
                return "current_recovered"
            if not passed:
                continue
            # The candidate and monitoring thread have stopped before restart.
            try:
                switched = self.apply(node, original)
            except WatchError as exc:
                self.event("switch_blocked", reason=str(exc))
                return "switch_blocked"
            if switched:
                self.state["failures"] = 0
                self.state["next_scan_at"] = 0
                self.state["last_switch_at"] = int(self.now())
                self.save()
                return "switched"
            if self.last_rollback_healthy:
                self.state["failures"] = 0
                self.state["next_scan_at"] = 0
                self.save()
                self.event("scan_aborted_current_recovered_after_rollback")
                return "current_recovered"
            # A completed rollback replaced the inode. Refresh its metadata only
            # when bytes still exactly match our original, then try the next node.
            restored = Snapshot.take(self.s.live_config)
            if restored.raw != original.raw:
                self.event("switch_blocked", reason="manual config change after rollback")
                return "switch_blocked"
            original = restored
        self.state["next_scan_at"] = self.now() + self.s.full_scan_cooldown_seconds
        self.save()
        self.event("scan_exhausted", next_scan_at=self.state["next_scan_at"])
        return "scan_exhausted"


@contextlib.contextmanager
def locked(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WatchError("another watcher/manual maintenance owns the lock") from exc
        yield
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("check", "validate", "once", "run"), default="check")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("settings.yaml"))
    parser.add_argument("--apply", action="store_true", help="authorize a single stateful once cycle")
    args = parser.parse_args(argv)
    if args.mode == "once" and not args.apply:
        parser.error("once requires explicit --apply; default check is read-only")
    if args.apply and args.mode not in ("once", "run"):
        parser.error("--apply is valid only with once or run")
    try:
        settings = Settings.load(args.config)
        if args.mode == "check":
            print(json.dumps({"status": "healthy", **probe(settings)}, sort_keys=True))
            return 0
        if args.mode == "validate":
            validate_live(read_bytes(settings.live_config)[0])
            nodes, rejected = candidates(settings.subscription)
            print(json.dumps({"schema_accepted": len(nodes), "rejected": rejected,
                              "automatic_switching_enabled": bool(settings.allowed_ips),
                              "note": "Full Mihomo -t validation also runs before every candidate/production start."}, sort_keys=True))
            return 0 if nodes and not rejected else 2
        private_dir(settings.state_dir)
        with locked(settings.state_dir / "watch.lock"):
            watcher = Watcher(settings)
            watcher.recover()
            if args.mode == "once":
                print(json.dumps({"status": watcher.tick()}))
                return 0
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            while True:
                started = time.monotonic()
                try:
                    status = watcher.tick()
                    if status != "healthy":
                        watcher.event(status)
                except (WatchError, OSError) as exc:
                    watcher.event("cycle_error", category=type(exc).__name__)
                time.sleep(max(0.1, settings.interval_seconds - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0
    except (WatchError, OSError) as exc:
        # OS exception strings may contain path/credential fragments; don't print.
        print(json.dumps({"status": "error", "reason": str(exc) if isinstance(exc, WatchError) else type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
