#!/usr/bin/env python3
"""Offline tests for the compaction of the task-manager (D-144, D-233, D-234, D-238; design §5.5):
the watcher's compaction_pass compacts when a run ended (D-224) or after 50 min idle, while the
prompt cache is warm, only at or above the break-even (compact_timing.py), never while a run uses
the session, a turn or subagent runs or the owner is typing; the save prompt right before the
/compact (D-233), the fresh hand-off given back and consumed; every failure or timeout on the way
(no new hand-off, a busy session, no compaction marker, DB and send failures) is recorded and
pauses automation (D-234/D-235). Starts never compact any more; they wait for a running one.
A fake tmux (fire / preflight / owner_typing) and a fake transcript; no real Claude, no tmux."""
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
import compact_timing as ct  # noqa: E402
import fillup  # noqa: E402
import keepalive as ka  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
SID = "abcdef01-2345-4678-9abc-def012345678"
L = timedelta(hours=5)
NIGHT = datetime(2026, 10, 5, 21, 0, tzinfo=UTC)        # Mon 23:00 Berlin (CEST): the night's first start
S2 = NIGHT + L                                          # 04:00 Berlin: the second session-window start
RESET = S2                                              # the 23:00 run's session window resets at 04:00
SAVE = "[keep-alive] Save your work now"
AFTER = "[keep-alive] This session was compacted"
_N = [0]


def entry(kind, ts, **kw):
    e = {"type": kind, "timestamp": ts.isoformat().replace("+00:00", "Z")}
    e.update(kw)
    return e


def assistant(ts, stop="end_turn", text="ok", content=None, ctx=None, write=0):
    """An assistant entry; ctx: a request with usage (a context of ctx tokens, `write` of them written)."""
    _N[0] += 1
    e = entry("assistant", ts, uuid=f"a-{_N[0]}", requestId=f"req-{_N[0]}",
              message={"role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
                       "content": content or [{"type": "text", "text": text}]})
    if ctx is not None:
        e["message"]["usage"] = {"input_tokens": 2, "cache_creation_input_tokens": write,
                                 "cache_read_input_tokens": ctx - write - 2, "output_tokens": 400,
                                 "cache_creation": {"ephemeral_1h_input_tokens": write}}
    return e


def owner(ts, text="owner: a question"):
    return entry("user", ts, origin={"kind": "human"}, promptSource="typed",
                 message={"role": "user", "content": text})


def keepalive_msg(ts, text="[keep-alive] A new run starts: go"):
    return entry("user", ts, origin={"kind": "human"}, promptSource="typed", message={"role": "user", "content": text})


class Env(unittest.TestCase):
    """Temp state files, transcripts root and PROGRESS.md; the keep-alive's host calls faked."""
    NAMES = ("PROJECTS_DIR", "STATE_FILE", "PROGRESS_FILE", "FILLUP_FILE", "DEFER_FILE",
             "USAGE_STATE_FILE", "alert", "fire", "preflight", "read_usage_cache", "db_paused",
             "agent_entries", "_limit_hit_in", "fresh_usage", "budget_eval", "COMPACT_TIMEOUT",
             "COMPACT_IDLE_WAIT", "COMPACT_POLL", "SAVE_TIMEOUT", "_fire_usage", "owner_typing", "start_soon",
             "VERIFY_TIMEOUT", "COMPACT_LOCK_WAIT")

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
        ka.owner_typing = lambda sid, now: self.typing
        ka.start_soon = lambda sid, now: self.soon
        ka.COMPACT_POLL = 0
        ka.VERIFY_TIMEOUT = timedelta(seconds=2)
        self.typing = self.soon = None
        self.alerts, self.sent = [], []
        self.now = datetime.now(UTC)
        self.usage = {"session": {"percent": 50.0, "resets_at": self.now + timedelta(hours=2)},
                      "weekly": {"percent": 40.0, "resets_at": self.now + timedelta(days=3)},
                      "fetched_at": self.now - timedelta(minutes=3)}
        ka.read_usage_cache = lambda: self.usage
        self.compacts = True                                # the fake session compacts on /compact
        self.saves = "write"                                # ... and answers the save prompt (see fake_fire)
        self.save_summary = "Fresh state at the compaction."
        ka.fire = self.fake_fire
        self.proj = os.path.join(d, "-proj")
        os.makedirs(self.proj)
        self.path = os.path.join(self.proj, f"{SID}.jsonl")
        ka._COMPACT_TRIED.clear()
        ka._COMPACT_LINE = None
        ct._CACHE.clear()
        self.idle_transcript()
        self.addCleanup(testenv.unpause)

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
        ct._CACHE.clear()

    def make_old(self):
        t = datetime.now(UTC).timestamp() - 600
        os.utime(self.path, (t, t))

    def idle_transcript(self, ctx=300_000, idle=timedelta(minutes=55)):
        """The owner asked something; the reply (a request with a context of ctx) came `idle` ago."""
        last = self.now - idle
        self.write([owner(last - timedelta(minutes=1)), assistant(last, ctx=ctx, write=5000)])
        self.make_old()

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
            self.write([keepalive_msg(now, message)], "a")
            if self.saves == "write":
                self.handoff(self.save_summary, next_steps=["go on"])
            if self.saves in ("write", "silent"):
                self.write([assistant(now + timedelta(seconds=1), text="HANDOFF SAVED")], "a")
        else:
            self.write([keepalive_msg(now, message), assistant(now + timedelta(seconds=1))], "a")
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

    def compact(self, args=None, now=None):
        """One compaction pass of the watcher -> (decision, the recorded attempt or None)."""
        d = ka.compaction_pass(SID, now or datetime.now(UTC), args or Armed())
        recs = ka.load_state().get("compactions") or {}
        return d, (recs.get(d.key) if d is not None and d.key else None)

    def paused(self):
        return afclaude_config.automation_pause()


