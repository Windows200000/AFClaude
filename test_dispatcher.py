#!/usr/bin/env python3
"""Offline tests for dispatcher.py: temp DB, fixture transcripts, and stubs on
PATH for ka_resume.sh, claude and tmux (so no real session or tmux session is
ever started, resumed or killed)."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import dispatcher as dp  # noqa: E402
import keepalive as ka  # noqa: E402
import stalled  # noqa: E402
import store  # noqa: E402
import pacing as budget  # noqa: E402

_CFG_DIR = tempfile.TemporaryDirectory()


_OLD_BUDGET = (budget.SAMPLES_FILE, budget.FIRE_FILES)


def setUpModule():
    # these tests use linear-rule usage numbers; BudgetModel below checks the budget model.
    # Hermetic: no real samples (the last mile's ratio is the default).
    testenv.setcfg(usage_model="linear")
    budget.SAMPLES_FILE = os.path.join(_CFG_DIR.name, "no_samples.jsonl")
    budget.FIRE_FILES = []


def tearDownModule():
    testenv.clear_settings()
    budget.SAMPLES_FILE, budget.FIRE_FILES = _OLD_BUDGET
    _CFG_DIR.cleanup()

UTC = timezone.utc
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)          # 01:00 Berlin, inside the window
DAY = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)          # 14:00 Berlin, outside
WEEK_RESET = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)


def sid(n):
    return f"{n:08x}-0000-4000-8000-000000000000"      # distinct first 8 = distinct tmux names


def usage(session=10.0, weekly=20.0):
    return {"fetched_at": NOW, "session": {"percent": session, "resets_at": NOW + timedelta(hours=3)},
            "weekly": {"percent": weekly, "resets_at": WEEK_RESET}}


def Z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(u, ts, cwd):
    return {"type": "user", "uuid": u, "timestamp": Z(ts), "cwd": cwd, "isSidechain": False,
            "message": {"role": "user", "content": "do things"}}


def reply(u, ts, cwd, stop="end_turn", model="claude-opus-5-5"):
    return {"type": "assistant", "uuid": u, "timestamp": Z(ts), "cwd": cwd, "isSidechain": False,
            "message": {"role": "assistant", "model": model, "stop_reason": stop,
                        "content": [{"type": "text", "text": "ok"}]}}


def stall(u, ts, cwd, reset="9pm"):
    return {"type": "assistant", "uuid": u, "timestamp": Z(ts), "cwd": cwd, "isSidechain": False,
            "isApiErrorMessage": True, "error": "rate_limit",
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": f"You've hit your session limit · resets {reset} (UTC)"}]}}


STUB_CLAUDE = """#!/bin/sh
echo "$@" >> {calls}
case "$*" in *agents*) echo '[]';; esac
exit 0
"""
STUB_TMUX = """#!/bin/sh
# alive.txt holds the names of the fake tmux sessions that exist
echo "$@" >> {calls}
name=$(echo "$3" | sed 's/^=//')
case "$1" in
  has-session) grep -qx "$name" {alive} 2>/dev/null; exit $?;;
  kill-session) grep -vx "$name" {alive} > {alive}.tmp; mv {alive}.tmp {alive}; exit 0;;
