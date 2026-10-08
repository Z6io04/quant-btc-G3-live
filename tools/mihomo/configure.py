#!/usr/bin/env python3
"""List supported subscription numbers or validate a private Mihomo candidate."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy_watch"))
import proxy_watch as w


def inventory(subscription):
    good, rejected = w.candidates(subscription)
    rows = [{"node": index + 1, "status": "supported", "protocol": node["type"]}
            for index, node in good]
    rows += [{"node": item["index"] + 1, "status": "unsupported", "reason": item["reason"]}
             for item in rejected]
    for row in sorted(rows, key=lambda row: row["node"]):
        print(json.dumps(row, ensure_ascii=False))
    return good


def prepare(subscription, node_number, binary, private):
    good, rejected = w.candidates(subscription)
    selected = dict(good).get(node_number - 1)
    if selected is None:
        raise w.WatchError("selected node number is missing or unsupported; use --list")
    w.private_dir(private)
    output = private / "config.candidate.yaml"
    if output.exists() or output.is_symlink():
        raise w.WatchError("candidate already exists; preserve or remove it explicitly before preparing another")
    settings = w.Settings(binary, private / "config.yaml", subscription, private)
    # This is a production-port CONFIG only. -t never starts its listener.
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=private) as tmp:
        folder = Path(tmp)
        candidate = folder / "config.yaml"
        w.atomic_write(candidate, w.config_bytes(selected, 17890))
        w.Runtime(settings).validate(candidate, folder)
        # Preserve both the live config and any concurrently created candidate.
        os.link(candidate, output)
    print(json.dumps({"status": "candidate_validated", "node": node_number,
                      "protocol": selected["type"], "live_config_changed": False,
                      "service_started": False, "public_connectivity_tested": False}))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", type=Path, default=Path("~/mihomo-subscription.yaml"))
    parser.add_argument("--binary", type=Path, default=Path("~/mihomo/bin/mihomo"))
    parser.add_argument("--private-dir", type=Path, default=Path("~/mihomo/private"))
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--list", action="store_true")
    selection.add_argument("--node", type=int, help="original subscription number, starting from 1")
    args = parser.parse_args(argv)
    try:
        subscription = args.subscription.expanduser().absolute()
        if args.list:
            return 0 if inventory(subscription) else 2
        if args.node < 1:
            raise w.WatchError("node number must start from 1")
        prepare(subscription, args.node, args.binary.expanduser().absolute(),
                args.private_dir.expanduser().absolute())
        return 0
    except Exception as exc:
        # Native stderr/YAML source/node names may contain credentials. Never echo them.
        reason = str(exc) if isinstance(exc, w.WatchError) else type(exc).__name__
        print(json.dumps({"status": "error", "reason": reason}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
