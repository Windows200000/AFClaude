"""The three services of the image, in containers (phase 5b K4, docs/phase5b_plan.md §3.1): the services layout,
docker/compose.services.yml; docker/compose.yml stays the single container until K7 (D-339).

Static checks (always): the compose file's shape (one image, three services, the hardening on each, only
AFCLAUDE_DATA required, no fixed subnet, no published port), the entrypoint's order of checks, and the
transitional override.

Container test (skipped with its reason without a usable docker, or with AFCLAUDE_TEST_DOCKER=0): compose up on a
temp data directory with only AFCLAUDE_DATA set (the repo's docker/.env is ignored: `--env-file /dev/null`), in
its own compose project, network and container names, never the live ones; the health checks are sped up through a
generated override (the file's own intervals are checked statically). Three healthy services, the web service's
socket answers (/health, the inbox API: 503 without a key, 401 with a wrong one, 200 with the right one), the MCP
service refuses every request, read-only file system, non-root, no capability, no published port, one master key;
without AFCLAUDE_DATA compose stops with the message; an unmounted data directory and a wrong master key stop each
service with exit 78; the web and MCP services cannot read the bridge key in <data>/bridge (D-339), the runner can; the
web service without an inbox key exits 1. Everything is torn down afterwards, the image tag included.
"""
import base64
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = os.path.join(HERE, "docker", "compose.services.yml")


