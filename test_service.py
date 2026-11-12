"""service.py (phase 5b K4): the start checks the three services share, and the /health report."""
import base64
import contextlib
import io
import os
import secrets
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: before the AFClaude imports)
import masterkey  # noqa: E402
import service  # noqa: E402
import store  # noqa: E402

MOUNTINFO = (
    "22 1 0:21 / / rw - overlay overlay rw\n"
    "40 22 8:1 /srv/data /app/data rw,relatime - ext4 /dev/sda1 rw\n"
    "41 22 8:1 /srv/odd /app/with\\040space rw - ext4 /dev/sda1 rw\n"
    "42 22 0:30 / /tmp rw - tmpfs tmpfs rw\n")


def mountinfo_file(test, text=MOUNTINFO):
    fh = tempfile.NamedTemporaryFile("w", suffix=".mountinfo", delete=False)
    fh.write(text)
    fh.close()
    test.addCleanup(os.unlink, fh.name)
    return fh.name


class MountCheck(unittest.TestCase):
    def test_mount_points_are_read_from_mountinfo_with_escapes(self):
        points = service.mount_points(mountinfo_file(self))
        self.assertEqual(points, {"/", "/app/data", "/app/with space", "/tmp"})

    def test_a_mounted_directory_passes(self):
        service.check_mounted("/app/data", mountinfo_file(self))

    def test_an_unmounted_directory_is_refused_with_the_fix(self):
        with self.assertRaises(service.ServiceError) as cm:
            service.check_mounted("/app/other", mountinfo_file(self))
        msg = str(cm.exception)
        self.assertIn("/app/other is not a mounted volume", msg)
        self.assertIn("AFCLAUDE_DATA", msg)
        self.assertIn("no default", msg)

    def test_a_subdirectory_of_a_mount_is_not_the_mount(self):
        with self.assertRaises(service.ServiceError):
            service.check_mounted("/app/data/sub", mountinfo_file(self))

    def test_the_data_directory_is_the_databases(self):
        self.assertEqual(service.data_dir(), os.path.dirname(os.path.abspath(store.DB_PATH)))


