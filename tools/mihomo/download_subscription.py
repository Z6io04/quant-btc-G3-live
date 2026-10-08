#!/usr/bin/env python3
"""Download a private HTTPS subscription using a hidden prompt, normally on Mac."""
import argparse
import getpass
import os
from pathlib import Path
import tempfile
import warnings
from urllib.parse import urlsplit
import urllib.request


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("HTTPS is required")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("~/mihomo-subscription.yaml"))
    args = parser.parse_args(argv)
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        print("Output already exists; preserve it or choose another --output.")
        return 2
    try:
        warnings.simplefilter("error", getpass.GetPassWarning)
        url = getpass.getpass("Paste Clash/Mihomo subscription HTTPS URL (hidden): ").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid subscription URL")
        request = urllib.request.Request(url, headers={"User-Agent": "clash.meta"})
        with urllib.request.build_opener(HTTPSOnly()).open(request, timeout=45) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise ValueError("subscription size is invalid")
        fd, name = tempfile.mkstemp(prefix=".subscription-", dir=output.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(name, output)
        finally:
            os.unlink(name)
        print("Saved private subscription (0600). Validate it on the server before use.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; no output installed.")
        return 130
    except Exception:
        # HTTP errors can include subscription tokens; never echo exception text.
        print("Download failed; check the private HTTPS URL and network. No output installed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