class Armed:
    arm, now, once = True, False, True


class DryRun:
    arm, now, once = False, False, True


# ---------------------------------------------------------------- no end-of-window save (D-233)

class NoWindowEndSave(Env):
    def test_the_watcher_and_the_fillup_send_no_save_prompt(self):
        """D-233: the hand-off is made right before the compaction only. The watcher has no
        end-of-window save pass, and a fill-up message carries no save prompt."""
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


# ---------------------------------------------------------------- the watcher's compaction (D-238)

class CompactWhenIdle(Env):
    def test_idle_above_the_break_even_saves_compacts_and_gives_the_handoff_back(self):
        old = self.handoff("Phase 1 state.", at=self.now - timedelta(days=3))
        self.save_summary = "Phase 2 merged."
        d, rec = self.compact()
        self.assertEqual((d.due, d.trigger), (True, "idle"))
        self.assertEqual([p for p, _ in self.sent], ["send-keys"] * 3)
        save, compact, after = (m for _, m in self.sent)
        self.assertTrue(save.startswith(SAVE))
        self.assertNotIn("\n", save)
        for part in ("you have been idle for 50 min", "COMPACTS", "right after your reply", "afclaude_handoff",
                     f'session "{SID}"', "tasks.py handoff write --session", "HANDOFF SAVED", "D-233", "D-238"):
            self.assertIn(part, save)
        self.assertTrue(compact.startswith("/compact Keep what the task-manager needs"))
        new = self.latest(unconsumed=False)
        self.assertTrue(after.startswith(AFTER + " while it was idle (D-144/D-238)"), after[:80])
        for part in (f"Hand-off #{new['id']}", "Summary: Phase 2 merged.", "Next steps: (1) go on",
                     "don't start any work", 'Reply "READY"', "SessionStart hook"):
            self.assertIn(part, after)
        self.assertNotIn("Phase 1 state.", after)
        self.assertNotIn("A new run starts", after)
        self.assertEqual(rec["status"], "compacted")
        self.assertEqual((rec["trigger"], rec["context"], rec["handoff_id"], rec["handoff_consumed"], rec["replied"]),
                         ("idle", 300_400, new["id"], True, True))
        self.assertEqual(rec["break_even"], ct.break_even(ct.builtin()))
        self.assertEqual(rec["steps"]["save"]["status"], "saved")
        self.assertIsNone(self.latest())                                       # consumed, the older one too
        self.assertEqual(new["consumed_by_start"], d.key)
        self.assertGreater(new["id"], old["id"])
        self.assertIn(f"compacted {SID[:8]} (idle, D-238): context 300k", self.progress())
        self.assertFalse(self.paused()["paused"])
        # the next pass: the context is small now (the compaction marker), nothing more
        self.sent.clear()
        d2, _ = self.compact()
        self.assertFalse(d2.due)
        self.assertIn("below the break-even", d2.reason)
        self.assertEqual(self.sent, [])

    def test_below_the_break_even_no_compaction(self):
        """D-238: a context under the break-even (~177k with the built-in model) is left alone."""
        self.idle_transcript(ctx=150_000)
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("below the break-even 177k", d.reason)
        self.assertEqual((self.sent, rec), ([], None))

    def test_not_before_50_min_idle(self):
        self.idle_transcript(idle=timedelta(minutes=30))
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("idle 30 min: compacts at", d.reason)
        self.assertEqual((self.sent, rec), ([], None))

    def test_the_cache_already_expired_is_recorded_missed(self):
        """Above the break-even, but the cache expired (the owner typed until then, the watcher was
        down): no compaction (it would re-read the whole context first), recorded "missed" once."""
        self.idle_transcript(idle=timedelta(minutes=58))
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertTrue(d.missed)
        self.assertEqual(rec["status"], "missed")
        self.assertEqual(self.sent, [])

    def test_a_running_turn_or_subagent_holds_it(self):
        last = self.now - timedelta(minutes=55)
        self.write([owner(last - timedelta(minutes=1)),
                    assistant(last, stop="tool_use", ctx=300_000,
                              content=[{"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}])])
        self.make_old()
        d, rec = self.compact()
        self.assertEqual((d.due, d.reason), (False, "a turn is running"))
        self.idle_transcript()
        sub = os.path.join(self.path[:-len(".jsonl")], "subagents")
        os.makedirs(sub)
        open(os.path.join(sub, "agent-1.jsonl"), "w").close()
        d, rec = self.compact()
        self.assertEqual((d.due, d.reason), (False, "a subagent is working"))
        self.assertEqual((self.sent, rec), ([], None))

    def test_the_owner_typing_holds_it(self):
        self.typing = "a draft in ka-abcdef01's input box (5 characters)"
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertEqual(d.reason, self.typing)
        self.assertEqual((self.sent, rec), ([], None))
        self.typing = None                                                     # gone: the next pass compacts
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted")

    def test_the_owner_holding_it_or_no_live_tmux_session(self):
        """The owner holding the session elsewhere (preflight), or no live tmux session to type
        /compact into: no compaction, no record, no pause."""
        for problem, why in ((f"{ka.RC_HELD} thread (pid [7]); ...", "the owner holds the session"),
                             (None, "not live in its tmux session (plan resume)")):
            ka.preflight = (lambda sid, p=problem: (False, [p], None)) if problem else \
                (lambda sid: (True, [], "resume"))
            d, rec = self.compact()
            self.assertIn(why, d.reason)
            self.assertIsNone(rec)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.paused()["paused"])
        # any other preflight problem is an error: it raises (run() reports it: fail_stop)
        ka.preflight = lambda sid: (False, ["host process info unavailable (OSError: x)"], None)
        with self.assertRaisesRegex(RuntimeError, "preflight failed: host process info unavailable"):
            self.compact()

    def test_a_start_soon_holds_it(self):
        self.soon = "the session-window start x is due within 40 min"
        d, rec = self.compact()
        self.assertEqual((d.due, d.reason, rec), (False, self.soon, None))

    def test_a_limit_notice_holds_it(self):
        self.write([assistant(self.now - timedelta(minutes=54), ctx=300_000),
                    entry("assistant", self.now - timedelta(minutes=53),
                          message={"role": "assistant", "model": "<synthetic>",
                                   "content": [{"type": "text", "text": "You've hit your session limit · resets 5pm (UTC)"}]})])
        self.make_old()
        d, rec = self.compact()
        self.assertEqual((d.due, d.reason), (False, "the session stopped at a usage limit"))

    def test_the_session_limit_nearly_used_up_holds_it(self):
        self.usage["session"]["percent"] = 98.0
        d, _ = self.compact()
        self.assertIn("the session limit is at 98%", d.reason)
        self.assertEqual(self.sent, [])

    def test_dry_run_records_without_sending(self):
        d, rec = self.compact(args=DryRun())
        self.assertTrue(d.due)
        self.assertEqual((rec["status"], self.sent), ("dry-run", []))
        d, _ = self.compact(args=DryRun())                                     # once per activity
        self.assertEqual(self.sent, [])

    def test_off_or_paused_does_nothing(self):
        testenv.set_setting("compaction_enabled", False)
        self.assertIsNone(self.compact()[0])
        testenv.clear_settings()
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "owner looks"}, actor="cli", via="cli")
        finally:
            conn.close()
        self.assertIsNone(self.compact()[0])
        self.assertEqual(self.sent, [])

    def test_a_start_holding_the_lock_defers_it(self):
        lock = ka.session_lock(SID)
        try:
            d, rec = self.compact()
        finally:
            ka.release_lock(lock)
        self.assertEqual((self.sent, rec), ([], None))
        d, rec = self.compact()                                                # the lock is free: now
        self.assertEqual(rec["status"], "compacted")

    def test_a_stored_cost_model_sets_the_break_even(self):
        """The break-even comes from the stored model (cost_model.set): a cheaper cache write moves it up."""
        m = ct.builtin()
        m["weights"]["cache_write"] = 5.0
        ct.store_model(m)
        try:
            d, rec = self.compact()
            self.assertFalse(d.due)
            self.assertIn(f"below the break-even {ct.break_even(m) / 1000:.0f}k", d.reason)
        finally:
            conn = store.connect()
            try:
                with store.transaction(conn):
                    conn.execute("UPDATE cost_model SET payload=? WHERE doc_key='model'", (json.dumps(ct.BUILTIN),))
            finally:
                conn.close()


class CompactAtRunEnd(Env):
    """D-238 (a): right when a run ended (D-224), not 50 min later; not if the owner chatted after it."""
    def run_state(self, stop=True):
        fire = self.now - timedelta(hours=2)
        st = {"handled": {"window-start-x": {"at": fire.isoformat(), "result": "continued-in-place",
                                             "session": SID}}, "fires": {}}
        if stop:
            st["run_stops"] = {(self.now - timedelta(minutes=5)).isoformat(): SID}
        ka.save_state(st)
        self.write([keepalive_msg(fire), assistant(self.now - timedelta(minutes=6), ctx=400_000, write=4000)])
        self.make_old()

    def test_the_run_end_compacts_at_once(self):
        self.run_state()
        d, rec = self.compact()
        self.assertEqual((d.due, d.trigger), (True, "run-end"))
        self.assertTrue(self.sent[0][1].startswith(SAVE))
        self.assertIn("the run is over", self.sent[0][1])
        self.assertTrue(self.sent[2][1].startswith(AFTER + " right after the run ended"))
        self.assertEqual(rec["status"], "compacted")
        # the compaction's own writes never revive the run (run_active)
        self.assertIsNone(ka.run_active(SID, datetime.now(UTC)))

    def test_a_run_still_going_holds_it(self):
        self.run_state(stop=False)                                             # written 10 min ago: active
        d, rec = self.compact()
        self.assertEqual((d.due, d.reason), (False, "a keep-alive run is using the session"))
        self.assertEqual(self.sent, [])

    def test_the_owner_chatting_after_the_run_gets_the_idle_trigger(self):
        self.run_state()
        self.write([owner(self.now - timedelta(minutes=4)), assistant(self.now - timedelta(minutes=3), ctx=410_000)], "a")
        self.make_old()
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("idle 3 min", d.reason)
        self.assertEqual(self.sent, [])


class CompactionFailures(Env):
    """D-234/D-235: every failure or timeout stops the compaction there, is recorded (never retried
    for the same activity), alerted, and pauses automation."""
    def assert_failed(self, rec, why, sent):
        self.assertEqual(rec["status"], "failed")
        self.assertIn(why, rec["error"])
        self.assertEqual(len(self.sent), len(sent), [m[:40] for _, m in self.sent])
        for (_, m), want in zip(self.sent, sent):
            self.assertTrue(m.startswith(want), (m[:60], want))
        p = self.paused()
        self.assertTrue(p["paused"])
        self.assertIn(f"keepalive: compaction of {SID[:8]} failed", p["reason"])
        self.assertEqual(p["by"], "runner:keepalive")
        self.assertTrue(any(a.startswith(f"AFClaude STOPPED: compaction of {SID[:8]} failed") for a in self.alerts),
                        self.alerts)
        self.assertIn("FAILED", self.progress())
        # never retried for this activity, even once the owner cleared the pause
        testenv.unpause()
        n = len(self.sent)
        self.compact()
        self.assertEqual(len(self.sent), n)
        return rec

    def test_save_timeout(self):
        """No hand-off in time (the prompt never reached the session): no /compact; an older
        hand-off is never given back instead."""
        ka.SAVE_TIMEOUT = timedelta(0)
        self.saves = "ignore"
        old = self.handoff("Old state.", at=self.now - timedelta(days=2))
        d, rec = self.compact()
        self.assert_failed(rec, "no new hand-off within 10 min of the save prompt", [SAVE])
        self.assertEqual(rec["steps"]["save"]["status"], "timeout")
        self.assertEqual(self.latest()["id"], old["id"])                      # not consumed

    def test_a_reply_without_a_handoff(self):
        self.saves = "silent"
        d, rec = self.compact()
        self.assert_failed(rec, "the session answered the save prompt without writing a hand-off", [SAVE])

    def test_save_prompt_send_failure(self):
        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith(SAVE):
                self.sent.append((plan, message))
                return 1, "", "no tmux session"
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        d, rec = self.compact()
        self.assert_failed(rec, "the save prompt could not be sent", [SAVE])
        self.assertIn("no tmux session", rec["error"])

    def test_save_still_running_at_the_timeout(self):
        ka.SAVE_TIMEOUT = timedelta(0)
        self.saves = "hang"

        def fire(sid, cwd, message, plan, new=False, **kw):
            r = self.fake_fire(sid, cwd, message, plan)
            os.utime(self.path, None)                                         # it just wrote: busy
            return r
        ka.fire = fire
        d, rec = self.compact()
        self.assert_failed(rec, "(the session was still working on it)", [SAVE])

    def test_compaction_marker_timeout(self):
        """No marker within COMPACT_TIMEOUT: no hand-off sent; the saved one stays unconsumed."""
        self.compacts = False
        ka.COMPACT_TIMEOUT = timedelta(0)
        d, rec = self.compact()
        self.assert_failed(rec, "no compaction marker within 10 min of /compact", [SAVE, "/compact "])
        self.assertEqual(self.latest()["id"], rec["steps"]["save"]["handoff_id"])

    def test_a_busy_session_after_the_check(self):
        """Mid-turn when the save prompt is due (a race with the pass's own check): no save prompt."""
        old = ka.session_idle
        calls = []

        def idle(path, now=None):
            calls.append(1)
            return len(calls) == 1                                            # the pass's check only
        ka.session_idle = idle
        ka.COMPACT_IDLE_WAIT = timedelta(0)
        try:
            d, rec = self.compact()
        finally:
            ka.session_idle = old
        self.assert_failed(rec, "the session was still mid-turn", [])

    def test_saved_but_not_found(self):
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
        d, rec = self.compact()
        self.assert_failed(rec, "the saved hand-off was not found", [SAVE, "/compact "])

    def test_db_error(self):
        old = store.latest_handoff

        def broken(*a, **k):
            raise store.DBUnavailable("the database is missing", "missing")
        store.latest_handoff = broken
        try:
            d, rec = self.compact()
        finally:
            store.latest_handoff = old
        self.assert_failed(rec, "the hand-offs could not be read", [])

    def test_the_handoff_send_failing_after_the_compaction(self):
        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith(AFTER):
                self.sent.append((plan, message))
                return 1, "", "send-keys failed"
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        d, rec = self.compact()
        self.assert_failed(rec, "the hand-off could not be sent after the compaction", [SAVE, "/compact ", AFTER])
        self.assertIsNotNone(self.latest())                                   # not consumed

    def test_a_raising_step(self):
        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith("/compact"):
                raise OSError("host bridge down")
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        d, rec = self.compact()
        self.assert_failed(rec, "the /compact step failed", [SAVE])
        self.assertIn("host bridge down", rec["error"])

    def test_no_reply_to_the_handoff(self):
        old = ka.verify_reply
        ka.verify_reply = lambda path, since, timeout=None: None
        try:
            d, rec = self.compact()
        finally:
            ka.verify_reply = old
        self.assertEqual((rec["status"], rec["replied"]), ("compacted", False))
        self.assertIn("no reply from abcdef01 to its hand-off after the compaction", self.paused()["reason"])

    def test_a_failing_pass_is_reported_by_the_watcher(self):
        """An error before the compaction (here the cost model can't be read) raises out of the pass;
        run() reports it (fail_stop) and goes on."""
        names = ("LOCK_FILE", "STOP_FILE", "last_mile_pass", "deferred_window_start_pass", "fillup_pass",
                 "watch_run_usage", "stall_status", "last_mile_next_start")
        old_w = {n: getattr(ka, n) for n in names}
        ka.LOCK_FILE, ka.STOP_FILE = os.path.join(self.tmp.name, "ka.lock"), os.path.join(self.tmp.name, "STOP")
        for n in names[2:]:
            setattr(ka, n, lambda *a, **k: None)
        ka.stall_status = lambda sid, now: ("NO_STALL", "x", None)
        old_load = ct.load

        def boom(conn=None):
            raise ValueError("the cost model's weights.cache_read must be a positive number")
        ct.load = boom
        try:
            ka.run(types.SimpleNamespace(session=SID, arm=True, once=True, now=False))
        finally:
            ct.load = old_load
            for n, v in old_w.items():
                setattr(ka, n, v)
        self.assertIn("compaction pass failed: ValueError", self.paused()["reason"])
        self.assertEqual(self.sent, [])


# ---------------------------------------------------------------- starts never compact (D-238)

class StartsNeverCompact(Env):
    KEY = "window-start-2026-10-07"

    def start(self, args=Armed()):
        at = NIGHT + timedelta(days=1)
        r = ka.handle_fire(SID, {"uuid": self.KEY, "timestamp": at, "budget": "budget for this run: +14%",
                                 "headroom": 14.0, "start_at": at}, "window start, CONTINUE: test", ka.load_state(), args)
        return r, ka.load_state()["handled"].get(self.KEY)

    def test_a_start_only_continues(self):
        """A big, old conversation at a start: only the continue goes out (no save prompt, no
        /compact, no hand-off); the run starts on the context the watcher left."""
        self.handoff("Unconsumed.")
        r, rec = self.start()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].startswith("[keep-alive] A new run starts"))
        self.assertNotIn("compaction", rec)
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertIsNotNone(self.latest())

    def test_a_start_waits_for_a_running_compaction(self):
        """The cron's window-start (its own process) finds the session lock held: it waits up to
        COMPACT_LOCK_WAIT, then the start is aborted (an error, automation paused)."""
        ka.COMPACT_LOCK_WAIT = timedelta(0)
        lock = ka.session_lock(SID)
        try:
            r, rec = self.start()
        finally:
            ka.release_lock(lock)
        self.assertEqual((r, rec["result"]), ("failed", "start-failed"))
        self.assertIn("the session is still busy with a compaction", rec["error"])
        self.assertEqual(self.sent, [])
        self.assertTrue(self.paused()["paused"])
        testenv.unpause()
        ka.save_state({"handled": {}, "fires": {}})
        r, rec = self.start()                                                  # free again: it fires
        self.assertEqual(rec["result"], "continued-in-place")

    def test_dry_run_start(self):
        r, rec = self.start(args=DryRun())
        self.assertEqual((self.sent, rec["result"]), ([], "dry-run"))
        self.assertNotIn("compaction", rec)


