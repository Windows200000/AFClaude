#!/usr/bin/env python3
"""Offline tests for the compaction of long-running sessions (D-144, D-191, D-214; design §5.5):
the D-191/D-218 rule (the conversation's first transcript entry before the session window), the save
prompt at the end of a session window (timing, the fill-up merge, dedup), and the compact-then-continue
sequence at the next start with the hand-off injected and consumed.
A fake tmux (fire / preflight) and a fake transcript; no real Claude, no tmux."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import fillup  # noqa: E402
import keepalive as ka  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
SID = "abcdef01-2345-4678-9abc-def012345678"
L = timedelta(hours=5)
NIGHT = datetime(2026, 10, 5, 21, 0, tzinfo=UTC)        # Mon 23:00 Berlin (CEST): the night's first start
S2 = NIGHT + L                                          # 04:00 Berlin: the second session-window start
RESET = S2                                              # the 23:00 run's session window resets at 04:00


def entry(kind, ts, **kw):
    e = {"type": kind, "timestamp": ts.isoformat().replace("+00:00", "Z")}
    e.update(kw)
    return e


def assistant(ts, stop="end_turn", text="ok", content=None):
    return entry("assistant", ts, uuid=f"a-{ts.timestamp()}",
                 message={"role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
                          "content": content or [{"type": "text", "text": text}]})


class Env(unittest.TestCase):
    """Temp state files, transcripts root and PROGRESS.md; the keep-alive's host calls faked."""
    NAMES = ("PROJECTS_DIR", "STATE_FILE", "PROGRESS_FILE", "FILLUP_FILE", "HANDOFF_FILE", "DEFER_FILE",
             "USAGE_STATE_FILE", "alert", "fire", "preflight", "conversation_started", "read_usage_cache", "db_paused",
             "agent_entries", "_limit_hit_in", "fresh_usage", "budget_eval", "COMPACT_TIMEOUT",
             "COMPACT_IDLE_WAIT", "COMPACT_POLL", "_fire_usage")

    def setUp(self):
        testenv.clear_settings()
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.old = {n: getattr(ka, n) for n in self.NAMES}
        ka.PROJECTS_DIR = d
        ka.STATE_FILE, ka.PROGRESS_FILE = os.path.join(d, "state.json"), os.path.join(d, "PROGRESS.md")
        testenv.notes_project(d)                     # the progress notes go to its doc in the DB (§7.7)
        ka.FILLUP_FILE, ka.HANDOFF_FILE = os.path.join(d, "fillup.json"), os.path.join(d, "handoff.json")
        ka.DEFER_FILE, ka.USAGE_STATE_FILE = os.path.join(d, "deferred.json"), os.path.join(d, "usage.json")
        ka.alert = lambda subject, body="": self.alerts.append(subject)
        ka.preflight = lambda sid: (True, [], "send-keys")
        ka.db_paused = lambda what: False
        ka.agent_entries = lambda sid: []
        ka._limit_hit_in = lambda sid, reset: False
        ka._fire_usage = lambda: None
        ka.COMPACT_POLL = 0
        self.alerts, self.sent = [], []
        self.first = NIGHT - timedelta(hours=10)            # the conversation is old: D-191/D-218 apply
        ka.conversation_started = lambda sid, path=None: self.first
        self.usage = {"session": {"percent": 50.0, "resets_at": RESET}, "weekly": {"percent": 40.0,
                      "resets_at": RESET + timedelta(days=3)}, "fetched_at": RESET - timedelta(minutes=3)}
        ka.read_usage_cache = lambda: self.usage
        self.compacts = True                                # the fake session compacts on /compact
        ka.fire = self.fake_fire
        self.proj = os.path.join(d, "-proj")
        os.makedirs(self.proj)
        self.path = os.path.join(self.proj, f"{SID}.jsonl")
        self.write([entry("user", NIGHT - timedelta(hours=1), message={"role": "user", "content": "go"}),
                    assistant(NIGHT - timedelta(minutes=50), text="HANDOFF SAVED")])
        self.make_old()

    def tearDown(self):
        for n, v in self.old.items():
            setattr(ka, n, v)
        self.tmp.cleanup()
        testenv.clear_settings()
        conn = store.connect()
        try:
            with store.transaction(conn):
                conn.execute("DELETE FROM handoffs")
        finally:
            conn.close()

    # ---- the fake session
    def write(self, entries, mode="w"):
        with open(self.path, mode) as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def make_old(self):
        t = datetime.now(UTC).timestamp() - 600
        os.utime(self.path, (t, t))

    def fake_fire(self, sid, cwd, message, plan, new=False, **kw):
        """ka_resume.sh into a live tmux session: the message is typed; the fake Claude compacts on
        /compact (a compact_boundary + summary in the same transcript) and answers anything else."""
        self.sent.append((plan, message))
        now = datetime.now(UTC)
        if message.startswith("/compact "):
            if self.compacts:
                self.write([entry("system", now, subtype="compact_boundary", content="Conversation compacted",
                                  compactMetadata={"trigger": "manual"}),
                            entry("user", now, isCompactSummary=True,
                                  message={"role": "user", "content": "This session is being continued ..."})], "a")
        else:
            self.write([entry("user", now, message={"role": "user", "content": message}),
                        assistant(now + timedelta(seconds=1))], "a")
        return 0, "sent-keys ka-abcdef01", ""

    # ---- helpers
    def handoff(self, summary="state", **body):
        conn = store.connect()
        try:
            return actions.perform(conn, "handoff.write", {"session_id": SID, "body": dict(summary=summary, **body)},
                                   actor="cli", via="cli")
        finally:
            conn.close()

    def latest(self, unconsumed=True):
        conn = store.connect()
        try:
            return store.latest_handoff(conn, SID, unconsumed)
        finally:
            conn.close()

    def progress(self):
        return testenv.notes(self.tmp.name)


