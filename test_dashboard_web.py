#!/usr/bin/env python3
"""The TS frontend scaffold's Python wrapper (D-208/D-209, docs/phase6a_plan.md §3.8, W3).

Runs the pinned Node (tools/node.py, D-292) over dashboard/web: `npm ci --ignore-scripts`,
`tsc --noEmit` (strict), the `node:test` suite (the client, the router, the shell and the
tooltip via preact-render-to-string, the source checks), `npm audit` (high/critical) and the
esbuild production build.

Without a usable pinned Node this is a skip, like test_mds/test_oidc's optional dependencies --
except when AFCLAUDE_REQUIRE_NODE=1 (the image's build stage; once Node is on this host, the
pre-commit hook too), where the same gap is a failure instead (D-235: no silent fallback once
the owner has said Node must be there).

    python3 -m unittest test_dashboard_web
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tools.node as node_tool  # noqa: E402

WEB = os.path.join(HERE, "dashboard", "web")
REQUIRE_NODE = os.environ.get("AFCLAUDE_REQUIRE_NODE") == "1"


def _ensure_node() -> "node_tool.NodeBin | None":
    try:
        return node_tool.ensure()
    except node_tool.NodeSetupError as exc:
        global _SETUP_ERROR
        _SETUP_ERROR = str(exc)
        return None


_SETUP_ERROR = ""
_NODE = _ensure_node()
_SKIP = "" if _NODE is not None else f"no pinned Node available (tools/node.py): {_SETUP_ERROR}"
if _SKIP and REQUIRE_NODE:
    raise RuntimeError(f"AFCLAUDE_REQUIRE_NODE=1: {_SKIP}")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    assert _NODE is not None
    env = dict(os.environ)
    env["PATH"] = _NODE.bin_dir + os.pathsep + env.get("PATH", "")
    return subprocess.run(args, cwd=WEB, env=env, capture_output=True, text=True, timeout=600)


@unittest.skipIf(bool(_SKIP), _SKIP)
class FrontendScaffold(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert _NODE is not None
        installed = _run([_NODE.npm, "ci", "--ignore-scripts"])
        if installed.returncode != 0:
            raise RuntimeError(f"npm ci failed:\n{installed.stdout}\n{installed.stderr}")

    def test_typecheck(self) -> None:
        assert _NODE is not None
        result = _run([_NODE.npm, "run", "-s", "typecheck"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unit_tests(self) -> None:
        assert _NODE is not None
        result = _run([_NODE.npm, "run", "-s", "test"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_audit_clean(self) -> None:
        # a registry we can't reach is not a finding: told apart by npm's own error text
        assert _NODE is not None
        result = _run([_NODE.npm, "audit", "--omit=dev", "--audit-level=high"])
        unreachable = ("ENOTFOUND" in result.stderr or "ETIMEDOUT" in result.stderr
                       or "ECONNREFUSED" in result.stderr)
        if result.returncode != 0 and not unreachable:
            self.fail(result.stdout + result.stderr)

    def test_build(self) -> None:
        assert _NODE is not None
        result = _run([_NODE.npm, "run", "-s", "build"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(WEB, "dist", "index.html")))
        assets = os.path.join(WEB, "dist", "assets")
        self.assertTrue(any(f.endswith(".js") for f in os.listdir(assets)))
        self.assertTrue(any(f.endswith(".css") for f in os.listdir(assets)))


if __name__ == "__main__":
    unittest.main()