esac
exit 1
"""
STUB_RESUME = """#!/bin/sh
printf '%s\\n' "$*" >> {calls}
[ -e {fail} ] && {{ echo "boom" >&2; exit 3; }}
while [ $# -gt 0 ]; do [ "$1" = --session ] && S="$2"; shift; done
T="ka-$(echo "$S" | cut -c1-8)"
grep -qx "$T" {alive} 2>/dev/null && {{ echo "sent-keys $T"; exit 0; }}
echo "$T" >> {alive}
echo "started $T"
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.proj = os.path.join(d, "projects")
        os.makedirs(os.path.join(self.proj, "-work"))
        self.work = os.path.join(d, "work")
        os.makedirs(self.work)
        self.bindir = os.path.join(d, "bin")
        os.makedirs(self.bindir)
        self.calls = {k: os.path.join(d, f"{k}.calls") for k in ("claude", "tmux", "resume")}
        self.alive = os.path.join(d, "alive.txt")
        open(self.alive, "w").close()
        self.failfile = os.path.join(d, "FAIL")
        for name, body in (("claude", STUB_CLAUDE.format(calls=self.calls["claude"])),
                           ("tmux", STUB_TMUX.format(calls=self.calls["tmux"], alive=self.alive)),
                           ("ka_resume.sh", STUB_RESUME.format(calls=self.calls["resume"], alive=self.alive,
                                                               fail=self.failfile))):
            p = os.path.join(self.bindir, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        self.saved = {"PATH": os.environ["PATH"], "ka": (ka.PROJECTS_DIR, ka.KA_RESUME, dict(ka.SCRUBBED_ENV),
                                                         ka.TAKE_OVER_IDLE, ka.IGNORE_WINDOW),
                      "dp": (dp.DATA_DIR, dp.STATE_FILE, dp.LOG_FILE, dp.LOCK_FILE, dp.CONFIG_FILE,
                             dp.keepalive_targets),
                      "own": stalled.OWN_LIST, "alert": ka.alert}
        self.alerts, self.logs = [], []
        self.saved["log"] = dp.log
        dp.log = self.logs.append                                    # quiet; tests read self.logs
        ka.alert = lambda subject, body="": self.alerts.append(subject)   # never ALERTS.md / a push
        os.environ["PATH"] = self.bindir + ":" + os.environ["PATH"]
        ka.PROJECTS_DIR = self.proj
        ka.KA_RESUME = os.path.join(self.bindir, "ka_resume.sh")
        ka.SCRUBBED_ENV["PATH"] = self.bindir + ":" + ka.SCRUBBED_ENV["PATH"]
        data = os.path.join(d, "data")
        dp.DATA_DIR, dp.STATE_FILE, dp.LOG_FILE = data, os.path.join(data, "st.json"), os.path.join(data, "d.log")
        dp.LOCK_FILE, dp.CONFIG_FILE = os.path.join(data, ".lock"), os.path.join(data, "cfg.json")
        dp.keepalive_targets = lambda: set()
        self.own = stalled.OWN_LIST = os.path.join(d, "own_sessions.txt")
        self.conn = store.connect(os.path.join(d, "t.db"))
        self.cfg = dp.load_config(os.path.join(d, "none.json"), {"keepalive_sessions": [], "take_over_idle": False})
        self.st = dp.load_state()

    def tearDown(self):
        self.conn.close()
        os.environ["PATH"] = self.saved["PATH"]
        (ka.PROJECTS_DIR, ka.KA_RESUME, env, ka.TAKE_OVER_IDLE, ka.IGNORE_WINDOW) = self.saved["ka"]
        ka.SCRUBBED_ENV.clear()
        ka.SCRUBBED_ENV.update(env)
        (dp.DATA_DIR, dp.STATE_FILE, dp.LOG_FILE, dp.LOCK_FILE, dp.CONFIG_FILE,
         dp.keepalive_targets) = self.saved["dp"]
        stalled.OWN_LIST = self.saved["own"]
        ka.alert = self.saved["alert"]
        dp.log = self.saved["log"]
        self.tmp.cleanup()

    # -- fixtures
    def transcript(self, s, entries):
        with open(os.path.join(self.proj, "-work", f"{s}.jsonl"), "w") as fh:
            for e in entries:
                fh.write(json.dumps(dict(e, sessionId=s)) + "\n")

    def stalled_session(self, s, prefix="", shared=(), extra_ts=None):
        t0 = datetime(2026, 9, 29, 17, 0, tzinfo=UTC)
        base = list(shared) or [user(f"{prefix}u1", t0, self.work),
                                reply(f"{prefix}a1", t0 + timedelta(minutes=5), self.work, model="claude-sonnet-test")]
        own = [user(f"{prefix}u2", extra_ts or t0 + timedelta(minutes=30), self.work),
               stall(f"{prefix}s", (extra_ts or t0 + timedelta(minutes=30)) + timedelta(minutes=1), self.work)]
        self.transcript(s, base + own)

    def scan(self):
        stalled.scan(self.conn, self.proj, self.own)

    def run_pass(self, arm=True, now=NOW, u=None, **cfg):
        self.cfg.update(cfg)
        return dp.run_pass(self.conn, self.cfg, self.st, now, arm, usage_getter=lambda n: u or usage())

    def resume_calls(self):
        if not os.path.exists(self.calls["resume"]):
            return []
        with open(self.calls["resume"]) as fh:
            return [ln.split() for ln in fh.read().splitlines() if ln]

    def resumed_sessions(self):
        return [c[c.index("--session") + 1] for c in self.resume_calls()]

    def project(self, name, rank=None, manager=None):
        path = os.path.join(self.d, name)
        os.makedirs(path, exist_ok=True)
        p = store.add_project(self.conn, name, path=path, rank=rank)
        if manager:
            p = store.update_project(self.conn, name, manager_session=manager)
        return p


class Stalled(Base):
    def test_approved_undecided_ignored(self):
        for n in (1, 2, 3):
            self.stalled_session(sid(n), prefix=f"x{n}")
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        store.decide_session(self.conn, sid(3), "ignore")
        rep = self.run_pass()
        self.assertEqual(self.resumed_sessions(), [sid(1)])
        call = self.resume_calls()[0]
        self.assertNotIn("--new", call)
        msg = " ".join(call)
        self.assertIn("The user asked AFClaude to continue", msg)   # continue_foreign.md: the user's session
        self.assertIn("Guard hooks", msg)
        self.assertNotIn("TASK-MANAGER", msg)                     # neutral: no manager rules
        self.assertEqual(call[call.index("--model") + 1], "claude-sonnet-test")   # its own model
        skips = "\n".join(rep["skip"])
        self.assertIn(f"{sid(2)[:8]}: undecided", skips)
        self.assertIn(f"{sid(3)[:8]}: ignored", skips)
        self.assertEqual(self.st["sessions"][sid(1)]["status"], "running")
        # the same stall is never continued twice
        rep = self.run_pass()
        self.assertEqual(len(self.resume_calls()), 1)
        self.assertTrue(any("already continued" in s for s in rep["skip"]))

    def test_waits_for_the_reset_then_continues_any_time(self):
        """D-206: an approved stall continues right at its reset (+ grace) at any time of day."""
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        rep = self.run_pass(now=datetime(2026, 9, 29, 20, 0, tzinfo=UTC))   # before the 21:00 UTC reset
        self.assertTrue(any("WAIT_RESET" in s for s in rep["skip"]))
        rep = self.run_pass(now=datetime(2026, 9, 29, 21, 1, tzinfo=UTC))   # inside the 90 s grace
        self.assertTrue(any("WAIT_RESET" in s for s in rep["skip"]))
        self.assertEqual(self.resume_calls(), [])
        rep = self.run_pass(now=datetime(2026, 9, 29, 21, 2, tzinfo=UTC))   # 23:02 Berlin
        self.assertEqual(self.resumed_sessions(), [sid(1)])
        self.assertIn("D-206", rep["continue"][0]["reason"])

    def test_outside_the_window_without_budget_gate(self):
        """D-206: no night window, no AFClaude budget/pacing gate, no session-usage stop, no cap."""
        for n in (1, 2, 3):
            self.stalled_session(sid(n), prefix=f"x{n}")
        self.scan()
        for n in (1, 2, 3):
            store.decide_session(self.conn, sid(n), "continue")
        rep = self.run_pass(now=DAY + timedelta(days=1), u=usage(session=90, weekly=80))   # 14:00 Berlin
        self.assertEqual(sorted(self.resumed_sessions()), [sid(1), sid(2), sid(3)])
        self.assertEqual(len(rep["continue"]), 3)
        self.assertNotIn("start", rep)

    def test_live_limit_still_on_waits(self):
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        u = usage(session=100)                                         # resets NOW + 3 h
        rep = self.run_pass(u=u)
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any("live usage still shows session limit 100%" in s for s in rep["skip"]))

    def test_keepalive_target_is_never_continued(self):
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        rep = self.run_pass(keepalive_sessions=[sid(1)])
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any("keepalive.py" in s for s in rep["skip"]))
        dp.keepalive_targets = lambda: {sid(1)}                    # a running watcher's --session
        rep = self.run_pass(keepalive_sessions=[])
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any("running keepalive.py watcher" in s for s in rep["skip"]))

    def test_keepalive_targets_reads_proc(self):
        self.assertIsInstance(self.saved["dp"][5](), set)