# ---------------------------------------------------------------- the owner-typing check and the start guard

CAPTURE_IDLE = ("\x1b[38;5;244m" + "─" * 80 + "\n"
                "\x1b[39m❯\xa0\x1b[2mb, run every night\x1b[0m\x1b[39m\x1b[49m\n"
                "\x1b[38;5;244m" + "─" * 80 + "\n"
                "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m · ← 2 agents\n")
CAPTURE_DRAFT = ("─" * 80 + "\n"
                 + "❯ please also che\x1b[7m \x1b[27m\n"
                 + "─" * 80 + "\n  ⏵⏵ auto mode on\n")
CAPTURE_BOX = ("╭" + "─" * 78 + "╮\n"
               "│ > \x1b[7m \x1b[0m\x1b[2mTry \"refactor\"\x1b[0m" + " " * 50 + "│\n"
               "╰" + "─" * 78 + "╯\n")
CAPTURE_DIALOG = ("─" * 80 + "\n Select model\n ❯ 1. Opus\n   2. Sonnet\n\n"
                  " \x1b[2mEnter to confirm · Esc to exit\x1b[0m\n")
# A real idle task-manager pane (trimmed, Claude Code 2.1.283): the input box's top border carries
# the session's title, an "Update installed" line above it, the agents panel below the footer.
CAPTURE_TITLED = ("\x1b[38;5;246m✻\x1b[39m \x1b[38;5;246mWaiting for \x1b[1m1\x1b[0m\x1b[38;5;246m\x1b[49m background "
                  "agent to finish\x1b[39m\n"
                  "                                        \x1b[38;5;114m✔ Update installed · Restart to update\x1b[39m\n"
                  "\x1b[38;5;244m" + "─" * 48 + " task-manager keep-alive build ─\n"
                  "\x1b[39m❯\xa0\x1b[2mok\x1b[0m\x1b[39m\x1b[49m\n"
                  "\x1b[38;5;244m" + "─" * 80 + "\n"
                  "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m · \x1b[38;5;44m1 shell, 1 monitor\x1b[38;5;246m · "
                  "← 1 agent · ↓ to manage\x1b[39m\n\n"
                  "\x1b[1m  ● main\n"
                  "\x1b[0m\x1b[38;5;246m\x1b[49m  ◯ afclaude-worker\x1b[39m  \x1b[38;5;246mFix input-box detection\x1b[39m "
                  "\x1b[38;5;246m3m 17s · ↓ 70.6k tokens\x1b[39m\n\n")
