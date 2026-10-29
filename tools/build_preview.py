#!/usr/bin/env python3
"""Builds and publishes the owner's dashboard preview (docs/phase6a_plan.md §3.9, D-211).

Host-only. `npm run build:preview` needs the pinned Node (tools/node.py, D-292); the manager
container has neither Node installed nor a writable root filesystem for dashboard/web (it is
`read_only: true` besides data/ and a tmpfs /tmp, docker/Dockerfile) and export_quickview.py's
3-minute export runs INSIDE that container -- so this is never run there, and export_quickview.py
never tries to build anything itself. This script is a separate step the task-manager (or the
owner) runs by hand on the host whenever dashboard/web's sources change; nothing here adds a cron
line or touches the crontab.

Publishes dashboard/web/dist-preview/ into export_quickview.OUT/<PREVIEW_SUBDIR>/, next to
status.json -- the same directory export_quickview.py's write_atomic/publish_page already use, so
quickview/nginx.conf's existing `location /afclaude/` serves it too, at
/afclaude/<PREVIEW_SUBDIR>/, once ovm3 has a matching /private proxy rule (D-253's pattern for
/private/AFClaude.html; not set up by this script -- see README "Quickview status page" and this
script's own notice). export_quickview.preview_block() reports what is published in status.json.

Skips the npm build entirely (no Node needed, nothing written) when dashboard/web's sources have
not changed since the last successful publish: a sha256 over every file in dashboard/web/src/,
package.json and package-lock.json, stored in preview_meta.json next to the published files.

Behind the preview_publish_enabled setting (default off, D-235): off, this does nothing at all
(printed, exit 0) -- the owner opts in once ovm3's proxy exists. On a new version it posts one
dashboard notice (D-306) through actions.py (notice.post); re-running after nothing changed posts
nothing (the source-hash gate above already skipped the work).

    python3 tools/build_preview.py            # build (if the sources changed) and publish
    python3 tools/build_preview.py --force    # rebuild and republish even if unchanged
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import afclaude_config  # noqa: E402
import export_quickview as qv  # noqa: E402
import store  # noqa: E402
import tools.node as node_tool  # noqa: E402

WEB = os.path.join(HERE, "dashboard", "web")
DIST_PREVIEW = os.path.join(WEB, "dist-preview")
SOURCE_FILES = (os.path.join(WEB, "package.json"), os.path.join(WEB, "package-lock.json"))
SOURCE_DIRS = (os.path.join(WEB, "src"),)
META_NAME = "preview_meta.json"
NPM_TIMEOUT = 300


class BuildError(RuntimeError):
    """A build or publish step failed (D-235: shown to whoever ran this, never swallowed)."""


def _source_files():
    out = list(SOURCE_FILES)
    for d in SOURCE_DIRS:
        for root, _dirs, files in os.walk(d):
            out += [os.path.join(root, f) for f in files]
    return sorted(f for f in out if os.path.isfile(f))


def source_hash():
    """sha256 over every dashboard/web source file's relative path and content, so a rename or a
    content change both change it; order-independent (sorted paths)."""
    digest = hashlib.sha256()
    for path in _source_files():
        with open(path, "rb") as fh:
            content = fh.read()
        digest.update(os.path.relpath(path, WEB).encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def tree_hash(root):
    """sha256 over a built output tree's relative paths and content (the published version)."""
    digest = hashlib.sha256()
    files = sorted(os.path.relpath(os.path.join(dp, f), root)
                   for dp, _dn, fn in os.walk(root) for f in fn)
    for rel in files:
        with open(os.path.join(root, rel), "rb") as fh:
            content = fh.read()
        digest.update(rel.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def read_meta(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def run(args, node):
    env = dict(os.environ)
    env["PATH"] = node.bin_dir + os.pathsep + env.get("PATH", "")
    r = subprocess.run(args, cwd=WEB, env=env, capture_output=True, text=True, timeout=NPM_TIMEOUT)
    if r.returncode != 0:
        raise BuildError(f"{' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")


def validate_dist(dist_dir):
    """D-211/D-235: refuse to publish anything that doesn't look like a real disconnected preview
    build -- an index.html with `connect-src 'none'` and at least one JS and one CSS asset. Raises
    BuildError with the reason; never publishes a stale or broken bundle instead."""
    index = os.path.join(dist_dir, "index.html")
    if not os.path.isfile(index):
        raise BuildError(f"{index} missing after the build")
    with open(index, encoding="utf-8") as fh:
        html = fh.read()
    if "connect-src 'none'" not in html:
        raise BuildError(f"{index} has no connect-src 'none' -- refusing to publish (D-211)")
    assets = os.path.join(dist_dir, "assets")
    js = [f for f in os.listdir(assets) if f.endswith(".js")] if os.path.isdir(assets) else []
    css = [f for f in os.listdir(assets) if f.endswith(".css")] if os.path.isdir(assets) else []
    if not js or not css:
        raise BuildError(f"{assets} is missing a JS or CSS bundle -- refusing to publish")


def build():
    """npm ci + npm run build:preview with the pinned Node (D-292); raises BuildError on anything
    that would make the publish wrong rather than publishing a stale or broken bundle (D-235)."""
    try:
        node = node_tool.ensure()
    except node_tool.NodeSetupError as e:
        raise BuildError(f"pinned Node unavailable (tools/node.py): {e}") from e
    shutil.rmtree(DIST_PREVIEW, ignore_errors=True)
    run([node.npm, "ci", "--ignore-scripts"], node)
    run([node.npm, "run", "-s", "build:preview"], node)
    validate_dist(DIST_PREVIEW)


def publish(version, src_hash, now):
    """Atomically swap OUT/PREVIEW_SUBDIR for a fresh copy of dist-preview/ plus its meta file."""
    dest = os.path.join(qv.OUT, qv.PREVIEW_SUBDIR)
    os.makedirs(qv.OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=qv.OUT, prefix=".tmp-preview-")
    try:
        shutil.rmtree(tmp)   # copytree wants a fresh (non-existing) destination
        shutil.copytree(DIST_PREVIEW, tmp)
        meta = {"version": version, "source_hash": src_hash, "built_at": now.isoformat(),
                "built_at_berlin": qv.bstr(now), "node_version": node_tool.NODE_VERSION}
        with open(os.path.join(tmp, META_NAME), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
        old = dest + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(dest):
            os.rename(dest, old)
        os.rename(tmp, dest)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def post_notice(version):
    """D-211/D-306: one dashboard notice per newly published version. Best-effort: the preview is
    already published on disk by the time this runs, so a DB hiccup here is logged, not raised."""
    title = f"Dashboard preview built ({version}, D-211)"
    text = (f"A new owner preview build of the dashboard UI is published on ovm1 at "
            f"`/afclaude/{qv.PREVIEW_SUBDIR}/` behind the quickview's key (version `{version}`). "
            f"Disconnected from the backend: dummy fixture data only, `connect-src 'none'`.\n\n"
            f"Not reachable yet as `/private/{qv.PREVIEW_SUBDIR}/` on the website host: that needs "
            f"one more nginx location on ovm3 proxying `/private/{qv.PREVIEW_SUBDIR}/` to this "
            f"export's `/afclaude/{qv.PREVIEW_SUBDIR}/` and adding the quickview key header "
            f"server-side, the same way `/private/AFClaude.html` already proxies to "
            f"`/afclaude/AFClaude.html` (D-253). Once that exists this notice's link will work for "
            f"every later version too.")
    try:
        conn = store.connect()
        try:
            actions.perform(conn, "notice.post", {"title": title, "text": text, "project": "AFClaude"},
                            actor="runner:build_preview", via="runner")
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - the publish already succeeded; a notice failure is not fatal
        print(f"tools/build_preview.py: publish ok, but could not post the dashboard notice: "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="rebuild and republish even if the sources match "
                                                              "the last published version")
    args = parser.parse_args(argv)
    if not afclaude_config.setting("preview_publish_enabled"):
        print("tools/build_preview.py: preview_publish_enabled is off; nothing done")
        return 0
    src_hash = source_hash()
    meta = read_meta(os.path.join(qv.OUT, qv.PREVIEW_SUBDIR, META_NAME))
    if not args.force and meta is not None and meta.get("source_hash") == src_hash:
        print(f"tools/build_preview.py: up to date (version {meta.get('version')}, unchanged sources)")
        return 0
    try:
        build()
    except BuildError as e:
        print(f"tools/build_preview.py: {e}", file=sys.stderr)
        return 1
    version = tree_hash(DIST_PREVIEW)[:12]
    now = datetime.now(timezone.utc)
    publish(version, src_hash, now)
    print(f"tools/build_preview.py: published version {version} to "
          f"{os.path.join(qv.OUT, qv.PREVIEW_SUBDIR)}")
    if meta is None or meta.get("version") != version:
        post_notice(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
