#!/usr/bin/env python3
"""Tests for the owner's answer path (phase 4b Q3 W2; D-102, D-104, D-244, D-246, D-248, D-252, D-263, D-279,
D-300, D-304, D-306): the two owner-only endpoints (key, JSON, rate, stale version), a plain answer as a
triaged inbox message with question_id, a decision answer's note without a Haiku call and no row for a bare
approve, the delivery header naming the question, a reply keeping the thread until dismissed, and
inbox.view's `questions`. Synthetic data only.

    python3 -m unittest test_question_answers
"""
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import decisions as dec
import inbox
import inbox_api
import inbox_worker
import owner_words
import store
from testenv import SID

OWNER_WORDS = "keep the titles short, always"
KEY = "k" * 40


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path_)


class Base(unittest.TestCase):
    """A DB of its own with one project whose task-manager is SID, and the API on a unix socket."""

    def setUp(self):
        testenv.fresh_db(self)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        store.add_project(self.conn, "Mgr")
        store.update_project(self.conn, "Mgr", manager_session=SID)
        self.conn.commit()
        patch = mock.patch.object(dec, "DEFAULT_SOURCE", os.path.join(tempfile.gettempdir(), "afc-no-such.md"))
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keyfile = os.path.join(self.tmp.name, "quickview.key")
        with open(self.keyfile, "w") as fh:
            fh.write(KEY + "\n")
        self.sock = os.path.join(self.tmp.name, "api", "inbox.sock")
        self.wake = threading.Event()
        self.srv = inbox_api.bind(self.sock, self.keyfile, self.wake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    # ---- helpers
    def call(self, method, path, body=None, key=KEY, ctype="application/json", raw=None):
        c = UnixHTTP(self.sock)
        headers = {}
        if key is not None:
            headers["X-AFClaude-Key"] = key
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = ctype
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"null"))
        c.close()
        return out

    def post(self, qid, verb, body, **kw):
        return self.call("POST", f"/afclaude/api/questions/{qid}/{verb}", body, **kw)

    def do(self, name, *, actor="cli", via="cli", autonomous=False, **params):
        return actions.perform(self.conn, name, params, actor=actor, via=via, autonomous=autonomous)

    def ask(self, text="Which way for the nightly run?", **kw):
        return self.do("task.ask", question=text, project="Mgr", **kw)

    def proposal(self, title="Reworded titles", supersedes=None):
        return self.do("decision.add", title=title, topics=["lists"], author="task-manager", summary="A rewording.",
                       supersedes=supersedes)

    def owner_entry(self, title="Short titles"):
        return self.do("decision.add", title=title, topics=["lists"], author="owner", words=OWNER_WORDS,
                       summary="Titles stay short.")

    def question_of(self, did):
        return store.open_decision_question(self.conn, did)

    def entry(self, did):
        return store.get_decision_entry(self.conn, did)

    def messages(self):
        return sorted(store.list_inbox(self.conn, limit=500), key=lambda m: m["id"])

    def audit(self, **kw):
        return store.audit_log(self.conn, 200, **kw)


