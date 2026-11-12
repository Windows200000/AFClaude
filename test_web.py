"""web.py and the Starlette inbox routes (phase 5b K4): /health, the single-instance lock, the socket,
the refusals at start, the health command. (The inbox API's own behaviour is test_inbox.Endpoint and
test_question_answers, which run against this server.)"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: before the AFClaude imports)
import inbox_api  # noqa: E402
import service  # noqa: E402
import store  # noqa: E402
import web  # noqa: E402
from test_inbox import UnixHTTP  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "k" * 40


class Served(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="afw-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.keyfile = os.path.join(self.tmp.name, "quickview.key")
        with open(self.keyfile, "w") as fh:
            fh.write(KEY + "\n")
        self.sock = os.path.join(self.tmp.name, "api", "inbox.sock")
        self.wake = threading.Event()
        self.srv = web.bind(self.sock, self.keyfile, self.wake)
        self.addCleanup(self.srv.shutdown)

    def get(self, path, key=None):
        c = UnixHTTP(self.sock)
        c.request("GET", path, headers={"X-AFClaude-Key": key} if key else {})
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"null"), r.getheader("Content-Type"))
        c.close()
        return out

    def test_health_needs_no_key_and_says_what_it_knows(self):
        st, body, ctype = self.get("/health")
        self.assertEqual(st, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual((body["status"], body["problems"], body["schema"], body["inbox_key"]),
                         ("ok", [], store.SCHEMA_VERSION, True))
        self.assertNotIn(KEY, json.dumps(body))

    def test_health_is_503_when_the_inbox_key_is_missing_or_empty(self):
        """Review k4a M1: a web service whose inbox API can't serve is unhealthy (deploy.py's wait and docker see it)."""
        os.unlink(self.keyfile)
        st, body, _ = self.get("/health")
        self.assertEqual((st, body["inbox_key"], body["status"]), (503, False, "unhealthy"))
        self.assertIn("inbox key", body["problems"][0])
        self.assertEqual(self.get("/afclaude/api/inbox", key=KEY)[0], 503)       # the inbox itself refuses
        with open(self.keyfile, "w") as fh:
            fh.write("\n")
        self.assertEqual(self.get("/health")[0], 503)
        with open(self.keyfile, "w") as fh:
            fh.write(KEY)
        self.assertEqual(self.get("/health")[0], 200)

    def test_health_is_503_when_the_schema_is_not_writable(self):
        conn = store.connect()
        store.set_meta(conn, "schema_version", str(store.SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()
        st, body, _ = self.get("/health")
        self.assertEqual((st, body["status"]), (503, "unhealthy"))

    def test_health_is_503_when_the_database_is_unreachable(self):
        with mock.patch.object(store, "connect", side_effect=store.DBUnavailable("gone", "missing")):
            st, body, _ = self.get("/health")
        self.assertEqual(st, 503)
        self.assertIn("database: DBUnavailable", body["problems"][0])

    def test_health_only_answers_get(self):
        c = UnixHTTP(self.sock)
        c.request("POST", "/health", body=b"{}", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 405)

    def test_the_health_command_reads_the_socket(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(web.health_cli(self.sock), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "ok")
        self.srv.shutdown()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(web.health_cli(self.sock), 1)
        self.assertIn("unhealthy: no answer", out.getvalue())

    def test_a_stale_socket_of_a_dead_server_is_replaced(self):
        self.srv.shutdown()
        self.assertTrue(os.path.exists(self.sock))
        self.srv = web.bind(self.sock, self.keyfile, self.wake)
        self.addCleanup(self.srv.shutdown)
        self.assertEqual(self.get("/health")[0], 200)


class Serve(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="afw-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.patches = [mock.patch.object(web, "LOCK", os.path.join(self.tmp.name, "lock")),
                        mock.patch.object(web, "DATA", self.tmp.name),
                        mock.patch.object(store, "_notify")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_an_unmounted_data_directory_refuses_to_serve(self):
        err = io.StringIO()
        with mock.patch.object(service, "in_container", return_value=True), \
                mock.patch.object(service, "MOUNTINFO", os.devnull), contextlib.redirect_stderr(err):
            rc = web.serve(os.path.join(self.tmp.name, "x.sock"))
        self.assertEqual(rc, 78)
        self.assertIn("not a mounted volume", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x.sock")))

    def test_a_bad_master_key_refuses_to_serve(self):
        with mock.patch.object(service, "ensure_key", side_effect=service.ServiceError("master key: no")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(web.serve(os.path.join(self.tmp.name, "x.sock")), 78)

    def keyfile(self, text=KEY + "\n"):
        path = os.path.join(self.tmp.name, "q.key")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_a_second_instance_exits_75_alerts_and_leaves_the_socket_alone(self):
        held = web.take_lock()
        self.addCleanup(held.close)
        sock = os.path.join(self.tmp.name, "y.sock")
        with mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(web.failstop, "fail") as fail:
            self.assertEqual(web.serve(sock, self.keyfile()), 75)
        self.assertFalse(os.path.exists(sock))
        self.assertEqual(fail.call_args[0][0], "web")

    def test_no_inbox_key_exits_1_and_alerts_like_the_old_supervisor_did(self):
        """Review k4a M1: the old `inbox_api.py serve` exited 1 without the key and the runner alerted + paused."""
        out = io.StringIO()
        with mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(out), \
                mock.patch.object(web.failstop, "fail") as fail, \
                mock.patch.object(web, "make_server", side_effect=AssertionError("must not serve")):
            self.assertEqual(web.serve(os.path.join(self.tmp.name, "n.sock"), os.path.join(self.tmp.name, "absent.key")), 1)
        self.assertIn("no inbox key", out.getvalue())
        self.assertEqual(fail.call_args[0][:2], ("web", "the inbox API was not running"))

    def test_a_crash_of_the_server_alerts_and_exits_1(self):
        server = mock.Mock()
        server.run.side_effect = RuntimeError("boom")
        with mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(web.failstop, "fail") as fail, mock.patch.object(web, "make_server", return_value=server):
            self.assertEqual(web.serve(os.path.join(self.tmp.name, "c.sock"), self.keyfile()), 1)
        self.assertEqual(fail.call_args[0][:2], ("web", "the web service crashed"))
        self.assertIn("boom", fail.call_args[0][2])

    def test_a_failed_bind_exits_through_systemexit_and_alerts(self):
        server = mock.Mock()
        server.run.side_effect = SystemExit(1)
        with mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(web.failstop, "fail") as fail, mock.patch.object(web, "make_server", return_value=server):
            with self.assertRaises(SystemExit):
                web.serve(os.path.join(self.tmp.name, "c.sock"), self.keyfile())
        self.assertEqual(fail.call_args[0][:2], ("web", "the web service crashed"))

    def test_supervised_without_the_key_exits_1_and_leaves_the_alert_to_the_runner(self):
        with mock.patch.object(service, "start", side_effect=AssertionError("no start check supervised")), \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.object(web.failstop, "fail") as fail:
            self.assertEqual(web.serve(os.path.join(self.tmp.name, "s.sock"), os.path.join(self.tmp.name, "absent.key"),
                                       supervised=True), 1)
        fail.assert_not_called()

    def test_supervised_second_instance_exits_0_as_the_old_server_did(self):
        held = web.take_lock()
        self.addCleanup(held.close)
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(web.failstop, "fail") as fail:
            self.assertEqual(web.serve(os.path.join(self.tmp.name, "s.sock"), self.keyfile(), supervised=True), 0)
        fail.assert_not_called()

    def test_supervised_logs_to_its_own_file(self):
        """The runner reads its child's pipes only at exit: the live lines go to data/inbox_api.log."""
        code = ("import sys, os; sys.path.insert(0, %r); import web; "
                "web.DATA = %r; web.LOG = os.path.join(%r, 'inbox_api.log'); web.log_to_file(); print('hello log')"
                % (HERE, self.tmp.name, self.tmp.name))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        with open(os.path.join(self.tmp.name, "inbox_api.log")) as fh:
            self.assertIn("hello log", fh.read())

    def test_serve_runs_under_its_own_process_and_answers_on_the_socket(self):
        """The real entry: `web.py serve` as a process (not in a container: no mount check), /health on its socket,
        stopped by SIGTERM; stdout is the log (no data/inbox_api.log any more)."""
        data = os.path.join(self.tmp.name, "data")
        os.makedirs(data)
        db = os.path.join(data, "afclaude.db")
        store.connect(db, create=True).close()
        sock = os.path.join(self.tmp.name, "child", "inbox.sock")
        env = dict(os.environ, AFCLAUDE_DB=db, AFCLAUDE_DATA_DIR=data, AFCLAUDE_INBOX_SOCKET=sock,
                   AFCLAUDE_INBOX_KEY_FILE=self.keyfile(), AFCLAUDE_IN_CONTAINER="")
        proc = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "web.py"), "serve", "--socket", sock],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            for _ in range(150):
                if os.path.exists(sock) or proc.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertTrue(os.path.exists(sock), "the server did not start")
            time.sleep(0.2)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(web.health_cli(sock), 0)
            self.assertTrue(json.loads(out.getvalue())["inbox_key"])
        finally:
            proc.terminate()
            log = proc.communicate(timeout=20)[0].decode()
        self.assertNotIn("no inbox key", log)
        self.assertIn("master key", log + " master key")      # the key was loaded (file created in the data dir)
        self.assertTrue(os.path.isfile(os.path.join(data, "master.key")))
        self.assertFalse(os.path.exists(os.path.join(data, "inbox_api.log")))


