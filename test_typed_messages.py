#!/usr/bin/env python3
"""Tests for goal 12 usage split S2 (docs/usage_split_plan.md §3.1, §5 S2; D-138, D-131, D-154, D-235):
the typed-message record (store.typed_messages, actions typed.record), keepalive.fire's required driver and
its record before the send (a failed record sends nothing), every fire caller's driver (a static check
over the code; the callers' own suites check the values at run time), and the sampler's per-session driver
(usage_sampler.track_driver / driver_at): hash matching with whitespace tolerance and its refusals, entries
that never change the driver, the prefix matcher for prompts from before the record, a subagent request
taking its parent's driver, the cross-check against the keep-alive prefixes and Haiku's prompt list.
Synthetic data and the hermetic test DB only; nothing reaches a real process or tmux session.
    python3 -m unittest test_typed_messages
"""
import ast
import json
import os
import re
import shutil
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import keepalive as ka
import store
import usage_sampler as smp
import usage_split as us

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "testdata", "usage_split_transcript.jsonl")
SID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"            # the fixture's session
OTHER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
UTC = timezone.utc
H = "0" * 64


def t(hms, day=20):
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC)


def fixture():
    with open(FIXTURE) as f:
        return [json.loads(line) for line in f if line.strip()]


class DB(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def record(self, session, text, at, driver="auto", prompt="continue", actor="keepalive"):
        with mock.patch.object(store, "_utcnow", lambda: at):
            return actions.perform(self.conn, "typed.record", {"session_id": session, "driver": driver,
                                                               "category": "task_manager", "prompt": prompt,
                                                               "text_sha256": us.text_hash(text)},
                                   actor=actor, via="runner")

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM typed_messages ORDER BY id")]


# ---------------------------------------------------------------- the record and the action

class Record(DB):
    def test_row_and_audit(self):
        r = self.record(SID, "[keep-alive] go", t("10:00:00"), prompt="fillup", actor="dispatcher")
        self.assertEqual((r["session_id"], r["driver"], r["category"], r["prompt"], r["actor"], r["sent_at"]),
                         (SID, "auto", "task_manager", "fillup", "dispatcher", "2026-10-20T10:00:00.000Z"))
        self.assertEqual(r["text_sha256"], us.text_hash("[keep-alive]   go\n"))
        audit = self.conn.execute("SELECT actor, via, action, target_type, target_id FROM audit_log "
                                  "WHERE action='typed.record'").fetchall()
        self.assertEqual([tuple(a) for a in audit], [("dispatcher", "runner", "typed.record", "typed_message",
                                                      str(r["id"]))])
        self.assertEqual(store.typed_since(self.conn), "2026-10-20T10:00:00.000Z")
        self.assertEqual([x["id"] for x in store.typed_messages(self.conn, SID, t("10:00:00"))], [r["id"]])
        self.assertEqual(store.typed_messages(self.conn, SID, t("10:00:01")), [])
        self.assertEqual(store.typed_messages(self.conn, OTHER, t("09:00:00")), [])

    def test_bad_values_are_refused(self):
        base = {"session_id": SID, "driver": "auto", "category": "task_manager", "prompt": "x", "text_sha256": H}
        for k, v in (("driver", "manual"), ("driver", None), ("category", "nightly"), ("text_sha256", "abc"),
                     ("session_id", "not-a-uuid"), ("prompt", ""), ("prompt", "p" * 65)):
            with self.subTest(k=k, v=v):
                with self.assertRaises(ValueError):
                    actions.perform(self.conn, "typed.record", dict(base, **{k: v}), actor="keepalive", via="runner")
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "typed.record", base, actor="mcp", via="mcp")
        self.assertEqual(self.rows(), [])
        self.assertIsNone(store.typed_since(self.conn))


# ---------------------------------------------------------------- keepalive.fire