CAPTURE_UNKNOWN = "Welcome back\n$ \n"


class OwnerTyping(unittest.TestCase):
    def test_pane_input(self):
        self.assertEqual(ka.pane_input(CAPTURE_IDLE), ("box", ""))            # a dimmed suggestion
        self.assertEqual(ka.pane_input(CAPTURE_DRAFT), ("box", "pleasealsoche"))
        self.assertEqual(ka.pane_input(CAPTURE_BOX), ("box", ""))             # cursor + placeholder
        self.assertEqual(ka.pane_input(CAPTURE_DIALOG), ("dialog", ""))
        multi = "─" * 20 + "\n❯ first line\n  second\n" + "─" * 20 + "\n"
        self.assertEqual(ka.pane_input(multi), ("box", "firstlinesecond"))

    def test_a_titled_border(self):
        """14.10.: the input box's top border carries the session's title; it was read as no input
        box (a dialog), which held the idle compaction until the cache expired."""
        self.assertEqual(ka.pane_input(CAPTURE_TITLED), ("box", ""))
        draft = CAPTURE_TITLED.replace("\x1b[2mok\x1b[0m", "check the logs\x1b[7m \x1b[27m")
        self.assertEqual(ka.pane_input(draft), ("box", "checkthelogs"))
        left = "── my session " + "─" * 60 + "\n❯ \n" + "─" * 80 + "\n"     # a title on the left
        self.assertEqual(ka.pane_input(left), ("box", ""))

    def test_dialogs_and_unreadable_panes(self):
        permission = ("─" * 80 + "\n Bash command\n\n   ls\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n")
        self.assertEqual(ka.pane_input(permission), ("dialog", ""))
        self.assertEqual(ka.pane_input("─" * 80 + "\n Resume session\n\n ↑/↓ to navigate · esc to cancel\n"),
                         ("dialog", ""))
        self.assertEqual(ka.pane_input(CAPTURE_UNKNOWN), (None, ""))
        self.assertEqual(ka.pane_input(""), (None, ""))
        # the footer's own hints aren't a dialog; a hint far up in the chat isn't either
        self.assertEqual(ka.pane_input("─" * 80 + "\n  ⏵⏵ auto mode on · esc to interrupt\n"), (None, ""))
        chat = "press Esc to cancel it\n" + "─" * 80 + "\nsome text\n" + "─" * 80 + "\nmore\n" + "─" * 80 + "\nend\n"
        self.assertEqual(ka.pane_input(chat), (None, ""))
        self.assertEqual(ka.pane_input("── a note ──\n❯ hi\n" + "─" * 80 + "\n"), (None, ""))   # too short for a rule

    def run_typing(self, clients, pane, rc=0):
        calls = []

        def run(argv, **k):
            calls.append(argv)
            out = clients if argv[1] == "list-clients" else pane
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr="boom")
        old = ka.host.run_on_host
        ka.host.run_on_host = run
        try:
            return ka.owner_typing(SID, datetime(2026, 10, 14, 12, 0, tzinfo=UTC)), calls
        finally:
            ka.host.run_on_host = old

    def test_owner_typing(self):
        testenv.tmux_row(SID)                    # its stored tmux name (3b S2), the one checked
        now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC).timestamp()
        why, calls = self.run_typing("", CAPTURE_IDLE)
        self.assertIsNone(why)
        self.assertEqual(calls, [["tmux", "list-clients", "-t", "=ka-abcdef01", "-F", "#{client_activity}"],
                                 ["tmux", "capture-pane", "-e", "-p", "-t", "=ka-abcdef01:"]])
        why, calls = self.run_typing(f"{int(now - 120)}\n", CAPTURE_IDLE)
        self.assertIn("the owner typed in a terminal attached to ka-abcdef01 2 min ago", why)
        self.assertEqual(len(calls), 1)
        why, _ = self.run_typing(f"{int(now - 3600)}\n", CAPTURE_DRAFT)
        self.assertEqual(why, "a draft in ka-abcdef01's input box (13 characters)")
        why, _ = self.run_typing("", CAPTURE_DIALOG)
        self.assertEqual(why, "a dialog or menu is open on ka-abcdef01's pane")
        why, _ = self.run_typing("", CAPTURE_TITLED)
        self.assertIsNone(why)
        with self.assertRaisesRegex(RuntimeError, r"can't read ka-abcdef01's pane: .*\(last lines: Welcome back \| \$\)"):
            self.run_typing("", CAPTURE_UNKNOWN)
        with self.assertRaisesRegex(RuntimeError, "tmux list-clients ka-abcdef01 failed rc=1"):
            self.run_typing("", "", rc=1)

    def test_the_bridge_shapes(self):
        import host
        self.assertEqual(host.bridge_argv(["tmux", "list-clients", "-t", "=ka-abcdef01", "-F", "#{client_activity}"]),
                         ["tmux-clients", "ka-abcdef01"])
        self.assertEqual(host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", "=ka-abcdef01:"]),
                         ["tmux-capture", "ka-abcdef01"])


