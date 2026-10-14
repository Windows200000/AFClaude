#!/usr/bin/env python3
"""Offline tests for the compaction of long-running sessions (D-144, D-191, D-214, D-233; design
§5.5): the D-191/D-218 rule (the conversation's first transcript entry before the session window),
and the save-then-compact-then-continue sequence at a start: the save prompt right before the
/compact (D-233), the new hand-off injected and consumed; any failure or timeout on the way (no
new hand-off, a busy session, no compaction marker, DB and send failures) aborts the start and
pauses automation (D-234).
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
import afclaude_config  # noqa: E402
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
SAVE = "[keep-alive] Save your work now"


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
    NAMES = ("PROJECTS_DIR", "STATE_FILE", "PROGRESS_FILE", "FILLUP_FILE", "DEFER_FILE",
             "USAGE_STATE_FILE", "alert", "fire", "preflight", "conversation_started", "read_usage_cache", "db_paused",
             "agent_entries", "_limit_hit_in", "fresh_usage", "budget_eval", "COMPACT_TIMEOUT",
             "COMPACT_IDLE_WAIT", "COMPACT_POLL", "SAVE_TIMEOUT", "_fire_usage")

    def setUp(self):
        testenv.clear_settings()
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.old = {n: getattr(ka, n) for n in self.NAMES}
        ka.PROJECTS_DIR = d
        ka.STATE_FILE, ka.PROGRESS_FILE = os.path.join(d, "state.json"), os.path.join(d, "PROGRESS.md")
        testenv.notes_project(d)                     # the progress notes go to its doc in the DB (§7.7)
        ka.FILLUP_FILE = os.path.join(d, "fillup.json")
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
        self.saves = "write"                                # ... and answers the save prompt (see fake_fire)
        self.save_summary = "Fresh state at the start."
        ka.fire = self.fake_fire
        self.proj = os.path.join(d, "-proj")
        os.makedirs(self.proj)
        self.path = os.path.join(self.proj, f"{SID}.jsonl")
        self.write([entry("user", NIGHT - timedelta(hours=1), message={"role": "user", "content": "go"}),
                    assistant(NIGHT - timedelta(minutes=50), text="done for now")])
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
        /compact (a compact_boundary + summary in the same transcript), answers the save prompt as
        self.saves says (write: a hand-off into the DB, then "HANDOFF SAVED"; silent: a reply
        without one; hang: still working on it; ignore: nothing reaches the transcript) and answers
        anything else. The transcript is then quiet (old mtime), so the session reads as idle."""
        self.sent.append((plan, message))
        now = datetime.now(UTC)
        if message.startswith("/compact "):
            if self.compacts:
                self.write([entry("system", now, subtype="compact_boundary", content="Conversation compacted",
                                  compactMetadata={"trigger": "manual"}),
                            entry("user", now, isCompactSummary=True,
                                  message={"role": "user", "content": "This session is being continued ..."})], "a")
        elif message.startswith(SAVE):
            if self.saves == "ignore":
                return 0, "sent-keys ka-abcdef01", ""
            self.write([entry("user", now, message={"role": "user", "content": message})], "a")
            if self.saves == "write":
                self.handoff(self.save_summary, next_steps=["go on"])
            if self.saves in ("write", "silent"):
                self.write([assistant(now + timedelta(seconds=1), text="HANDOFF SAVED")], "a")
        else:
            self.write([entry("user", now, message={"role": "user", "content": message}),
                        assistant(now + timedelta(seconds=1))], "a")
        self.make_old()
        return 0, "sent-keys ka-abcdef01", ""

    # ---- helpers
    def handoff(self, summary="state", at=None, **body):
        conn = store.connect()
        try:
            h = actions.perform(conn, "handoff.write", {"session_id": SID, "body": dict(summary=summary, **body)},
                                actor="cli", via="cli")
            if at is not None:                               # an older hand-off (written at `at`)
                with store.transaction(conn):
                    conn.execute("UPDATE handoffs SET created_at=? WHERE id=?",
                                 (at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"), h["id"]))
                h = store.get_handoff(conn, h["id"])
            return h
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


# ---------------------------------------------------------------- no end-of-window save (D-233)

class NoWindowEndSave(Env):
    def test_the_watcher_and_the_fillup_send_no_save_prompt(self):
        """D-233: the hand-off is made right before the compaction only. The watcher has no
        end-of-window save pass any more, and a fill-up message carries no save prompt."""
        self.assertFalse(hasattr(ka, "handoff_save_pass"))
        seen = []
        old = ka.handle_fire
        ka.handle_fire = lambda sid, stall, reason, st, args: seen.append(stall)
        try:
            ka._fire_fillup(SID, RESET - timedelta(minutes=6),
                            {"key": fillup.key(RESET), "reset": RESET, "remaining": 5.0, "cost": 0.7}, Armed())
        finally:
            ka.handle_fire = old
        self.assertNotIn("insert", seen[0])
        self.assertNotIn("HANDOFF", json.dumps(seen[0], default=str))
        names = ("LOCK_FILE", "STOP_FILE", "last_mile_pass", "deferred_window_start_pass", "fillup_pass",
                 "watch_run_usage")
        old_w = {n: getattr(ka, n) for n in names}
        ka.LOCK_FILE, ka.STOP_FILE = os.path.join(self.tmp.name, "ka.lock"), os.path.join(self.tmp.name, "STOP")
        ka.last_mile_pass = lambda *a: None
        ka.deferred_window_start_pass = lambda *a: None
        ka.fillup_pass = lambda *a: None
        ka.watch_run_usage = lambda *a: None
        try:
            ka.run(types.SimpleNamespace(session=SID, arm=True, once=True, now=False))
        finally:
            for n, v in old_w.items():
                setattr(ka, n, v)
        self.assertEqual(self.sent, [])


# ---------------------------------------------------------------- save + compact + resume at a start

class CompactAtStart(Env):
    KEY = "window-start-2026-10-07"

    def start(self, args=Armed(), at=None):
        """The next night's 23:00 session-window start fires a continue for SID."""
        at = at or NIGHT + timedelta(days=1)
        stall = {"uuid": self.KEY, "timestamp": at, "budget": "budget for this run: +14%", "headroom": 14.0,
                 "start_at": schedule.latest_session_start(at + timedelta(seconds=5)) or at}
        ka.handle_fire(SID, stall, "window start, CONTINUE: test", ka.load_state(), args)
        return ka.load_state()["handled"][self.KEY]

    def test_save_then_compact_then_continue_with_the_fresh_handoff(self):
        old = self.handoff("Phase 1 state.", at=NIGHT - timedelta(days=3))
        self.save_summary = "Phase 2 merged."
        rec = self.start()
        self.assertEqual([p for p, _ in self.sent], ["send-keys"] * 3)
        save, compact, cont = (m for _, m in self.sent)
        self.assertTrue(save.startswith(SAVE))
        self.assertNotIn("\n", save)
        for part in ("COMPACTS", "right after your reply", "afclaude_handoff", f'session "{SID}"',
                     "tasks.py handoff write --session", "HANDOFF SAVED", "D-233"):
            self.assertIn(part, save)
        self.assertTrue(compact.startswith("/compact Keep what the task-manager needs"))
        self.assertNotIn("\n", compact)
        self.assertTrue(cont.startswith("[keep-alive] A new run starts"))
        new = self.latest(unconsumed=False)
        for part in ("compacted at this start", f"Hand-off #{new['id']}", "Summary: Phase 2 merged.",
                     "Next steps: (1) go on", "SessionStart hook"):
            self.assertIn(part, cont)
        self.assertNotIn("Phase 1 state.", cont)
        self.assertNotIn("STALE", cont)
        # the session runs in the repo (session_cwd falls back to HERE): the rules come from the
        # SessionStart hook after the compaction, the message only points at them (D-225)
        self.assertLess(cont.index("Hand-off #"), cont.index("SessionStart hook"))
        self.assertNotIn("TASK-MANAGER", cont)
        self.assertNotIn("Guard hooks", cont)
        self.assertEqual(rec["result"], "continued-in-place")
        c = rec["compaction"]
        self.assertEqual((c["save"]["status"], c["save"]["handoff_id"], c["status"], c["handoff_id"],
                          c["handoff_consumed"]), ("saved", new["id"], "compacted", new["id"], True))
        self.assertIsNone(self.latest())                                     # consumed, the older one too
        self.assertEqual(new["consumed_by_start"], self.KEY)
        self.assertGreater(new["id"], old["id"])
        self.assertNotIn("WARNING", self.progress())

    def test_interactive_only_window_gets_a_fresh_handoff(self):
        """The 13.10. bug: no keep-alive run in the window before the start, only the owner's chats
        after an old hand-off. The start still saves first, so the hand-off injected after the
        compaction is the fresh one, never the old one."""
        self.handoff("Old hand-off from three days ago.", at=NIGHT - timedelta(days=2))
        day = NIGHT + timedelta(hours=14)
        self.write([entry("user", day, message={"role": "user", "content": "owner: new decision D-x"}),
                    assistant(day + timedelta(minutes=1), text="noted"),
                    entry("user", day + timedelta(hours=3), message={"role": "user", "content": "owner: and this"}),
                    assistant(day + timedelta(hours=3, minutes=1), text="done")], "a")
        self.make_old()
        ka.save_state({"handled": {}, "fires": {}})
        self.save_summary = "Includes the owner's chats."
        rec = self.start()
        self.assertTrue(self.sent[0][1].startswith(SAVE))
        cont = self.sent[2][1]
        self.assertIn("Summary: Includes the owner's chats.", cont)
        self.assertNotIn("Old hand-off", cont)
        self.assertEqual(rec["compaction"]["save"]["status"], "saved")
        self.assertEqual(rec["compaction"]["save"]["active"], (day + timedelta(hours=3, minutes=1)).isoformat())

    # ---- D-234: every failure or timeout on the way aborts the start and pauses automation
    def assert_aborted(self, rec, why, sent):
        """The start was aborted: recorded as start-failed with `why`, exactly the messages `sent`
        (prefixes) went out (never a continue), automation is paused with the reason, the owner got
        an alert and the progress doc says so."""
        self.assertEqual(rec["result"], "start-failed")
        self.assertIn(why, rec["error"])
        self.assertEqual(len(self.sent), len(sent), [m[:40] for _, m in self.sent])
        for (_, m), want in zip(self.sent, sent):
            self.assertTrue(m.startswith(want), (m[:60], want))
        self.assertFalse(any("compacted at this start" in m or m.startswith("[keep-alive] A new run")
                             for _, m in self.sent))
        p = afclaude_config.automation_pause()
        self.assertTrue(p["paused"])
        self.assertIn(f"keepalive: start of {SID[:8]} aborted: compaction failed", p["reason"])
        self.assertEqual(p["by"], "runner:keepalive")
        self.assertTrue(any(a.startswith(f"AFClaude STOPPED: start of {SID[:8]} aborted: compaction failed")
                            for a in self.alerts), self.alerts)
        self.assertIn("ABORTED", self.progress())
        self.assertNotIn("compacted at this start", json.dumps(rec))
        return rec

    def test_save_timeout_aborts_the_start(self):
        """D-234: the save prompt got no hand-off in time (here: it never reached the session). No
        /compact, no continue; an older hand-off is never injected (no stale label any more)."""
        ka.SAVE_TIMEOUT = timedelta(0)
        self.saves = "ignore"
        old = self.handoff("Old state.", at=NIGHT - timedelta(days=2))
        logs = []
        old_log = ka.log
        ka.log = logs.append
        try:
            rec = self.start()
        finally:
            ka.log = old_log
        self.assert_aborted(rec, "no new hand-off within 10 min of the save prompt", [SAVE])
        self.assertEqual(rec["compaction"]["save"]["status"], "timeout")
        self.assertTrue(any(ln.startswith("ERROR: keepalive: start of") for ln in logs), logs)
        self.assertEqual(self.latest()["id"], old["id"])                      # not consumed
        self.assertNotIn("STALE", json.dumps(self.sent))

    def test_a_reply_without_a_handoff_aborts_the_start(self):
        self.saves = "silent"                                  # it answered, but wrote none
        self.handoff("Written after the last chat.", at=NIGHT - timedelta(minutes=20))
        rec = self.start()
        self.assert_aborted(rec, "the session answered the save prompt without writing a hand-off", [SAVE])
        self.assertEqual(rec["compaction"]["save"]["status"], "no-handoff")

    def test_save_prompt_send_failure_aborts_the_start(self):
        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith(SAVE):
                self.sent.append((plan, message))
                return 1, "", "no tmux session"
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        rec = self.start()
        self.assert_aborted(rec, "the save prompt could not be sent", [SAVE])
        self.assertIn("no tmux session", rec["error"])
        self.assertEqual(rec["compaction"]["save"]["status"], "send-failed")

    def test_save_still_running_at_the_timeout_aborts_the_start(self):
        """The session is still working on the save at the timeout: an error, no /compact."""
        ka.SAVE_TIMEOUT = timedelta(0)
        self.saves = "hang"
        old = self.handoff("Old state.", at=NIGHT - timedelta(days=2))
        os.utime(self.path, None)                                             # it just wrote: busy
        rec = self.start()
        self.assert_aborted(rec, "(the session was still working on it)", [SAVE])
        self.assertEqual(rec["compaction"]["save"]["status"], "timeout")
        self.assertEqual(self.latest()["id"], old["id"])

    def test_session_outside_the_repo_gets_the_rules_inline(self):
        """No SessionStart hook outside the repo's root: the continue carries the full rules (D-225)."""
        old = ka.session_cwd
        ka.session_cwd = lambda path: self.tmp.name
        try:
            self.start()
        finally:
            ka.session_cwd = old
        cont = self.sent[2][1]
        for part in ("Guard hooks", "TASK-MANAGER", "usage_report.py", "AFK rules (unattended runs)", "--run-stopped"):
            self.assertIn(part, cont)
        self.assertNotIn("SessionStart hook;", cont)

    def test_not_due_continues_normally(self):
        """D-191/D-218: a conversation begun at the previous start is neither saved nor compacted."""
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
        self.assertFalse(self.sent[0][1].startswith(("/compact", SAVE)))

    def test_the_transcript_decides_and_a_resume_keeps_the_age(self):
        """D-218: the real transcript reader decides (no stub). The conversation began a night ago and
        was resumed into a new tmux session at the previous start (`claude --resume` appends to the same
        transcript): the next start into that tmux session saves and compacts it."""
        ka.conversation_started = self.old["conversation_started"]
        self.write([entry("user", NIGHT - timedelta(days=1), message={"role": "user", "content": "old"}),
                    assistant(NIGHT - timedelta(days=1) + timedelta(seconds=9)),
                    entry("user", NIGHT + timedelta(seconds=30), message={"role": "user", "content": "resumed"}),
                    assistant(NIGHT + timedelta(seconds=40), text="ok")])
        self.make_old()
        rec = self.start(at=S2)
        self.assertTrue(rec["compaction"]["due"])
        self.assertTrue(self.sent[0][1].startswith(SAVE))
        self.assertTrue(self.sent[1][1].startswith("/compact "))
        self.assertIn("conversation since", rec["compaction"]["why"])
        # a conversation begun at 23:00 is not compacted at 04:00
        self.write([entry("user", NIGHT + timedelta(seconds=30), message={"role": "user", "content": "go"}),
                    assistant(NIGHT + timedelta(seconds=40))])
        self.assertFalse(ka.compaction_plan(SID, S2)["due"])

    def test_compaction_marker_timeout_aborts_the_start(self):
        """No compaction marker within COMPACT_TIMEOUT: no continue; the saved hand-off stays
        unconsumed for the next start."""
        self.compacts = False
        ka.COMPACT_TIMEOUT = timedelta(0)
        rec = self.start()
        self.assert_aborted(rec, "no compaction marker within 10 min of /compact", [SAVE, "/compact "])
        self.assertEqual((rec["compaction"]["save"]["status"], rec["compaction"]["status"]), ("saved", "timeout"))
        self.assertEqual(self.latest()["id"], rec["compaction"]["save"]["handoff_id"])

    def test_busy_session_aborts_the_start(self):
        """Still mid-turn COMPACT_IDLE_WAIT after the start: no save prompt, no /compact, no continue."""
        self.write([assistant(datetime.now(UTC), stop="tool_use",
                              content=[{"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}])], "a")
        self.make_old()
        ka.COMPACT_IDLE_WAIT = timedelta(0)
        self.handoff()
        rec = self.start()
        self.assert_aborted(rec, "the session was still mid-turn", [])
        self.assertEqual(rec["compaction"]["save"]["status"], "busy")
        self.assertIsNotNone(self.latest())                                   # kept

    def test_saved_but_not_found_is_an_error(self):
        """The edge case of the D-233 build: the save wrote a hand-off, but by the continue there is
        no unconsumed one that new (here something consumed it meanwhile). An error, never a silent
        "none saved" continue."""
        orig = self.fake_fire

        def fire(sid, cwd, message, plan, new=False, **kw):
            r = orig(sid, cwd, message, plan, new, **kw)
            if message.startswith("/compact "):
                h = self.latest()
                conn = store.connect()
                try:
                    actions.perform(conn, "handoff.consume", {"handoff_id": h["id"], "start_key": "other"},
                                    actor="keepalive", via="runner")
                finally:
                    conn.close()
            return r
        ka.fire = fire
        rec = self.start()
        self.assert_aborted(rec, "the saved hand-off was not found", [SAVE, "/compact "])
        self.assertIn("the latest unconsumed one of abcdef01 is none", rec["error"])

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

    def test_last_activity(self):
        self.assertEqual(ka.last_activity(self.path), NIGHT - timedelta(minutes=50))
        self.assertIsNone(ka.last_activity(None))
        self.assertIsNone(ka.last_activity(os.path.join(self.tmp.name, "gone.jsonl")))

    def test_db_error_aborts_the_start(self):
        """The hand-offs can't be read (after store.retrying, D-171): no save prompt, no continue."""
        h = self.handoff()
        old = store.latest_handoff

        def broken(*a, **k):
            raise store.DBUnavailable("the database is missing", "missing")
        store.latest_handoff = broken
        try:
            rec = self.start()
        finally:
            store.latest_handoff = old
        self.assert_aborted(rec, "the hand-offs could not be read", [])
        self.assertIn("DBUnavailable", rec["error"])
        self.assertEqual(self.latest()["id"], h["id"])                        # not consumed

    def test_a_failing_compact_step_aborts_the_start(self):
        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith("/compact"):
                raise OSError("host bridge down")
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        rec = self.start()
        self.assert_aborted(rec, "the /compact step failed", [SAVE])
        self.assertIn("host bridge down", rec["error"])
        self.assertIsNotNone(self.latest())                                   # the saved one waits

    def test_a_failing_save_step_aborts_the_start(self):
        old = ka.save_before_compaction

        def boom(*a):
            raise RuntimeError("unexpected")
        ka.save_before_compaction = boom
        try:
            rec = self.start()
        finally:
            ka.save_before_compaction = old
        self.assert_aborted(rec, "the save step failed", [])
        self.assertIn("RuntimeError: unexpected", rec["error"])

    def test_consume_failure_is_an_error_after_the_continue(self):
        """The continue went out (the run goes on), but the hand-off could not be marked consumed:
        recorded, alerted and automation paused (the next start would inject it again)."""
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
        p = afclaude_config.automation_pause()
        self.assertTrue(p["paused"])
        self.assertIn("not marked consumed", p["reason"])

    def test_paused_automation_starts_nothing(self):
        """D-234/D-235: while automation_paused is set, a start sends nothing and is not handled
        (it is decided again once the owner cleared the pause)."""
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "test"}, actor="cli", via="cli")
        finally:
            conn.close()
        at = NIGHT + timedelta(days=1)
        r = ka.handle_fire(SID, {"uuid": self.KEY, "timestamp": at, "budget": "b", "headroom": 1.0,
                                 "start_at": at}, "window start, CONTINUE: test", ka.load_state(), Armed())
        self.assertEqual(r, "paused")
        self.assertEqual(self.sent, [])
        self.assertNotIn(self.KEY, ka.load_state()["handled"])

    def test_dry_run_records_the_plan(self):
        rec = self.start(args=DryRun())
        self.assertEqual(self.sent, [])
        self.assertEqual((rec["result"], rec["compaction"]["due"]), ("dry-run", True))
        self.assertIn("after a save prompt and compacting it (D-191/D-218/D-233)", self.progress())

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


# ---------------------------------------------------------------- D-234/D-235: the other start failures

class FailStopAtStart(Env):
    """Every failure or timeout of a start (not only the compaction) is an error: alerted, recorded,
    automation paused; before the continue went out the start is aborted (start-failed)."""
    KEY = "window-start-2026-10-07"

    def setUp(self):
        super().setUp()
        self.first = NIGHT + timedelta(days=1) - L + timedelta(seconds=5)   # not due: no compaction
        self.addCleanup(testenv.unpause)

    def start(self):
        at = NIGHT + timedelta(days=1)
        r = ka.handle_fire(SID, {"uuid": self.KEY, "timestamp": at, "budget": "b", "headroom": 1.0,
                                 "start_at": at}, "window start, CONTINUE: test", ka.load_state(), Armed())
        return r, ka.load_state()["handled"].get(self.KEY)

    def paused(self):
        return afclaude_config.automation_pause()

    def test_no_reply_within_the_verify_timeout_pauses(self):
        old = ka.verify_reply
        ka.verify_reply = lambda path, since, timeout=None: None
        try:
            r, rec = self.start()
        finally:
            ka.verify_reply = old
        self.assertEqual(rec["result"], "no-reply-within-timeout")             # the run counts (RUN_RESULTS)
        self.assertIn("keep-alive continue of abcdef01 failed: no-reply-within-timeout", self.paused()["reason"])
        self.assertEqual(self.alerts, ["AFClaude STOPPED: keep-alive continue of abcdef01 failed: "
                                       "no-reply-within-timeout"])

    def test_launcher_failure_pauses(self):
        ka.fire = lambda *a, **k: (1, "", "ka_resume.sh: no such session")
        r, rec = self.start()
        self.assertEqual(rec["result"], "launcher-failed")
        self.assertTrue(self.paused()["paused"])

    def test_fire_raising_aborts_the_start(self):
        """e.g. the bridge call timed out, or a take-over's holder did not exit."""
        def fire(*a, **k):
            raise ka.subprocess.TimeoutExpired("ka_resume.sh", 120)
        ka.fire = fire
        r, rec = self.start()
        self.assertEqual((r, rec["result"]), ("failed", "start-failed"))
        self.assertIn("TimeoutExpired", rec["error"])
        self.assertIn("keepalive: start of abcdef01 aborted: start failed", self.paused()["reason"])
        self.assertNotIn("fires", json.dumps(rec))
        self.assertIn("ABORTED", self.progress())

    def test_preflight_refusal_pauses(self):
        ka.preflight = lambda sid: (False, ["host process info unavailable (TimeoutExpired: x)"], None)
        r, rec = self.start()
        self.assertEqual((rec["result"], rec["held"]), ("preflight-failed", False))
        self.assertIn("keep-alive did NOT continue abcdef01: preflight failed", self.paused()["reason"])
        self.assertEqual(self.sent, [])

    def test_the_owner_holding_the_session_is_a_skip_not_a_failure(self):
        """Task-manager decision on D-234/D-235: Remote Control, an attached terminal, another live
        holder or a busy turn at a start is a normal skip (log + progress note), no alert, no pause."""
        for problem in (f"{ka.RC_HELD} thread (pid [7]); a take-over would fork ...",
                        f"{ka.OTHER_HELD} [8] (~/.claude/sessions, outside our tmux); ...",
                        "session is busy in another process (someone is using it)",
                        "held by an idle interactive process [9] outside tmux; a resume would FORK a copy"):
            ka.save_state({"handled": {}, "fires": {}})
            ka.preflight = lambda sid, p=problem: (False, [p], None)
            r, rec = self.start()
            self.assertEqual((rec["result"], rec["held"]), ("preflight-failed", True), problem)
            self.assertFalse(self.paused()["paused"], problem)
        self.assertEqual((self.alerts, self.sent), ([], []))
        self.assertIn("skipped: the owner holds the session", self.progress())
        self.assertFalse(ka.owner_holds(["host process info unavailable (OSError: x); not touching the session"]))
        self.assertFalse(ka.owner_holds([]))

    def test_an_unreadable_transcript_aborts_the_start(self):
        def boom(sid, path=None):
            raise OSError("I/O error")
        ka.conversation_started = boom
        r, rec = self.start()
        self.assertEqual(rec["result"], "start-failed")
        self.assertIn("OSError: I/O error", rec["error"])
        self.assertEqual(self.sent, [])

    def test_a_failed_process_check_after_the_continue_pauses(self):
        def boom(sid):
            raise OSError("`claude agents` failed rc=1")
        ka.agent_entries = boom
        r, rec = self.start()
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertIn("OSError", rec["env_error"])
        self.assertIn("process check after the continue of abcdef01 failed", self.paused()["reason"])

    def test_paused_window_start_neither_reads_usage_nor_fires(self):
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "owner looks"}, actor="cli", via="cli")
        finally:
            conn.close()
        ka.fresh_usage = lambda now, force=False: self.fail("no /usage call while paused")
        d = ka.window_start_pass(SID, NIGHT + timedelta(days=1, seconds=40), types.SimpleNamespace(arm=True, now=False))
        self.assertFalse(d["go"])
        self.assertIn("PAUSED: automation is paused (owner looks)", d["reason"])
        self.assertEqual(self.sent, [])
        self.assertIn("tasks.py automation resume", self.progress())


    def test_paused_last_stretch_slot_waits_without_reading_usage(self):
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "owner looks"}, actor="cli", via="cli")
        finally:
            conn.close()
        wr = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
        self.usage["weekly"] = {"percent": 90.0, "resets_at": wr}
        ka.fresh_usage = lambda now, force=False: self.fail("no /usage call while paused")
        old = ka.last_mile_hours
        ka.last_mile_hours = lambda pct, r=None, n=None: 10
        try:
            now = wr - timedelta(hours=7)
            self.assertEqual(ka.last_mile_pass(SID, now, {"handled": {}}, Armed()), now + ka.LAST_MILE_RECHECK)
        finally:
            ka.last_mile_hours = old
        self.assertEqual(self.sent, [])


class KaResumeFailures(unittest.TestCase):
    """ka_resume.sh: a tmux call that fails exits 1 (the keep-alive sees it), never a silent
    "sent-keys" / "started" with exit 0 (D-234/D-235). A stub tmux on PATH; nothing real runs."""
    def run_script(self, fail_on):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            tmux = os.path.join(d, "tmux")
            with open(tmux, "w") as fh:
                fh.write("#!/bin/sh\n"
                         f'case "$1" in has-session) exit {0 if fail_on.startswith("send") else 1};; esac\n'
                         f'case "$1 $*" in *{fail_on}*) exit 1;; esac\nexit 0\n')
            os.chmod(tmux, 0o755)
            env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], HOME=d, KA_TRUST_ROOT=d)
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ka_resume.sh")
            return subprocess.run(["bash", script, "--session", SID, "--message", "go", "--cwd", d],
                                  env=env, capture_output=True, text=True, timeout=60)

    def test_send_keys_failure_exits_1(self):
        r = self.run_script("send-keys")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("send-keys to ka-abcdef01 failed", r.stderr)
        self.assertNotIn("sent-keys", r.stdout)


