#!/usr/bin/env python3
"""Offline tests for owner_text.py (D-345, D-347): every owner text is plain words (actions.notice_unreadable
finds nothing), the subjects the alerts doc shows most get their own sentence, any other subject the generic
one (never the raw subject), the push and the pause banner say the same, and notify.notify / failstop.fail
push the owner text while the alerts doc keeps the technical subject and detail."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import claude_auth  # noqa: E402
import failstop  # noqa: E402
import owner_text as ot  # noqa: E402

# subjects from the alerts doc (the most frequent kinds) and the runners' fail-stops: each has its own sentence
KNOWN = {
    "one-off usage review (D-228 chain) did not finish in time for the ETA review": ot.REVIEW,
    "a review did not finish in time": ot.REVIEW,
    "usage reading failed: /usage did not refresh the usage cache": ot.USAGE,
    claude_auth.SUBJECT: ot.LOGIN,
    "host check logged_in failed": ot.LOGIN,
    "keepalive.py crashed: RuntimeError": ot.CRASH,
    "the runner crashed": ot.CRASH,
    "watcher pass failed: KeyError": ot.CRASH,
    "inbox message triage failed": ot.INBOX_MSG,
    "inbox message not delivered": ot.INBOX_MSG,
    "AFClaude DB error (schema): decision.add failed again": ot.DB,
    "budget model failed": ot.BUDGET,
    "start of 39d4e2ae aborted: the host check failed": ot.START,
    "keep-alive did NOT continue 39d4e2ae: preflight failed": ot.START,
    "run timing failed at a session-window start": ot.START,
    "start 7: its PICK row could not be written to run_log": ot.RECORD,
    "hand-off #12 of 39d4e2ae not marked consumed after the reset": ot.RECORD,
}
OTHER = (  # a sentence of their own (not the generic one), checked for plain words below
    "compaction of f2897285 failed: no fresh hand-off before the compaction: the session answered the save prompt",
    "owner role given to machine user 1", "inbox API was not running", "the web service crashed",
    "the previous runner did not exit cleanly", "missed session-window start", "host check disk failed",
    "host check tmux failed", "the new task-manager of AFClaude (39d4e2ae) did not reply",
    "2 agent proposal(s) waiting for review for over 24 h", "deploy: docker compose up failed",
    "run-now request #4 (work_on_now 3) failed", "the nightly task gate was not done",
    "a review hit the usage limit again", "sampler failed", "window_start interrupted by a runner restart",
    "quickview timed out", "AFClaude DB healthy again: automation resumes", "role refusal: task.add for agent",
    "the master key is not usable", "AFCLAUDE_LAYOUT is not valid", "cut-over: the ROLLBACK failed",
)


class Texts(unittest.TestCase):
    def all_texts(self):
        texts = [ot.GENERIC, ot.HAND] + [t for _, t in ot.RULES]
        texts += [ot.matched(f"{job} {what}") for job in ot.JOBS for what in ot.JOB_WHAT]
        return texts

    def test_every_owner_text_is_plain_words(self):
        """D-345/D-347: the same check the owner's notices get (no ids, commands, file names, hashes, code
        names, no 'Nothing to do' line)."""
        for t in self.all_texts():
            self.assertTrue(t)
            self.assertEqual(actions.notice_unreadable("", t), [], t)

    def test_an_install_wide_push_is_never_cut(self):
        for t in self.all_texts():
            self.assertLessEqual(len("AFClaude paused: " + t), ot.PUSH_MAX, t)

    def test_the_frequent_subjects_have_their_own_sentence(self):
        for subject, text in KNOWN.items():
            self.assertEqual(ot.for_subject(subject), text, subject)
        for subject in OTHER:
            self.assertNotEqual(ot.for_subject(subject), ot.GENERIC, subject)

    def test_a_subject_without_a_rule_gets_the_generic_sentence(self):
        for subject in ("something new broke: 39d4e2ae at foo.py:12", "frobnicate_widget failed"):
            self.assertIsNone(ot.matched(subject))
            self.assertEqual(ot.for_subject(subject), ot.GENERIC)

    def test_every_runner_job_has_a_plain_name(self):
        import runner
        self.assertEqual(set(runner.JOBS) - set(ot.JOBS), set())
        self.assertEqual(ot.for_subject("sampler timed out"),
                         "The regular reading of your Claude usage took too long and was stopped.")


class Push(unittest.TestCase):
    def test_a_failstop_says_paused_and_a_project_by_its_name(self):
        self.assertEqual(ot.push_line("AFClaude STOPPED: budget model failed"), "AFClaude paused: " + ot.BUDGET)
        self.assertEqual(ot.push_line("AFClaude STOPPED (Garden): inbox message triage failed"),
                         ot.fit("AFClaude paused work on Garden: " + ot.INBOX_MSG))
        self.assertEqual(ot.push_line("host check disk failed"), "AFClaude: The server is running low on disk space.")

    def test_fit_cuts_at_a_sentence_else_a_word(self):
        self.assertEqual(ot.fit("One two three. Four five six seven.", 20), "One two three.")
        self.assertEqual(ot.fit("aa bb cc dd ee ff gg", 10), "aa bb cc…")
        self.assertEqual(ot.fit("short", 10), "short")


class Banner(unittest.TestCase):
    def test_failstop_entries_one_sentence_per_cause(self):
        reason = ("21.10. 23:45 keepalive: usage reading failed: /usage did not refresh the usage cache | also: "
                  "22.10. 05:16 keepalive: keepalive.py crashed: RuntimeError | also: 22.10. 06:00 dispatcher: "
                  "dispatcher.py crashed: KeyError | also: 22.10. 07:00 runner: brand new thing...")
        self.assertEqual(ot.for_pause(reason, "runner:keepalive"), [ot.USAGE, ot.CRASH, ot.GENERIC])

    def test_other_reasons(self):
        self.assertEqual(ot.for_pause("restored from a backup of 2026-10-01: check and resume (D-173)", "cli"),
                         [ot.matched("restored from a backup")])
        self.assertEqual(ot.for_pause("the pause setting could not be read (OperationalError: locked)", None),
                         [ot.matched("no database at x")])
        self.assertEqual(ot.for_pause("keepalive: start aborted", "runner:keepalive"), [ot.GENERIC])
        self.assertEqual(ot.for_pause(None), [ot.GENERIC])
        # by hand: plain words are shown as written, anything else is HAND
        self.assertEqual(ot.for_pause("I am moving the server tonight", "owner"), ["I am moving the server tonight"])
        self.assertEqual(ot.for_pause("held for D-283 review of keepalive.py", "cli"), [ot.HAND])


class Delivery(unittest.TestCase):
    """notify.notify and failstop.fail: the push carries the owner text, the alerts doc the technical subject and
    detail plus the line the owner got. Nothing real is pushed or written (push and append_note are recorders)."""
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        import notify
        self.pushed, self.notes = [], []
        for p in (mock.patch.object(notify, "push", side_effect=lambda text: self.pushed.append(text) or "test push"),
                  mock.patch("project_docs.append_note",
                             side_effect=lambda doc, text, actor: self.notes.append((doc, text)))):
            p.start()
            self.addCleanup(p.stop)

    def test_notify_pushes_the_owner_text(self):
        import notify
        self.assertEqual(notify.notify("owner role given to machine user 1", "cli granted it (D-172)."), "test push")
        self.assertEqual(self.pushed, ["AFClaude: " + ot.matched("owner role given to x")])
        (doc, text), _progress = self.notes
        self.assertEqual(doc, "alerts")
        self.assertIn("owner role given to machine user 1\n  cli granted it (D-172).\n", text)
        self.assertIn(f"for the owner: {self.pushed[0]}; push: test push", text)

    def test_failstop_pushes_plain_words_and_keeps_the_detail_for_the_task_manager(self):
        out = failstop.fail("keepalive", "compaction of 39d4e2ae failed: no fresh hand-off", "tmux: no client\nTraceback",
                            log=lambda line: None)
        self.assertTrue(out["paused"] and out["alerted"])
        self.assertEqual(self.pushed, [ot.fit("AFClaude paused: " + ot.for_subject("compaction of x failed"))])
        self.assertEqual(actions.notice_unreadable("", self.pushed[0]), [])
        alert = self.notes[0][1]
        self.assertIn("AFClaude STOPPED: compaction of 39d4e2ae failed: no fresh hand-off", alert)
        self.assertIn("tmux: no client", alert)
        self.assertIn(failstop.RESUME_CMD, alert)          # the fail-stop's own text is unchanged (D-234/D-235)
        self.assertTrue(failstop.paused()["paused"])


if __name__ == "__main__":
    unittest.main()
