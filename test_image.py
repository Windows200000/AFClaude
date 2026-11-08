"""The image's contents and build (phase 5b K1, docs/phase5b_plan.md §5).

Static checks (always run): no machine-specific path of docker/env.example in what the image carries
(its Dockerfile ENV, the Python code constants), the base image and the dependencies are pinned,
`pip install --require-hashes` refuses an unpinned package, and no host-facing call or lookup in the
code uses the code's own directory (HERE), which is /app in the image.
Docker checks (opt-in: AFCLAUDE_TEST_DOCKER=1, a slow build; never on the live tag): the image builds
as a throwaway tag, `afclaude --help` runs in it, its env holds no env.example path, and the unit
suites of the modules K1 touched pass inside it. The tag is removed afterwards.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# machine-specific files that run on the HOST (the kit) or are launch helpers: until K2 moves the root
# and the repo into host.json they keep their own paths; they are not what the image's code reads
HOST_KIT = {"docker/host_exec.py", "hooks/delegation_reminder.py"}


def env_example_paths():
    out = {}
    with open(os.path.join(HERE, "docker", "env.example")) as fh:
        for line in fh:
            m = re.match(r"^([A-Z_]+)=(/\S+)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def code_files():
    for base, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "data", "node_modules", "__pycache__", ".mypy_cache"}
                   and not d.startswith("AFClaude-wt")]
        for f in files:
            if f.endswith(".py") and not f.startswith("test_") and f != "testenv.py":
                path = os.path.relpath(os.path.join(base, f), HERE)
                if path not in HOST_KIT:
                    yield path


class NoHostPathsInTheImage(unittest.TestCase):
    def test_env_example_has_paths_to_check(self):
        self.assertTrue(env_example_paths().get("AFCLAUDE_HOST_REPO", "").startswith("/"))

    def test_no_env_example_path_in_the_python_code_constants(self):
        paths = list(env_example_paths().values())
        bad = []
        for rel in code_files():
            with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    bad += [f"{rel}:{node.lineno}: {p}" for p in paths if p in node.value]
        self.assertEqual(bad, [], "a machine-specific path in code the image runs")

    def test_no_env_example_path_in_the_dockerfile_or_compose_values(self):
        paths = list(env_example_paths().values())
        for rel in ("docker/Dockerfile", "docker/compose.yml", "docker/compose.dev.yml", "docker/entrypoint.sh",
                    "docker/requirements.txt", "docker/requirements.in"):
            with open(os.path.join(HERE, rel)) as fh:
                text = fh.read()
            for p in paths:
                self.assertNotIn(p, text, rel)

    def test_the_image_env_names_no_host_path_variable(self):
        with open(os.path.join(HERE, "docker", "Dockerfile")) as fh:
            text = fh.read()
        env = re.search(r"^ENV\b(.*?)(?=^\S)", text, re.S | re.M).group(1)
        for name in ("AFCLAUDE_DIR", "AFCLAUDE_HOST_REPO", "HOST_HOME"):
            self.assertNotIn(name, env)
        self.assertNotIn("AFCLAUDE_DIR", text)

    def test_the_at_shim_and_the_cli_are_links_into_the_code(self):
        with open(os.path.join(HERE, "docker", "Dockerfile")) as fh:
            text = fh.read()
        self.assertIn('ln -s "${APP}/docker/at_shim.py" /usr/local/bin/at', text)
        self.assertIn('ln -s "${APP}/afclaude.py" /usr/local/bin/afclaude', text)

    def test_the_code_is_copied_to_app(self):
        with open(os.path.join(HERE, "docker", "Dockerfile")) as fh:
            text = fh.read()
        self.assertIn("ARG APP=/app", text)
        self.assertIn("COPY . ${APP}", text)
        self.assertIn('WORKDIR ${APP}', text)


class Pinned(unittest.TestCase):
    def test_the_base_image_is_pinned_by_digest(self):
        with open(os.path.join(HERE, "docker", "Dockerfile")) as fh:
            froms = [l for l in fh if l.startswith("FROM ")]
        self.assertEqual(len(froms), 1)
        self.assertRegex(froms[0], r"@sha256:[0-9a-f]{64}\s*$")

    def test_every_requirement_is_pinned_with_hashes(self):
        with open(os.path.join(HERE, "docker", "requirements.txt")) as fh:
            text = fh.read().replace("\\\n", " ")
        reqs = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertGreater(len(reqs), 10)
        for l in reqs:
            self.assertRegex(l, r"^[A-Za-z0-9_.\-]+==[^\s=]+(\s+--hash=sha256:[0-9a-f]{64})+\s*$", l[:80])
        self.assertTrue(any(l.startswith("mcp==") for l in reqs))

    def test_the_dockerfile_installs_only_with_require_hashes(self):
        with open(os.path.join(HERE, "docker", "Dockerfile")) as fh:
            text = fh.read()
        installs = [l for l in text.splitlines() if "pip install" in l]
        self.assertTrue(installs)
        for l in installs:
            self.assertIn("--require-hashes", l)

    @staticmethod
    def fake_wheel(d):
        """A minimal valid wheel in `d`, so pip has a candidate without any network."""
        import zipfile
        name = os.path.join(d, "afcfake-1.0-py3-none-any.whl")
        with zipfile.ZipFile(name, "w") as z:
            z.writestr("afcfake.py", "")
            z.writestr("afcfake-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: afcfake\nVersion: 1.0\n")
            z.writestr("afcfake-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            z.writestr("afcfake-1.0.dist-info/RECORD", "")

    def test_pip_refuses_an_unpinned_or_unhashed_package(self):
        """pip's own check, offline (a local wheel): the same flag the image build uses."""
        for line in ("afcfake\n", "afcfake==1.0\n"):
            with tempfile.TemporaryDirectory() as d:
                self.fake_wheel(d)
                req = os.path.join(d, "r.txt")
                with open(req, "w") as fh:
                    fh.write(line)
                cmd = [sys.executable, "-m", "pip", "install", "--no-index", "--find-links", d, "--disable-pip-version-check",
                       "--target", os.path.join(d, "t"), "-r", req]
                ok = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=d)
                self.assertEqual(ok.returncode, 0, ok.stderr)               # without the flag it installs
                r = subprocess.run(cmd[:3] + ["--require-hashes"] + cmd[3:], capture_output=True, text=True, timeout=120, cwd=d)
                self.assertNotEqual(r.returncode, 0, line)
                self.assertRegex(r.stderr, r"(?i)hash", line)