class FireFailures(unittest.TestCase):
    """keepalive.fire: a take-over whose holder does not exit, a bg worker that does not stop, and
    `claude agents` failing raise instead of resuming into a fork (D-234/D-235)."""
    def setUp(self):
        self.old = {n: getattr(ka, n) for n in ("pid_alive", "TAKE_OVER_WAIT")}
        self.old_host = {n: getattr(ka.host, n) for n in ("kill_claude", "run_on_host")}
        self.runs = []
        ka.host.run_on_host = lambda argv, **k: self.runs.append(argv) or types.SimpleNamespace(
            returncode=self.rc, stdout="[]", stderr="nope")
        ka.host.kill_claude = lambda pid: True
        self.rc = 0

    def tearDown(self):
        for n, v in self.old.items():
            setattr(ka, n, v)
        for n, v in self.old_host.items():
            setattr(ka.host, n, v)

    def test_take_over_holder_still_alive_raises(self):
        ka.pid_alive = lambda pid: True
        ka.TAKE_OVER_WAIT = 0
        with self.assertRaisesRegex(RuntimeError, r"pid\(s\) \[42\] still alive"):
            ka.fire(SID, "/tmp", "go", "take-over:42")
        self.assertEqual(self.runs, [])                                      # never resumed

    def test_bg_stop_failure_raises(self):
        self.rc = 1
        with self.assertRaisesRegex(RuntimeError, "claude stop abcdef01. failed rc=1"):
            ka.fire(SID, "/tmp", "go", "stop-bg-then-resume")
        self.assertEqual(len(self.runs), 1)                                  # only the stop, no resume

    def test_a_failed_usage_cache_read_raises_in_the_decision_paths(self):
        old = ka.CLAUDE_JSON
        ka.CLAUDE_JSON = "/nonexistent/claude.json"
        try:
            self.assertIsNone(ka.read_usage_cache())                        # the read-only views
            with self.assertRaises(OSError):
                ka.usage_cache_checked()                                    # the runners' decisions
            with self.assertRaises(OSError):
                ka.last_mile_pass(SID, datetime.now(UTC), {"handled": {}}, Armed())
        finally:
            ka.CLAUDE_JSON = old

    def test_agent_entries_failure_raises(self):
        self.rc = 1
        with self.assertRaisesRegex(OSError, "claude agents. failed rc=1"):
            ka.agent_entries(SID)
        self.rc = 0
        self.assertEqual(ka.agent_entries(SID), [])
        ka.host.run_on_host = lambda argv, **k: types.SimpleNamespace(returncode=0, stdout="not json", stderr="")
        with self.assertRaises(ValueError):
            ka.agent_entries(SID)


if __name__ == "__main__":
    unittest.main()