class Forks(Base):
    def fork_pair(self):
        t0 = datetime(2026, 9, 29, 17, 0, tzinfo=UTC)
        shared = [user("f-u1", t0, self.work), reply("f-a1", t0 + timedelta(minutes=5), self.work)]
        self.stalled_session(sid(1), prefix="one", shared=shared, extra_ts=t0 + timedelta(minutes=20))
        self.stalled_session(sid(2), prefix="two", shared=shared, extra_ts=t0 + timedelta(minutes=40))
        self.stalled_session(sid(3), prefix="other")                # unrelated
        self.scan()

    def test_family_detection(self):
        self.fork_pair()
        fams = dp.fork_families(self.conn)
        self.assertEqual(fams[sid(1)], [sid(1), sid(2)])
        self.assertNotIn(sid(3), fams)

    def test_most_recent_own_activity_wins(self):
        self.fork_pair()
        store.add_rule(self.conn, "project", self.work, "continue")
        rep = self.run_pass()
        self.assertEqual(sorted(self.resumed_sessions()), [sid(2), sid(3)])
        self.assertTrue(any(f"{sid(1)[:8]}: fork of {sid(2)[:8]}" in s for s in rep["skip"]))

    def test_explicit_decision_wins(self):
        self.fork_pair()
        store.add_rule(self.conn, "project", self.work, "continue")
        store.decide_session(self.conn, sid(1), "continue")          # one-off beats the project rule
        rep = self.run_pass()
        self.assertIn(sid(1), self.resumed_sessions())
        self.assertNotIn(sid(2), self.resumed_sessions())
        self.assertTrue(any("explicitly decided" in s for s in rep["skip"]))