def read(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()


def service_block(text, name):
    m = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", text)
    assert m, name
    return m.group(1)


class ComposeShape(unittest.TestCase):
    SERVICES = ("afclaude-runner", "afclaude-mcp", "afclaude-web")

    def test_three_services_from_one_image_with_the_hardening_of_k0(self):
        text = read("docker/compose.services.yml")
        for name in self.SERVICES:
            self.assertRegex(text, rf"(?m)^  {name}:\n    <<: \*service$")
        anchor = re.search(r"(?ms)^x-service: &service\n(.*?)^services:", text).group(1)
        self.assertIn("image: afclaude:${AFCLAUDE_CODE_VERSION:-latest}", anchor)      # one image for all three
        for line in ("read_only: true", "cap_drop: [ALL]", 'security_opt: ["no-new-privileges:true"]',
                     'user: "1000:1000"', "restart: unless-stopped"):
            self.assertIn(line, anchor)
        self.assertEqual(len(re.findall(r"(?m)^    build:$", text)), 1, "the image is built once, by the runner")
        for forbidden in ("privileged", "cap_add", "docker.sock", "network_mode", "ports:", "pid:"):
            self.assertNotIn(forbidden, re.sub(r"(?m)^\s*#.*$", "", text), forbidden)
        for name in self.SERVICES[1:]:
            self.assertIn("pull_policy: never", service_block(text, name))

    def test_only_afclaude_data_is_required_and_its_message_has_no_colon(self):
        text = read("docker/compose.services.yml")
        required = re.findall(r"\$\{(\w+):\?([^}]*)\}", text)
        self.assertEqual([r[0] for r in required], ["AFCLAUDE_DATA"])
        self.assertNotIn(":", required[0][1], "a colon in a short-syntax volume's message breaks compose's parser (K0)")
        self.assertIn("AFCLAUDE_DATA is not set", required[0][1])
        # the others are optional with their defaults; nothing machine-specific is in the base file
        self.assertNotIn("HOST_HOME", text)
        self.assertNotIn("AFCLAUDE_HOST_REPO", re.sub(r"(?m)^\s*#.*$", "", text))
        self.assertNotIn("ipam", text)

    def test_health_checks_and_the_start_order(self):
        text = read("docker/compose.services.yml")
        self.assertIn('test: ["CMD", "python3", "mcp_service.py", "health"]', text)
        self.assertIn('test: ["CMD", "python3", "web.py", "health"]', text)
        for name in ("afclaude-mcp", "afclaude-web"):
            self.assertIn("condition: service_healthy", service_block(text, name))      # after the runner migrated
        self.assertNotIn("depends_on", service_block(text, "afclaude-runner"))
        self.assertIn("stop_grace_period: 90s", service_block(text, "afclaude-runner"))
        for name, command in zip(self.SERVICES, ("runner", "mcp", "web")):
            self.assertIn(f'command: ["{command}"]', service_block(text, name))

    def test_the_host_specific_mounts_are_in_the_transitional_override_only(self):
        override = read("docker/compose.host-mounts.yml")
        for needle in ("/host/repo:ro", ".claude/projects", ".claude/sessions", "host_bridge_ed25519",
                       "afclaude_quickview.key"):
            self.assertIn(needle, override)
        self.assertIn("pins", read("docker/compose.subnet.yml") + "pins")      # the file exists
        self.assertIn("172.28.0.0/24", read("docker/compose.subnet.yml"))

    def test_the_single_container_layout_is_still_the_default_compose_file(self):
        text = read("docker/compose.yml")
        self.assertRegex(text, r"(?m)^  afclaude:$")
        self.assertIn("container_name: afclaude\n", text)
        self.assertNotIn("AFCLAUDE_LAYOUT", text)            # unset = single, in the entrypoint and the runner
        self.assertNotIn("afclaude-runner", text)
        self.assertEqual(read("docker/compose.dev.yml").count("afclaude:"), 1)      # main's dev override, unchanged
        self.assertFalse(os.path.exists(os.path.join(HERE, "docker", "compose.single.yml")),
                         "no second copy of the single layout: a rollback is the old commit/image (review k4a M2)")

    def test_the_services_say_so_and_web_and_mcp_do_not_see_the_bridge_key(self):
        text = read("docker/compose.services.yml")
        self.assertRegex(text, r"(?m)^    AFCLAUDE_LAYOUT: services\b")
        for name in ("afclaude-mcp", "afclaude-web"):
            self.assertIn("      - /app/data/bridge:ro,", service_block(text, name), name)
        self.assertNotIn("/app/data/bridge", service_block(text, "afclaude-runner"))


class EntrypointOrder(unittest.TestCase):
    def test_the_mount_check_comes_before_anything_is_written(self):
        sh = read("docker/entrypoint.sh")
        order = [sh.index(x) for x in ('service.py" check-mount "$SVC"', "store.py\" migrate", 'service.py" ensure-key runner',
                                       "runner.py\" serve")]
        self.assertEqual(order, sorted(order))
        self.assertLess(sh.index('check-mount'), sh.index("alert_once"), "even the unknown-mode alert needs a mounted /data")
        for svc, program in (("mcp", "mcp_service.py\" serve"), ("web", "web.py\" serve")):
            block = re.search(rf"(?s)\n  {svc}\)\n(.*?);;", sh).group(1)
            self.assertIn("ensure-key", block)
            self.assertIn(f'exec python3 -u "$D/{program}', block)
            self.assertNotIn("migrate", block)      # only the runner migrates

    def test_unknown_service_is_refused(self):
        r = subprocess.run(["sh", os.path.join(HERE, "docker", "entrypoint.sh"), "nope"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown service nope", r.stderr)


def docker_usable():
    if os.environ.get("AFCLAUDE_TEST_DOCKER") == "0":
        return "AFCLAUDE_TEST_DOCKER=0"
    if shutil.which("docker") is None:
        return "no docker on this machine"
    try:
        r = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30)
        if r.returncode != 0:
            return "no docker compose plugin"
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker does not answer ({type(e).__name__})"
    return None if r.returncode == 0 else "the docker daemon is not reachable"


def short_tmp():
    """A data directory under /tmp: a unix socket's path is limited to ~100 bytes."""
    return tempfile.mkdtemp(prefix="afk4-", dir="/tmp")


def unique_suffix() -> str:
    """A name part that stays unique per test class and per run. Not os.getpid(): under `unshare --pid` it is always 1,
    so two suites running at once would share a compose project, an image tag and a key path."""
    return secrets.token_hex(4)


def require_files(*paths: str) -> None:
    """Every host path docker bind-mounts as a file must exist as one before `compose up`: for a missing source
    docker creates a root-owned DIRECTORY there that nothing in the tests can remove."""
    for path in paths:
        if not os.path.isfile(path):
            raise RuntimeError(f"bind-mount source {path} is not a file: docker would create a directory there")


NO_DOCKER = docker_usable()


@unittest.skipIf(NO_DOCKER is not None, f"container test: {NO_DOCKER}")
class ThreeServicesInContainers(unittest.TestCase):
    """Needs docker. Slow on a cold build (the image: apt + pip), about a minute otherwise."""

    @classmethod
    def setUpClass(cls):
        suffix = unique_suffix()
        cls.tag = f"k4t{suffix}"
        cls.project = f"afk4t{suffix}"
        cls.data = short_tmp()
        os.chmod(cls.data, 0o700)
        cls.override_dir = tempfile.mkdtemp(prefix="afk4o-", dir="/tmp")     # holds the key file too: removed with it
        cls.keyfile = os.path.join(cls.override_dir, "quickview.key")
        os.makedirs(os.path.join(cls.data, "bridge"))      # what the host kit's install leaves there (K2)
        cls.bridge_key = os.path.join(cls.data, "bridge", "host_bridge_ed25519")
        with open(cls.bridge_key, "w") as fh:
            fh.write("-----BEGIN FAKE KEY-----\n")
        os.chmod(cls.bridge_key, 0o644)       # readable by any uid: only the overlay can hide it from web and mcp
        cls.key = secrets.token_hex(20)
        with open(cls.keyfile, "w") as fh:
            fh.write(cls.key + "\n")
        os.chmod(cls.keyfile, 0o600)
        cls.override = os.path.join(cls.override_dir, "override.yml")
        hc = "{interval: 3s, timeout: 20s, retries: 5, start_period: 0s}"
        with open(cls.override, "w") as fh:
            fh.write("services:\n"
                     f"  afclaude-runner:\n    healthcheck: {hc}\n"
                     f"  afclaude-mcp:\n    healthcheck: {hc}\n"
                     f"  afclaude-web:\n    healthcheck: {hc}\n    volumes:\n"
                     f"      - {cls.keyfile}:/run/secrets/afclaude_quickview.key:ro\n")
        cls.env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")}
        cls.env.update(AFCLAUDE_DATA=cls.data, AFCLAUDE_CODE_VERSION=cls.tag, AFCLAUDE_NAME=cls.project)
        cls.started = False
        require_files(cls.keyfile)
        # an initialised database: nothing creates one implicitly (D-163)
        subprocess.run([sys.executable, os.path.join(HERE, "store.py"), "init", "--db", os.path.join(cls.data, "afclaude.db")],
                       check=True, capture_output=True, env=dict(os.environ, AFCLAUDE_DATA_DIR=cls.data))
        cls.compose("build", "afclaude-runner", timeout=900, check=True)
        r = cls.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=300)
        cls.up = r
        cls.started = True

    @classmethod
    def compose(cls, *args, timeout=120, check=False, env=None):
        cmd = ["docker", "compose", "--env-file", "/dev/null", "-p", cls.project, "-f", COMPOSE, "-f", cls.override, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or cls.env, cwd=HERE)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {r.stderr[-1500:]}")
        return r

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "env", None):
            cls.compose("down", "--volumes", "--remove-orphans", timeout=180)
            subprocess.run(["docker", "rmi", f"afclaude:{cls.tag}"], capture_output=True, timeout=60)
        for path in (getattr(cls, "data", None), getattr(cls, "override_dir", None)):
            if path and path.startswith("/tmp/afk4"):
                shutil.rmtree(path, ignore_errors=True)

    names = ("runner", "mcp", "web")

    def container(self, svc):
        return f"{self.project}-{svc}"

    def inspect(self, svc):
        r = subprocess.run(["docker", "inspect", self.container(svc)], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(r.stdout)[0]

    def exec(self, svc, *cmd):
        return subprocess.run(["docker", "exec", self.container(svc), *cmd], capture_output=True, text=True, timeout=60)

    def request(self, method, path, key=None, body=None):
        """HTTP over the web service's unix socket in the data directory (through a directory fd: the
        socket's own path may be too long for sun_path)."""
        dfd = os.open(os.path.join(self.data, "api"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            class Conn(http.client.HTTPConnection):
                def connect(inner):
                    inner.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    inner.sock.settimeout(15)
                    inner.sock.connect(f"/proc/self/fd/{dfd}/inbox.sock")
            c = Conn("localhost")
            headers = {"X-AFClaude-Key": key} if key is not None else {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = c.getresponse()
            out = (r.status, json.loads(r.read() or b"null"))
            c.close()
            return out
        finally:
            os.close(dfd)

    def test_up_with_only_afclaude_data_and_three_healthy_services(self):
        self.assertEqual(self.up.returncode, 0, self.up.stderr[-1500:])
        for svc in self.names:
            st = self.inspect(svc)["State"]
            self.assertEqual((st["Status"], st["Health"]["Status"]), ("running", "healthy"), svc)

    def test_the_web_service_answers_on_the_socket(self):
        st, body = self.request("GET", "/health")
        self.assertEqual((st, body["status"], body["problems"], body["schema"]), (200, "ok", [], 5))
        self.assertTrue(body["inbox_key"])
        self.assertEqual(self.request("GET", "/afclaude/api/inbox")[0], 401)                    # no key
        self.assertEqual(self.request("GET", "/afclaude/api/inbox", key="wrong")[0], 401)
        st, box = self.request("GET", "/afclaude/api/inbox", key=self.key)                       # the key
        self.assertEqual(st, 200, box)
        self.assertEqual(self.request("PUT", "/afclaude/api/inbox", key=self.key)[0], 405)
        self.assertEqual(oct(os.stat(os.path.join(self.data, "api", "inbox.sock")).st_mode & 0o777), "0o666")

    def test_the_mcp_service_refuses_every_request_until_the_token_check(self):
        r = self.exec("mcp", "python3", "-c", (
            "import http.client as h, json\n"
            "c = h.HTTPConnection('127.0.0.1', 8000, timeout=10)\n"
            "c.request('POST', '/mcp', body='{}', headers={'Content-Type': 'application/json'})\n"
            "r = c.getresponse(); r.read(); print(r.status, r.getheader('WWW-Authenticate'))\n"
            "c.request('POST', '/mcp', body='{}', headers={'Authorization': 'Bearer anything'})\n"
            "print(c.getresponse().status)"))
        self.assertEqual(r.stdout.split(), ["401", "Bearer", "401"], r.stderr)

    def test_read_only_file_system_and_a_writable_data_directory(self):
        for svc in self.names:
            self.assertTrue(self.inspect(svc)["HostConfig"]["ReadonlyRootfs"], svc)
            self.assertNotEqual(self.exec(svc, "touch", "/app/x").returncode, 0, svc)
            self.assertNotEqual(self.exec(svc, "touch", "/usr/local/x").returncode, 0, svc)
            self.assertEqual(self.exec(svc, "touch", "/app/data/.rw-check").returncode, 0, svc)
            self.assertEqual(self.exec(svc, "touch", "/tmp/x").returncode, 0, svc)
        os.unlink(os.path.join(self.data, ".rw-check"))

    def test_non_root_and_no_capability_and_nothing_published(self):
        for svc in self.names:
            info = self.inspect(svc)
            hc = info["HostConfig"]
            self.assertEqual(info["Config"]["User"], "1000:1000", svc)
            self.assertEqual(self.exec(svc, "id", "-u").stdout.strip(), "1000", svc)
            self.assertEqual(hc["CapDrop"], ["ALL"], svc)
            self.assertFalse(hc["CapAdd"], svc)
            self.assertFalse(hc["Privileged"], svc)
            self.assertIn("no-new-privileges:true", hc["SecurityOpt"], svc)
            caps = re.search(r"CapEff:\s*(\w+)", self.exec(svc, "cat", "/proc/self/status").stdout).group(1)
            self.assertEqual(int(caps, 16), 0, svc)
            self.assertFalse(info["NetworkSettings"]["Ports"] and any(info["NetworkSettings"]["Ports"].values()), svc)
            self.assertFalse([m for m in info["Mounts"] if "docker.sock" in m["Source"]], svc)
            self.assertEqual(sorted(m["Destination"] for m in info["Mounts"]
                                    if m["Destination"] not in ("/run/secrets/afclaude_quickview.key", "/app/data/bridge")),
                             ["/app/data"], svc)

    def test_web_and_mcp_cannot_read_the_bridge_key_but_the_runner_can(self):
        """D-339 (review k4a M3): an empty tmpfs over <data>/bridge in the services that face a network."""
        path = "/app/data/bridge/host_bridge_ed25519"
        self.assertEqual(self.exec("runner", "cat", path).stdout, "-----BEGIN FAKE KEY-----\n")
        for svc in ("web", "mcp"):
            self.assertNotEqual(self.exec(svc, "cat", path).returncode, 0, svc)
            self.assertEqual(self.exec(svc, "ls", "-A", "/app/data/bridge").stdout, "", svc)
            self.assertNotEqual(self.exec(svc, "touch", "/app/data/bridge/x").returncode, 0, svc)     # read-only too
            self.assertEqual(self.exec(svc, "test", "-s", "/app/data/master.key").returncode, 0, svc)  # the key stays
        self.assertTrue(os.path.isfile(self.bridge_key))       # the host side is untouched

    def test_web_without_an_inbox_key_exits_1_and_says_why(self):
        """Review k4a M1: not a silent 503 server; failstop alerts and pauses (in this throwaway database; the
        alert needs AFCLAUDE_HOST_REPO: a K7 item). Its own data directory: the running web holds the lock of the
        shared one."""
        other = short_tmp()
        try:
            os.chmod(other, 0o700)
            subprocess.run([sys.executable, os.path.join(HERE, "store.py"), "init", "--db", os.path.join(other, "afclaude.db")],
                           check=True, capture_output=True, env=dict(os.environ, AFCLAUDE_DATA_DIR=other))
            cmd = ["docker", "run", "--rm", "--read-only", "--cap-drop", "ALL", "--tmpfs", "/tmp:mode=1777",
                   "-e", "AFCLAUDE_LAYOUT=services", "-e", "AFCLAUDE_HOST_REPO=/srv/afclaude", "-v", f"{other}:/app/data",
                   f"afclaude:{self.tag}", "web"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1, (r.stdout, r.stderr))
            self.assertIn("no inbox key", r.stdout + r.stderr)
            self.assertIn("the inbox API was not running", r.stdout + r.stderr)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_one_master_key_for_all_three(self):
        path = os.path.join(self.data, "master.key")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        ids = set()
        for svc in self.names:
            logs = subprocess.run(["docker", "logs", self.container(svc)], capture_output=True, text=True, timeout=30)
            m = re.search(r"master key (\w{12}) loaded", logs.stdout + logs.stderr)
            self.assertTrue(m, f"{svc}: {logs.stdout[-300:]} {logs.stderr[-300:]}")
            ids.add(m.group(1))
        self.assertEqual(len(ids), 1)

    def test_without_afclaude_data_compose_stops_before_any_container_is_touched(self):
        env = {k: v for k, v in self.env.items() if k != "AFCLAUDE_DATA"}
        r = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-p", f"{self.project}x", "-f", COMPOSE, "up", "-d"],
                           capture_output=True, text=True, timeout=60, env=env, cwd=HERE)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("AFCLAUDE_DATA is not set - choose the data directory (see setup)", r.stderr)
        ps = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}x"],
                            capture_output=True, text=True, timeout=30)
        self.assertEqual(ps.stdout.strip(), "")

    def run_image(self, svc, *extra, mount=True):
        cmd = ["docker", "run", "--rm", "--read-only", "--cap-drop", "ALL", "--tmpfs", "/tmp:mode=1777", "-e", "AFCLAUDE_LAYOUT=services", *extra]
        if mount:
            cmd += ["-v", f"{self.data}:/app/data"]
        return subprocess.run(cmd + [f"afclaude:{self.tag}", svc], capture_output=True, text=True, timeout=120)

    def test_an_unmounted_data_directory_stops_each_service_with_exit_78(self):
        for svc in self.names:
            r = self.run_image(svc, mount=False)
            self.assertEqual(r.returncode, 78, (svc, r.stdout, r.stderr))
            self.assertIn("is not a mounted volume", r.stderr)
            self.assertIn("AFCLAUDE_DATA", r.stderr)

    def test_a_wrong_master_key_stops_each_service_with_exit_78(self):
        wrong = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        for svc in ("mcp", "web"):
            r = self.run_image(svc, "-e", f"AFCLAUDE_MASTER_KEY={wrong}")
            self.assertEqual(r.returncode, 78, (svc, r.stdout, r.stderr))
            self.assertIn("master key", r.stderr)
        self.assertEqual(os.stat(os.path.join(self.data, "master.key")).st_mode & 0o777, 0o600)    # nothing replaced

    def test_the_recreate_keeps_the_key_and_the_data(self):
        def key_bytes():
            with open(os.path.join(self.data, "master.key"), "rb") as fh:
                return fh.read()
        before = key_bytes()
        r = self.compose("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "240", timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-1000:])
        self.assertEqual(key_bytes(), before)
        self.assertEqual(self.request("GET", "/health")[0], 200)


SSH_HOST_KEY = "/etc/ssh/ssh_host_ed25519_key.pub"
SINGLE = os.path.join(HERE, "docker", "compose.yml")


@unittest.skipIf(NO_DOCKER is not None or not os.path.exists(SSH_HOST_KEY), f"container test: {NO_DOCKER or 'no host key to mount'}")
class SingleLayoutInContainer(unittest.TestCase):
    """D-339 (review k4a H1): docker/compose.yml, the default until K7, still brings up ONE container whose runner
    supervises the inbox API (`web.py serve --supervised`), with no master key. Its own project, network (own
    subnet), container name and image tag: compose.yml fixes `afclaude` and `afclaude-manager:latest`, so an
    override renames both, and `docker compose config` is checked before anything starts. Never the live ones."""

    @classmethod
    def setUpClass(cls):
        cls.started = False
        suffix = unique_suffix()
        cls.project = f"afk4s{suffix}"
        cls.tag = f"k4s{suffix}"
        cls.root = tempfile.mkdtemp(prefix="afk4s-", dir="/tmp")
        cls.repo = os.path.join(cls.root, "repo")
        cls.home = os.path.join(cls.root, "home")
        cls.data = os.path.join(cls.repo, "data")
        for d in (cls.data, os.path.join(cls.repo, "docker", "secrets"), os.path.join(cls.home, ".claude", "projects"),
                  os.path.join(cls.home, ".claude", "sessions"), os.path.join(cls.home, ".config", "afclaude")):
            os.makedirs(d)
        os.chmod(cls.data, 0o700)
        cls.key = secrets.token_hex(20)
        for path, text in ((os.path.join(cls.repo, "docker", "secrets", "host_bridge_ed25519"), "x\n"),
                           (os.path.join(cls.home, ".config", "afclaude", "quickview.key"), cls.key + "\n")):
            with open(path, "w") as fh:
                fh.write(text)
            os.chmod(path, 0o644)
        require_files(os.path.join(cls.repo, "docker", "secrets", "host_bridge_ed25519"),
                      os.path.join(cls.home, ".config", "afclaude", "quickview.key"), SSH_HOST_KEY)
        cls.override = os.path.join(cls.root, "override.yml")
        with open(cls.override, "w") as fh:
            fh.write("services:\n  afclaude:\n"
                     f"    image: afclaude:{cls.tag}\n    container_name: {cls.project}\n"
                     "    healthcheck: {interval: 3s, timeout: 20s, retries: 5, start_period: 0s}\n")
        cls.env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")}
        cls.env.update(AFCLAUDE_HOST_REPO=cls.repo, HOST_HOME=cls.home, AFCLAUDE_BRIDGE_USER="nobody",
                       AFCLAUDE_SCHEDULER="runner", AFCLAUDE_SUBNET="172.31.213.0/24", AFCLAUDE_CODE_VERSION=cls.tag)
        subprocess.run([sys.executable, os.path.join(HERE, "store.py"), "init", "--db", os.path.join(cls.data, "afclaude.db")],
                       check=True, capture_output=True, env=dict(os.environ, AFCLAUDE_DATA_DIR=cls.data))
        cfg = cls.compose("config", "--format", "json", check=True)
        names = json.loads(cfg.stdout)
        svc = names["services"]["afclaude"]
        assert svc["container_name"] == cls.project and svc["image"] == f"afclaude:{cls.tag}", "would touch the live container"
        assert names["name"] == cls.project, "would join the live compose project"
        cls.started = True        # from here down() cleans up
        cls.compose("build", timeout=900, check=True)
        cls.up = cls.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=300)

    @classmethod
    def compose(cls, *args, timeout=120, check=False):
        cmd = ["docker", "compose", "--env-file", "/dev/null", "-p", cls.project, "-f", SINGLE, "-f", cls.override, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=cls.env, cwd=HERE)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {r.stderr[-1500:]}")
        return r

    @classmethod
    def tearDownClass(cls):
        if cls.started:
            cls.compose("down", "--volumes", "--remove-orphans", timeout=180)
            subprocess.run(["docker", "rmi", f"afclaude:{cls.tag}"], capture_output=True, timeout=60)
        if getattr(cls, "root", "").startswith("/tmp/afk4s-"):
            shutil.rmtree(cls.root, ignore_errors=True)

    def test_one_healthy_container_whose_runner_supervises_the_inbox_api(self):
        self.assertEqual(self.up.returncode, 0, self.up.stderr[-1500:])
        info = json.loads(subprocess.run(["docker", "inspect", self.project], capture_output=True, text=True, timeout=30,
                                         check=True).stdout)[0]
        self.assertEqual((info["State"]["Status"], info["State"]["Health"]["Status"]), ("running", "healthy"))
        ps = subprocess.run(["docker", "exec", self.project, "ps", "-eo", "args"], capture_output=True, text=True, timeout=60)
        self.assertIn("web.py serve --supervised", ps.stdout)
        self.assertIn("runner.py serve", ps.stdout)
        self.assertNotIn("mcp_service.py", ps.stdout)

    def test_the_inbox_api_answers_on_the_socket_and_no_master_key_is_made(self):
        dfd = os.open(os.path.join(self.data, "api"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            class Conn(http.client.HTTPConnection):
                def connect(inner):
                    inner.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    inner.sock.settimeout(15)
                    inner.sock.connect(f"/proc/self/fd/{dfd}/inbox.sock")
            for key, want in ((None, 401), (self.key, 200)):
                c = Conn("localhost")
                c.request("GET", "/afclaude/api/inbox", headers={"X-AFClaude-Key": key} if key else {})
                r = c.getresponse()
                r.read()
                c.close()
                self.assertEqual(r.status, want)
        finally:
            os.close(dfd)
        self.assertFalse(os.path.exists(os.path.join(self.data, "master.key")))       # single: no key before K7
        self.assertTrue(os.path.isfile(os.path.join(self.data, "inbox_api.log")))      # the child's own log


if __name__ == "__main__":
    unittest.main()
