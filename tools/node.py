#!/usr/bin/env python3
"""A pinned, checksum-verified Node.js for building the dashboard frontend (D-292).

No system-wide install and no service: `ensure()` downloads one exact Node LTS tarball for this
host's architecture into dashboard/web/.node/ (gitignored), verifies its SHA-256 against the
value pinned below -- copied by hand from nodejs.org's signed `SHASUMS256.txt` for this version,
not recomputed from the download -- and refuses to use it on a mismatch (D-235: no silent
fallback). Used only to check, test and build dashboard/web (test_dashboard_web.py, `npm run
build`); nothing here runs as a server or touches anything outside dashboard/web/.node/.

    python3 tools/node.py             # ensure the pinned Node is present; print its root dir
    python3 tools/node.py --node-bin  # same, print its bin/ dir (for PATH)
    python3 tools/node.py --force     # re-download and re-extract even if already present
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass

NODE_VERSION = "24.22.1"   # Active LTS "Krypton" as of 2026-10-25; bump deliberately, with a new pin
# SHA-256 of the official tarball for each supported platform, copied from the signed
# https://nodejs.org/dist/v{NODE_VERSION}/SHASUMS256.txt (fetched over TLS from nodejs.org and
# checked against its .sig before pinning here) -- never recomputed from a download at run time.
PINNED_SHA256 = {
    "linux-arm64": "dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e",  # public-check: allow (a published checksum, not a secret)
}
DIST_URL = "https://nodejs.org/dist/v{version}/node-v{version}-{platform}.tar.xz"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_DIR = os.path.join(ROOT, "dashboard", "web", ".node")   # gitignored


class NodeSetupError(RuntimeError):
    """Raised instead of falling back to something unpinned or unverified (D-235)."""


@dataclass(frozen=True)
class NodeBin:
    root: str   # the extracted node-v<version>-<platform>/ directory

    @property
    def bin_dir(self) -> str:
        return os.path.join(self.root, "bin")

    @property
    def node(self) -> str:
        return os.path.join(self.bin_dir, "node")

    @property
    def npm(self) -> str:
        return os.path.join(self.bin_dir, "npm")


def host_platform() -> str:
    system = platform.system()
    machine = platform.machine()
    if system != "Linux":
        raise NodeSetupError(f"no pinned Node build for {system} (D-292: linux arm64 only so far)")
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    raise NodeSetupError(f"no pinned Node build for machine {machine!r} (D-292: linux arm64 only so far)")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    """Refuse any member whose path would land outside `dest` (a crafted archive)."""
    dest_abs = os.path.abspath(dest)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest_abs, member.name))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            raise NodeSetupError(f"refusing to extract {member.name!r}: escapes {dest}")
    tar.extractall(dest_abs)


def ensure(*, force: bool = False) -> NodeBin:
    """Download, verify and extract the pinned Node if it is not already there. Raises
    NodeSetupError on any mismatch or failure -- never returns a partially-verified build."""
    plat = host_platform()
    expected = PINNED_SHA256.get(plat)
    if expected is None:
        raise NodeSetupError(f"no pinned SHA-256 for {plat} (add one to tools/node.py from "
                              f"nodejs.org's signed SHASUMS256.txt before using this arch)")
    dirname = f"node-v{NODE_VERSION}-{plat}"
    extracted = os.path.join(NODE_DIR, dirname)
    node_bin = NodeBin(root=extracted)
    if not force and os.path.isfile(node_bin.node):
        return node_bin
    os.makedirs(NODE_DIR, exist_ok=True)
    if force and os.path.isdir(extracted):
        shutil.rmtree(extracted)
    archive = os.path.join(NODE_DIR, f"{dirname}.tar.xz")
    if force and os.path.isfile(archive):
        os.remove(archive)
    if not os.path.isfile(archive):
        url = DIST_URL.format(version=NODE_VERSION, platform=plat)
        tmp = archive + ".part"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out)
        except OSError as exc:
            raise NodeSetupError(f"could not download {url}: {exc}") from exc
        os.replace(tmp, archive)
    got = _sha256(archive)
    if got != expected:
        os.remove(archive)   # never leave a mismatched file around to be picked up on a later run
        raise NodeSetupError(f"SHA-256 mismatch for {archive}: got {got}, expected {expected} "
                              f"(refusing to use it; D-292)")
    with tarfile.open(archive, "r:xz") as tar:
        _safe_extract(tar, NODE_DIR)
    if not os.path.isfile(node_bin.node):
        raise NodeSetupError(f"extracted archive has no {node_bin.node}")
    return node_bin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--node-bin", action="store_true", help="print only the bin/ directory")
    parser.add_argument("--force", action="store_true", help="re-download and re-extract")
    args = parser.parse_args(argv)
    try:
        node = ensure(force=args.force)
    except NodeSetupError as exc:
        print(f"tools/node.py: {exc}", file=sys.stderr)
        return 1
    print(node.bin_dir if args.node_bin else node.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
