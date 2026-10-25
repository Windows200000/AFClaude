#!/usr/bin/env python3
"""Offline tests for the prompt registry (prompts.py, phase 4a P1 of docs/phase4_plan.md; temp DBs):
the registry pinned to prompts/*.md, the strict render, the project variables (D-197) and their
audited actions, the migrate step, and the before/after proof: every prompt kind through every
sender path renders byte for byte what the old loaders gave."""
import testenv  # noqa: F401  (first: a temp DB, no live settings)

import glob
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import actions
import inbox
import keepalive as ka
import nightly_tasks
import prompts
import store
import usage_review as ur

UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
SID = "11111111-2222-3333-4444-555555555555"


def files():
    return sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(HERE, "prompts", "*.md"))
                  if not p.endswith("README.md"))


class RegistryTest(unittest.TestCase):
    def test_every_file_registered_and_back(self):
        self.assertEqual(sorted(prompts.PROMPTS), files())

    def test_placeholders_match_the_files(self):
        for name, spec in prompts.PROMPTS.items():
            with self.subTest(name=name):
                self.assertEqual(prompts.placeholders(prompts.default(name)), set(spec.system) | set(spec.project))
                self.assertEqual(len(set(spec.system)), len(spec.system))
                self.assertTrue(set(spec.project) <= set(prompts.PROJECT_VARS))
                self.assertTrue(spec.used_by)

    def test_readme_names_every_prompt(self):
        with open(os.path.join(HERE, "prompts", "README.md")) as fh:
            readme = fh.read()
        for name in prompts.PROMPTS:
            self.assertIn(f"`{name}.md`", readme)

    def test_seed_is_valid(self):
        for k, v in prompts.CHECKOUT_SEED.items():
            self.assertEqual(prompts.check_var(k, v), k)

    def test_default_is_the_file_byte_for_byte(self):
        for name in prompts.PROMPTS:
            with open(os.path.join(HERE, "prompts", name + ".md")) as fh:
                self.assertEqual(prompts.default(name), fh.read())

    def test_unknown_name(self):
        for bad in ("nope", "continue.md", "../continue", None, ""):
            with self.assertRaises(prompts.UnknownPrompt):
                prompts.default(bad)
        self.assertTrue(issubclass(prompts.UnknownPrompt, OSError))   # nightly_tasks.add reports "no prompt"

    def test_only_prompts_py_reads_the_prompt_files(self):
        """The senders load through prompts.default: no other module builds the path of a prompt
        file (prompts.path does; export_quickview.py publishes them; actions.py's deploy import
        lists the directory)."""
        pat = re.compile(r"""os\.path\.join\([^)]*(["']prompts["']|PROMPTS_DIR)[^)]*\.md""")
        readers = set()
        for p in glob.glob(os.path.join(HERE, "*.py")):
            b = os.path.basename(p)
            if b.startswith("test_") or b == "prompts.py":
                continue
            with open(p) as fh:
                if pat.search(fh.read()):
                    readers.add(b)
        self.assertEqual(readers, {"export_quickview.py"})


class RenderTest(unittest.TestCase):
    def test_strict_system_variables(self):
        self.assertEqual(prompts.render("continue", reason="r", progress="P.md"),
                         prompts.default("continue").format(reason="r", progress="P.md"))
        with self.assertRaisesRegex(ValueError, r"missing variable\(s\) \['progress'\]"):
            prompts.render("continue", reason="r")
        with self.assertRaisesRegex(ValueError, r"unknown \['x'\]"):
            prompts.render("continue", reason="r", progress="p", x=1)
        self.assertEqual(prompts.render("manager"), prompts.default("manager").format())

    def test_project_variables(self):
        reg = dict(prompts.PROMPTS, t=prompts.Prompt(("a",), ("public_guard", "project_notes"), used_by="test"))
        with mock.patch.object(prompts, "PROMPTS", reg), \
                mock.patch.object(prompts, "default", return_value="{a} [{public_guard}] [{project_notes}] {{x}}"):
            self.assertEqual(prompts.render("t", a=1), "1 [] [] {x}")
            self.assertEqual(prompts.render("t", {"public_guard": "g", "other": "ignored"}, a=1), "1 [g] [] {x}")
            with self.assertRaises(ValueError):
                prompts.render("t", {"public_guard": "g"}, a=1, public_guard="system can't fill it")

    def test_check_var(self):
        with self.assertRaisesRegex(ValueError, "unknown prompt variable"):
            prompts.check_var("nope", "x")
        with self.assertRaisesRegex(ValueError, "must be text"):
            prompts.check_var("project_notes", 3)
        with self.assertRaisesRegex(ValueError, "too long"):
            prompts.check_var("public_guard", "x" * 501)
        prompts.check_var("project_notes", "x" * 2000)