class NoStarts(Base):
    """D-205: the 10-min pass starts no task sessions and no task-manager runs; D-204: AFClaude
    runs (task-managers, task sessions) are never continued at a limit reset."""
    def test_pending_tasks_are_never_started(self):
        self.project("p1")
        ts = [store.add_task(self.conn, f"t{i}", project="p1") for i in range(3)]
        for when in (NOW, DAY):
            rep = self.run_pass(now=when)
            self.assertEqual(self.resume_calls(), [])
            self.assertNotIn("start", rep)
        for t in ts:
            self.assertEqual(store.get_task(self.conn, t["id"])["status"], "pending")
        self.assertFalse(os.path.exists(self.own))
        self.assertFalse(hasattr(dp.Pass, "start_task"))

    def test_task_manager_of_a_managed_project_is_not_continued(self):
        mgr = sid(7)
        self.stalled_session(mgr)
        self.scan()
        self.project("managed", manager=mgr)
        store.add_task(self.conn, "managed stage", project="managed")
        rep = self.run_pass()                                       # undecided
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any(f"{mgr[:8]}: task-manager of the project 'managed'" in s and "D-204" in s
                            for s in rep["skip"]), rep["skip"])
        store.decide_session(self.conn, mgr, "continue")            # even an explicit continue
        rep = self.run_pass(now=NOW + timedelta(hours=1))
        self.assertEqual(self.resume_calls(), [])
        self.assertEqual(rep["continue"], [])
        self.assertEqual([p["name"] for p in dp.start_project_seam(self.conn)], ["managed"])   # phase 3b seam

    def test_managed_manager_ignored_or_kept_by_keepalive(self):
        mgr = sid(7)
        self.stalled_session(mgr)
        self.scan()
        self.project("managed", manager=mgr)
        rep = self.run_pass(keepalive_sessions=[mgr])              # the AFClaude task-manager
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any("keepalive.py" in s for s in rep["skip"]))
        store.decide_session(self.conn, mgr, "ignore")
        rep = self.run_pass(keepalive_sessions=[])
        self.assertEqual(self.resume_calls(), [])
        self.assertFalse(any(mgr[:8] in s for s in rep["skip"]))   # ignored: quietly left alone

    def test_task_session_is_not_continued(self):
        self.project("p1")
        t = store.add_task(self.conn, "long one", project="p1")
        s = sid(5)
        store.start_task(self.conn, t["id"], s)
        store.add_rule(self.conn, "session", s, "continue", note=f"dispatcher task #{t['id']}")
        t0 = NOW - timedelta(hours=6)
        self.transcript(s, [user("k1", t0, self.work), reply("k2", t0, self.work, stop="tool_use"),
                            stall("k3", t0 + timedelta(minutes=1), self.work, reset="9pm")])
        self.scan()
        rep = self.run_pass()
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any(f"task #{t['id']}" in x and "D-204" in x for x in rep["skip"]), rep["skip"])
        store.finish_task(self.conn, t["id"], "ok")                 # an old tracked task session
        self.st["sessions"][s] = {"tmux": ka.tmux_name(s), "status": "ended", "kind": "task"}
        rep = self.run_pass()
        self.assertEqual(self.resume_calls(), [])


class Settings(Base):
    """The tunables are DB settings (D-146): load_config reads them on every pass; the file
    keeps only session ids, and a tunable left in a --config file is ignored (logged)."""
    def tearDown(self):
        testenv.setcfg(usage_model="linear")
        super().tearDown()

    def test_defaults_and_db_values(self):
        cfg = dp.load_config(os.path.join(self.d, "none.json"))
        self.assertEqual({k: cfg[k] for k in dp.SETTING_KEYS},
                         {"idle_cleanup_hours": 2.0, "finished_grace_minutes": 10.0, "verify_minutes": 15.0,
                          "take_over_idle": True})
        testenv.setcfg(usage_model="linear", cleanup_idle_hours=1, cleanup_finished_grace_minutes=3,
                       stall_verify_minutes=7, stall_take_over_idle=False)
        cfg = dp.load_config(os.path.join(self.d, "none.json"))
        self.assertEqual({k: cfg[k] for k in dp.SETTING_KEYS},
                         {"idle_cleanup_hours": 1.0, "finished_grace_minutes": 3.0, "verify_minutes": 7.0,
                          "take_over_idle": False})
        self.assertEqual(dp.load_config(os.path.join(self.d, "none.json"), {"verify_minutes": 2})["verify_minutes"],
                         2)                                          # an explicit override (tests) still wins

    def test_file_keeps_only_session_ids(self):
        p = os.path.join(self.d, "custom.json")
        with open(p, "w") as fh:
            json.dump({"exclude_sessions": [sid(9)], "verify_minutes": 1, "_comment": "x"}, fh)
        cfg = dp.load_config(p)
        self.assertEqual((cfg["exclude_sessions"], cfg["verify_minutes"]), ([sid(9)], 15.0))
        self.assertTrue(any("['verify_minutes'] ignored" in ln for ln in self.logs), self.logs)

    def test_verify_minutes_from_the_db(self):
        """The pass uses the DB value: a continue without a reply alerts after stall_verify_minutes."""
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        testenv.setcfg(usage_model="linear", stall_verify_minutes=5)
        self.cfg = dp.load_config(os.path.join(self.d, "none.json"), {"keepalive_sessions": [],
                                                                       "take_over_idle": False})
        self.run_pass()
        self.assertEqual(self.resumed_sessions(), [sid(1)])
        self.alerts.clear()
        self.run_pass(now=NOW + timedelta(minutes=6))
        self.assertTrue(any("did not reply within 5" in a for a in self.alerts), self.alerts)


