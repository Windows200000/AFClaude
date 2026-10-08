#!/usr/bin/env python3
"""Offline tests for the settings in the DB (phase 2a, D-146; docs/dashboard_design.md §7.4):
the one-time import of the file tunables (data/afclaude.json, data/dispatcher.json, the pacing
keys of data/user_model.json) through actions.py, the accessor afclaude_config.setting()
(DB value > code default; the DB unavailable -> the code defaults, logged), and that the
runners read the DB values (keepalive here; pacing in test_pacing, the dispatcher in
test_dispatcher). Temp files and temp DBs only, never the live data/.

    python3 -m unittest test_settings
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import afclaude_config as ac  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
SID = "00000000-0000-4000-8000-000000000001"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class FileCase(unittest.TestCase):
    """Own temp DB and own config files per test."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.saved = (store.DB_PATH, ac.CONFIG_FILE, ac.DISPATCHER_CONFIG_FILE, ac.USER_MODEL_FILE,
                      ac._IMPORT_CHECKED, ac.LOG)
        store.DB_PATH = os.path.join(t, "afclaude.db")
        ac.CONFIG_FILE, ac.DISPATCHER_CONFIG_FILE, ac.USER_MODEL_FILE = (
            os.path.join(t, "afclaude.json"), os.path.join(t, "dispatcher.json"), os.path.join(t, "user_model.json"))
        self.logs = []
        ac.LOG = self.logs.append
        ac._IMPORT_CHECKED = False
        ac._LOGGED.clear()

    def tearDown(self):
        (store.DB_PATH, ac.CONFIG_FILE, ac.DISPATCHER_CONFIG_FILE, ac.USER_MODEL_FILE,
         ac._IMPORT_CHECKED, ac.LOG) = self.saved
        ac._LOGGED.clear()
        self.tmp.cleanup()

    def write(self, path, d):
        with open(path, "w") as fh:
            json.dump(d, fh)

    def read(self, path):
        with open(path) as fh:
            return json.load(fh)

    def db(self):
        # like a real setup's explicit init: a test that wants a database manipulates one into
        # existence itself, same as `python3 store.py init` would (tests that instead check the
        # "no database yet" behavior, e.g. test_unavailable_db_logs_once_..., never call this).
        return store.connect(store.DB_PATH, create=True)

    def saved_settings(self):
        conn = self.db()
        try:
            return {k: r["value"] for k, r in store.setting_rows(conn).items()}
        finally:
            conn.close()

    def imports(self):
        conn = self.db()
        try:
            return [r for r in reversed(store.audit_log(conn, limit=100, target_type="setting"))
                    if r["action"] == "setting.import"]
        finally:
            conn.close()