class StartSoon(Env):
    def setUp(self):
        super().setUp()
        ka.start_soon = self.old["start_soon"]
        self.old_lm = ka.last_mile_next_start
        ka.last_mile_next_start = lambda now, usage=None: None

    def tearDown(self):
        ka.last_mile_next_start = self.old_lm
        super().tearDown()

    def test_start_soon(self):
        before = NIGHT - timedelta(minutes=30)                                 # 22:30 Berlin
        self.assertIn("the session-window start", ka.start_soon(SID, before))
        self.assertIsNone(ka.start_soon(SID, NIGHT - timedelta(hours=3)))
        ka.defer_window_start("window-start-x", SID, NIGHT - timedelta(hours=3) + timedelta(minutes=10), "user active")
        self.assertIn("a postponed session-window start", ka.start_soon(SID, NIGHT - timedelta(hours=3)))
        ka.save_deferred({})
        ka.save_fillup({"plan": {"status": "wait", "start": (NIGHT - timedelta(hours=2, minutes=40)).isoformat()}})
        self.assertIn("the fill-up run is planned", ka.start_soon(SID, NIGHT - timedelta(hours=3)))
        ka.save_fillup({"plan": {"status": "hold"}})
        ka.last_mile_next_start = lambda now, usage=None: now + timedelta(minutes=20)
        self.assertIn("the last-stretch slot starts", ka.start_soon(SID, NIGHT - timedelta(hours=3)))