class NoGate(Base):
    """D-206 with the budget model: an active owner, a week above the threshold, a full session
    window: none of them stops an approved stall (the windows and pacing are only for AFClaude)."""
    def setUp(self):
        super().setUp()
        testenv.setcfg(usage_model="pacing")
        self._bm = budget.minutes_since_user

    def tearDown(self):
        budget.minutes_since_user = self._bm
        testenv.setcfg(usage_model="linear")
        super().tearDown()

    def test_active_owner_does_not_postpone(self):
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        budget.minutes_since_user = lambda now, rows=None: 2.0
        rep = self.run_pass(u=usage(session=86, weekly=95))
        self.assertEqual(self.resumed_sessions(), [sid(1)])
        self.assertNotIn("budget for this run", rep["continue"][0]["reason"])


class Cleanup(Base):
    def track(self, s, task_id=None, own_tmux=True):
        self.st["sessions"][s] = {"tmux": ka.tmux_name(s), "status": "running", "own_tmux": own_tmux,
                                  "task_id": task_id, "sent_at": Z(NOW - timedelta(hours=5)), "verified": True}
        with open(self.alive, "a") as fh:
            fh.write(ka.tmux_name(s) + "\n")

    def idle(self, s, minutes):
        self.transcript(s, [user(f"{s}-u", NOW - timedelta(minutes=minutes + 1), self.work),
                            reply(f"{s}-a", NOW - timedelta(minutes=minutes), self.work)])

    def killed(self):
        if not os.path.exists(self.calls["tmux"]):
            return []
        return [ln.split()[2].lstrip("=") for ln in open(self.calls["tmux"]).read().splitlines()
                if ln.startswith("kill-session")]

    def test_only_finished_own_sessions_are_killed(self):
        self.project("p1")
        done = store.add_task(self.conn, "done one", project="p1")
        store.start_task(self.conn, done["id"], sid(1))
        store.finish_task(self.conn, done["id"], "ok")
        busy = store.add_task(self.conn, "running one", project="p1")
        store.start_task(self.conn, busy["id"], sid(3))
        self.track(sid(1), done["id"]); self.idle(sid(1), 20)       # task done, idle 20 min -> kill
        self.track(sid(2)); self.idle(sid(2), 180)                  # idle 3 h -> kill
        self.track(sid(3), busy["id"]); self.idle(sid(3), 30)       # task running, idle 30 min -> keep
        self.track(sid(4))                                          # stalled -> keep
        self.transcript(sid(4), [user("s4u", NOW - timedelta(hours=5), self.work),
                                 stall("s4s", NOW - timedelta(hours=5), self.work)])
        self.track(sid(5)); self.transcript(sid(5), [user("s5u", NOW - timedelta(hours=4), self.work),
                                                     reply("s5a", NOW - timedelta(hours=4), self.work, "tool_use")])
        self.track(sid(6), own_tmux=False); self.idle(sid(6), 300)  # only typed into: release, never kill
        with open(self.alive, "a") as fh:                           # untracked + the manager
            fh.write("ka-99999999\n" + ka.tmux_name(dp.MANAGER_SESSION) + "\n")
        mgr = dp.MANAGER_SESSION
        self.st["sessions"][mgr] = {"tmux": ka.tmux_name(mgr), "status": "running", "own_tmux": True}
        self.idle(mgr, 600)
        rep = self.run_pass()
        self.assertEqual(sorted(self.killed()), sorted([ka.tmux_name(sid(1)), ka.tmux_name(sid(2))]))
        self.assertEqual({c["sid"] for c in rep["cleanup"]}, {sid(1), sid(2)})
        self.assertEqual(self.st["sessions"][sid(1)]["status"], "cleaned")
        self.assertEqual(self.st["sessions"][sid(6)]["status"], "released")
        for s in (sid(3), sid(4), sid(5)):
            self.assertEqual(self.st["sessions"][s]["status"], "running")
        alive = open(self.alive).read().split()
        self.assertIn("ka-99999999", alive)
        self.assertIn(ka.tmux_name(dp.MANAGER_SESSION), alive)
        self.assertIn(ka.tmux_name(sid(6)), alive)

    def test_gone_session_with_open_task_alerts(self):
        self.project("p1")
        t = store.add_task(self.conn, "x", project="p1")
        store.start_task(self.conn, t["id"], sid(1))
        self.st["sessions"][sid(1)] = {"tmux": ka.tmux_name(sid(1)), "status": "running", "own_tmux": True,
                                       "task_id": t["id"], "verified": True}
        self.run_pass(now=DAY)
        self.run_pass(now=DAY)
        self.assertEqual(self.st["sessions"][sid(1)]["status"], "ended")
        self.assertEqual(len(self.alerts), 1)

    def approved(self):
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")

    def test_verify_alerts_once_without_a_reply(self):
        self.approved()
        self.run_pass()
        s = self.resumed_sessions()[0]
        self.run_pass(now=NOW + timedelta(minutes=5))
        self.assertIsNone(self.st["sessions"][s]["verified"])
        self.run_pass(now=NOW + timedelta(minutes=20))
        self.run_pass(now=NOW + timedelta(minutes=30))
        self.assertFalse(self.st["sessions"][s]["verified"])
        self.assertEqual(len(self.alerts), 1)

    def test_verify(self):
        self.approved()
        self.run_pass()
        s = self.resumed_sessions()[0]
        self.assertIsNone(self.st["sessions"][s]["verified"])
        sent = store.parse_iso(self.st["sessions"][s]["sent_at"])
        self.transcript(s, [user("v1", sent, self.work), reply("v2", sent + timedelta(seconds=30), self.work)])
        rep = self.run_pass(now=DAY)
        self.assertTrue(self.st["sessions"][s]["verified"])
        self.assertIn(f"{s[:8]} verified", rep["verify"])