class ProjectVarsTest(unittest.TestCase):
    def setUp(self):
        self.db = testenv.fresh_db(self)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        self.p = store.add_project(self.conn, "Other", path="/srv/other")

    def do(self, _action, /, actor="cli", via="cli", **params):
        return actions.perform(self.conn, _action, params, actor=actor, via=via)

    def audits(self, action):
        return [dict(r) for r in self.conn.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def test_set_reset_audited_and_versioned(self):
        r = self.do("prompt_var.set", project="Other", name="public_guard", value="tools/guard.py")
        self.assertEqual((r["value"], r["version"], r["updated_by"]), ("tools/guard.py", 1, "cli"))
        self.assertEqual(prompts.project_values(self.conn, self.p["id"])["public_guard"], "tools/guard.py")
        self.assertEqual(prompts.project_values(self.conn, self.p["id"])["project_notes"], "")
        self.do("prompt_var.set", project="Other", name="public_guard", value="tools/guard.py")   # no-op
        self.assertEqual(len(self.audits("prompt_var.set")), 1)
        with self.assertRaises(actions.Conflict):
            self.do("prompt_var.set", project="Other", name="public_guard", value="y", version=0)
        r = self.do("prompt_var.set", project=self.p["id"], name="public_guard", value="y", version=1)
        self.assertEqual(r["version"], 2)
        r = self.do("prompt_var.reset", project="Other", name="public_guard")
        self.assertIsNone(r["value"])
        self.assertEqual(r["version"], 3)
        self.assertEqual(prompts.project_values(self.conn, self.p["id"])["public_guard"], "")
        self.do("prompt_var.reset", project="Other", name="public_guard")    # already reset: no-op
        a = self.audits("prompt_var.reset")
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0]["target_type"], a[0]["target_id"]), ("prompt_var", f"{self.p['id']}:public_guard"))
        self.assertIn('"y"', a[0]["before"])

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.do("prompt_var.set", project="Other", name="nope", value="x")
        with self.assertRaises(store.NotFound):
            self.do("prompt_var.set", project="Ghost", name="public_guard", value="x")
        for name, params in (("prompt_var.set", {"project": "Other", "name": "public_guard", "value": "x"}),
                             ("prompt_var.reset", {"project": "Other", "name": "public_guard"}),
                             ("prompt_var.migrate", {})):
            with self.assertRaises(actions.Forbidden):     # owner only: never an autonomous session
                actions.perform(self.conn, name, params, actor="mcp:x", via="mcp", autonomous=True)
        self.assertEqual(store.list_prompt_vars(self.conn), [])

    def test_migrate_seeds_the_checkout_project_once(self):
        self.assertEqual(prompts.migrate_plan(self.conn), [])          # no project at this checkout
        r = self.do("prompt_var.migrate", actor="runner:deploy")
        self.assertEqual(r["seeded"], [])
        self.assertEqual(self.audits("prompt_var.migrate"), [])
        me = store.add_project(self.conn, "Here", path=prompts.HERE)
        store.add_project(self.conn, "Below", path=os.path.join(prompts.HERE, "sub"))
        self.do("prompt_var.set", project="Here", name="goals_location", value="docs/GOALS.md")
        self.do("prompt_var.set", project="Here", name="questions_location", value="x")
        self.do("prompt_var.reset", project="Here", name="questions_location")
        r = self.do("prompt_var.migrate", actor="runner:deploy")
        self.assertEqual(sorted(w["name"] for w in r["seeded"]), ["public_guard", "worktree_files"])
        vals = prompts.project_values(self.conn, me["id"])
        self.assertEqual(vals["public_guard"], prompts.CHECKOUT_SEED["public_guard"])
        self.assertEqual(vals["worktree_files"], prompts.CHECKOUT_SEED["worktree_files"])
        self.assertEqual(vals["goals_location"], "docs/GOALS.md")      # set by hand: kept
        self.assertEqual(vals["questions_location"], "")               # reset by hand: kept
        self.assertEqual({r["project_id"] for r in store.list_prompt_vars(self.conn)}, {me["id"]})
        self.assertEqual(len(self.audits("prompt_var.migrate")), 1)
        self.assertEqual(self.do("prompt_var.migrate")["seeded"], [])   # idempotent, no audit row
        self.assertEqual(len(self.audits("prompt_var.migrate")), 1)

    def test_cli(self):
        with mock.patch("sys.stdout") as out:
            self.assertEqual(prompts.main(["set-var", "Other", "project_notes", "hi"]), 0)
            self.assertEqual(prompts.main(["vars", "--project", "Other"]), 0)
            self.assertEqual(prompts.main(["render", "continue", "--set", "reason=r", "--set", "progress=p"]), 0)
            self.assertEqual(prompts.main(["migrate", "--dry-run"]), 0)
            self.assertEqual(prompts.main(["list"]), 0)
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn('"project_notes": "hi"', text)
        self.assertIn(prompts.default("continue").format(reason="r", progress="p"), text)
        with mock.patch("sys.stderr"):
            self.assertEqual(prompts.main(["render", "continue", "--set", "reason=r"]), 1)
            self.assertEqual(prompts.main(["set-var", "Other", "nope", "x"]), 1)