class KeyAndStart(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.data = os.path.dirname(store.DB_PATH)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(masterkey.ENV, None)
        saved = masterkey._key
        self.addCleanup(setattr, masterkey, "_key", saved)

    def test_ensure_key_creates_the_key_once_and_returns_its_id(self):
        first = service.ensure_key()
        self.assertTrue(os.path.isfile(os.path.join(self.data, masterkey.KEY_FILE)))
        self.assertEqual(service.ensure_key(), first)

    def test_a_wrong_key_is_a_service_error_and_nothing_is_replaced(self):
        service.ensure_key()
        with open(os.path.join(self.data, masterkey.KEY_FILE), "rb") as fh:
            before = fh.read()
        os.environ[masterkey.ENV] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        with self.assertRaises(service.ServiceError) as cm:
            service.ensure_key()
        self.assertIn("master key", str(cm.exception))
        with open(os.path.join(self.data, masterkey.KEY_FILE), "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_missing_database_is_a_service_error(self):
        with mock.patch.object(store, "connect", side_effect=store.DBUnavailable("no database", "missing")):
            with self.assertRaises(service.ServiceError) as cm:
                service.ensure_key()
        self.assertIn("master key can't be checked", str(cm.exception))

    def test_start_checks_the_mount_unless_told_not_to(self):
        with self.assertRaises(service.ServiceError):
            service.start("web")             # this test's data directory is no mount point
        self.assertTrue(service.start("web", mounted=False))
        with self.assertRaises(service.ServiceError):
            service.start("nope", mounted=False)

    def test_in_container_reads_the_image_flag(self):
        self.assertTrue(service.in_container({"AFCLAUDE_IN_CONTAINER": "1"}))
        self.assertFalse(service.in_container({"AFCLAUDE_IN_CONTAINER": ""}))
        self.assertFalse(service.in_container({}))


class Refusal(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        patch = mock.patch.object(store, "_notify")      # no push from a test
        self.notified = patch.start()
        self.addCleanup(patch.stop)

    def run_main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = service.main(list(argv))
        return rc, err.getvalue()

    def test_an_unmounted_data_directory_exits_78_with_the_message_and_no_alert_file(self):
        with mock.patch.object(service, "MOUNTINFO", mountinfo_file(self, "22 1 0:21 / / rw - overlay overlay rw\n")):
            rc, err = self.run_main("check-mount", "web")
        self.assertEqual(rc, service.EXIT_REFUSED)
        self.assertEqual(service.EXIT_REFUSED, 78)
        self.assertIn("afclaude web: refusing to start", err)
        self.assertIn("not a mounted volume", err)
        self.assertFalse(os.path.exists(store.fallback_path()))      # nowhere to write one (read-only image)
        self.notified.assert_not_called()

    def test_a_key_problem_alerts_the_owner_once(self):
        with mock.patch.object(service, "ensure_key", side_effect=service.ServiceError("master key: mismatch")):
            rc, err = self.run_main("ensure-key", "mcp")
            rc2, _ = self.run_main("ensure-key", "mcp")
        self.assertEqual((rc, rc2), (78, 78))
        self.assertIn("master key: mismatch", err)
        self.assertEqual(self.notified.call_count, 1)                 # one alert per episode
        self.assertTrue(os.path.exists(store.fallback_path()))

    def test_an_alert_that_cannot_be_written_is_said_on_stderr(self):
        with mock.patch.object(store, "alert_once", side_effect=OSError("read-only")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(service.refuse("web", service.ServiceError("x")), 78)
        self.assertIn("could not be alerted either: OSError: read-only", err.getvalue())

    def test_usage_errors(self):
        for argv in ([], ["check-mount"], ["check-mount", "nope"], ["frobnicate", "web"]):
            self.assertEqual(self.run_main(*argv)[0], 2, argv)

    def test_ensure_key_command_prints_the_key_id(self):
        out = io.StringIO()
        with mock.patch.object(service, "ensure_key", return_value="abc123"), contextlib.redirect_stdout(out):
            self.assertEqual(service.main(["ensure-key", "runner"]), 0)
        self.assertIn("master key abc123 loaded", out.getvalue())


class HealthReport(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def test_ok_without_a_runner_heartbeat(self):
        r = service.health_report(self.conn)
        self.assertEqual((r["status"], r["problems"], r["schema"], r["runner_heartbeat_age_s"], r["inbox_key"]),
                         ("ok", [], store.SCHEMA_VERSION, None, None))

    def test_the_runner_heartbeat_age_is_reported_not_judged(self):
        now = datetime.now(timezone.utc)
        store.put_runner_job(self.conn, "runner", {"heartbeat_at": store.iso(now - timedelta(minutes=30))})
        self.conn.commit()
        r = service.health_report(self.conn, now=now)
        self.assertEqual((r["status"], r["runner_heartbeat_age_s"]), ("ok", 1800))

    def test_the_inbox_key_file_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "k")
            self.assertFalse(service.health_report(self.conn, key_file=path)["inbox_key"])
            open(path, "w").close()
            self.assertFalse(service.health_report(self.conn, key_file=path)["inbox_key"])     # empty = no key
            with open(path, "w") as fh:
                fh.write("secret\n")
            r = service.health_report(self.conn, key_file=path)
            self.assertTrue(r["inbox_key"])
            self.assertNotIn("secret", str(r))                                                 # never the key itself

    def test_a_schema_this_code_may_not_write_is_unhealthy(self):
        store.set_meta(self.conn, "schema_version", str(store.SCHEMA_VERSION + 1))
        self.conn.commit()
        r = service.health_report(self.conn)
        self.assertEqual(r["status"], "unhealthy")
        self.assertTrue(r["problems"][0].startswith("schema:"))


if __name__ == "__main__":
    unittest.main()