class DryRun(Base):
    def test_dry_run_changes_nothing(self):
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        self.project("p1")
        t = store.add_task(self.conn, "t", project="p1")
        s2 = sid(2)
        self.st["sessions"][s2] = {"tmux": ka.tmux_name(s2), "status": "running", "own_tmux": True}
        with open(self.alive, "a") as fh:
            fh.write(ka.tmux_name(s2) + "\n")
        self.transcript(s2, [user("d1", NOW - timedelta(hours=4), self.work),
                             reply("d2", NOW - timedelta(hours=3), self.work)])
        before_state = json.dumps(self.st, sort_keys=True)
        db = os.path.join(self.d, "t.db")
        dump = lambda: "\n".join(sqlite3.connect(db).iterdump())  # noqa: E731
        before_db = dump()
        rep = self.run_pass(arm=False)
        self.assertEqual([c["sid"] for c in rep["continue"]], [sid(1)])
        self.assertEqual(store.get_task(self.conn, t["id"])["status"], "pending")
        self.assertEqual([c["sid"] for c in rep["cleanup"]], [s2])
        self.assertEqual(self.resume_calls(), [])
        self.assertEqual(self.killed_calls(), [])
        self.assertEqual(json.dumps(self.st, sort_keys=True), before_state)
        self.assertEqual(dump(), before_db)
        self.assertFalse(os.path.exists(self.own))

    def killed_calls(self):
        if not os.path.exists(self.calls["tmux"]):
            return []
        return [ln for ln in open(self.calls["tmux"]).read().splitlines() if ln.startswith("kill")]

    def test_cli_dry_run(self):
        """The real CLI in a subprocess, dry-run: it never calls ka_resume."""
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")
        self.project("p1")
        store.add_task(self.conn, "t", project="p1")
        data = os.path.join(self.d, "clidata")
        env = dict(os.environ, AFCLAUDE_DB=os.path.join(self.d, "t.db"), DISPATCHER_DATA_DIR=data,
                   KEEPALIVE_PROJECTS_DIR=self.proj, KEEPALIVE_KA_RESUME=ka.KA_RESUME,
                   AFCLAUDE_OWN_LIST=self.own, KA_TRUST_ROOT=self.d)
        # stub claude inside SCRUBBED_ENV too, and no usage cache: an approved stall needs no budget (D-206)
        code = ("import sys, keepalive as ka, dispatcher as dp;"
                f"ka.SCRUBBED_ENV['PATH']={self.bindir!r}+':'+ka.SCRUBBED_ENV['PATH'];"
                f"ka.CLAUDE_JSON={os.path.join(self.d, 'no-claude.json')!r};"
                f"ka.USAGE_STATE_FILE={os.path.join(self.d, 'usage_state.json')!r};"
                "ka.alert=lambda s, b='': print('ALERT', s);"
                "sys.exit(dp.main(['--once', '--json']))")
        r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY-RUN", r.stdout)
        self.assertIn("would continue 1", r.stdout)
        self.assertNotIn("ALERT", r.stdout)
        self.assertEqual(self.resume_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(data, "dispatcher_state.json")))
        self.assertTrue(os.path.exists(os.path.join(data, "dispatcher.log")))

    def run_cli_into(self, data, out_path):
        """The CLI like cron runs it: `>> out_path 2>&1`."""
        env = dict(os.environ, AFCLAUDE_DB=os.path.join(self.d, "t.db"), DISPATCHER_DATA_DIR=data,
                   KEEPALIVE_PROJECTS_DIR=self.proj, KEEPALIVE_KA_RESUME=ka.KA_RESUME,
                   AFCLAUDE_OWN_LIST=self.own, KA_TRUST_ROOT=self.d)
        code = ("import sys, keepalive as ka, dispatcher as dp;"
                f"ka.SCRUBBED_ENV['PATH']={self.bindir!r}+':'+ka.SCRUBBED_ENV['PATH'];"
                f"ka.CLAUDE_JSON={os.path.join(self.d, 'no-claude.json')!r};"
                f"ka.USAGE_STATE_FILE={os.path.join(self.d, 'usage_state.json')!r};"
                "ka.alert=lambda s, b='': None;"
                "sys.exit(dp.main(['--once']))")
        with open(out_path, "a") as out:
            r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, stdout=out,
                               stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(r.returncode, 0)

    def test_cron_redirect_logs_each_line_once(self):
        """Cron appends stdout+stderr to data/dispatcher.log, which log() also writes:
        every line must land there exactly once (it used to be twice)."""
        data = os.path.join(self.d, "clidata")
        os.makedirs(data)
        logf = os.path.join(data, "dispatcher.log")
        self.run_cli_into(data, logf)
        with open(logf) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(sum("pass (DRY-RUN" in ln for ln in lines), 1, lines)
        self.assertTrue(lines)
        self.assertEqual(len(lines), len(set(lines)), lines)     # no line twice
        self.run_cli_into(data, logf)
        with open(logf) as fh:
            self.assertEqual(sum("pass (DRY-RUN" in ln for ln in fh), 2)
        # stdout elsewhere (a terminal, another file): still a full copy there
        other = os.path.join(self.d, "other.out")
        self.run_cli_into(data, other)
        with open(other) as a, open(logf) as b:
            self.assertEqual(sum("pass (DRY-RUN" in ln for ln in a), 1)
            self.assertEqual(sum("pass (DRY-RUN" in ln for ln in b), 3)

    def test_is_log_file(self):
        import io
        os.makedirs(dp.DATA_DIR, exist_ok=True)
        self.assertFalse(dp.is_log_file(io.StringIO()))
        with open(dp.LOG_FILE, "a") as fh:
            self.assertTrue(dp.is_log_file(fh))
        with open(os.path.join(dp.DATA_DIR, "x.log"), "a") as fh:
            self.assertFalse(dp.is_log_file(fh))