class Import(FileCase):
    """The one-time import assumes a real install's own database already exists (it predates
    only the settings table, not the whole DB): pre-create it, like setup would have."""
    IDENTITY = {"_comment": "local", "manager_session": SID, "trust_root": "/tmp/x",
                "dashboard": {"base_path": "/p/"}}

    def setUp(self):
        super().setUp()
        store.connect(store.DB_PATH, create=True).close()

    def full_files(self):
        self.write(ac.CONFIG_FILE, dict(self.IDENTITY, window_start="22:00", window_hours=10, last_mile_hours=3,
                                        reserve_threshold="auto", usage_model="linear"))
        self.write(ac.DISPATCHER_CONFIG_FILE, {"keepalive_sessions": [SID], "exclude_sessions": [],
                                               "verify_minutes": 20, "take_over_idle": True,
                                               "max_concurrent": 2, "session_usage_stop": 80})
        self.write(ac.USER_MODEL_FILE, {"schema": "afclaude.user_model/1", "envelope_weekly_pct_by_hours": {"1": 9},
                                        "idle_min": 45, "session_cap": 85})

    def test_moves_the_tunables_and_keeps_identity(self):
        self.full_files()
        res = ac.import_file_settings()
        self.assertEqual([r["source"] for r in res], ["data/afclaude.json", "data/dispatcher.json",
                                                      "data/user_model.json"])
        self.assertEqual(self.saved_settings(), {
            "window_days": {d: {"start": "22:00", "n": 2, "group": "weekly"} for d in DAYS},
            "last_mile_hours": 3.0, "usage_model": "linear", "stall_verify_minutes": 20.0, "pacing_idle_min": 45.0})
        # values equal to the code default are not saved: they stay "default" (and follow it)
        self.assertEqual(res[0]["default"], ["reserve_threshold"])
        self.assertEqual(sorted(res[1]["default"]), ["stall_take_over_idle"])
        self.assertEqual(res[2]["default"], ["pacing_session_cap"])
        self.assertIn("obsolete", res[1]["notes"]["max_concurrent"])
        self.assertIn("obsolete", res[1]["notes"]["session_usage_stop"])
        # the files keep only machine identity (and the user model its predictor data)
        self.assertEqual(self.read(ac.CONFIG_FILE), self.IDENTITY)
        self.assertEqual(self.read(ac.DISPATCHER_CONFIG_FILE), {"keepalive_sessions": [SID], "exclude_sessions": []})
        self.assertEqual(self.read(ac.USER_MODEL_FILE), {"schema": "afclaude.user_model/1",
                                                         "envelope_weekly_pct_by_hours": {"1": 9}})
        self.assertEqual(ac.manager_session(), SID)
        # one audit row per file, by the runner (D-154), nothing lost: notes and defaults too
        rows = self.imports()
        self.assertEqual([(r["actor"], r["via"], r["target_id"]) for r in rows],
                         [("runner:import", "runner", "data/afclaude.json"),
                          ("runner:import", "runner", "data/dispatcher.json"),
                          ("runner:import", "runner", "data/user_model.json")])
        self.assertEqual(rows[0]["after"]["settings"]["last_mile_hours"], 3.0)
        self.assertIn("max_concurrent", rows[1]["after"]["notes"])
        self.assertEqual(rows[2]["after"]["settings"], {"pacing_idle_min": 45.0})
        self.assertTrue(any("imported from data/afclaude.json" in ln for ln in self.logs), self.logs)

    def test_idempotent_and_the_db_wins(self):
        self.full_files()
        ac.import_file_settings()
        n = len(self.imports())
        before = {p: self.read(p) for p in (ac.CONFIG_FILE, ac.DISPATCHER_CONFIG_FILE, ac.USER_MODEL_FILE)}
        self.assertEqual(ac.import_file_settings(), [])               # nothing left to import
        self.assertEqual(len(self.imports()), n)
        self.assertEqual({p: self.read(p) for p in before}, before)   # files untouched
        # a tunable that shows up again (e.g. an old copy of the file) never overrides the DB
        conn = self.db()
        actions.perform(conn, "setting.set", {"key": "last_mile_hours", "value": 4}, actor="owner", via="dashboard")
        conn.close()
        self.write(ac.CONFIG_FILE, dict(self.IDENTITY, last_mile_hours=1))
        res = ac.import_file_settings()
        self.assertEqual((res[0]["imported"], res[0]["kept"]), ({}, {"last_mile_hours": 4.0}))
        self.assertEqual(self.saved_settings()["last_mile_hours"], 4.0)
        self.assertEqual(self.read(ac.CONFIG_FILE), self.IDENTITY)

    def test_legacy_invalid_and_partial_sessions(self):
        self.write(ac.CONFIG_FILE, {"manager_session": SID, "week_target": 92, "window_hours": 7.5,
                                    "usage_model": "reserve", "last_mile_hours": -1})
        res = ac.import_file_settings()[0]
        self.assertEqual(res["imported"], {"reserve_threshold": 92.0})
        self.assertEqual(res["default"], ["window_days"])               # 23:00 x 2 = the default window
        self.assertIn("legacy week_target", res["notes"]["week_target"])
        self.assertIn("not whole 5-h session windows (D-148): imported as 2 x 5 h", res["notes"]["window_hours"])
        self.assertIn("invalid", res["notes"]["usage_model"])           # the retired "reserve" model
        self.assertIn("0..168", res["notes"]["last_mile_hours"])
        self.assertEqual(self.read(ac.CONFIG_FILE), {"manager_session": SID})
        self.assertEqual(self.imports()[0]["after"]["notes"]["usage_model"], res["notes"]["usage_model"])

    def test_bad_window_and_pacing_object(self):
        self.write(ac.CONFIG_FILE, {"window_start": "23:15"})
        self.write(ac.USER_MODEL_FILE, {"idle_min": 10, "pacing": {"idle_min": 20, "last_mile_yield": False,
                                                                   "forecast_margin": 1.25}})
        res = ac.import_file_settings()
        self.assertEqual(res[0]["imported"], {})
        self.assertIn("30-minute grid", res[0]["notes"]["window_start"])
        # as pacing.py read it: the "pacing" object wins; the shadowed top-level key is dropped
        self.assertEqual(res[1]["imported"], {"pacing_idle_min": 20.0, "pacing_last_mile_yield": False})
        self.assertIn("ignored before", res[1]["notes"]["idle_min"])
        self.assertEqual(self.read(ac.USER_MODEL_FILE), {"pacing": {"forecast_margin": 1.25}})

    def test_first_use_imports(self):
        self.write(ac.CONFIG_FILE, {"manager_session": SID, "usage_model": "linear", "last_mile_hours": 0})
        self.assertEqual(ac.setting("usage_model"), "linear")          # the first read imports first
        self.assertEqual(ac.last_mile_setting(), 0.0)
        self.assertEqual(self.read(ac.CONFIG_FILE), {"manager_session": SID})
        self.write(ac.CONFIG_FILE, {"usage_model": "pacing"})          # once per process
        self.assertEqual(ac.setting("usage_model"), "linear")
        self.assertEqual(self.read(ac.CONFIG_FILE), {"usage_model": "pacing"})

    def test_a_failed_import_leaves_the_file(self):
        self.write(ac.CONFIG_FILE, {"usage_model": "linear"})
        os.remove(store.DB_PATH)
        os.makedirs(store.DB_PATH)                                     # the DB path is a directory
        self.assertEqual(ac.setting("usage_model"), "pacing")          # code default, no crash
        self.assertEqual(self.read(ac.CONFIG_FILE), {"usage_model": "linear"})
        self.assertTrue(any("settings import from data/afclaude.json failed" in ln for ln in self.logs), self.logs)

    def test_import_validates_its_input(self):
        conn = self.db()
        try:
            for params, msg in (({"source": "x", "values": {"nope": 1}}, "unknown setting"),
                                ({"source": "x", "values": []}, "values must be"),
                                ({"source": "", "values": {}}, "source must not be empty"),
                                ({"source": "x", "values": {}, "notes": {"a": 1}}, "notes must be")):
                with self.assertRaisesRegex(ValueError, msg):
                    actions.perform(conn, "setting.import", params, actor="runner:import", via="runner")
            with self.assertRaises(actions.Forbidden):                 # owner-only, like setting.set
                actions.perform(conn, "setting.import", {"source": "x", "values": {}}, actor="mcp",
                                via="mcp", autonomous=True)
        finally:
            conn.close()