# ---------------------------------------------------------------- before / after

def _legacy_ka(name):              # keepalive.load_prompt before phase 4a
    with open(os.path.join(ka.HERE, "prompts", name + ".md")) as fh:
        return fh.read()


def _legacy_nightly(name):         # nightly_tasks._prompt before phase 4a
    with open(os.path.join(HERE, "prompts", name + ".md")) as fh:
        return fh.read()


def _legacy_inbox(name):           # inbox.prompt before phase 4a
    return actions.prompt_default(name + ".md")


class SameTextTest(unittest.TestCase):
    """Every prompt kind through every sender path: the new loaders (prompts.default) give the
    same bytes as the old ones did, and every path really goes through the registry."""

    def setUp(self):
        self.db = testenv.fresh_db(self)
        conn = store.connect(self.db)
        try:
            h = actions.perform(conn, "handoff.write", {"session_id": SID, "body": {
                "summary": "s", "done": ["a"], "in_progress": [], "next_steps": ["b"]}}, actor="cli", via="cli")
            self.handoff = h["id"]
            for text, verdict in (("a note", "note"), ("a question", "now")):
                m = actions.perform(conn, "inbox.post", {"text": text, "session_id": SID}, actor="owner",
                                    via="dashboard")
                actions.perform(conn, "inbox.triage", {"message_id": m["id"], "verdict": verdict, "reason": "r"},
                                actor="runner:inbox", via="runner")
        finally:
            conn.close()

    def renders(self):
        out = {}
        end = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)
        insert = "INSERT {braces} text"
        kinds = {"continue": {"reason": "r", "progress": "P.md"}, "last_mile": {"reason": "r", "progress": "P.md"},
                 "fillup": {"reason": "r", "progress": "P.md", "remaining": "5", "reset": "04:00"},
                 "compact_after": {"resume": "R"}, "project_start": {"title": "T", "desc": "D", "slug": "s"},
                 "continue_foreign": {"reason": "r", "context": ""},
                 "inbox_now": {"id": 1, "at": "21.10. 10:00", "text": "t", "notes": ""}}
        for name, fields in kinds.items():
            for afclaude in (True, False):
                for manager in (True, False):
                    for hooked in (True, False):
                        for ins in (None, insert):
                            out[f"session_message {name} {afclaude} {manager} {hooked} {ins}"] = ka.session_message(
                                name, afclaude=afclaude, manager=manager, insert=ins, hooked=hooked, **fields)
        for part in ka.RULES_PARTS:
            out[f"standing_rules {part}"] = ka.standing_rules(part)
        out["compact_command"] = ka.compact_command()
        for trigger in ka.TRIGGER_TEXT:
            out[f"save_text {trigger}"] = ka.save_text(SID, trigger)
            out[f"compact_resume {trigger}"] = ka.compact_resume_text(
                SID, {"status": "saved", "handoff_id": self.handoff}, trigger)[0]
        out["save_retry_text"] = ka.save_retry_text(SID, 8)
        for chained in (end, end + timedelta(hours=5)):
            with mock.patch.object(ka, "next_start_after", return_value=chained):
                out[f"run_pace {chained}"] = ka.run_pace_text(SID, {"run_end": end}, end - timedelta(hours=3),
                                                              {"session": {"resets_at": end}})
        conn = store.connect(self.db)
        try:
            out["inbox notes"] = inbox.notes_insert(conn, SID)[0]
            out["inbox held"] = inbox.held_insert(conn, SID)[0]
        finally:
            conn.close()
        out["inbox triage"] = inbox.triage_prompt("a {message}")
        out["inbox now"] = inbox.now_message({"id": 7, "created_at": "2026-10-21T08:00:00Z", "text": "x\ny"}, "N")
        task = {"name": "t1", "title": "T", "report": "review:t1", "fields": {"date": "2026-10-23", "since": "x",
                                                                              "report": "review:t1"}}
        for ps in (list(ur.MONTHLY_PROMPTS), ["stage_eta_review_once", "stage_eta_review_rerun"],
                   ["compaction_handoff_review"]):
            with mock.patch.object(nightly_tasks, "get", return_value=dict(task, prompts=ps)):
                out[f"nightly brief {ps}"] = nightly_tasks.brief("t1")
        with mock.patch.object(nightly_tasks, "due", return_value=[dict(task, prompts=[])]):
            out["nightly due"] = nightly_tasks.start_insert(end)[0]
        sent = []
        with mock.patch.object(ka, "fire", side_effect=lambda sid, cwd, msg, *a, **k: sent.append(msg) or
                               (0, "started", "")), \
                mock.patch.object(ur, "record_review"), mock.patch.object(ka, "log"), \
                mock.patch.object(ka, "progress_note"), mock.patch.dict(ka.LAUNCH):
            for which, extra in (("usage", None), ("stage-eta", None), ("stage-eta", "after usage")):
                ur.run_once({"runs": []}, end, which, extra=extra)
        extra = ka.load_prompt("stage_eta_review_once_after_usage").format(report="R")
        for i, msg in enumerate(sent):
            out[f"usage_review one-off {i}"] = msg
        out["usage_review chain extra"] = extra
        return out

    def test_byte_identical_before_and_after(self):
        after = self.renders()
        with mock.patch.object(ka, "load_prompt", _legacy_ka), \
                mock.patch.object(nightly_tasks, "_prompt", _legacy_nightly), \
                mock.patch.object(inbox, "prompt", _legacy_inbox):
            before = self.renders()
        self.assertEqual(sorted(before), sorted(after))
        self.assertGreater(len(after), 120)
        for k in after:
            with self.subTest(kind=k):
                self.assertIsInstance(after[k], str)
                self.assertEqual(after[k].encode(), before[k].encode())
        import usage_sampler
        self.assertEqual(usage_sampler.HAIKU_PROMPT, _legacy_nightly("usage_haiku").rstrip("\n"))

    def test_every_path_goes_through_the_registry(self):
        loaded = []
        real = prompts.default

        def spy(name):
            loaded.append(name)
            return real(name)
        with mock.patch.object(prompts, "default", spy):
            self.renders()
        used = set(loaded)
        senders = set(prompts.PROMPTS) - {"task_start", "usage_haiku"}   # unused since D-205; read at import
        self.assertEqual(senders - used, set())


if __name__ == "__main__":
    unittest.main()