class Prompts(Base):
    def test_messages_are_one_line_and_filled(self):
        c = ka.session_message("continue_foreign", manager=False, reason="r", context="ctx")
        self.assertIn("ctx", c)
        self.assertNotIn("{", c)
        self.assertNotIn("TASK-MANAGER", c)

    def test_rc_server_argv(self):
        server = ["claude", "rc"]
        child = ["/home/u/.local/share/claude/versions/2.1.283", "--print", "--sdk-url", "https://x/y",
                 "--session-id", "cse_1", "--input-format", "stream"]
        self.assertTrue(ka.is_rc_server(server))
        self.assertTrue(ka.is_rc_server(["claude", "remote-control"]))
        self.assertFalse(ka.is_rc_server(child))
        self.assertFalse(ka.is_rc_server(["claude", "--resume", sid(1), "--remote-control"]))
        self.assertFalse(ka.rc_held(os.getpid()))                   # this test process: no rc server

    def test_keepalive_message_keeps_all_manager_rules(self):
        m = ka.session_message("continue", reason="r", progress="P.md")
        for part in ("TASK-MANAGER", "usage_report.py", "OPEN_QUESTIONS.md", "PUBLIC", "worktree"):
            self.assertIn(part, m)


class Holders(Base):
    """preflight with fake holder processes: an rc-server thread is never taken over,
    an idle plain interactive holder (terminal, `claude --resume` in a tty) is."""
    PROCS = {100: (50, ["/x/versions/2.1.283", "--print", "--sdk-url", "https://x", "--session-id", "cse_1"]),
             50: (1, ["claude", "rc"]),
             200: (10, ["claude", "--resume", sid(1), "--remote-control"]),
             10: (1, ["-bash"])}

    def setUp(self):
        super().setUp()
        self.orig = (ka.agent_entries, ka.pid_alive, ka.proc_argv, ka.proc_ppid, ka.SESSIONS_DIR)
        self.sessdir = tempfile.TemporaryDirectory()
        ka.SESSIONS_DIR = self.sessdir.name                         # empty registry: never the real one
        self.rows = []
        ka.agent_entries = lambda s: list(self.rows)
        ka.pid_alive = lambda pid: pid in self.PROCS
        ka.proc_argv = lambda pid: self.PROCS.get(pid, (None, []))[1]
        ka.proc_ppid = lambda pid: self.PROCS.get(pid, (None, []))[0]

    def tearDown(self):
        ka.agent_entries, ka.pid_alive, ka.proc_argv, ka.proc_ppid, ka.SESSIONS_DIR = self.orig
        self.sessdir.cleanup()
        super().tearDown()

    def test_preflight(self):
        ka.TAKE_OVER_IDLE = True
        self.rows = [{"sessionId": sid(1), "pid": 100, "kind": "interactive", "status": "idle"}]
        ok, problems, plan = ka.preflight(sid(1))
        self.assertFalse(ok)
        self.assertTrue(problems[0].startswith(ka.RC_HELD))
        self.rows = [{"sessionId": sid(1), "pid": 50, "kind": "interactive", "status": "idle"}]   # the server
        self.assertFalse(ka.preflight(sid(1))[0])
        self.rows = [{"sessionId": sid(1), "pid": 100, "kind": "interactive", "status": "busy"}]
        self.assertTrue(ka.preflight(sid(1))[1][0].startswith(ka.RC_HELD))
        self.rows = [{"sessionId": sid(1), "pid": 200, "kind": "interactive", "status": "idle"}]
        self.assertEqual(ka.preflight(sid(1)), (True, [], "take-over:200"))
        ka.TAKE_OVER_IDLE = False
        self.assertFalse(ka.preflight(sid(1))[0])

    def approved_stall(self):
        self.stalled_session(sid(1))
        self.scan()
        store.decide_session(self.conn, sid(1), "continue")

    def test_dispatcher_skips_rc_held_quietly(self):
        self.approved_stall()
        self.rows = [{"sessionId": sid(1), "pid": 100, "kind": "interactive", "status": "idle"}]
        for _ in range(3):
            rep = self.run_pass(take_over_idle=True)                 # the default, and still refused
            self.assertTrue(any("held by a `claude rc` server" in s for s in rep["skip"]))
        self.assertEqual(self.resume_calls(), [])
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.st["handled"], {})                     # retried once the rc thread is gone
        self.assertEqual(sum("not continuing" in ln for ln in self.logs), 1)   # logged once per stall
        self.rows = []
        rep = self.run_pass()
        self.assertEqual(self.resumed_sessions(), [sid(1)])

    def test_dispatcher_refuses_a_busy_holder_outside_the_window(self):
        """D-206 drops the window and budget gates, never the preflight."""
        self.approved_stall()
        self.rows = [{"sessionId": sid(1), "pid": 200, "kind": "interactive", "status": "busy"}]
        rep = self.run_pass(now=DAY + timedelta(days=1), take_over_idle=True)
        self.assertEqual(self.resume_calls(), [])
        self.assertTrue(any("preflight refused" in s for s in rep["skip"]), rep["skip"])
        self.assertEqual(len(self.alerts), 1)

    def test_dispatcher_takes_over_plain_idle_holder(self):
        self.approved_stall()
        self.rows = [{"sessionId": sid(1), "pid": 200, "kind": "interactive", "status": "idle"}]
        rep = self.run_pass(arm=False, take_over_idle=True)          # dry-run: no SIGTERM to a fake pid
        self.assertEqual([(c["sid"], c["plan"]) for c in rep["continue"]], [(sid(1), "take-over:200")])
        self.assertEqual(self.resume_calls(), [])


class Store(unittest.TestCase):
    def test_manager_session_column(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "old.db")
            # a v3 projects table from before the column
            raw = sqlite3.connect(db)
            raw.execute(store.PROJECTS_DDL.replace("manager_session TEXT,", ""))
            raw.execute("INSERT INTO projects(name, rank, created_at, updated_at) VALUES ('p', 1, 'x', 'x')")
            raw.commit()
            self.assertNotIn("manager_session", [r[1] for r in raw.execute("PRAGMA table_info(projects)")])
            raw.close()
            c = store.connect(db)
            self.assertIsNone(store.get_project(c, "p")["manager_session"])
            p = store.update_project(c, "p", manager_session=sid(1))
            self.assertEqual(p["manager_session"], sid(1))
            self.assertEqual(store.list_projects(c)[0]["manager_session"], sid(1))
            self.assertIsNone(store.update_project(c, "p", manager_session="")["manager_session"])
            self.assertEqual(store.get_meta(c, "schema_version"), str(store.SCHEMA_VERSION))
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