class HostFacingPathsAreNotTheCodeDirectory(unittest.TestCase):
    """The plan's K1 risk: a host path computed from HERE (the code's own directory: /app in the image)
    and sent through host.py, or used to look up a project (projects.path holds host paths)."""
    SENDERS = {"run_on_host", "fire", "rc_send", "append_note", "get_project", "_project_by_path"}

    def test_no_sender_takes_here(self):
        bad = []
        for rel in code_files():
            with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in self.SENDERS and any(isinstance(n, ast.Name) and n.id == "HERE"
                                                    for a in list(node.args) + [k.value for k in node.keywords]
                                                    for n in ast.walk(a)):
                        bad.append(f"{rel}:{node.lineno}: {name}(... HERE ...)")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bad += [f"{rel}:{node.lineno}: default HERE in {node.name}" for d in node.args.defaults + node.args.kw_defaults
                            if isinstance(d, ast.Name) and d.id == "HERE"]
        self.assertEqual(bad, [])


@unittest.skipUnless(os.environ.get("AFCLAUDE_TEST_DOCKER") == "1" and shutil.which("docker"),
                     "set AFCLAUDE_TEST_DOCKER=1 (and have docker) to build the image")
class BuiltImage(unittest.TestCase):
    TAG = "afclaude-k1-test"

    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["docker", "build", "-q", "-f", "docker/Dockerfile", "-t", cls.TAG, "."],
                           cwd=HERE, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise AssertionError(f"docker build failed: {r.stderr[-800:]}")
        cls.addClassCleanup(subprocess.run, ["docker", "rmi", cls.TAG], capture_output=True)

    def run_in(self, *argv, timeout=600):
        # AFCLAUDE_IN_CONTAINER off for the suites: they use local stubs, not the bridge (README, Deploy)
        return subprocess.run(["docker", "run", "--rm", "-e", "AFCLAUDE_IN_CONTAINER=", "--entrypoint", argv[0], self.TAG, *argv[1:]],
                              capture_output=True, text=True, timeout=timeout)

    def test_afclaude_help(self):
        r = self.run_in("afclaude", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("runner", r.stdout)

    def test_the_code_is_at_app_and_the_env_has_no_env_example_path(self):
        r = subprocess.run(["docker", "image", "inspect", self.TAG, "--format",
                            "{{json .Config.Env}}|{{.Config.WorkingDir}}"], capture_output=True, text=True)
        env, workdir = r.stdout.strip().rsplit("|", 1)
        self.assertEqual(workdir, "/app")
        for p in env_example_paths().values():
            self.assertNotIn(p, env)

    def test_the_at_shim_finds_the_code_through_its_link(self):
        r = self.run_in("at", "-l")           # no data dir content: it may fail, but not on an import
        self.assertNotIn("ModuleNotFoundError", r.stderr)

    def test_the_unit_suites_pass_inside_the_image(self):
        r = self.run_in("python3", "-m", "unittest", "test_afclaude_cli", "test_host", "test_image", "test_prompts",
                        "test_dispatcher", "test_runner_state")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])


if __name__ == "__main__":
    unittest.main()