class Endpoints(Base):
    def test_key_json_and_field_rules_are_the_message_boxes(self):
        q = self.ask()
        path = f"/afclaude/api/questions/{q['id']}/answer"
        for key in (None, "", "wrong", KEY[:-1], KEY + "x"):
            self.assertEqual(self.call("POST", path, {"answer": "a"}, key=key)[0], 401, key)
        self.assertEqual(self.call("POST", path, {"answer": "a"}, ctype="text/plain")[0], 415)
        self.assertEqual(self.call("POST", path, raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", path, raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", path, {"answer": "a", "extra": 1})[0], 400)
        self.assertEqual(self.call("POST", path, {"answer": "  "})[0], 400)
        self.assertEqual(self.call("POST", path, {})[0], 400)
        self.assertEqual(self.call("POST", path, raw=b"{" + b" " * 20000 + b"}")[0], 413)
        self.assertEqual(self.call("GET", path)[0], 405)
        self.assertEqual(self.call("PUT", path, {"answer": "a"})[0], 405)
        self.assertEqual(self.call("POST", f"/afclaude/api/questions/{q['id']}/nope", {})[0], 404)
        self.assertEqual(self.call("POST", "/afclaude/api/questions/999/answer", {"answer": "a"})[0], 404)
        self.assertEqual((self.messages(), store.get_task(self.conn, q["id"])["status"]), ([], "blocked"))
        self.assertFalse(self.wake.is_set())
        os.unlink(self.keyfile)
        self.assertEqual(self.post(q["id"], "answer", {"answer": "a"}, key="")[0], 503)

    def test_decide_has_the_same_rules(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        path = f"/afclaude/api/questions/{q['id']}/decide"
        body = {"choice": "approve", "version": p["version"]}
        self.assertEqual(self.call("POST", path, body, key="wrong")[0], 401)
        self.assertEqual(self.call("POST", path, body, ctype="text/plain")[0], 415)
        self.assertEqual(self.call("POST", path, {**body, "extra": 1})[0], 400)
        self.assertEqual(self.call("POST", path, {"choice": "approve"})[0], 400)          # no version
        self.assertEqual(self.call("POST", path, {**body, "choice": "confirm"})[0], 400)  # not a proposal's choice
        self.assertEqual(self.call("POST", path, {**body, "choice": "edit"})[0], 400)     # edit needs its text
        self.assertEqual((self.entry(p["id"])["state"], store.get_task(self.conn, q["id"])["status"]),
                         ("proposed", "blocked"))

    def test_a_plain_answer_is_stored_on_the_question_and_as_an_owner_inbox_message(self):
        q = self.ask(title="Nightly run", options=["Weekly", "Daily"])
        text = "  Daily,\n but only on weekdays "
        st, r = self.post(q["id"], "answer", {"answer": text})
        self.assertEqual((st, r["question"], r["status"]), (200, q["id"], "done"))
        t = store.get_task(self.conn, q["id"])
        self.assertEqual((t["status"], t["answer"]), ("done", text.strip()))
        (m,) = self.messages()
        self.assertEqual((m["text"], m["created_by"], m["question_id"], m["session_id"], m["status"], m["verdict"]),
                         (text, "owner", q["id"], SID, "pending", None))      # verbatim, waiting for the triage
        self.assertEqual(r["inbox"], m["id"])
        self.assertTrue(self.wake.is_set())                                   # the runner triages it now
        acts = sorted((a["action"], a["actor"], a["via"]) for a in self.audit())
        self.assertIn(("question.answer", "owner", "dashboard"), acts)
        self.assertIn(("inbox.post", "owner", "dashboard"), acts)
        self.assertNotIn(text, json.dumps(self.audit()))                      # D-241: the row is the only copy
        # it is gone from the open list at once, and a second submit of the same text changes nothing
        self.assertEqual(inbox.view(self.conn)["questions"], [])
        self.wake.clear()
        st, r2 = self.post(q["id"], "answer", {"answer": text})
        self.assertEqual((st, r2["inbox"]), (200, None))
        self.assertEqual(len(self.messages()), 1)
        st, r3 = self.post(q["id"], "answer", {"answer": "another"})
        self.assertEqual(st, 409)                                              # answered already: the page reloads

    def test_a_plain_answer_is_triaged_and_delivered_like_any_other_message(self):
        q = self.ask("Which way?", title="Nightly run")
        self.post(q["id"], "answer", {"answer": "Daily"})
        (m,) = self.messages()
        calls = []

        def haiku(text):
            calls.append(text)
            return {"verdict": "note", "reason": "an answer", "cost_usd": 0.001, "usage": None}
        with mock.patch.object(inbox_worker, "triage_call", haiku), \
                mock.patch.object(inbox_worker, "manager_active", return_value=None):
            self.assertEqual(inbox_worker.process(self.conn), {"note": 1})
        self.assertEqual(calls, ["Daily"])
        self.assertEqual(store.get_inbox(self.conn, m["id"])["status"], "noted")
        notes, ids = inbox.notes_insert(self.conn, SID)
        self.assertEqual(ids, [m["id"]])
        self.assertIn(f"#{m['id']} (", notes)
        self.assertIn(f"answer to your question #{q['id']} Nightly run", notes)
        self.assertIn('"Daily"', notes)

    def test_the_delivery_header_names_the_question(self):
        q = self.ask("Which way?", title="Nightly run")
        self.post(q["id"], "answer", {"answer": "Daily"})
        m = self.messages()[0]
        about = inbox.question_about(self.conn, m)
        self.assertEqual(about, f"answer to your question #{q['id']} Nightly run")
        now = inbox.now_message(m, None, about)
        self.assertIn(f"[AFClaude inbox #{m['id']}]", now)
        self.assertIn(about, now)
        self.assertIn('"Daily"', now)
        # the build of a send-keys message (inbox_worker) carries it too; a plain message carries nothing extra
        msg, _ = inbox_worker.build(self.conn, m, "send-keys", "")
        self.assertIn(about, msg)
        plain = actions.perform(self.conn, "inbox.post", {"text": "hello"}, actor="owner", via="dashboard")
        self.assertIsNone(inbox.question_about(self.conn, plain))
        self.assertNotIn("answer to your question", inbox_worker.build(self.conn, plain, "send-keys", "")[0])
        # a held "now" message of the answer, in a start's message
        self.conn.execute("UPDATE inbox_messages SET verdict='now' WHERE id=?", (m["id"],))
        self.conn.commit()
        held, ids = inbox.held_insert(self.conn, SID)
        self.assertEqual(ids, [m["id"]])
        self.assertIn(about, held)

    def test_a_reply_keeps_question_answer_and_reply_until_dismissed(self):
        q = self.ask("Which way?", title="Nightly run")
        self.post(q["id"], "answer", {"answer": "Daily"})
        m = self.messages()[0]
        self.do("inbox.reply", message_id=m["id"], answer="Done: daily.", actor="mcp:" + SID, via="mcp",
                autonomous=True)
        v = inbox.view(self.conn)
        (th,) = v["threads"]
        self.assertEqual((th["id"], th["text"], th["answer"]), (m["id"], "Daily", "Done: daily."))
        self.assertEqual(th["question"], {"id": q["id"], "title": "Nightly run", "text": "Which way?"})
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/{m['id']}/dismiss", {})[0], 200)
        self.assertEqual(inbox.view(self.conn)["threads"], [])


class Decisions(Base):
    def test_approve_applies_the_change_and_writes_no_inbox_row(self):
        a = self.owner_entry("First")
        p = self.proposal(supersedes=[a["id"]])
        q = self.question_of(p["id"])
        st, r = self.post(q["id"], "decide", {"choice": "approve", "version": p["version"]})
        self.assertEqual((st, r["inbox"]), (200, None))
        self.assertEqual((self.entry(p["id"])["state"], self.entry(a["id"])), ("active", None))   # originals deleted
        self.assertEqual(self.entry(p["id"])["confirmed_by"], "owner")
        self.assertEqual(store.get_task(self.conn, q["id"])["answer"], "approve")
        self.assertEqual(self.messages(), [])
        self.assertFalse(self.wake.is_set())
        self.assertEqual(inbox.view(self.conn)["questions"], [])
        self.assertEqual([x["actor"] for x in self.audit() if x["action"] == "question.decide"], ["owner"])

    def test_a_stale_version_is_refused_and_nothing_is_written(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        seen = p["version"]
        self.do("decision.edit", decision=p["id"], summary="Reworded once more.", actor="mcp:" + SID, via="mcp",
                autonomous=True)
        n = len(self.audit())
        st, r = self.post(q["id"], "decide", {"choice": "approve", "version": seen, "note": "ok"})
        self.assertEqual(st, 409)
        self.assertIn("reload", r["error"])
        self.assertEqual((self.entry(p["id"])["state"], store.get_task(self.conn, q["id"])["status"]),
                         ("proposed", "blocked"))
        self.assertEqual((len(self.audit()), self.messages()), (n, []))
        # the view carries the new version: the page reloads and the next try works
        (qv,) = inbox.view(self.conn)["questions"]
        self.assertEqual(qv["decision"]["version"], self.entry(p["id"])["version"])
        self.assertEqual(self.post(q["id"], "decide", {"choice": "approve", "version": qv["decision"]["version"]})[0], 200)

    def test_a_note_is_noted_without_a_triage_and_holds_only_what_the_owner_typed(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        st, r = self.post(q["id"], "decide", {"choice": "reject", "version": p["version"], "note": "not now, thanks"})
        self.assertEqual(st, 200)
        (m,) = self.messages()
        self.assertEqual((m["text"], m["status"], m["verdict"], m["question_id"], m["created_by"]),
                         ("not now, thanks", "noted", "note", q["id"], "owner"))
        self.assertIsNotNone(m["triaged_at"])
        self.assertEqual(r["inbox"], m["id"])
        self.assertFalse(self.wake.is_set())
        with mock.patch.object(inbox_worker, "triage_call", side_effect=AssertionError("Haiku was called")):
            self.assertEqual(inbox_worker.process(self.conn), {})
        # owner_words sees the typed text only, never the choice or the question's wording
        self.assertEqual([e.text for e in owner_words.inbox_messages(self.conn, SID)], ["not now, thanks"])
        # it goes along with the next message into the session, with the choice in the header
        notes, ids = inbox.notes_insert(self.conn, SID)
        self.assertEqual(ids, [m["id"]])
        self.assertIn(f"answer to your question #{q['id']} Approve {p['id']}: Reworded titles (the owner chose: reject)",
                      notes)
        self.assertIn('"not now, thanks"', notes)

    def test_an_edit_text_and_a_note_are_one_message_of_exactly_the_typed_words(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        st, _ = self.post(q["id"], "decide", {"choice": "edit", "version": p["version"],
                                              "text": "Titles stay short and plain.", "note": "closer"})
        self.assertEqual(st, 200)
        (m,) = self.messages()
        self.assertEqual((m["text"], m["status"], m["verdict"]), ("Titles stay short and plain.\n\ncloser", "noted", "note"))
        e = self.entry(p["id"])
        self.assertEqual((e["state"], e["summary"]), ("active", "Titles stay short and plain."))

    def test_confirm_and_drop_an_unconfirmed_task_manager_entry_through_the_endpoint(self):
        ids = []
        for title in ("Mine", "Other"):
            p = self.proposal(title=title)
            self.do("decision.confirm", decision=p["id"])
            # active but unconfirmed: what the seeding left (written as the old code would have)
            self.conn.execute("UPDATE decisions SET confirmed_at = NULL, confirmed_by = NULL WHERE id = ?", (p["id"],))
            self.conn.commit()
            with self.conn:
                ids.append(actions.ask_decision_question(self.conn, actions.Ctx("cli", "cli"), self.entry(p["id"])))
        (qa, qb) = ids
        (va,) = [x for x in inbox.view(self.conn)["questions"] if x["id"] == qa["id"]]
        self.assertEqual((va["decision"]["kind"], va["decision"]["choices"]), ("confirm", ["confirm", "drop"]))
        did_a, did_b = qa["decision_id"], qb["decision_id"]
        st, r = self.post(qa["id"], "decide", {"choice": "confirm", "version": va["decision"]["version"]})
        self.assertEqual((st, r["inbox"]), (200, None))
        self.assertEqual((self.entry(did_a)["confirmed_by"], self.messages()), ("owner", []))
        st, r = self.post(qb["id"], "decide", {"choice": "drop", "version": self.entry(did_b)["version"],
                                              "note": "obsolete"})
        self.assertEqual(st, 200)
        self.assertIsNone(self.entry(did_b))
        (m,) = self.messages()
        self.assertEqual((m["text"], m["status"], m["verdict"]), ("obsolete", "noted", "note"))
        self.assertEqual(inbox.view(self.conn)["questions"], [])

    def test_the_rate_rule_covers_an_answer_and_a_decision_that_writes_a_message(self):
        for i in range(inbox.RATE_MAX):
            self.do("inbox.post", text=f"m{i}", actor="owner", via="dashboard")
        q = self.ask()
        self.assertEqual(self.post(q["id"], "answer", {"answer": "a"})[0], 429)
        p = self.proposal()
        dq = self.question_of(p["id"])
        self.assertEqual(self.post(dq["id"], "decide", {"choice": "approve", "version": p["version"],
                                                       "note": "fine"})[0], 429)
        self.assertEqual(self.entry(p["id"])["state"], "proposed")            # nothing written
        self.assertEqual(self.post(dq["id"], "decide", {"choice": "approve", "version": p["version"]})[0], 200)

    def test_the_actions_are_the_owners_not_mcps_or_an_autonomous_sessions(self):
        q = self.ask()
        p = self.proposal()
        dq = self.question_of(p["id"])
        for via, auto in (("mcp", False), ("mcp", True), ("cli", True)):
            with self.assertRaises(actions.Forbidden):
                self.do("question.answer", task_id=q["id"], answer="a", actor=f"mcp:{SID}", via=via, autonomous=auto)
            with self.assertRaises(actions.Forbidden):
                self.do("question.decide", task_id=dq["id"], choice="approve", decision_version=p["version"],
                        actor=f"mcp:{SID}", via=via, autonomous=auto)
        self.assertEqual((self.messages(), store.get_task(self.conn, q["id"])["status"]), ([], "blocked"))
        # a decision question has no typed answer, a plain one no choice
        with self.assertRaisesRegex(ValueError, "question.decide"):
            self.do("question.answer", task_id=dq["id"], answer="a", actor="owner", via="dashboard")
        with self.assertRaisesRegex(ValueError, "isn't a decision question"):
            self.do("question.decide", task_id=q["id"], choice="approve", decision_version=1, actor="owner",
                    via="dashboard")

    def test_a_task_manager_less_project_is_an_error_not_a_lost_answer(self):
        store.add_project(self.conn, "Orphan")
        self.conn.commit()
        q = self.do("task.ask", question="Who reads this?", project="Orphan")
        st, r = self.post(q["id"], "answer", {"answer": "nobody"})
        self.assertEqual(st, 400)
        self.assertIn("no task-manager session", r["error"])
        self.assertEqual((store.get_task(self.conn, q["id"])["status"], self.messages()), ("blocked", []))


class View(Base):
    def test_every_open_question_shows_in_full_with_its_options_or_choices(self):
        long = "Detail line. " * 80
        plain = self.ask(long + "\nSecond paragraph?", title="Long one", options=["Yes", "No"])
        free = self.ask("Free text only?")
        a = self.owner_entry("First")
        p = self.proposal(supersedes=[a["id"]])
        done = self.ask("Already answered?")
        self.do("task.cancel", task_id=done["id"], reason="settled")
        v = inbox.view(self.conn)
        qs = {x["id"]: x for x in v["questions"]}
        self.assertEqual(sorted(qs), sorted([plain["id"], free["id"], self.question_of(p["id"])["id"]]))
        self.assertEqual([x["id"] for x in v["questions"]], sorted(qs))      # oldest first
        pq = qs[plain["id"]]
        self.assertEqual((pq["text"], pq["title"], pq["options"], pq["decision"], pq["project"]),
                         (long + "\nSecond paragraph?", "Long one", ["Yes", "No"], None, "Mgr"))
        self.assertEqual((qs[free["id"]]["options"], qs[free["id"]]["decision"]), ([], None))
        dq = qs[self.question_of(p["id"])["id"]]
        self.assertEqual(dq["options"], [])                                  # its choices are the decision's
        d = dq["decision"]
        self.assertEqual((d["id"], d["kind"], d["choices"], d["version"], d["summary"]),
                         (p["id"], "proposal", ["approve", "edit", "reject"], p["version"], "A rewording."))
        self.assertIn("First", dq["text"])                                   # the replaced entry, in full (D-279)
        self.assertIn(OWNER_WORDS, dq["text"])
        json.dumps(v)

    def test_an_entry_that_needs_no_decision_any_more_shows_without_choices(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        self.conn.execute("DELETE FROM decisions WHERE id=?", (p["id"],))
        self.conn.commit()
        (qv,) = inbox.view(self.conn)["questions"]
        self.assertEqual((qv["id"], qv["decision"]["kind"], qv["decision"]["choices"], qv["decision"]["version"]),
                         (q["id"], None, [], None))


if __name__ == "__main__":
    unittest.main()
