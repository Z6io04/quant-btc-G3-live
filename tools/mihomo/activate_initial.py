#!/usr/bin/env python3
"""Install the first validated live config; never overwrite existing production."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxy_watch"))
import proxy_watch as w


def activate(binary, private):
    w.private_dir(private)
    live = private / "config.yaml"
    if live.exists() or live.is_symlink():
        raise w.WatchError("production config already exists; initial activation will not overwrite it")
    raw = w.read_bytes(private / "config.candidate.yaml")[0]
    w.validate_live(raw)
    settings = w.Settings(binary, live, private / "unused-subscription.yaml", private)
    with tempfile.TemporaryDirectory(prefix="activate-", dir=private) as tmp:
        folder = Path(tmp)
        candidate = folder / "config.yaml"
        w.atomic_write(candidate, raw)
        w.Runtime(settings).validate(candidate, folder)
        os.link(candidate, live)
    print(json.dumps({"status": "initial_config_created", "service_started": False}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("~/mihomo/bin/mihomo"))
    parser.add_argument("--private-dir", type=Path, default=Path("~/mihomo/private"))
    args = parser.parse_args(argv)
    try:
        activate(args.binary.expanduser().absolute(), args.private_dir.expanduser().absolute())
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, w.WatchError) else type(exc).__name__
        print(json.dumps({"status": "error", "reason": reason}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