class Accessor(FileCase):
    def test_db_value_else_code_default(self):
        self.assertEqual(ac.setting("pacing_idle_min"), 60.0)          # no DB at all: the code default
        conn = self.db()
        actions.perform(conn, "setting.set", {"key": "pacing_idle_min", "value": 25}, actor="cli", via="cli")
        conn.close()
        self.assertEqual(ac.setting("pacing_idle_min"), 25.0)
        self.assertEqual(ac.settings("pacing_idle_min", "pacing_min_gap"), {"pacing_idle_min": 25.0,
                                                                            "pacing_min_gap": 1.0})
        self.assertEqual(set(ac.settings()), set(actions.SETTINGS))
        with self.assertRaisesRegex(ValueError, "unknown setting"):
            ac.setting("nope")
        with open(ac.CONFIG_FILE, "w") as fh:                          # no env or file override
            json.dump({"pacing_idle_min": 5}, fh)
        os.environ["AFCLAUDE_PACING_IDLE_MIN"] = "5"
        try:
            self.assertEqual(ac.setting("pacing_idle_min"), 25.0)
        finally:
            del os.environ["AFCLAUDE_PACING_IDLE_MIN"]

    def test_unavailable_db_logs_once_and_uses_the_defaults(self):
        self.assertEqual(ac.setting("usage_model"), "pacing")
        self.assertEqual(ac.setting("usage_model"), "pacing")
        self.assertEqual(len([ln for ln in self.logs if "no database" in ln]), 1)   # once, not every loop
        with open(store.DB_PATH, "w") as fh:
            fh.write("garbage " * 200)
        import schedule
        self.assertEqual(schedule.load(), schedule.Config.every_day())          # the code defaults
        self.assertTrue(any("unreadable (DatabaseError" in ln for ln in self.logs), self.logs)
        os.unlink(store.DB_PATH)
        store.connect(store.DB_PATH, create=True).close()    # restored: a deliberate act, not automatic
        testenv.set_setting("usage_model", "linear", db=store.DB_PATH)
        self.assertEqual(ac.setting("usage_model"), "linear")
        self.assertEqual(ac._LOGGED, set())                            # healthy again: a new problem logs again

    def test_a_newer_schema_is_still_read(self):
        """The schema guard opens a newer DB read-only: the runners keep reading its settings."""
        store.connect(store.DB_PATH, create=True).close()
        testenv.set_setting("usage_model", "linear", db=store.DB_PATH)
        conn = self.db()
        store.set_meta(conn, "schema_version", store.SCHEMA_VERSION + 1)
        conn.commit()
        conn.close()
        self.assertEqual(ac.setting("usage_model"), "linear")


