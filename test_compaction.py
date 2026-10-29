#!/usr/bin/env python3
"""Offline tests for the compaction of the task-manager (D-144, D-233, D-234, D-238; design §5.5):
the watcher's compaction_pass compacts when a run ended (D-224) or after 50 min idle, while the
prompt cache is warm, only at or above the threshold (compaction_threshold_tokens, D-307), and during
a run once the context passed it (D-307: the run goes on after it, and background subagents may
still be working); otherwise never while a run uses the session, a turn or subagent runs or the
owner is typing; the save prompt right before the /compact (D-233), asked for again when a turn
(a task notification) made it out of date, the fresh hand-off given back and consumed; every failure or timeout on the way
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
RESEND = "[keep-alive] No new hand-off found"
REFRESH = "[keep-alive] Your hand-off #"
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
             "VERIFY_TIMEOUT", "COMPACT_LOCK_WAIT", "COMPACT_TURN_WAIT", "session_idle")

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
        self.turn = self.turn_polls = 0
        self.turn_handoff = None
        self.alerts, self.sent = [], []
        self.now = datetime.now(UTC)
        self.usage = {"session": {"percent": 50.0, "resets_at": self.now + timedelta(hours=2)},
                      "weekly": {"percent": 40.0, "resets_at": self.now + timedelta(days=3)},
                      "fetched_at": self.now - timedelta(minutes=3)}
        ka.read_usage_cache = lambda: self.usage
        self.compacts = True                                # the fake session compacts on /compact
        self.saves = "write"                                # ... and answers the save prompt (see fake_fire)
        self.resaves = "silent"                             # ... and its re-send (the same modes)
        self.refreshes = "write"                            # ... and a hand-off asked for again (D-307)
        self.after_save = []                                # [(input, turn polls)]: what arrives after a hand-off
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
        elif message.startswith((SAVE, RESEND, REFRESH)):
            mode = {SAVE: self.saves, RESEND: self.resaves, REFRESH: self.refreshes}[
                next(p for p in (SAVE, RESEND, REFRESH) if message.startswith(p))]
            if mode == "ignore":
                return 0, "sent-keys ka-abcdef01", ""
            self.write([keepalive_msg(now, message)], "a")
            if mode == "write":
                self.handoff(self.save_summary, next_steps=["go on"])
            if mode in ("write", "silent"):
                self.write([assistant(now + timedelta(seconds=1), text="HANDOFF SAVED")], "a")
            if mode == "write" and self.after_save:
                self.arrive(*self.after_save.pop(0))
        else:
            self.write([keepalive_msg(now, message), assistant(now + timedelta(seconds=1))], "a")
        self.make_old()
        return 0, "sent-keys ka-abcdef01", ""

    def arrive(self, kind, polls):
        """Something reaches the session right after its hand-off (D-307): a background subagent's
        task notification starts a turn that runs for `polls` idle checks (0: a message queued and
        delivered into the turn that is ending, no turn of its own)."""
        t = datetime.now(UTC)                                 # after the hand-off's created_at
        if kind == "notification":
            e = entry("user", t, origin={"kind": "task-notification"},
                      message={"role": "user", "content": "<task-notification><task-id>a1</task-id></task-notification>"})
        elif kind == "handback":
            e = entry("user", t, isMeta=True, origin={"kind": "peer", "from": "a1", "handback": True},
                      message={"role": "user", "content": "Another Claude session sent a message: report"})
        else:
            e = entry("queue-operation", t, operation="enqueue", content="<task-notification>")
        self.write([e], "a")
        self.turn = polls
        real = self.old["session_idle"]

        def idle(path, now=None):
            if self.turn > 0:
                self.turn -= 1
                self.turn_polls += 1
                if self.turn == 0:                            # the turn ends
                    if self.turn_handoff:                     # ... having brought the hand-off up to date
                        self.handoff(self.turn_handoff)
                    self.write([assistant(datetime.now(UTC), text="merged it")], "a")
                    self.make_old()
                return False
            return real(path, now)
        ka.session_idle = idle

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
    def test_idle_above_the_threshold_saves_compacts_and_gives_the_handoff_back(self):
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
        self.assertEqual((rec["threshold"], rec["break_even"]), (150_000, ct.break_even(ct.builtin())))
        self.assertEqual(rec["steps"]["save"]["status"], "saved")
        self.assertIsNone(self.latest())                                       # consumed, the older one too
        self.assertEqual(new["consumed_by_start"], d.key)
        self.assertGreater(new["id"], old["id"])
        self.assertIn(f"compacted {SID[:8]} (idle, D-238/D-307): context 300k >= threshold 150k", self.progress())
        self.assertFalse(self.paused()["paused"])
        # the next pass: the context is small now (the compaction marker), nothing more
        self.sent.clear()
        d2, _ = self.compact()
        self.assertFalse(d2.due)
        self.assertIn("below the threshold", d2.reason)
        self.assertEqual(self.sent, [])

    def test_below_the_threshold_no_compaction(self):
        """D-307: a context under the threshold (150k by default) is left alone."""
        self.idle_transcript(ctx=149_000)
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("below the threshold 150k", d.reason)
        self.assertEqual((self.sent, rec), ([], None))

    def test_not_before_50_min_idle(self):
        self.idle_transcript(idle=timedelta(minutes=30))
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("idle 30 min: compacts at", d.reason)
        self.assertEqual((self.sent, rec), ([], None))

    def test_the_cache_already_expired_is_recorded_missed(self):
        """Above the threshold, but the cache expired (the owner typed until then, the watcher was
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

    def test_the_threshold_setting_decides_not_the_break_even(self):
        """D-307: the setting compaction_threshold_tokens is the trigger; a stored cost model whose
        break-even (cheaper cache writes: ~550k) lies above the context no longer holds it."""
        testenv.set_setting("compaction_threshold_tokens", 400_000)
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("below the threshold 400k", d.reason)
        testenv.clear_settings()
        m = ct.builtin()
        m["weights"]["cache_write"] = 5.0
        ct.store_model(m)
        try:
            self.assertGreater(ct.break_even(m), 300_400)
            d, rec = self.compact()
            self.assertEqual((d.due, rec["status"], rec["break_even"]), (True, "compacted", ct.break_even(m)))
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
        """A run-end or idle compaction waits for the run's end; a mid-run one (D-307) waits for an idle
        point (CompactMidRun)."""
        self.run_state(stop=False)                                             # written 10 min ago: active
        self.write([assistant(self.now - timedelta(minutes=5), stop="tool_use", ctx=401_000,
                              content=[{"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}])], "a")
        self.make_old()
        d, rec = self.compact()
        self.assertEqual((d.due, d.reason), (False, "a keep-alive run is using the session (no mid-run compaction: "
                                                    "a turn is running)"))
        self.assertEqual(self.sent, [])

    def test_the_owner_chatting_after_the_run_gets_the_idle_trigger(self):
        self.run_state()
        self.write([owner(self.now - timedelta(minutes=4)), assistant(self.now - timedelta(minutes=3), ctx=410_000)], "a")
        self.make_old()
        d, rec = self.compact()
        self.assertFalse(d.due)
        self.assertIn("idle 3 min", d.reason)
        self.assertEqual(self.sent, [])


class CompactMidRun(Env):
    """D-307: during a run, once the context passed the threshold, the watcher compacts at the next
    idle point; the run goes on after it (compact_after_run.md, not READY), the compaction is no run
    end, and the loop and owner guards (compact_timing.mid_run_hold) hold the next one."""
    BUDGET = "budget for this run: +4.0% weekly"

    def setUp(self):
        super().setUp()
        ka.budget_eval = lambda usage, now, display=False: {"mode": "pacing", "go": True, "reason": "GO",
                                                            "headroom": 4.0, "text": self.BUDGET}
        self.fire = self.now - timedelta(hours=1)
        self.run_end = self.now + timedelta(hours=3)

    def run_state(self, ctx=200_000, prompt="continue", extra=()):
        """A run fired an hour ago and still going (the transcript written 10 min ago); its last
        turn ended 6 min ago with a context of ctx."""
        ka.save_state({"handled": {"window-start-x": {
            "at": self.fire.isoformat(), "result": "continued-in-place", "session": SID, "prompt": prompt,
            "reason": "window start, GO", "run_end": self.run_end.isoformat()}}, "fires": {}})
        self.write([keepalive_msg(self.fire)] + list(extra)
                   + [assistant(self.now - timedelta(minutes=6), ctx=ctx, write=4000)])
        self.make_old()

    def test_the_run_goes_on_after_a_mid_run_compaction(self):
        self.run_state()
        d, rec = self.compact()
        self.assertEqual((d.due, d.trigger), (True, "mid-run"))
        save, compact, after = (m for _, m in self.sent)
        self.assertTrue(save.startswith(SAVE))
        self.assertIn("your context passed the compaction threshold during the run (D-307); the run goes on", save)
        self.assertTrue(compact.startswith("/compact "))
        self.assertTrue(after.startswith(AFTER + " during the run, when its context passed the threshold"), after[:90])
        new = self.latest(unconsumed=False)
        for part in (f"Hand-off #{new['id']}", "The run goes on (D-307)", "passed 150k tokens",
                     f"The run started at {ka._hm(self.fire)} (window start, GO)", self.BUDGET,
                     f"which ends at {ka._hm(self.run_end)}", "hand-off's next steps", "--run-stopped"):
            self.assertIn(part, after)
        self.assertNotIn("READY", after)
        self.assertNotIn("fill-up", after)
        self.assertEqual((rec["status"], rec["trigger"], rec["threshold"]), ("compacted", "mid-run", 150_000))
        self.assertIn(f"compacted {SID[:8]} (mid-run, D-238/D-307): context 200k", self.progress())
        self.assertIn("the run goes on", self.progress())
        self.assertFalse(self.paused()["paused"])
        # no run end: the run is still going (its fire), so a run-end compaction doesn't follow
        self.assertEqual(ka.run_active(SID, datetime.now(UTC)), self.fire)
        # the loop guard: the context grows past the threshold again within the hour: held
        self.sent.clear()
        self.write([keepalive_msg(datetime.now(UTC)),
                    assistant(datetime.now(UTC) + timedelta(seconds=2), ctx=180_000)], "a")
        self.make_old()
        d2, rec2 = self.compact()
        self.assertFalse(d2.due)
        self.assertIn("no mid-run compaction: compacted at", d2.reason)
        self.assertEqual((self.sent, rec2), ([], None))

    def test_a_fill_up_run_keeps_its_rule(self):
        self.run_state(prompt="fillup")
        self.compact()
        self.assertIn("It is a fill-up run (D-212): keep working until the session reset", self.sent[-1][1])

    def test_the_owner_chatting_holds_it(self):
        """D-262/D-307: the owner's message within 10 min (typed, or from the dashboard) holds it."""
        for text in ("owner: how far is it?", "[AFClaude inbox #3] The owner wrote this on the dashboard"):
            ct._CACHE.clear()
            self.run_state(extra=[owner(self.now - timedelta(minutes=7), text)])
            d, rec = self.compact()
            self.assertFalse(d.due)
            self.assertIn("no mid-run compaction: the owner wrote 7 min ago", d.reason)
            self.assertEqual((self.sent, rec), ([], None))

    def test_below_the_threshold_the_run_holds_it(self):
        self.run_state(ctx=140_000)
        d, rec = self.compact()
        self.assertIn("below the threshold 150k", d.reason)
        self.assertEqual(self.sent, [])

    def test_a_failed_budget_evaluation_types_nothing(self):
        """D-235: the run's part of the message is built first; its failure is an error and nothing
        has been typed into the session."""
        ka.budget_eval = lambda usage, now, display=False: ka.failed_eval("ValueError: x")
        self.run_state()
        d, rec = self.compact()
        self.assertTrue(d.due)
        self.assertEqual(self.sent, [])
        self.assertEqual(rec["status"], "failed")
        self.assertIn("the run's budget could not be evaluated", rec["error"])
        self.assertTrue(self.paused()["paused"])

    def test_the_fire_record_keeps_the_run_end_and_its_prompt(self):
        """_fire_start stores what a mid-run compaction's message needs: the run's window end and its kind."""
        old = ka.verify_reply
        ka.verify_reply = lambda path, since: {"message": {"model": "m"}}
        try:
            st = {"handled": {}, "fires": {}}
            ka._fire_start(SID, {"uuid": "k", "timestamp": self.now, "prompt": "last_mile", "run_end": self.run_end,
                                 "budget": "b"}, "r", st, Armed(), "k", "night", "send-keys")
        finally:
            ka.verify_reply = old
        self.assertEqual((st["handled"]["k"]["prompt"], st["handled"]["k"]["run_end"]),
                         ("last_mile", self.run_end.isoformat()))


class SaveResend(Env):
    """Inbox #37 (22.10.): the session wrote a hand-off on its own, then ended the run; the save
    prompt five minutes later got "HANDOFF SAVED" pointing at that earlier one, and the compaction
    failed. The save prompt now asks for a NEW hand-off, and a reply without one gets it re-sent once."""
    def test_a_reply_pointing_at_an_earlier_handoff_gets_one_resend(self):
        early = self.handoff("Written before the save prompt.", at=self.now - timedelta(minutes=5))
        self.saves, self.resaves = "silent", "write"
        self.save_summary = "Written after the re-send."
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted", rec.get("error"))
        self.assertEqual([m[:len(SAVE)] for _, m in self.sent][:2], [SAVE, RESEND[:len(SAVE)]])
        save, resend, compact, after = (m for _, m in self.sent)
        self.assertTrue(resend.startswith(RESEND))
        self.assertNotIn("\n", resend)
        for part in (f"#{early['id']}", "written before the save prompt", f'session "{SID}"',
                     "tasks.py handoff write --session", "HANDOFF SAVED", "only reminder"):
            self.assertIn(part, resend)
        self.assertTrue(compact.startswith("/compact "))
        new = self.latest(unconsumed=False)
        self.assertGreater(new["id"], early["id"])
        self.assertIn("Summary: Written after the re-send.", after)
        self.assertNotIn("Written before the save prompt.", after)
        steps = rec["steps"]["save"]
        self.assertEqual((steps["status"], steps["handoff_id"]), ("saved", new["id"]))
        self.assertGreaterEqual(steps["resent_at"], steps["sent_at"])
        self.assertFalse(self.paused()["paused"])
        self.assertEqual(self.alerts, [])

    def test_a_fresh_handoff_needs_no_resend(self):
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted")
        self.assertFalse(any(m.startswith(RESEND) for _, m in self.sent))
        self.assertNotIn("resent_at", rec["steps"]["save"])

    def test_the_save_prompt_asks_for_a_new_handoff(self):
        text = ka.save_text(SID)
        for part in ("write a NEW short, structured hand-off", "even if you wrote one earlier",
                     "only a hand-off written after this message counts"):
            self.assertIn(part, text)
        self.assertIn("(the newest one is none,", ka.save_retry_text(SID, 0))

    def test_the_save_prompts_ask_for_the_d305_fields(self):
        """D-305: both the save prompt and its re-send name every hand-off field (the new ones are
        what only the compaction summary used to carry), and each is one the store accepts."""
        for text in (ka.save_text(SID), ka.save_retry_text(SID, 3)):
            for k in store.HANDOFF_KEYS:
                self.assertIn(k, text)
            self.assertNotIn("\n", text)
        self.assertIn("VERBATIM", ka.save_text(SID))


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
        """The save prompt is re-sent once; a second reply without one fails (no third prompt)."""
        self.saves = "silent"
        d, rec = self.compact()
        self.assert_failed(rec, "the session answered the save prompt and its re-send without writing a hand-off",
                           [SAVE, RESEND])
        self.assertIn("resent_at", rec["steps"]["save"])

    def test_the_resend_timing_out(self):
        self.saves, self.resaves = "silent", "ignore"
        ka.SAVE_TIMEOUT = timedelta(0)
        d, rec = self.compact()
        self.assert_failed(rec, "no new hand-off within 10 min of the save prompt (after the save prompt was "
                                "re-sent: the first reply had no hand-off)", [SAVE, RESEND])

    def test_the_resend_failing_to_send(self):
        self.saves = "silent"

        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith(RESEND):
                self.sent.append((plan, message))
                return 1, "", "no tmux session"
            return self.fake_fire(sid, cwd, message, plan)
        ka.fire = fire
        d, rec = self.compact()
        self.assert_failed(rec, "the save prompt could not be sent (after the save prompt was re-sent", [SAVE, RESEND])

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


# ---------------------------------------------------------------- mid-run with subagents running (D-307)

class MidRunWithSubagents(Env):
    """D-307 follow-up: the task-manager almost always waits on background subagents, so a mid-run
    compaction goes on while they work, once the session itself is idle at its prompt. A turn that
    starts after the hand-off (their task notification) is waited for before /compact (bounded,
    COMPACT_TURN_WAIT), and the hand-off it made out of date is asked for again
    (session_end_save_refresh.md); a timeout or a hand-off still out of date is an error, as before."""
    def setUp(self):
        super().setUp()
        ka.budget_eval = lambda usage, now, display=False: {"mode": "pacing", "go": True, "reason": "GO",
                                                            "headroom": 4.0, "text": "budget: +4% weekly"}
        fire = self.now - timedelta(hours=1)
        ka.save_state({"handled": {"window-start-x": {
            "at": fire.isoformat(), "result": "continued-in-place", "session": SID, "prompt": "continue",
            "reason": "window start, GO", "run_end": (self.now + timedelta(hours=3)).isoformat()}}, "fires": {}})
        self.write([keepalive_msg(fire), assistant(self.now - timedelta(minutes=6), ctx=200_000, write=4000)])
        self.make_old()
        sub = os.path.join(self.path[:-len(".jsonl")], "subagents")   # a background subagent, written just now
        os.makedirs(sub)
        open(os.path.join(sub, "agent-a1.jsonl"), "w").close()

    def test_a_running_subagent_does_not_hold_a_mid_run_compaction(self):
        d, rec = self.compact()
        self.assertEqual((d.due, d.trigger), (True, "mid-run"), d.reason)
        self.assertEqual([m[:9] for _, m in self.sent], [SAVE[:9], "/compact ", AFTER[:9]])
        self.assertEqual(rec["status"], "compacted", rec.get("error"))
        self.assertEqual(rec["steps"]["save"]["refreshes"], [])
        # the run-end and idle triggers still wait for it (the subagent is part of the session's work)
        self.assertEqual(ka.compaction_busy_reason(self.path, datetime.now(UTC)), "a subagent is working")
        self.assertIsNone(ka.compaction_busy_reason(self.path, datetime.now(UTC), subagents=False))

    def test_the_save_prompt_asks_for_the_subagents_in_flight(self):
        text = ka.save_text(SID, "mid-run")
        for part in ("in_flight", "subagent still running in the background with its agent id", "its branch",
                     "keeps running through the compaction"):
            self.assertIn(part, text)

    def test_a_notification_turn_after_the_handoff_is_waited_for_then_compacted(self):
        """The subagent reports back right after the hand-off: its turn runs, the keep-alive waits
        for it, asks for the hand-off again (the first one lists the subagent as still in flight),
        then compacts and gives back the current one."""
        self.after_save = [("notification", 4)]
        self.save_summary = "Hand-off at the save prompt."
        first = []
        orig = self.fake_fire

        def fire(sid, cwd, message, plan, new=False, **kw):
            if message.startswith(REFRESH):
                first.append(self.latest(unconsumed=False)["id"])
                self.save_summary = "Hand-off after the notification."
            return orig(sid, cwd, message, plan, new, **kw)
        ka.fire = fire
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted", rec.get("error"))
        save, refresh, compact, after = (m for _, m in self.sent)
        self.assertTrue(refresh.startswith(f"{REFRESH}{first[0]} is out of date"), refresh[:60])
        for part in ("a task notification (from ", f'session "{SID}"', "tasks.py handoff write --session",
                     "HANDOFF SAVED", "no new work"):
            self.assertIn(part, refresh)
        self.assertNotIn("\n", refresh)
        self.assertTrue(compact.startswith("/compact "))
        self.assertEqual(self.turn_polls, 4)                                     # waited for the turn's end
        new = self.latest(unconsumed=False)
        self.assertGreater(new["id"], first[0])
        self.assertIn("Summary: Hand-off after the notification.", after)
        self.assertNotIn("Hand-off at the save prompt.", after)
        steps = rec["steps"]["save"]
        self.assertEqual((steps["status"], steps["handoff_id"], len(steps["refreshes"])), ("saved", new["id"], 1))
        self.assertEqual((steps["refreshes"][0]["stale_id"], steps["refreshes"][0]["handoff_id"]), (first[0], new["id"]))
        self.assertIsNone(self.latest())                                         # both consumed
        self.assertFalse(self.paused()["paused"])
        self.assertEqual(self.alerts, [])

    def test_a_handoff_the_notification_turn_wrote_itself_needs_no_refresh(self):
        self.after_save = [("notification", 2)]
        self.turn_handoff = "Brought up to date in the notification turn."
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted", rec.get("error"))
        self.assertEqual([m[:9] for _, m in self.sent], [SAVE[:9], "/compact ", AFTER[:9]])
        self.assertIn("Summary: Brought up to date in the notification turn.", self.sent[-1][1])
        self.assertEqual(rec["steps"]["save"]["handoff_id"], self.latest(unconsumed=False)["id"])

    def test_a_subagent_hand_back_after_the_handoff_gets_it_asked_for_again(self):
        """A finished worker reports as a peer message ("Another Claude session sent a message",
        isMeta): that is input that makes the hand-off out of date too."""
        self.after_save = [("handback", 2)]
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted", rec.get("error"))
        self.assertEqual([m[:len(REFRESH)] for _, m in self.sent][1], REFRESH)
        self.assertIn("a subagent's message (from ", self.sent[1][1])
        self.assertEqual(len(rec["steps"]["save"]["refreshes"]), 1)

    def test_a_message_delivered_into_the_save_turn_after_the_handoff_counts_too(self):
        self.after_save = [("queued", 0)]
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted", rec.get("error"))
        self.assertEqual([m[:len(REFRESH)] for _, m in self.sent][1], REFRESH)
        self.assertIn("a queued message", self.sent[1][1])

    def test_the_turn_wait_timing_out_fails_as_before(self):
        """D-234/D-235: the turn still running after COMPACT_TURN_WAIT: no /compact, recorded,
        alerted, automation paused; the hand-off stays unconsumed."""
        ka.COMPACT_TURN_WAIT = timedelta(0)
        self.after_save = [("notification", 10 ** 6)]
        d, rec = self.compact()
        CompactionFailures.assert_failed(self, rec, "was still running 10 min later", [SAVE])
        self.assertEqual(rec["steps"]["save"]["status"], "turn-busy")
        self.assertIsNotNone(self.latest())

    def test_a_handoff_out_of_date_again_after_every_refresh_fails(self):
        self.after_save = [("notification", 1)] * (ka.HANDOFF_REFRESHES + 1)
        d, rec = self.compact()
        CompactionFailures.assert_failed(self, rec, f"new input kept reaching the session after its hand-off "
                                                    f"({ka.HANDOFF_REFRESHES} hand-offs asked for again",
                                         [SAVE] + [REFRESH] * ka.HANDOFF_REFRESHES)
        self.assertEqual(len(rec["steps"]["save"]["refreshes"]), ka.HANDOFF_REFRESHES)

    def test_a_refresh_answered_without_a_handoff_fails(self):
        self.after_save = [("notification", 1)]
        self.refreshes = "silent"
        d, rec = self.compact()
        CompactionFailures.assert_failed(self, rec, "the session answered the save prompt and its re-send without "
                                                    "writing a hand-off (asking again for the hand-off that new "
                                                    "input made out of date, D-307)", [SAVE, REFRESH])

    def test_inputs_since(self):
        t0 = datetime.now(UTC)
        self.write([entry("user", t0 - timedelta(seconds=5), origin={"kind": "task-notification"},
                          message={"role": "user", "content": "<task-notification>early"}),
                    entry("user", t0 + timedelta(seconds=1),
                          message={"role": "user", "content": [{"type": "tool_result", "content": "x"}]}),
                    entry("user", t0 + timedelta(seconds=2), isMeta=True, message={"role": "user", "content": "meta"}),
                    entry("user", t0 + timedelta(seconds=3), isSidechain=True, message={"role": "user", "content": "s"}),
                    entry("queue-operation", t0 + timedelta(seconds=4), operation="dequeue"),
                    entry("queue-operation", t0 + timedelta(seconds=5), operation="remove", content="<task-n"),
                    entry("user", t0 + timedelta(seconds=6), origin={"kind": "task-notification"},
                          message={"role": "user", "content": "<task-notification>late"}),
                    owner(t0 + timedelta(seconds=7)),
                    entry("user", t0 + timedelta(seconds=8), isMeta=True,     # a subagent's hand-back (peer)
                          origin={"kind": "peer", "from": "a1", "handback": True},
                          message={"role": "user", "content": "Another Claude session sent a message:\n"
                                                              "<agent-message from=\"a1\">done</agent-message>"})])
        self.assertEqual([w for _, w in ka.inputs_since(self.path, t0)],
                         ["a queued message", "a task notification", "a typed message", "a subagent's message"])
        self.assertEqual(ka.inputs_since(self.path, t0 + timedelta(seconds=8)), [])


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


# ---------------------------------------------------------------- the same through the runner (phase 3a S6)

class ThroughTheRunner:
    """The compaction tests again with the resident runner as the caller: its compaction_check tick
    (keepalive.runner_pass in a fresh process) stores the act, then the runner starts the act under
    its run key (another fresh process). The decision a test sees is the check's. The runner's passes
    are always armed (as the watcher ran), so the dry-run test is the watcher's only."""
    def setUp(self):
        super().setUp()
        testenv.clear_watch()
        self.addCleanup(testenv.clear_watch)
        self.checks = []
        plan = ka.compaction_plan

        def spy(sid, now, args):
            out = plan(sid, now, args)
            self.checks.append(out[0])
            return out
        ka.compaction_plan = spy
        self.addCleanup(setattr, ka, "compaction_plan", plan)

    def compact(self, args=None, now=None):
        if args is not None and not getattr(args, "arm", False):
            self.skipTest("the runner's passes are always armed")
        t = now or datetime.now(UTC)
        self.checks.clear()
        res = testenv.runner_pass(ka, "compaction_check", now=t, session=SID)
        self.assertEqual(res["status"], "ok")
        d = self.checks[0]
        act = testenv.act_of(ka, "compaction", SID)
        if act is not None:
            self.assertEqual(act["key"], d.key)
            testenv.runner_act(ka, "compaction", now=t, session=SID)
            self.assertIsNone(testenv.act_of(ka, "compaction", SID))          # the act ended: its entry is dropped
        recs = ka.load_state().get("compactions") or {}
        return d, (recs.get(d.key) if d is not None and d.key else None)


class CompactWhenIdleRunner(ThroughTheRunner, CompactWhenIdle):
    def test_the_owner_holding_it_or_no_live_tmux_session(self):
        """As the watcher's; any other preflight problem is reported by the check itself (fail_stop),
        as run() reports it."""
        for problem, why in ((f"{ka.RC_HELD} thread (pid [7]); ...", "the owner holds the session"),
                             (None, "not live in its tmux session (plan resume)")):
            ka.preflight = (lambda sid, p=problem: (False, [p], None)) if problem else \
                (lambda sid: (True, [], "resume"))
            d, rec = self.compact()
            self.assertIn(why, d.reason)
            self.assertIsNone(rec)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.paused()["paused"])
        ka.preflight = lambda sid: (False, ["host process info unavailable (OSError: x)"], None)
        res = testenv.runner_pass(ka, "compaction_check", now=datetime.now(UTC), session=SID)
        self.assertEqual(res["note"], "the check failed")
        self.assertIn("compaction pass failed: RuntimeError", self.paused()["reason"])
        self.assertIsNone(testenv.act_of(ka, "compaction", SID))
        self.assertEqual(self.sent, [])


class CompactAtRunEndRunner(ThroughTheRunner, CompactAtRunEnd):
    pass


class CompactionFailuresRunner(ThroughTheRunner, CompactionFailures):
    def test_a_busy_session_after_the_check(self):
        """Mid-turn when the save prompt is due (after the check tick and the act's own check)."""
        old = ka.session_idle
        calls = []

        def idle(path, now=None):
            calls.append(1)
            return len(calls) <= 2                                            # the two checks only
        ka.session_idle = idle
        ka.COMPACT_IDLE_WAIT = timedelta(0)
        try:
            d, rec = self.compact()
        finally:
            ka.session_idle = old
        self.assert_failed(rec, "the session was still mid-turn", [])


class RunnerActs(ThroughTheRunner, Env):
    def test_a_check_due_stores_the_act_and_a_changed_plan_is_not_run(self):
        t = datetime.now(UTC)
        testenv.runner_pass(ka, "compaction_check", now=t, session=SID)
        act = testenv.act_of(ka, "compaction", SID)
        self.assertEqual(act["key"], self.checks[0].key)
        self.assertEqual(ka.parse_ts(act["at"]), t)
        testenv.runner_pass(ka, "compaction_check", now=t + timedelta(minutes=1), session=SID)   # a minute on: still pending
        self.assertEqual(testenv.act_of(ka, "compaction", SID), act)                     # same run key: never twice
        res = testenv.runner_pass(ka, "compaction", "compact-other.20261005T000000", now=t, session=SID)
        self.assertIn("its plan changed", res["note"])
        self.assertEqual(self.sent, [])
        testenv.runner_act(ka, "compaction", now=t + timedelta(minutes=1), session=SID)
        self.assertEqual(len(self.sent), 3)

    def test_the_lock_held_by_a_start_drops_the_act_and_the_next_check_plans_again(self):
        t = datetime.now(UTC)
        testenv.runner_pass(ka, "compaction_check", now=t, session=SID)
        first = testenv.act_of(ka, "compaction", SID)
        lock = ka.session_lock(SID)
        try:
            testenv.runner_act(ka, "compaction", now=t, session=SID)
        finally:
            ka.release_lock(lock)
        self.assertIsNone(testenv.act_of(ka, "compaction", SID))
        testenv.runner_pass(ka, "compaction_check", now=t + timedelta(minutes=1), session=SID)
        again = testenv.act_of(ka, "compaction", SID)
        self.assertNotEqual(again["run"], first["run"])                              # a new run key
        testenv.runner_act(ka, "compaction", now=t + timedelta(minutes=1), session=SID)
        self.assertEqual(ka.load_state()["compactions"][again["key"]]["status"], "compacted")

    def test_a_hold_is_logged_once_across_the_runs(self):
        self.idle_transcript(idle=timedelta(minutes=30))
        lines = []
        old = ka.log
        ka.log = lines.append
        try:
            for k in range(3):
                testenv.runner_pass(ka, "compaction_check", now=self.now + timedelta(seconds=k), session=SID)
        finally:
            ka.log = old
        self.assertEqual(len([ln for ln in lines if "idle 30 min" in ln]), 1, lines)


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