class FailStopAtStart(Env):
    """Every failure or timeout of a start (not only the compaction) is an error: alerted, recorded,
    automation paused; before the continue went out the start is aborted (start-failed)."""
    KEY = "window-start-2026-10-07"

    def start(self):
        at = NIGHT + timedelta(days=1)
        r = ka.handle_fire(SID, {"uuid": self.KEY, "timestamp": at, "budget": "b", "headroom": 1.0,
                                 "start_at": at}, "window start, CONTINUE: test", ka.load_state(), Armed())
        return r, ka.load_state()["handled"].get(self.KEY)

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
        def boom(path):
            raise OSError("I/O error")
        old = ka.session_cwd
        ka.session_cwd = boom
        try:
            r, rec = self.start()
        finally:
            ka.session_cwd = old
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
    def run_script(self, fail_on, tmux_args=("--tmux", "ka-abcdef01"), extra=()):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            tmux = os.path.join(d, "tmux")
            with open(tmux, "w") as fh:
                fh.write("#!/bin/sh\n"
                         f'echo "$*" >> {d}/tmux.calls\n'
                         f'case "$1" in has-session) exit {0 if fail_on.startswith("send") else 1};; esac\n'
                         f'case "$1 $*" in *{fail_on}*) exit 1;; esac\nexit 0\n')
            os.chmod(tmux, 0o755)
            env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], HOME=d, KA_TRUST_ROOT=d)
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ka_resume.sh")
            r = subprocess.run(["bash", script, "--session", SID, *tmux_args, "--message", "go", "--cwd", d, *extra],
                               env=env, capture_output=True, text=True, timeout=60)
            calls = os.path.join(d, "tmux.calls")
            self.tmux_calls = open(calls).read().splitlines() if os.path.exists(calls) else []
            return r

    def test_send_keys_failure_exits_1(self):
        r = self.run_script("send-keys")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("send-keys to ka-abcdef01 failed", r.stderr)
        self.assertNotIn("sent-keys", r.stdout)

    def test_the_tmux_name_is_the_callers(self):
        """3b S2: --tmux names the session (readable afc-... or today's ka-<id8>); missing or
        anything else (a path, uppercase, a space, over 40 characters) exits 2, before any tmux call."""
        r = self.run_script("send-keys-ok", tmux_args=("--tmux", "afc-afclaude-task-manager"))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "sent-keys afc-afclaude-task-manager"))
        self.assertIn("has-session -t =afc-afclaude-task-manager", self.tmux_calls)
        for bad in ((), ("--tmux", "../../x"), ("--tmux", "afc-Upper"), ("--tmux", "afc-a b"),
                    ("--tmux", "afc-" + "x" * 37), ("--tmux", "ka-abcdef0"), ("--tmux", "")):
            r = self.run_script("send-keys-ok", tmux_args=bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("need --tmux", r.stderr)
            self.assertEqual(self.tmux_calls, [], bad)

    def test_new_session_never_types_into_a_running_one(self):
        r = self.run_script("send-keys-ok", tmux_args=("--tmux", "afc-review"), extra=("--new",))
        self.assertEqual(r.returncode, 1)
        self.assertIn("tmux session afc-review already exists", r.stderr)
        self.assertFalse(any(c.startswith("send-keys") for c in self.tmux_calls), self.tmux_calls)


class FireFailures(unittest.TestCase):
    """keepalive.fire: a take-over whose holder does not exit, a bg worker that does not stop, and
    `claude agents` failing raise instead of resuming into a fork (D-234/D-235)."""
    def setUp(self):
        testenv.tmux_row(SID)                                            # its stored tmux name (3b S2)
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