class Armed:
    arm, now, once = True, False, True


class DryRun:
    arm, now, once = False, False, True


# ---------------------------------------------------------------- the D-191 rule

class Rule(unittest.TestCase):
    def test_ran_before_the_window(self):
        ws = S2                                          # this session window began at 04:00
        self.assertTrue(ka.compaction_rule(NIGHT + timedelta(minutes=10), ws))     # 23:10: ran before
        self.assertTrue(ka.compaction_rule(NIGHT - timedelta(days=1), ws))
        self.assertFalse(ka.compaction_rule(S2 + timedelta(seconds=5), ws))         # began in this window
        self.assertFalse(ka.compaction_rule(S2 - timedelta(minutes=10), ws))        # an early start: tolerance
        self.assertFalse(ka.compaction_rule(None, ws))

    def test_first_compaction_after_one_to_two_session_lengths(self):
        """A conversation begun at the 23:00 start: not compacted at 04:00 (it ran < 1 session
        length), compacted at the next night's 23:00, and from then on at every start."""
        first = NIGHT + timedelta(seconds=5)
        old = ka.conversation_started
        ka.conversation_started = lambda sid, path=None: first
        try:
            self.assertFalse(ka.compaction_plan(SID, S2)["due"])
            self.assertTrue(ka.compaction_plan(SID, NIGHT + timedelta(days=1))["due"])
            self.assertTrue(ka.compaction_plan(SID, S2 + timedelta(days=1))["due"])
            testenv.set_setting("compaction_enabled", False)
            self.assertEqual(ka.compaction_plan(SID, NIGHT + timedelta(days=1)),
                             {"due": False, "why": "compaction_enabled is off", "first_activity": None})
        finally:
            ka.conversation_started = old
            testenv.clear_settings()

    def test_conversation_started_reads_the_first_transcript_entry(self):
        """The first timestamped user/assistant entry; metadata lines without one (summary, snapshots)
        and a half-written last line are skipped; no transcript = unknown (None) = no compaction."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, f"{SID}.jsonl")
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "summary", "summary": "x"}) + "\n")
                fh.write(json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n")
                fh.write(json.dumps(entry("system", NIGHT - timedelta(hours=3), subtype="x")) + "\n")
                fh.write(json.dumps(entry("user", NIGHT - timedelta(hours=2), message={"content": "hi"})) + "\n")
                fh.write(json.dumps(assistant(NIGHT)) + "\n")
                fh.write('{"type": "user", "timest')
            self.assertEqual(ka.conversation_started(SID, path), NIGHT - timedelta(hours=2))
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "user", "timestamp": "2026-10-05T19:00:00"}) + "\n")  # naive: UTC
            self.assertEqual(ka.conversation_started(SID, path), datetime(2026, 10, 5, 19, 0, tzinfo=UTC))
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "summary"}) + "\n")
            self.assertIsNone(ka.conversation_started(SID, path))
            self.assertIsNone(ka.conversation_started(SID, os.path.join(d, "gone.jsonl")))
            old = ka.PROJECTS_DIR
            ka.PROJECTS_DIR = d
            try:
                self.assertIsNone(ka.conversation_started("00000000-0000-4000-8000-000000000000"))
            finally:
                ka.PROJECTS_DIR = old


# ---------------------------------------------------------------- the save prompt (session end)

class SavePrompt(Env):
    def setUp(self):
        super().setUp()
        ka.save_state({"handled": {"window-start-2026-10-06": {"at": (NIGHT + timedelta(seconds=40)).isoformat(),
                                                               "result": "continued-in-place"}}, "fires": {}})

    def test_due_before_the_reset_once_per_window(self):
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=30), Armed()), "wait")
        self.assertEqual(self.sent, [])
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=19), Armed()), "sent")
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=18), Armed()), "done")   # dedup
        self.assertEqual(len(self.sent), 1)
        plan, msg = self.sent[0]
        self.assertEqual(plan, "send-keys")
        self.assertNotIn("\n", msg)
        for part in ("COMPACT", "afclaude_handoff", f'session "{SID}"', "tasks.py handoff write --session", SID,
                     "HANDOFF SAVED", "04:00 CEST"):
            self.assertIn(part, msg)
        rec = ka.load_handoff_state()["saves"][ka.save_key(RESET)]
        self.assertEqual((rec["result"], rec["trigger"]), ("sent", "time"))
        self.assertIn("sent the compaction save prompt", self.progress())

    def test_session_stop_pct_triggers_it_earlier(self):
        self.usage["session"]["percent"] = 95.0             # = session_stop_pct (default 95)
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(hours=1), Armed()), "sent")
        self.assertEqual(ka.load_handoff_state()["saves"][ka.save_key(RESET)]["trigger"], "session %")
        testenv.set_setting("handoff_before_reset_min", 90)
        self.usage["session"]["resets_at"] = RESET + L      # the next window: time-triggered at reset - 90 min
        ka.save_state({"handled": {"window-start-2026-10-06-s2": {"at": (S2 + timedelta(seconds=30)).isoformat(),
                                                                  "result": "continued-in-place"}}, "fires": {}})
        self.usage["session"]["percent"] = 10.0
        self.assertEqual(ka.handoff_save_pass(SID, RESET + L - timedelta(minutes=91), Armed()), "wait")
        self.assertEqual(ka.handoff_save_pass(SID, RESET + L - timedelta(minutes=89), Armed()), "sent")

    def test_not_due_when_the_conversation_began_in_this_window(self):
        self.first = NIGHT + timedelta(seconds=5)            # D-191/D-218: begun in this session window
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()), "not-due")
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=9), Armed()), "done")
        self.assertEqual(self.sent, [])
        self.assertIn("D-218", ka.load_handoff_state()["saves"][ka.save_key(RESET)]["why"])

    def test_no_prompt_without_a_run_paused_limit_or_off(self):
        ka.save_state({"handled": {}, "fires": {}})
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()),
                         "no run in this session window")
        ka.save_state({"handled": {"window-start-2026-10-06": {"at": (NIGHT + timedelta(seconds=40)).isoformat(),
                                                               "result": "continued-in-place"}}, "fires": {}})
        testenv.set_setting("automation_paused", True)
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()), "hold")
        testenv.set_setting("automation_paused", False)
        testenv.set_setting("compaction_enabled", False)
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()), "off")
        testenv.set_setting("compaction_enabled", True)
        ka._limit_hit_in = lambda sid, reset: True          # D-204: the run ended at the limit
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()), "skipped")
        self.assertEqual(self.sent, [])

    def test_only_into_our_live_tmux_session(self):
        ka.preflight = lambda sid: (True, [], "resume")      # no tmux session: never start one for this
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()), "skipped")
        self.assertEqual(self.sent, [])

    def test_dry_run_only_logs(self):
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), DryRun()), "dry-run")
        self.assertEqual(self.sent, [])
        self.assertIn("DRY-RUN would have sent the compaction save prompt", self.progress())

    def test_planned_fillup_carries_it(self):
        """A fill-up planned for this window (D-212): the save prompt is not sent separately; it goes
        into the fill-up message, once. If the fill-up is dropped, it is sent on its own."""
        fk = fillup.key(RESET)
        ka.save_fillup({"plan": {"key": fk, "status": "wait", "reason": "planned"}})
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=10), Armed()), "with-fillup")
        self.assertEqual(self.sent, [])
        ka.save_fillup({"plan": {"key": fk, "status": "skip", "reason": "the owner is active"}})
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=5), Armed()), "sent")

    def test_fillup_message_merge_and_dedup(self):
        seen = []

        def fake_handle_fire(sid, stall, reason, st, args):
            seen.append(stall)
            st["handled"][stall["uuid"]] = {"at": datetime.now(UTC).isoformat(), "result": "continued-in-place"}
            ka.save_state(st)
        old = ka.handle_fire
        ka.handle_fire = fake_handle_fire
        try:
            d = {"key": fillup.key(RESET), "reset": RESET, "remaining": 5.0, "cost": 0.7}
            ka._fire_fillup(SID, RESET - timedelta(minutes=6), d, Armed())
        finally:
            ka.handle_fire = old
        self.assertEqual(seen[0]["prompt"], "fillup")
        self.assertIn("HANDOFF SAVED", seen[0]["insert"])
        self.assertEqual(ka.load_handoff_state()["saves"][ka.save_key(RESET)]["result"], "merged-into-fillup")
        self.assertEqual(ka.handoff_save_pass(SID, RESET - timedelta(minutes=5), Armed()), "done")
        self.assertEqual(self.sent, [])
        msg = ka.session_message("fillup", insert=seen[0]["insert"], reason="r", progress="P.md", remaining="5",
                                 reset="04:00")
        self.assertLess(msg.index("Fill-up run"), msg.index("HANDOFF SAVED"))
        self.assertLess(msg.index("HANDOFF SAVED"), msg.index("Guard hooks"))

    def test_fillup_without_compaction_has_no_save_text(self):
        self.first = NIGHT + timedelta(seconds=5)
        seen = []
        old = ka.handle_fire
        ka.handle_fire = lambda sid, stall, reason, st, args: seen.append(stall)
        try:
            ka._fire_fillup(SID, RESET - timedelta(minutes=6),
                            {"key": fillup.key(RESET), "reset": RESET, "remaining": 5.0, "cost": 0.7}, Armed())
        finally:
            ka.handle_fire = old
        self.assertNotIn("insert", seen[0])
        self.assertNotIn("saves", ka.load_handoff_state())

    def test_watcher_survives_a_failing_save_pass(self):
        """A broken settings read / DB problem in the save pass never takes the watcher down."""
        names = ("LOCK_FILE", "STOP_FILE", "last_mile_pass", "deferred_window_start_pass", "fillup_pass",
                 "watch_run_usage", "handoff_save_pass", "log")
        old = {n: getattr(ka, n) for n in names}
        logs = []
        ka.LOCK_FILE, ka.STOP_FILE = os.path.join(self.tmp.name, "ka.lock"), os.path.join(self.tmp.name, "STOP")
        ka.last_mile_pass = lambda *a: None
        ka.deferred_window_start_pass = lambda *a: None
        ka.fillup_pass = lambda *a: None
        ka.watch_run_usage = lambda *a: None

        def boom(*a):
            raise store.DBUnavailable("the database is missing", "missing")
        ka.handoff_save_pass = boom
        ka.log = logs.append
        try:
            ka.run(types.SimpleNamespace(session=SID, arm=True, once=True, now=False))
        finally:
            for n, v in old.items():
                setattr(ka, n, v)
        self.assertTrue(any(ln.startswith("save-prompt pass failed: DBUnavailable") for ln in logs), logs)


# ---------------------------------------------------------------- compact + resume at the next start

class CompactAtStart(Env):
    KEY = "window-start-2026-10-07"

    def start(self, args=Armed(), at=None):
        """The next night's 23:00 session-window start fires a continue for SID."""
        at = at or NIGHT + timedelta(days=1)
        stall = {"uuid": self.KEY, "timestamp": at, "budget": "budget for this run: +14%", "headroom": 14.0,
                 "start_at": schedule.latest_session_start(at + timedelta(seconds=5)) or at}
        ka.handle_fire(SID, stall, "window start, CONTINUE: test", ka.load_state(), args)
        return ka.load_state()["handled"][self.KEY]

    def test_compact_then_continue_with_the_handoff(self):
        h = self.handoff("Phase 2 merged.", next_steps=["merge compaction"], open_question_ids=[12])
        rec = self.start()
        self.assertEqual([p for p, _ in self.sent], ["send-keys", "send-keys"])
        compact, cont = self.sent[0][1], self.sent[1][1]
        self.assertTrue(compact.startswith("/compact Keep what the task-manager needs"))
        self.assertNotIn("\n", compact)
        self.assertTrue(cont.startswith("[keep-alive] A new run starts"))
        for part in ("compacted at this start", f"Hand-off #{h['id']}", "Summary: Phase 2 merged.",
                     "Next steps: (1) merge compaction", "#12", "SessionStart hook"):
            self.assertIn(part, cont)
        # the session runs in the repo (session_cwd falls back to HERE): the rules come from the
        # SessionStart hook after the compaction, the message only points at them (D-225)
        self.assertLess(cont.index("Hand-off #"), cont.index("SessionStart hook"))
        self.assertNotIn("TASK-MANAGER", cont)
        self.assertNotIn("Guard hooks", cont)
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertEqual((rec["compaction"]["status"], rec["compaction"]["handoff_id"],
                          rec["compaction"]["handoff_consumed"]), ("compacted", h["id"], True))
        self.assertIsNone(self.latest())                                     # consumed ...
        self.assertEqual(self.latest(unconsumed=False)["consumed_by_start"], self.KEY)   # ... by this start

    def test_session_outside_the_repo_gets_the_rules_inline(self):
        """No SessionStart hook outside the repo's root: the continue carries the full rules (D-225)."""
        old = ka.session_cwd
        ka.session_cwd = lambda path: self.tmp.name
        try:
            self.start()
        finally:
            ka.session_cwd = old
        cont = self.sent[1][1]
        for part in ("Guard hooks", "TASK-MANAGER", "usage_report.py", "AFK rules (unattended runs)", "--run-stopped"):
            self.assertIn(part, cont)
        self.assertNotIn("SessionStart hook;", cont)

    def test_no_handoff_says_so(self):
        rec = self.start()
        cont = self.sent[1][1]
        self.assertIn("no hand-off from before the compaction (none was saved)", cont)
        self.assertIn("PROGRESS.md", cont)
        self.assertEqual((rec["compaction"]["status"], rec["compaction"]["handoff_id"]), ("compacted", None))

    def test_not_due_continues_normally(self):
        """D-191/D-218: a conversation begun at the previous start is not compacted; the hand-off stays."""
        self.first = NIGHT + timedelta(days=1) - L + timedelta(seconds=5)
        self.handoff()
        rec = self.start()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("compacted at this start", self.sent[0][1])
        self.assertFalse(rec["compaction"]["due"])
        self.assertIsNotNone(self.latest())

    def test_no_compaction_at_a_resume_start_or_a_fillup(self):
        """A start that has to resume (no live tmux session) is not compacted at that start (nothing to
        type /compact into first); the fill-up continues the same session window and never compacts."""
        ka.preflight = lambda sid: (True, [], "resume")
        rec = self.start()
        self.assertEqual([p for p, _ in self.sent], ["resume"])
        self.assertNotIn("compaction", rec)
        ka.preflight = lambda sid: (True, [], "send-keys")
        self.sent.clear()
        ka.handle_fire(SID, {"uuid": "fillup-x", "timestamp": RESET, "prompt": "fillup", "budget": "b",
                             "fields": {"remaining": "5", "reset": "04:00"}}, "fill-up", ka.load_state(), Armed())
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.sent[0][1].startswith("/compact"))

    def test_the_transcript_decides_and_a_resume_keeps_the_age(self):
        """D-218: the real transcript reader decides (no stub). The conversation began a night ago and
        was resumed into a new tmux session at the previous start (`claude --resume` appends to the same
        transcript): the save prompt is due and the next start into that tmux session compacts it."""
        ka.conversation_started = self.old["conversation_started"]
        self.write([entry("user", NIGHT - timedelta(days=1), message={"role": "user", "content": "old"}),
                    assistant(NIGHT - timedelta(days=1) + timedelta(seconds=9)),
                    entry("user", NIGHT + timedelta(seconds=30), message={"role": "user", "content": "resumed"}),
                    assistant(NIGHT + timedelta(seconds=40), text="HANDOFF SAVED")])
        self.make_old()
        self.assertEqual(ka.save_due(SID, RESET)[0], True)
        rec = self.start(at=S2)
        self.assertTrue(rec["compaction"]["due"])
        self.assertTrue(self.sent[0][1].startswith("/compact "))
        self.assertIn("conversation since", rec["compaction"]["why"])
        # a conversation begun at 23:00 is not compacted at 04:00, and its save prompt is not due
        self.write([entry("user", NIGHT + timedelta(seconds=30), message={"role": "user", "content": "go"}),
                    assistant(NIGHT + timedelta(seconds=40))])
        self.assertEqual(ka.save_due(SID, RESET)[0], False)
        self.assertFalse(ka.compaction_plan(SID, S2)["due"])

    def test_timeout_continues_anyway(self):
        self.compacts = False
        ka.COMPACT_TIMEOUT = timedelta(0)
        h = self.handoff()
        rec = self.start()
        self.assertEqual(len(self.sent), 2)
        self.assertIn(f"Hand-off #{h['id']}", self.sent[1][1])
        self.assertEqual(rec["compaction"]["status"], "timeout")
        self.assertIn("no compaction marker", self.progress())

    def test_busy_session_is_not_compacted_this_time(self):
        self.write([assistant(datetime.now(UTC), stop="tool_use",
                              content=[{"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}])], "a")
        self.make_old()
        ka.COMPACT_IDLE_WAIT = timedelta(0)
        self.handoff()
        rec = self.start()
        self.assertEqual(len(self.sent), 1)                                   # no /compact
        self.assertNotIn("compacted at this start", self.sent[0][1])
        self.assertEqual(rec["compaction"]["status"], "busy")
        self.assertIsNotNone(self.latest())                                   # kept for the next start
        self.assertIn("still mid-turn", self.progress())

    def test_session_idle(self):
        now = datetime.now(UTC)
        self.assertTrue(ka.session_idle(self.path))                           # end_turn, quiet
        self.assertFalse(ka.session_idle(self.path, now=datetime.fromtimestamp(os.path.getmtime(self.path), UTC)))
        self.write([entry("user", now, message={"role": "user", "content": "more"})], "a")
        self.make_old()
        self.assertFalse(ka.session_idle(self.path))                          # a queued prompt
        self.write([entry("user", now, message={"role": "user", "content": "<local-command-stdout>x"})], "a")
        self.make_old()
        self.assertTrue(ka.session_idle(self.path))
        self.assertTrue(ka.session_idle(None))

    def test_db_error_never_stops_the_continue(self):
        h = self.handoff()
        old = store.connect

        def broken(*a, **k):
            raise store.DBUnavailable("the database is missing", "missing")
        store.connect = broken
        try:
            rec = self.start()
        finally:
            store.connect = old
        self.assertEqual(len(self.sent), 2)
        self.assertIn("database problem, DBUnavailable", self.sent[1][1])
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertEqual(self.latest()["id"], h["id"])                        # not consumed

    def test_a_failing_compaction_never_stops_the_start(self):
        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith("/compact"):
                raise OSError("host bridge down")
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        self.handoff()
        rec = self.start()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("compacted at this start", self.sent[0][1])
        self.assertEqual((rec["result"], rec["compaction"]["status"]), ("continued-in-place", "error"))
        self.assertIsNotNone(self.latest())

    def test_consume_failure_is_logged_only(self):
        self.handoff()
        old = store.consume_handoff

        def broken(*a, **k):
            raise store.DBUnavailable("disk full", "disk_full")
        store.consume_handoff = broken
        try:
            rec = self.start()
        finally:
            store.consume_handoff = old
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertFalse(rec["compaction"]["handoff_consumed"])

    def test_dry_run_records_the_plan(self):
        rec = self.start(args=DryRun())
        self.assertEqual(self.sent, [])
        self.assertEqual((rec["result"], rec["compaction"]["due"]), ("dry-run", True))
        self.assertIn("after compacting it (D-191/D-218)", self.progress())

    def test_window_start_and_last_mile_pass_the_nominal_start(self):
        """window_start_pass gives handle_fire the session-window start (23:00 sharp, not the cron's
        23:00:40), the last-stretch slot its slot start."""
        seen = []
        old = ka.handle_fire
        ka.handle_fire = lambda sid, stall, reason, st, args: seen.append(stall)
        ka.fresh_usage = lambda now, force=False: self.usage
        ka.budget_eval = lambda u, now: {"go": True, "reason": "CONTINUE: t", "text": "budget t", "headroom": 5.0,
                                         "postpone": False, "recheck_at": None}
        try:
            at = NIGHT + timedelta(days=1, seconds=40)
            ka.window_start_pass(SID, at, types.SimpleNamespace(arm=False, now=False))
            wr = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
            self.usage["weekly"] = {"percent": 90.0, "resets_at": wr}
            old_lm = ka.last_mile_hours
            ka.last_mile_hours = lambda pct, r=None, n=None: 10
            try:
                ka.last_mile_pass(SID, wr - timedelta(hours=7), {"handled": {}}, Armed())
            finally:
                ka.last_mile_hours = old_lm
        finally:
            ka.handle_fire = old
        self.assertEqual(seen[0]["start_at"], NIGHT + timedelta(days=1))
        self.assertEqual(seen[1]["start_at"], wr - 2 * L)                     # slot 2 starts 10 h before


if __name__ == "__main__":
    unittest.main()