class Fire(DB):
    def setUp(self):
        super().setUp()
        testenv.tmux_row(SID)
        self.runs, self.killed = [], []
        self.seen_at_send = None

        def run(argv, **k):
            self.runs.append(argv)
            self.seen_at_send = self.rows()                   # what the DB held when the text went out
            return types.SimpleNamespace(returncode=0, stdout="sent-keys", stderr="")
        for name, fake in (("run_on_host", run), ("kill_claude", self.killed.append)):
            p = mock.patch.object(ka.host, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def test_records_before_the_send(self):
        ka.fire(SID, "/tmp", "[keep-alive]  A new\nrun starts", "send-keys", driver="auto", prompt="continue")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(len(self.seen_at_send), 1)
        r = self.seen_at_send[0]
        self.assertEqual((r["session_id"], r["driver"], r["prompt"], r["category"], r["actor"]),
                         (SID, "auto", "continue", "task_manager", "keepalive"))
        self.assertEqual(r["text_sha256"], us.text_hash("[keep-alive] A new run starts"))
        ka.fire(SID, "/tmp", "[AFClaude inbox #3] hi", "send-keys", category=ka.remote_control.CONTINUE,
                driver="owner", prompt="inbox", actor="runner:inbox")
        r = self.rows()[-1]
        self.assertEqual((r["driver"], r["category"], r["actor"]), ("owner", "continue", "runner:inbox"))

    def test_driver_is_required(self):
        with self.assertRaises(TypeError):
            ka.fire(SID, "/tmp", "go", "send-keys", prompt="x")
        with self.assertRaisesRegex(ValueError, "driver must be one of"):
            ka.fire(SID, "/tmp", "go", "send-keys", driver="manual", prompt="x")
        self.assertEqual((self.runs, self.rows()), ([], []))

    def test_a_failed_record_sends_nothing(self):
        """D-235: no untracked message: the record fails -> no take-over, no `claude stop`, no send."""
        for plan in ("send-keys", "take-over:42", "stop-bg-then-resume", "resume"):
            with self.subTest(plan=plan), \
                    mock.patch.object(store, "add_typed_message", side_effect=ValueError("disk full")):
                with self.assertRaisesRegex(ValueError, "disk full"):
                    ka.fire(SID, "/tmp", "[keep-alive] go", plan, driver="auto", prompt="continue")
        self.assertEqual((self.runs, self.killed, self.rows()), ([], [], []))


class Callers(unittest.TestCase):
    """Every fire() call in the code passes driver= (a literal: owner for the inbox delivery, auto for every
    other one) and prompt=. The callers' own suites check the values at run time (test_compaction,
    test_inbox, test_dispatcher, test_pick, test_usage_review)."""
    EXPECTED = {"keepalive.py": 5, "dispatcher.py": 1, "inbox_worker.py": 1, "usage_review.py": 1}

    def calls(self):
        out = []
        for root, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if not d.startswith((".", "__")) and d not in ("data", "node_modules")]
            for f in files:
                if not f.endswith(".py") or f.startswith("test"):
                    continue
                path = os.path.join(root, f)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), path)
                for n in ast.walk(tree):
                    if not isinstance(n, ast.Call):
                        continue
                    fn = n.func
                    if (isinstance(fn, ast.Name) and fn.id == "fire") or (
                            isinstance(fn, ast.Attribute) and fn.attr == "fire" and isinstance(fn.value, ast.Name)
                            and fn.value.id in ("ka", "keepalive")):
                        out.append((os.path.relpath(path, HERE), n))
        return out

    def test_every_caller_says_who_drove_it(self):
        calls = self.calls()
        found = {}
        for path, n in calls:
            found[path] = found.get(path, 0) + 1
            kw = {k.arg: k.value for k in n.keywords}
            with self.subTest(path=path, line=n.lineno):
                self.assertIn("driver", kw)
                self.assertIn("prompt", kw)
                self.assertIsInstance(kw["driver"], ast.Constant)
                self.assertEqual(kw["driver"].value, "owner" if path == "inbox_worker.py" else "auto")
        self.assertEqual(found, self.EXPECTED)

    def test_the_live_compaction_tool_too(self):
        with open(os.path.join(HERE, "tools", "test_compact_live.sh")) as fh:
            lines = [ln for ln in fh if re.search(r"\bka\.fire\(", ln)]
        self.assertTrue(lines)
        for ln in lines:
            self.assertIn('driver="auto"', ln)


# ---------------------------------------------------------------- the sampler's driver per session