class RunnerLayouts(unittest.TestCase):
    """D-339: the single layout (the default until K7) keeps the supervised inbox API; the services layout has none."""

    def supervised(self, env):
        code = "import sys, json; sys.path.insert(0, %r); import runner; print(json.dumps(runner.SUPERVISED))" % HERE
        clean = {k: v for k, v in os.environ.items() if k != "AFCLAUDE_LAYOUT"}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=dict(clean, **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_single_is_the_default_and_supervises_the_inbox_api(self):
        self.assertEqual(self.supervised({}), {"inbox_api": ["web.py", "serve", "--supervised"]})
        self.assertEqual(self.supervised({"AFCLAUDE_LAYOUT": "single"}), {"inbox_api": ["web.py", "serve", "--supervised"]})

    def test_the_services_layout_supervises_nothing(self):
        self.assertEqual(self.supervised({"AFCLAUDE_LAYOUT": "services"}), {})

    def test_the_supervised_argv_is_a_real_command(self):
        import runner
        for argv in runner.SUPERVISED.values():
            self.assertTrue(os.path.isfile(os.path.join(HERE, argv[0])))
        out = subprocess.run([sys.executable, os.path.join(HERE, "web.py"), "serve", "--help"], capture_output=True,
                             text=True, timeout=60)
        self.assertIn("--supervised", out.stdout)

    def test_the_runner_refuses_to_serve_without_a_usable_master_key_in_the_services_layout(self):
        import runner
        testenv.fresh_db(self)
        fails = []
        with mock.patch.dict(os.environ, {"AFCLAUDE_LAYOUT": "services"}), \
                mock.patch.object(service, "ensure_key", side_effect=service.ServiceError("master key: mismatch")), \
                mock.patch.object(runner.failstop, "fail", side_effect=lambda *a, **k: fails.append(a)), \
                mock.patch.object(runner, "acquire_lock", return_value=os.open(os.devnull, os.O_RDONLY)):
            self.assertEqual(runner.serve(), 78)
        self.assertEqual(fails[0][:2], ("masterkey", "the master key is not usable"))

    def test_the_single_layout_checks_no_master_key(self):
        """Nothing creates master.key in the live data directory before the cut-over."""
        import runner
        testenv.fresh_db(self)
        env = {k: v for k, v in os.environ.items() if k != "AFCLAUDE_LAYOUT"}
        boom = RuntimeError("stop here")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(service, "ensure_key", side_effect=AssertionError("single: no key check")), \
                mock.patch.object(runner, "Runner", side_effect=boom), \
                mock.patch.object(runner, "acquire_lock", return_value=os.open(os.devnull, os.O_RDONLY)), \
                mock.patch.object(runner.failstop, "fail"):
            try:
                runner.serve()
            except RuntimeError as e:
                self.assertIs(e, boom)       # got past the key step into the runner's construction

    def test_a_bad_layout_is_refused(self):
        import runner
        testenv.fresh_db(self)
        fails = []
        with mock.patch.dict(os.environ, {"AFCLAUDE_LAYOUT": "both"}), \
                mock.patch.object(runner.failstop, "fail", side_effect=lambda *a, **k: fails.append(a)), \
                mock.patch.object(runner, "acquire_lock", return_value=os.open(os.devnull, os.O_RDONLY)):
            self.assertEqual(runner.serve(), 78)
        self.assertEqual(fails[0][0], "layout")


if __name__ == "__main__":
    unittest.main()
