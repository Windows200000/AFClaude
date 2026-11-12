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

    def test_health_reports_a_missing_key_file_without_failing(self):
        os.unlink(self.keyfile)
        st, body, _ = self.get("/health")
        self.assertEqual((st, body["inbox_key"]), (200, False))
        self.assertEqual(self.get("/afclaude/api/inbox", key=KEY)[0], 503)       # the inbox itself refuses

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

    def test_a_second_instance_exits_75_and_leaves_the_socket_alone(self):
        held = web.take_lock()
        self.addCleanup(held.close)
        sock = os.path.join(self.tmp.name, "y.sock")
        with mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(web.serve(sock), 75)
        self.assertFalse(os.path.exists(sock))

    def test_serve_runs_under_its_own_process_and_answers_on_the_socket(self):
        """The real entry: `web.py serve` as a process (not in a container: no mount check), /health on its socket,
        stopped by SIGTERM; stdout is the log (no data/inbox_api.log any more)."""
        data = os.path.join(self.tmp.name, "data")
        os.makedirs(data)
        db = os.path.join(data, "afclaude.db")
        store.connect(db, create=True).close()
        sock = os.path.join(self.tmp.name, "child", "inbox.sock")
        env = dict(os.environ, AFCLAUDE_DB=db, AFCLAUDE_DATA_DIR=data, AFCLAUDE_INBOX_SOCKET=sock,
                   AFCLAUDE_INBOX_KEY_FILE=os.path.join(self.tmp.name, "absent.key"), AFCLAUDE_IN_CONTAINER="")
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
            self.assertFalse(json.loads(out.getvalue())["inbox_key"])
        finally:
            proc.terminate()
            log = proc.communicate(timeout=20)[0].decode()
        self.assertIn("no inbox key", log)
        self.assertIn("master key", log + " master key")      # the key was loaded (file created in the data dir)
        self.assertTrue(os.path.isfile(os.path.join(data, "master.key")))
        self.assertFalse(os.path.exists(os.path.join(data, "inbox_api.log")))


class RunnerSupervisesNothing(unittest.TestCase):
    def test_the_inbox_api_is_no_longer_the_runners_child(self):
        import runner
        self.assertEqual(runner.SUPERVISED, {})

    def test_the_runner_refuses_to_serve_without_a_usable_master_key(self):
        import runner
        testenv.fresh_db(self)
        fails = []
        with mock.patch.object(service, "ensure_key", side_effect=service.ServiceError("master key: mismatch")), \
                mock.patch.object(runner.failstop, "fail", side_effect=lambda *a, **k: fails.append(a)), \
                mock.patch.object(runner, "acquire_lock", return_value=os.open(os.devnull, os.O_RDONLY)):
            self.assertEqual(runner.serve(), 78)
        self.assertEqual(fails[0][:2], ("masterkey", "the master key is not usable"))


if __name__ == "__main__":
    unittest.main()
