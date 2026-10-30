#!/usr/bin/env python3
"""Tests for the owner's words as evidence (phase 4b Q2 W1; D-104, D-198, D-235, D-285): owner_words.find over
the inbox and a transcript, decision.add by an autonomous session (actions, CLI), decision.confirm against an
owner message that names the id, a proposal's `supersedes`, and that every text AFClaude types into tmux starts
with a keep-alive prefix (so it never passes for the owner's). Synthetic data only.

    python3 -m unittest test_owner_words
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import keepalive
import owner_words
import prompts
import store
import tasks

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
AT = "2026-10-23T14:19:00.000Z"          # 16:19 in Berlin


def entry(text, *, kind="human", uuid="u1", ts=AT, side=False, meta=False, content=None, type="user"):
    e = {"type": type, "uuid": uuid, "timestamp": ts, "isSidechain": side, "isMeta": meta,
         "message": {"role": "user", "content": content if content is not None else text}}
    if kind is not None:
        e["origin"] = {"kind": kind}
    return e


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-ownerwords-")
        self.db = os.path.join(self.tmp, "afclaude.db")
        self.proj = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.proj, "p"))
        self.conn = store.connect(self.db, create=True)
        for patch in (mock.patch.object(keepalive, "PROJECTS_DIR", self.proj),
                      mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SID}, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def inbox(self, text, session=SID, by="owner", at=None):
        m = store.add_inbox(self.conn, text, session, by)
        if at:
            self.conn.execute("UPDATE inbox_messages SET created_at = ? WHERE id = ?", (at, m["id"]))
        self.conn.commit()
        return m["id"]

    def transcript(self, *entries, session=SID):
        with open(os.path.join(self.proj, "p", f"{session}.jsonl"), "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def find(self, quotes, **kw):
        return owner_words.find(self.conn, quotes, kw.pop("session", SID), **kw)


class Lookup(Env):
    def test_an_inbox_message_matches_and_a_mismatch_is_refused(self):
        n = self.inbox("Yes, record it:   keep the\ntitles, and add topics.")
        ev = self.find("keep the titles, and add topics")                      # whitespace is normalised
        self.assertEqual(ev.ref, f"inbox:{n}")
        self.assertEqual(ev.label[:9], f"inbox #{n} ")
        for bad in ("Keep the titles", "keep the titels", "never said"):       # case and typos must match exactly
            with self.assertRaisesRegex(owner_words.OwnerWordsError, "none of the owner's messages"):
                self.find(bad)

    def test_only_the_owners_messages_of_this_session_count(self):
        self.inbox("said by someone else", by="cli")
        self.inbox("said in another session", session=OTHER)
        for q in ("someone else", "another session"):
            with self.assertRaises(owner_words.OwnerWordsError):
                self.find(q)

    def test_a_transcript_owner_prompt_matches(self):
        self.transcript(entry("Recording questions, A", uuid="abc"))
        ev = self.find("Recording questions, A")
        self.assertEqual(ev.ref, f"chat:{SID}:abc")
        self.assertEqual(ev.at, datetime(2026, 10, 23, 14, 19, tzinfo=timezone.utc))   # the message's time
        self.assertEqual(ev.label, f"chat {SID} 23.10. 16:19")
        self.assertEqual(self.find("questions", ref=f"chat:{SID}:abc").ref, ev.ref)

    def test_what_is_not_an_owner_prompt_is_refused(self):
        words = "the words that are quoted"
        self.transcript(
            entry("[keep-alive] A new run starts: " + words, uuid="a"),                    # AFClaude's own texts
            entry("[AFClaude inbox #3] The owner wrote this: " + words, uuid="b"),         # an inbox delivery
            entry("This session was compacted: " + words, uuid="c"),
            entry("/compact " + words, uuid="d"),
            entry(words, uuid="e", side=True),                                              # a sidechain
            entry(words, uuid="f", kind="peer"),                                            # a peer message
            entry(words, uuid="g", kind=None),                                              # no origin
            entry(words, uuid="h", meta=True),
            entry(None, uuid="i", content=[{"type": "tool_result", "content": words}]),     # a tool result
            entry(words, uuid="j", type="assistant"),
            entry("[Request interrupted by user] " + words, uuid="k"))
        with self.assertRaisesRegex(owner_words.OwnerWordsError, "none of the owner's messages"):
            self.find(words)
        self.transcript(entry("really: " + words, uuid="z"))                               # the owner's own
        self.assertEqual(self.find(words).ref, f"chat:{SID}:z")

    def test_every_keepalive_prefix_is_what_the_rule_refuses(self):
        for p in owner_words.KEEPALIVE_PREFIXES:
            self.assertIsNone(owner_words.owner_prompt(entry(p + " owner words here")), p)
        self.assertEqual(owner_words.owner_prompt(entry("owner words here")), "owner words here")

    def test_several_matches_need_from(self):
        a = self.inbox("first: use the quick path")
        self.transcript(entry("later again: use the quick path", uuid="u2", ts="2026-10-24T08:00:00.000Z"))
        with self.assertRaises(owner_words.OwnerWordsError) as cm:
            self.find("use the quick path")
        self.assertIn("2 owner messages", str(cm.exception))
        self.assertIn(f"inbox:{a}", str(cm.exception))
        self.assertIn(f"chat:{SID}:u2", str(cm.exception))
        self.assertEqual(self.find("use the quick path", ref=f"inbox:{a}").ref, f"inbox:{a}")
        self.assertEqual(self.find("use the quick path", ref=f"chat:{SID}:u2").ref, f"chat:{SID}:u2")

    def test_a_bad_from_is_refused(self):
        self.inbox("some words")
        for ref, msg in (("garbage", "write inbox"), (f"chat:{OTHER}:u", "another session"),
                         ("inbox:999", "no such owner message"), (f"chat:{SID}:nope", "no such owner message")):
            with self.assertRaisesRegex(owner_words.OwnerWordsError, msg):
                self.find("some words", ref=ref)

    def test_several_quotes_all_come_from_one_message(self):
        n = self.inbox("alpha beta gamma delta")
        self.inbox("alpha only")
        self.assertEqual(self.find(["alpha", "delta"]).ref, f"inbox:{n}")
        with self.assertRaises(owner_words.OwnerWordsError):
            self.find(["alpha", "only", "delta"])

    def test_empty_quote_no_session_and_unreadable_transcript(self):
        for q in ("", "  ", []):
            with self.assertRaisesRegex(owner_words.OwnerWordsError, "quote of the owner's words"):
                self.find(q)
        with self.assertRaisesRegex(owner_words.OwnerWordsError, "no session id"):
            owner_words.find(self.conn, "x", None)
        os.makedirs(os.path.join(self.proj, "p", f"{OTHER}.jsonl"))                         # exists, can't be read
        with self.assertRaisesRegex(owner_words.OwnerWordsError, "can't read the transcript"):
            self.find("x", session=OTHER)

    def test_word_and_since(self):
        self.inbox("please confirm D-123 now")
        with self.assertRaises(owner_words.OwnerWordsError):
            self.find("D-12", word=True)                                                  # not inside D-123
        self.assertTrue(self.find("D-123", word=True).ref.startswith("inbox:"))
        with self.assertRaisesRegex(owner_words.OwnerWordsError, "none of the owner's messages"):
            self.find("D-123", word=True, since=datetime(2999, 1, 1, tzinfo=timezone.utc))


class Write(Env):
    def do(self, name, *, actor="cli", via="cli", autonomous=False, **params):
        return actions.perform(self.conn, name, params, actor=actor, via=via, autonomous=autonomous)

    def record(self, quotes="the words", **kw):
        kw.setdefault("session", SID)
        return self.do("decision.add", title="Said in chat", topics=["chat"], author="owner", words=quotes,
                       autonomous=True, **kw)

    def propose(self, title="Reworded", **kw):
        return self.do("decision.add", title=title, topics=["t"], author="task-manager", **kw)

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    def test_an_autonomous_session_records_against_an_inbox_message(self):
        n = self.inbox("the owner says: the words, verbatim")
        r = self.record("the words,   verbatim")
        d = store.get_decision_entry(self.conn, r["id"])
        self.assertEqual((d["state"], d["author"], d["words"]), ("active", "owner", "the words, verbatim"))
        self.assertEqual(d["date"], datetime.now(owner_words.BERLIN).strftime("%Y-%m-%d %H:%M"))   # the message's time
        self.assertTrue(d["source"].startswith(f"inbox #{n} "), d["source"])
        a = [x for x in store.audit_log(self.conn, 10, target_type="decision") if x["action"] == "decision.add"][0]
        self.assertEqual(a["after"]["evidence"], f"inbox:{n}")

    def test_the_date_and_source_come_from_a_transcript_message(self):
        self.transcript(entry("we decided: the words", uuid="m1"))
        r = self.record("the words", source="a note")
        self.assertEqual((r["date"], r["source"]), ("2026-10-23 16:19", f"chat {SID} 23.10. 16:19; a note"))
        self.assertEqual(r["evidence"], f"chat:{SID}:m1")
        self.assertIn("**Date:** 2026-10-23 16:19", store.get_decision_entry(self.conn, r["id"])["body"])

    def test_the_mcp_actor_s_session_is_the_one_searched(self):
        self.inbox("the words")
        r = self.do("decision.add", title="T", topics=["t"], author="owner", words="the words", actor=f"mcp:{SID}",
                    via="mcp", autonomous=True)                                          # no `session` parameter
        self.assertEqual(r["evidence"].split(":")[0], "inbox")
        with self.assertRaisesRegex(actions.Forbidden, "no stored|none of the owner's messages"):
            self.do("decision.add", title="T", topics=["t"], author="owner", words="the words",
                    actor=f"mcp:{OTHER}", via="mcp", autonomous=True)

    def test_an_autonomous_session_without_evidence_is_refused_and_nothing_is_written(self):
        self.inbox("something else entirely")
        for kw, msg in (({"quotes": "never said"}, "none of the owner's messages"),
                        ({"session": None}, "no session id"),
                        ({"quotes": "  "}, "quote of the owner's words"),
                        ({"quotes": None}, "quote of the owner's words")):
            with self.assertRaisesRegex(actions.Forbidden, msg):
                self.record(**kw)
        self.assertEqual(self.count(), 0)

    def test_several_matches_need_from_and_the_date_is_not_passed(self):
        a = self.inbox("the words, once")
        b = self.inbox("the words, twice")
        with self.assertRaisesRegex(actions.Forbidden, "2 owner messages"):
            self.record("the words")
        self.assertEqual(self.record("the words", ref=f"inbox:{b}")["evidence"], f"inbox:{b}")
        with self.assertRaisesRegex(ValueError, "takes its date from the owner's message"):
            self.record("the words", ref=f"inbox:{a}", date="2020-01-01")

    def test_repeated_quotes_all_from_one_message(self):
        self.inbox("alpha and beta, said once")
        self.inbox("gamma, said elsewhere")
        r = self.record(["alpha", "beta"])
        self.assertEqual(store.get_decision_entry(self.conn, r["id"])["words"], 'alpha" ... "beta')
        with self.assertRaisesRegex(actions.Forbidden, "none of the owner's messages"):
            self.record(["alpha", "gamma"])

    def test_the_owners_own_calls_are_unchanged(self):
        r = self.do("decision.add", title="Own", topics=["t"], author="owner", words="anything at all")
        self.assertEqual(store.get_decision_entry(self.conn, r["id"])["words"], "anything at all")
        with self.assertRaisesRegex(ValueError, "needs the owner's exact words"):
            self.do("decision.add", title="Own", topics=["t"], author="owner")
        with self.assertRaisesRegex(ValueError, "not used for the owner's own entry"):
            self.do("decision.add", title="Own", topics=["t"], author="owner", words="x", ref="inbox:1")
        self.assertEqual(self.propose()["state"], "proposed")                              # its own: no evidence

    def test_confirm_by_an_autonomous_session_needs_a_message_naming_the_id(self):
        p = self.propose()["id"]
        kw = dict(actor=f"mcp:{SID}", via="mcp", autonomous=True)
        with self.assertRaisesRegex(actions.Forbidden, "needs a message of the owner"):
            self.do("decision.confirm", decision=p, **kw)
        self.inbox(f"please confirm {p[:-1]}x and D-9{p[-1]}")                              # not the id
        with self.assertRaises(actions.Forbidden):
            self.do("decision.confirm", decision=p, **kw)
        n = self.inbox(f"Yes, {p} is fine.")
        r = self.do("decision.confirm", decision=p, **kw)
        self.assertEqual((r["state"], r["evidence"]), ("active", f"inbox:{n}"))
        a = [x for x in store.audit_log(self.conn, 10, target_type="decision") if x["action"] == "decision.confirm"][0]
        self.assertEqual(a["after"]["evidence"], f"inbox:{n}")

    def test_a_message_older_than_the_proposal_does_not_confirm_it(self):
        self.inbox("D-001 sounds good", at="2020-01-01T00:00:00.000Z")
        p = self.propose()["id"]
        self.assertEqual(p, "D-001")
        with self.assertRaisesRegex(actions.Forbidden, "none of the owner's messages"):
            self.do("decision.confirm", decision=p, actor=f"mcp:{SID}", via="mcp", autonomous=True)

    def test_several_messages_naming_the_id_need_from(self):
        p = self.propose()["id"]
        self.inbox(f"{p} once")
        b = self.inbox(f"{p} twice")
        with self.assertRaisesRegex(actions.Forbidden, "2 owner messages"):
            self.do("decision.confirm", decision=p, actor=f"mcp:{SID}", via="mcp", autonomous=True)
        r = self.do("decision.confirm", decision=p, actor=f"mcp:{SID}", via="mcp", autonomous=True, ref=f"inbox:{b}")
        self.assertEqual(r["evidence"], f"inbox:{b}")

    def test_the_owner_confirms_without_a_message_and_a_confirmed_one_is_a_noop(self):
        p = self.propose()["id"]
        self.assertEqual(self.do("decision.confirm", decision=p)["state"], "active")
        self.assertNotIn("evidence", self.do("decision.confirm", decision=p))
        with self.assertRaisesRegex(ValueError, "names the owner's message"):
            self.do("decision.confirm", decision=p, ref="inbox:1")

    def test_confirming_a_proposal_deletes_what_it_supersedes_atomically(self):
        a = self.do("decision.add", title="Original A", topics=["t"], author="owner", words="words of a")["id"]
        b = self.do("decision.add", title="Original B", topics=["t"], author="owner", words="words of b")["id"]
        p = self.propose(supersedes=[a, b], words="words of a")
        self.assertEqual(p["supersedes"], f"{a}, {b}")
        d = store.get_decision_entry(self.conn, p["id"])
        self.assertIn(f"**Supersedes:** {a}, {b}", d["body"])
        self.assertEqual(self.count(), 3)                                                    # still in force while waiting
        r = self.do("decision.confirm", decision=p["id"])
        self.assertEqual((r["state"], r["replaced"]), ("active", [a, b]))
        self.assertEqual([x["id"] for x in store.list_decision_entries(self.conn)], [p["id"]])
        log = [x for x in store.audit_log(self.conn, 20, target_type="decision") if x["action"] == "decision.delete"]
        self.assertEqual(sorted(x["target_id"] for x in log), [a, b])
        for x in log:
            self.assertEqual(x["after"]["reason"], f"replaced by {p['id']}")
            self.assertIn("words of", x["before"]["body"])                       # the whole old entry

    def test_the_confirm_is_refused_when_an_original_is_gone_and_nothing_changes(self):
        a = self.do("decision.add", title="Original A", topics=["t"], author="owner", words="wa")["id"]
        b = self.do("decision.add", title="Original B", topics=["t"], author="owner", words="wb")["id"]
        p = self.propose(supersedes=[a, b])["id"]
        self.do("decision.delete", decision=b, reason="gone")
        with self.assertRaisesRegex(store.NotFound, f"replaces {b}, which no longer exists"):
            self.do("decision.confirm", decision=p)
        self.assertEqual(store.get_decision_entry(self.conn, p)["state"], "proposed")
        self.assertIsNotNone(store.get_decision_entry(self.conn, a))                          # not deleted either

    def test_supersedes_is_checked_when_the_proposal_is_written(self):
        a = self.do("decision.add", title="Original", topics=["t"], author="owner", words="w")["id"]
        with self.assertRaisesRegex(store.NotFound, "no decision D-099"):
            self.propose(supersedes=["D-099"])
        with self.assertRaisesRegex(ValueError, "belongs to a task-manager proposal"):
            self.do("decision.add", title="x", topics=["t"], author="owner", words="w", supersedes=[a])
        self.conn.execute("UPDATE decisions SET origin = 'file' WHERE id = ?", (a,))
        with self.assertRaisesRegex(ValueError, "mirrors DECISIONS.md"):
            self.propose(supersedes=[a])

    def test_a_confirmed_proposal_edit_keeps_its_supersedes_line(self):
        a = self.do("decision.add", title="Original", topics=["t"], author="owner", words="w")["id"]
        p = self.propose(supersedes=[a])["id"]
        self.do("decision.edit", decision=p, scope="everywhere")
        self.assertIn(f"**Supersedes:** {a}", store.get_decision_entry(self.conn, p)["body"])


class Cli(Env):
    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            code = tasks.main(["--db", self.db, "decision", *argv])
        return code, out.getvalue(), err.getvalue()

    UNATTENDED = {"CLAUDE_GUARD_DISABLE": "1"}

    def test_an_unattended_cli_records_against_an_owner_message(self):
        n = self.inbox("alpha beta gamma")
        add = ["add", "--title", "Said", "--topic", "t", "--author", "owner"]
        code, _, err = self.cli(*add, "--words", "never said", env=self.UNATTENDED)
        self.assertEqual(code, 1)
        self.assertIn("none of the owner's messages", err)
        code, out, _ = self.cli(*add, "--words", "alpha", "--words", "gamma", env=self.UNATTENDED)
        self.assertEqual(code, 0)
        self.assertIn(f"recorded against the owner's message inbox:{n}", out)
        d = store.get_decision_entry(store.connect(self.db), "D-001")
        self.assertEqual((d["words"], d["state"]), ('alpha" ... "gamma', "active"))
        self.inbox("alpha beta gamma, again")
        code, _, err = self.cli(*add, "--words", "alpha", env=self.UNATTENDED)
        self.assertEqual(code, 1)
        self.assertIn("--from", err)
        code, _, _ = self.cli(*add, "--words", "alpha", "--from", f"inbox:{n}", env=self.UNATTENDED)
        self.assertEqual(code, 0)

    def test_the_owners_own_cli_needs_no_message(self):
        code, _, _ = self.cli("add", "--title", "Said", "--topic", "t", "--author", "owner", "--words", "anything")
        self.assertEqual(code, 0)

    def test_confirm_and_supersedes_from_the_cli(self):
        self.cli("add", "--title", "Original", "--topic", "t", "--author", "owner", "--words", "w")
        self.cli("add", "--title", "Rewording", "--topic", "t", "--author", "task-manager", "--supersedes", "D-001")
        code, _, err = self.cli("confirm", "D-002", env=self.UNATTENDED)
        self.assertEqual(code, 1)
        self.assertIn("needs a message of the owner", err)
        self.inbox("go ahead with D-002")
        code, out, _ = self.cli("confirm", "D-002", env=self.UNATTENDED)
        self.assertEqual(code, 0)
        self.assertIn("replaced (deleted, in the audit log): D-001", out)
        self.assertIsNone(store.get_decision_entry(store.connect(self.db), "D-001"))


class Typed(unittest.TestCase):
    """What AFClaude types into a tmux session reaches the transcript as a "human" prompt: it must start with a
    keep-alive prefix so owner_words never takes it for the owner's (D-198)."""
    def test_every_bundle_typed_into_tmux_starts_with_a_keepalive_prefix(self):
        checked = []
        for kind, b in prompts.BUNDLES.items():
            if not b.flat:
                continue                                                  # hook context or a Haiku call: not typed
            if kind == "compact":
                text = keepalive.compact_command()                        # /compact <instruction>
            else:
                system = {v: "x" for v in prompts.bundle_system(kind)}
                text = prompts.bundle(kind, insert="an insert", **system)
            self.assertTrue(text.lstrip().startswith(owner_words.KEEPALIVE_PREFIXES), f"{kind}: {text[:60]!r}")
            self.assertIsNone(owner_words.owner_prompt(entry(text)), kind)
            checked.append(kind)
        self.assertIn("continue", checked)
        self.assertIn("inbox_now", checked)
        self.assertIn("session_end_save", checked)


if __name__ == "__main__":
    unittest.main()
