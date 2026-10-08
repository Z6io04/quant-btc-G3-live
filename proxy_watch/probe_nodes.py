#!/usr/bin/env python3
"""Observe candidate connectivity and exit IPs without replacing the live proxy."""
import argparse
import dataclasses
import ipaddress
import json
from pathlib import Path
import signal
import subprocess
import tempfile

import proxy_watch as w


def watcher_stopped():
    """Read-only service check; scanning must not race the automatic watcher."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "proxy-watch.service"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise w.WatchError("cannot verify proxy-watch.service is stopped") from exc
    if result.returncode not in (3, 4) or result.stdout.strip() not in {"inactive", "failed", "unknown"}:
        raise w.WatchError("stop proxy-watch.service before probing candidates")


def safe_reason(exc):
    # WatchError messages from proxy_watch are controlled categories. Never print
    # arbitrary exception text, node names, hosts, authentication, or HTTP bodies.
    return str(exc) if isinstance(exc, w.WatchError) else type(exc).__name__


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def parse_nodes(value):
    """Select original subscription positions, never credentials or node names."""
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part.isascii() or not part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError("use comma-separated node numbers, for example 1,2,3")
    numbers = tuple(int(part) for part in parts)
    if any(not 1 <= number <= 200 for number in numbers) or len(set(numbers)) != len(numbers):
        raise argparse.ArgumentTypeError("node numbers must be unique integers from 1 to 200")
    return numbers


def scan(settings, selected=None):
    good, rejected = w.candidates(settings.subscription)
    if selected is not None:
        selected = set(selected)
        available = {index + 1 for index, _ in good} | {item["index"] + 1 for item in rejected}
        if selected - available:
            raise w.WatchError("selected node number does not exist")
        good = [(index, node) for index, node in good if index + 1 in selected]
        rejected = [item for item in rejected if item["index"] + 1 in selected]
    rejected_by_reason = {}
    for rejected_node in rejected:
        rejected_by_reason.setdefault(rejected_node["reason"], []).append(rejected_node["index"] + 1)
    for reason, indexes in rejected_by_reason.items():
        emit(nodes=indexes, status="unsupported", reason=reason,
             stage="candidate_start", okx_public=False, ip_lookup=False)
    successes, okx_reachable = 0, 0
    # Every writable candidate file and log is temporary. The production state,
    # configuration and credentials are not changed. No automatic IP approval.
    with tempfile.TemporaryDirectory(prefix="mihomo-node-probe-") as tmp:
        staged = dataclasses.replace(settings, state_dir=Path(tmp), allowed_ips=())
        runtime = w.Runtime(staged)
        for index, node in good:
            stage, okx_public, ip_lookup = "candidate_start", False, False
            emit(node=index + 1, status="checking", stage=stage,
                 okx_public=okx_public, ip_lookup=ip_lookup)
            try:
                # candidates() already strictly validates and deep-copies every
                # supported field, including TLS/ECH; do not strip any options.
                with runtime.candidate(node) as port:
                    stage = "okx_public"
                    w.probe(staged, port)
                    okx_public = True
                    okx_reachable += 1
                    stage = "exit_ip"
                    payload = w.https_json(staged.ip_url, port, staged.probe_timeout_seconds)
                    try:
                        value = payload["ip"]
                        if not isinstance(value, str):
                            raise ValueError()
                        address = ipaddress.ip_address(value)
                        if not address.is_global:
                            raise ValueError()
                    except (KeyError, TypeError, ValueError) as exc:
                        raise w.WatchError("invalid public exit IP response") from exc
                    ip_lookup = True
                emit(node=index + 1, status="healthy", stage=stage,
                     okx_public=okx_public, ip_lookup=ip_lookup,
                     observed_ipify_exit_ip=str(address))
                successes += 1
            except Exception as exc:
                emit(node=index + 1, status="partial" if okx_public else "failed",
                     stage=stage, okx_public=okx_public, ip_lookup=ip_lookup,
                     reason=safe_reason(exc))
    emit(status="finished", healthy_nodes=successes, okx_reachable_nodes=okx_reachable,
         note="只观察公开接口和 ipify 出口；不代表欧易私有 API 白名单或交易权限已通过。未切换节点、未修改 allowed_ips。")
    return 0 if successes else 1


def terminated(signum, frame):
    # Propagate through context managers so the temporary Mihomo is terminated.
    raise KeyboardInterrupt()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("settings.yaml"))
    parser.add_argument("--nodes", type=parse_nodes,
                        help="test only these original subscription numbers, e.g. 1,2,3")
    args = parser.parse_args(argv)
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, terminated)
    try:
        watcher_stopped()
        settings = w.Settings.load(args.config)
        return scan(settings, args.nodes)
    except KeyboardInterrupt:
        emit(status="interrupted", reason="candidate probe interrupted; temporary process cleaned up")
        return 130
    except Exception as exc:
        emit(status="error", reason=safe_reason(exc))
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