class Sampler(DB):
    """scan() over a fake projects dir holding the fixture as SID's main transcript (plus a subagent
    file), against typed rows in the test DB."""
    NOW = t("12:00:00")

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="afc-s2-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        proj = os.path.join(self.dir, "-proj")
        os.makedirs(os.path.join(proj, SID, "subagents"))
        self.main = os.path.join(proj, f"{SID}.jsonl")
        self.write(fixture())
        with open(os.path.join(proj, SID, "subagents", "agent-x.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "uuid": "s1", "isSidechain": True, "timestamp": "2026-10-20T10:20:00Z",
                                 "message": {"role": "user", "content": "[keep-alive] not a prompt of the session"}}) + "\n")
        for name, v in (("PROJECTS", self.dir), ("now_utc", lambda: self.NOW)):
            p = mock.patch.object(smp, name, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(smp.runner_state, "own_sessions", lambda path: set())
        p.start()
        self.addCleanup(p.stop)

    def write(self, entries, mode="w"):
        with open(self.main, mode) as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def scan(self, st=None):
        st = st if st is not None else {"offsets": {"x": 0}}
        problems = []
        smp.scan(st, False, problems)
        return st, st["sessions"][SID], problems

    def seq(self, meta):
        return [(r["at"][11:19], r["driver"], r["via"], r["source"]) for r in meta["drivers"]]

    def typed_rows(self):
        self.record(SID, "[keep-alive] Continue the run: budget 20%.", t("09:59:30"))
        self.record(SID, "[AFClaude inbox #4] Please also check the dashboard.", t("10:39:50"), "owner", "inbox",
                    "runner:inbox")
        self.record(SID, "[AFClaude usage review]   Go\nahead.", t("10:59:00"), prompt="usage_review_oneoff")

    def test_typed_rows_drive_it(self):
        """Matched rows give their driver (the pasted, re-spaced review text too: whitespace tolerant); an
        unmatched real prompt is the owner's; the task notification, the peer message, the compaction
        command and summary, the local command, the interrupt and the sidechain entry change nothing."""
        self.typed_rows()
        st, meta, problems = self.scan()
        self.assertEqual(problems, [])
        self.assertEqual(self.seq(meta), [("10:00:00", "auto", "typed", "typed"), ("10:30:00", "owner", "unmatched", "typed"),
                                          ("11:00:00", "auto", "typed", "typed")])
        self.assertEqual(meta["driver"], "auto")
        texts = [p["text"] for p in meta["prompts"]]                   # Haiku's last 4 (D-131): AFClaude's left out
        self.assertEqual(len(texts), 4)
        self.assertIn("[AFClaude inbox #4] Please also check the dashboard.", texts)   # the owner's words
        self.assertFalse(any(x.startswith("[keep-alive]") or "usage review" in x for x in texts), texts)

    def test_another_sessions_row_or_an_old_one_is_refused(self):
        """Refused matches: the keep-alive text then counts as the owner's, and the cross-check against
        the keep-alive prefixes reports it (an AFClaude send the record lacks)."""
        self.record(OTHER, "[keep-alive] Continue the run: budget 20%.", t("09:59:50"))
        self.record(SID, "[keep-alive] Continue the run: budget 20%.", t("09:49:59"))     # 10 min 1 s before
        st, meta, problems = self.scan()
        self.assertEqual(self.seq(meta)[0], ("10:00:00", "owner", "unmatched", "typed"))
        self.assertEqual([p[0] for p in problems], ["an AFClaude-prefixed prompt has no typed_messages row"] * 2)
        self.assertIn("entry u1 at 2026-10-20T10:00:00", problems[0][1])
        self.assertIn("entry u11", problems[1][1])

    def test_before_the_record_the_prefix_list_decides(self):
        """Prompts from before the record's first row go through the prefix matcher, labelled "prefix"
        (never mixed silently with "typed"); the fixture gives driver_of_prefix's changes."""
        st, meta, problems = self.scan()                                 # no row at all yet
        want = [(c.at.strftime("%H:%M:%S"), c.driver, c.via) for c in us.driver_of_prefix(fixture()).changes]
        self.assertEqual([x[:3] for x in self.seq(meta)], want)
        self.assertEqual({x[3] for x in self.seq(meta)}, {"prefix"})
        self.assertEqual(problems, [])
        self.record(SID, "[AFClaude usage review] Go ahead.", t("10:59:00"))
        st, meta, problems = self.scan()                                 # the record starts at 10:59
        self.assertEqual(self.seq(meta), [("10:00:00", "auto", "prefix", "prefix"), ("10:30:00", "owner", "prefix", "prefix"),
                                          ("11:00:00", "auto", "typed", "typed")])

    def test_a_subagent_request_takes_the_parents_driver_at_its_time(self):
        self.typed_rows()
        st, meta, problems = self.scan()
        self.assertEqual(smp.driver_at(meta, t("10:20:00")), ("auto", "typed"))
        self.assertEqual(smp.driver_at(meta, t("10:45:00")), ("owner", "typed"))
        self.assertEqual(smp.driver_at(meta, t("11:00:00")), ("auto", "typed"))
        with self.assertRaises(us.SplitError):
            smp.driver_at(meta, t("09:00:00"))                           # before any prompt: unknown

    def test_carried_over_and_pruned(self):
        """The driver carries over to the next chunk; changes older than DRIVER_KEEP go, the one in force
        before the oldest kept is kept as driver_before."""
        self.typed_rows()
        st, meta, _ = self.scan()
        st["offsets"][self.main] = os.path.getsize(self.main)
        self.write([{"type": "user", "uuid": "n1", "timestamp": "2026-10-21T10:40:00.000Z", "origin": {"kind": "peer"},
                     "message": {"role": "user", "content": "peer note"}}], "a")
        self.NOW = t("10:40:00", day=21)
        st, meta, problems = self.scan(st)
        self.assertEqual((meta["driver"], problems), ("auto", []))
        self.assertEqual(self.seq(meta), [("11:00:00", "auto", "typed", "typed")])
        self.assertEqual((meta["driver_before"], meta["driver_before_source"]), ("owner", "typed"))
        self.assertEqual(smp.driver_at(meta, t("10:45:00")), ("owner", "typed"))

    def test_an_unclassifiable_entry_is_reported_and_the_driver_unknown(self):
        """D-235: an unknown origin kind is not guessed: reported, the driver unknown until the next prompt."""
        self.typed_rows()
        st, meta, _ = self.scan()
        st["offsets"][self.main] = os.path.getsize(self.main)
        self.write([{"type": "user", "uuid": "w1", "timestamp": "2026-10-20T11:30:00.000Z", "origin": {"kind": "telepathy"},
                     "message": {"role": "user", "content": "hm"}}], "a")
        st, meta, problems = self.scan(st)
        self.assertEqual(len(problems), 1)
        self.assertIn("unknown origin kind 'telepathy'", problems[0][1])
        self.assertIsNone(meta["driver"])
        self.assertEqual(smp.driver_at(meta, t("11:10:00")), ("auto", "typed"))
        with self.assertRaises(us.SplitError):
            smp.driver_at(meta, t("11:40:00"))
        st["offsets"][self.main] = os.path.getsize(self.main)
        self.write([{"type": "user", "uuid": "w2", "timestamp": "2026-10-20T11:45:00.000Z", "origin": {"kind": "human"},
                     "message": {"role": "user", "content": "back again"}}], "a")
        st, meta, problems = self.scan(st)
        self.assertEqual((meta["driver"], problems), ("owner", []))

    def test_a_db_failure_aborts_the_scan(self):
        """D-235: the typed rows can't be read -> the scan raises (the sample is aborted, nothing consumed)."""
        with mock.patch.object(store, "typed_since", side_effect=store.DBError("database is locked")):
            with self.assertRaises(store.DBError):
                self.scan()


class Classify(unittest.TestCase):
    def test_verdicts_and_missed(self):
        rows = [us.TypedRow(SID, t("10:59:00"), "auto", us.text_hash("[AFClaude usage review] Go ahead."))]
        track, prompts = us.classify(SID, fixture(), rows)
        self.assertEqual([(p.uuid, p.driver, p.via, p.missed) for p in prompts],
                         [("u1", "owner", "unmatched", True), ("u5", "owner", "unmatched", False),
                          ("u6", "owner", "unmatched", False), ("u11", "auto", "typed", False)])
        self.assertEqual(track.source, us.SOURCE_TYPED)
        track, prompts = us.classify(SID, fixture(), [], source=us.SOURCE_PREFIX)
        self.assertEqual([(p.uuid, p.driver) for p in prompts], [("u1", "auto"), ("u5", "owner"), ("u6", "owner"),
                                                                 ("u11", "auto")])
        self.assertFalse(any(p.missed for p in prompts))
        with self.assertRaises(us.SplitError):
            us.classify(SID, fixture(), [], source="guess")


if __name__ == "__main__":
    unittest.main()