class RunnersReadTheDB(unittest.TestCase):
    """keepalive's window, linear rule and last mile follow the DB on every read."""
    R = datetime(2026, 10, 1, 16, 59, 59, tzinfo=UTC)                  # Thu 18:59:59 Berlin, a weekly reset

    def setUp(self):
        import keepalive
        self.ka = keepalive
        testenv.setcfg(usage_model="linear")

    def tearDown(self):
        testenv.clear_settings()

    def usage(self, pct):
        return {"fetched_at": self.R, "weekly": {"percent": pct, "resets_at": self.R},
                "session": {"percent": 0.0, "resets_at": None}}

    def test_linear_rule_settings(self):
        now = self.R - timedelta(hours=20)                             # Wed 22:59 Berlin, in the window
        go, why = self.ka.budget_decision(self.usage(80), now)
        self.assertFalse(go, why)                                       # projected ~91% >= 90, reset after 11:00
        self.assertIn("weekly reset after 2026-10-01 11:00:00 CEST", why)
        testenv.setcfg(usage_model="linear", projection_threshold=95)
        self.assertTrue(self.ka.budget_decision(self.usage(80), now)[0])
        testenv.setcfg(usage_model="linear", cutoff_after_window_hours=10)   # 09:00 + 10 h = 19:00 >= the reset
        go, why = self.ka.budget_decision(self.usage(80), now)
        self.assertTrue(go, why)
        self.assertIn("but weekly reset <= 2026-10-01 19:00:00 CEST", why)
        extra, text = self.ka.budget_headroom(self.usage(10), now)
        testenv.setcfg(usage_model="linear", projection_threshold=50)
        self.assertLess(self.ka.budget_headroom(self.usage(10), now)[0], extra)

    def test_usage_model_and_last_mile(self):
        now = self.R - timedelta(hours=4)
        self.assertEqual(self.ka.budget_eval(self.usage(95), now)["mode"], "linear")
        self.assertTrue(self.ka.in_last_mile(now, self.usage(95)))
        testenv.setcfg(usage_model="linear", last_mile_hours=0)
        self.assertFalse(self.ka.in_last_mile(now, self.usage(95)))
        self.assertEqual(self.ka.last_mile_hours(95), 0.0)

    def test_the_window_settings_and_the_quickview(self):
        """schedule.py reads window_days / session_hours / window_tz on every call."""
        import export_quickview as qv
        import schedule
        testenv.setcfg(usage_model="linear",
                       window_days={d: {"start": "21:30", "n": 1, "group": "weekly"} for d in DAYS})
        self.assertEqual(schedule.current_or_next(self.R).span(), "21:30–02:30")
        self.assertEqual(qv.window_info(self.R)["window_berlin"], "21:30–02:30")
        testenv.setcfg(usage_model="linear", window_tz="America/New_York", session_hours=4)
        info = qv.window_info(self.R)
        self.assertEqual((info["window_berlin"], info["window_tz"]), ("23:00–07:00", "America/New_York"))
        self.assertEqual(info["next_window_berlin"], "Fri 02.10. 05:00 CEST")   # 23:00 EDT
        testenv.setcfg(usage_model="linear")
        self.assertEqual(qv.window_info(self.R)["window_berlin"], "23:00–09:00")


if __name__ == "__main__":
    unittest.main()
