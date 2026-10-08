#!/usr/bin/env python3
"""Install a verified official Mihomo Linux amd64 release without replacing files."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import tempfile
import urllib.error
import urllib.request

API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/"
RELEASES = "https://github.com/MetaCubeX/mihomo/releases/download/"
MAX_ARCHIVE = 96 * 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024


class InstallError(Exception):
    pass


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in {
            "github.com", "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
        }:
            raise InstallError("unexpected release download redirect")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch(url, limit):
    request = urllib.request.Request(url, headers={
        "User-Agent": "btc-G3-mihomo-bootstrap/1", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.build_opener(HTTPSRedirect()).open(request, timeout=60) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise InstallError("release response exceeds size limit")
    return value


def select_asset(release):
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise InstallError("invalid official release metadata")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise InstallError("unexpected stable release tag")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise InstallError("only published stable releases are supported")
    name = "mihomo-linux-amd64-compatible-" + tag + ".gz"
    matches = [asset for asset in release["assets"] if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        raise InstallError("official compatible amd64 archive not found")
    asset = matches[0]
    checksum = asset.get("digest")
    if not isinstance(checksum, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
        raise InstallError("official GitHub SHA256 digest missing; refusing unverified install")
    if asset.get("browser_download_url") != RELEASES + tag + "/" + name:
        raise InstallError("asset is not at the expected official release URL")
    return tag, asset


def validate_elf(raw):
    if len(raw) < 64 or raw[:6] != b"\x7fELF\x02\x01" or struct.unpack("<H", raw[18:20])[0] != 62:
        raise InstallError("download is not a 64-bit little-endian x86_64 ELF binary")


def install(destination, tag=None, inspect=False):
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise InstallError("run on the Linux amd64 server; this is not a Mac installer")
    if tag is not None and not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise InstallError("--tag must be a stable version such as v1.2.3")
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise InstallError("binary already exists; preserve the installed production version")
    release = json.loads(fetch(API + ("tags/" + tag if tag else "latest"), 2 * 1024 * 1024))
    resolved_tag, asset = select_asset(release)
    if tag is not None and resolved_tag != tag:
        raise InstallError("release tag mismatch")
    receipt = {"repository": "MetaCubeX/mihomo", "tag": resolved_tag,
               "asset": asset["name"], "archive_sha256": asset["digest"][7:],
               "source": asset["browser_download_url"]}
    if inspect:
        print(json.dumps(receipt, sort_keys=True))
        return
    archive = fetch(asset["browser_download_url"], MAX_ARCHIVE)
    if hashlib.sha256(archive).hexdigest() != receipt["archive_sha256"]:
        raise InstallError("official release SHA256 mismatch; nothing installed")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = destination.with_name(destination.name + ".release.json")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise InstallError("release receipt already exists; refusing overwrite")
    with tempfile.TemporaryDirectory(prefix=".mihomo-install-", dir=destination.parent) as tmp:
        folder = Path(tmp)
        compressed = folder / "download.gz"
        compressed.write_bytes(archive)
        candidate = folder / "mihomo"
        with gzip.open(compressed, "rb") as source, candidate.open("xb") as target:
            remaining = MAX_BINARY
            while True:
                chunk = source.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    raise InstallError("uncompressed release exceeds size limit")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        with candidate.open("rb") as source:
            validate_elf(source.read(64))
        candidate.chmod(0o700)
        receipt["binary_sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        staged_receipt = folder / "release.json"
        staged_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        staged_receipt.chmod(0o600)
        # link is an atomic create-if-absent; it cannot replace an existing binary.
        os.link(candidate, destination)
        os.link(staged_receipt, receipt_path)
    print(json.dumps({"status": "installed", "tag": resolved_tag, "asset": asset["name"],
                      "sha256_verified": True, "service_started": False}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("~/mihomo/bin/mihomo"))
    parser.add_argument("--tag", help="pin an official stable tag; default resolves latest")
    parser.add_argument("--inspect", action="store_true", help="show official release metadata only")
    args = parser.parse_args(argv)
    try:
        install(args.destination, args.tag, args.inspect)
    except InstallError as exc:
        print("Install stopped: " + str(exc))
        return 2
    except (OSError, ValueError, urllib.error.URLError, EOFError):
        print("Install stopped: download, archive, or filesystem failure; check access and retry.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
