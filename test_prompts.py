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


class BundleTest(unittest.TestCase):
    """prompts.BUNDLES / prompts.bundle (phase 4a P2, §3.2)."""

    def test_every_part_names_a_real_prompt(self):
        for kind, b in prompts.BUNDLES.items():
            with self.subTest(kind=kind):
                self.assertTrue(b.used_by)
                self.assertTrue(b.parts)
                for part in b.parts:
                    self.assertEqual(sum(bool(x) for x in (part.prompt, part.insert, part.rules)), 1,
                                     "a part is exactly one of prompt / insert / rules")
                    if part.prompt:
                        self.assertIn(part.prompt, prompts.PROMPTS)

    def test_unknown_bundle(self):
        for bad in ("nope", None, "", 3):
            with self.assertRaises(prompts.UnknownBundle):
                prompts.bundle(bad)
        self.assertTrue(issubclass(prompts.UnknownBundle, LookupError))

    def test_flat_joins_one_line_non_flat_keeps_paragraphs(self):
        msg = prompts.bundle("continue", reason="r", progress="P.md", insert="x\ny", hooked=True)
        self.assertNotIn("\n", msg)
        rules = prompts.bundle("manager_rules", part=1, what="the task-manager rules")
        self.assertIn("\n\n", rules)

    def test_rules_tail_variants(self):
        # hooked + manager: the pointer alone; manager=False: guard_respect alone (neutral message,
        # e.g. the user's own session); the exact byte-for-byte proof against the old code is
        # SameTextTest below.
        base = dict(reason="r", progress="P.md")
        hooked_manager = prompts.bundle("continue", hooked=True, manager=True, **base)
        self.assertIn(prompts.default("rules_pointer").strip(), hooked_manager)
        self.assertNotIn(prompts.default("manager").strip(), hooked_manager)
        neutral = prompts.bundle("continue_foreign", manager=False, reason="r", context="")
        self.assertIn(prompts.default("guard_respect").strip(), neutral)
        self.assertNotIn(prompts.default("manager").strip(), neutral)

    def test_system_vars_routed_per_part_not_shared_blindly(self):
        """A bundle with several prompt parts: one **system dict serves all of them, each part
        using only the names it declares (manager_rules: session_rules needs part/what,
        manager needs nothing)."""
        text = prompts.bundle("manager_rules", part=2, what="the AFClaude-repo rules", unrelated="ignored by both")
        self.assertIn("the AFClaude-repo rules", text)

    def test_name_keyed_system_variable_does_not_collide(self):
        """nightly_task.md declares a system variable literally called "name"; render()/bundle()
        take `name`/`kind` positional-only so a **fields dict with a "name" key still works
        (nightly_tasks.brief passes the task's own fields straight through)."""
        text = prompts.bundle("nightly_task", name="t1", title="T", report="review:t1")
        self.assertEqual(text, prompts.default("nightly_task").format(name="t1", title="T", report="review:t1")
                         .strip())
        self.assertEqual(prompts.render("nightly_task", name="t1", title="T", report="review:t1"),
                         prompts.default("nightly_task").format(name="t1", title="T", report="review:t1"))

    def test_bundle_kinds_match_the_known_sender_paths(self):
        """Every BUNDLES kind (§3.2) is reachable from one of the senders the plan names: a
        session_message kind, a compaction prompt sent as is, a standing_rules part
        (ka.STANDING_RULES_BUNDLES), a usage_review one-off (ur.ONLY_BUNDLES), the nightly task
        head, or a Haiku judge (documented for F8/D-111, not sent through a bundle of its own)."""
        session_message_kinds = {"continue", "fillup", "last_mile", "continue_foreign", "project_start",
                                 "manager_first_start", "task_start", "compact_after", "compact_after_run", "inbox_now"}
        compaction_kinds = {"session_end_save", "session_end_save_retry", "session_end_save_refresh", "compact"}
        other = (set(ka.STANDING_RULES_BUNDLES.values()) | set(ur.ONLY_BUNDLES.values())
                | {"nightly_task", "usage_haiku", "inbox_triage"})
        self.assertEqual(set(prompts.BUNDLES), session_message_kinds | compaction_kinds | other)

    def test_cli_bundles_and_bundle(self):
        with mock.patch("sys.stdout") as out:
            self.assertEqual(prompts.main(["bundles"]), 0)
            self.assertEqual(prompts.main(["bundle", "continue", "--set", "reason=r", "--set", "progress=p",
                                           "--set", "session_stop_pct=95"]), 0)
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("continue", text)
        self.assertIn(" ".join(prompts.default("manager").split()), text)   # the bundle is flat: squashed
        with mock.patch("sys.stderr"):
            self.assertEqual(prompts.main(["bundle", "nope"]), 1)


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
        reg = dict(prompts.PROMPTS, t=prompts.Prompt(("a",), ("public_repo_rule", "project_notes"), used_by="test"))
        with mock.patch.object(prompts, "PROMPTS", reg), \
                mock.patch.object(prompts, "default", return_value="{a} [{public_repo_rule}] [{project_notes}] {{x}}"):
            self.assertEqual(prompts.render("t", a=1), "1 [] [] {x}")
            self.assertEqual(prompts.render("t", {"public_repo_rule": "g", "other": "ignored"}, a=1), "1 [g] [] {x}")
            with self.assertRaises(ValueError):
                prompts.render("t", {"public_repo_rule": "g"}, a=1, public_repo_rule="system can't fill it")

    def test_check_var(self):
        with self.assertRaisesRegex(ValueError, "unknown prompt variable"):
            prompts.check_var("nope", "x")
        with self.assertRaisesRegex(ValueError, "must be text"):
            prompts.check_var("project_notes", 3)
        with self.assertRaisesRegex(ValueError, "too long"):
            prompts.check_var("goals_location", "x" * 501)
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
        r = self.do("prompt_var.set", project="Other", name="public_repo_rule", value="tools/guard.py")
        self.assertEqual((r["value"], r["version"], r["updated_by"]), ("tools/guard.py", 1, "cli"))
        self.assertEqual(prompts.project_values(self.conn, self.p["id"])["public_repo_rule"], "tools/guard.py")
        self.assertEqual(prompts.project_values(self.conn, self.p["id"])["project_notes"], "")
        self.do("prompt_var.set", project="Other", name="public_repo_rule", value="tools/guard.py")   # no-op
        self.assertEqual(len(self.audits("prompt_var.set")), 1)
        with self.assertRaises(actions.Conflict):
            self.do("prompt_var.set", project="Other", name="public_repo_rule", value="y", version=0)
        r = self.do("prompt_var.set", project=self.p["id"], name="public_repo_rule", value="y", version=1)
        self.assertEqual(r["version"], 2)
        r = self.do("prompt_var.reset", project="Other", name="public_repo_rule")
        self.assertIsNone(r["value"])
        self.assertEqual(r["version"], 3)
        self.assertEqual(prompts.project_values(self.conn, self.p["id"])["public_repo_rule"], "")
        self.do("prompt_var.reset", project="Other", name="public_repo_rule")    # already reset: no-op
        a = self.audits("prompt_var.reset")
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0]["target_type"], a[0]["target_id"]), ("prompt_var", f"{self.p['id']}:public_repo_rule"))
        self.assertIn('"y"', a[0]["before"])

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.do("prompt_var.set", project="Other", name="nope", value="x")
        with self.assertRaises(store.NotFound):
            self.do("prompt_var.set", project="Ghost", name="public_repo_rule", value="x")
        for name, params in (("prompt_var.set", {"project": "Other", "name": "public_repo_rule", "value": "x"}),
                             ("prompt_var.reset", {"project": "Other", "name": "public_repo_rule"}),
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
        self.assertEqual(sorted(w["name"] for w in r["seeded"]),
                         ["deploy_rule", "docs_rule", "progress_location", "project_notes", "public_repo_rule",
                          "worktree_rule"])
        vals = prompts.project_values(self.conn, me["id"])
        for k in ("public_repo_rule", "worktree_rule", "project_notes", "docs_rule", "deploy_rule"):
            self.assertEqual(vals[k], prompts.CHECKOUT_SEED[k])
        self.assertEqual(vals["goals_location"], "docs/GOALS.md")      # set by hand: kept
        self.assertEqual(vals["questions_location"], prompts.PROJECT_VARS["questions_location"].default)   # reset: kept
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


# manager_afclaude.md as it was before P3 (kept until P5 removes it): the text AFClaude's own rules
# must still render to, byte for byte. Its one absolute path was the live checkout's.
OLD_AFCLAUDE_RULES = os.path.join(HERE, "testdata", "manager_afclaude_before_p3.md")
OLD_CHECKOUT = "/mnt/BlockVolume/Claude/work/AFClaude"


def old_afclaude_rules(system):
    with open(OLD_AFCLAUDE_RULES) as fh:
        text = fh.read()
    return text.replace(OLD_CHECKOUT, system["afclaude_dir"]).format(session_stop_pct=system["session_stop_pct"])


def seed_checkout(conn):
    """The AFClaude project at this checkout, with the migrate step's seed values (the deploy's)."""
    p = store.add_project(conn, "AFClaude", path=prompts.HERE)
    actions.perform(conn, "prompt_var.migrate", {}, actor="runner:deploy", via="runner")
    return p


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
            seed_checkout(conn)
        finally:
            conn.close()

    def renders(self):
        out = {}
        end = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)
        insert = "INSERT {braces} text"
        kinds = {"continue": {"reason": "r", "progress": "P.md"}, "last_mile": {"reason": "r", "progress": "P.md"},
                 "fillup": {"reason": "r", "progress": "P.md", "remaining": "5", "reset": "04:00"},
                 "compact_after": {"resume": "R"}, "project_start": {"title": "T", "desc": "D", "slug": "s"},
                 "manager_first_start": {"project": "N", "path": "/p", "desc": "D", "progress": "P.md"},
                 "compact_after_run": {"resume": "R", "threshold": "150k", "started": "23:40 CET", "reason": "r",
                                       "budget": "b", "pace": "p"},
                 "continue_foreign": {"reason": "r", "context": ""},
                 "inbox_now": {"id": 1, "at": "21.10. 10:00", "text": "t", "notes": ""}}
        for name, fields in kinds.items():
            # AFClaude's own text only: another project's inline rules are P3's change (SecondProjectTest)
            for manager in (True, False):
                for hooked in (True, False):
                    for ins in (None, insert):
                        out[f"session_message {name} {manager} {hooked} {ins}"] = ka.session_message(
                            name, manager=manager, insert=ins, hooked=hooked, **fields)
        out["run_project"] = ka.load_prompt("run_project").format(project="N")   # a fragment, as keepalive uses it
        for part in ka.RULES_PARTS:
            out[f"standing_rules {part}"] = ka.standing_rules(part)
        out["compact_command"] = ka.compact_command()
        for trigger in ka.TRIGGER_TEXT:
            out[f"save_text {trigger}"] = ka.save_text(SID, trigger)
            out[f"compact_resume {trigger}"] = ka.compact_resume_text(
                SID, {"status": "saved", "handoff_id": self.handoff}, trigger, ka.PROGRESS_FILE)[0]   # 3c S1: as before
        out["save_retry_text"] = ka.save_retry_text(SID, 8)
        out["save_refresh_text"] = ka.save_refresh_text(SID, 9, "a task notification (from 01:10 CET)")
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
        real_render = prompts.render

        def legacy_render(name, project_values=None, /, **system):    # P3: manager_project.md = the old file
            if name == "manager_project":
                return old_afclaude_rules(system)
            return real_render(name, project_values, **system)
        with mock.patch.object(ka, "load_prompt", _legacy_ka), \
                mock.patch.object(nightly_tasks, "_prompt", _legacy_nightly), \
                mock.patch.object(inbox, "prompt", _legacy_inbox), \
                mock.patch.object(prompts, "render", legacy_render):
            before = self.renders()
        self.assertEqual(sorted(before), sorted(after))
        self.assertGreater(len(after), 60)
        # the fixture really is in play: the AFClaude rules are in the inline and the hook texts
        inline = after[f"session_message continue True False None"]
        self.assertIn(" ".join(old_afclaude_rules({"afclaude_dir": prompts.HERE, "session_stop_pct": "95"}).split()),
                      inline)
        self.assertIn(old_afclaude_rules({"afclaude_dir": prompts.HERE, "session_stop_pct": "95"}).strip(),
                      after["standing_rules afclaude"])
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


class SecondProjectTest(unittest.TestCase):
    """Phase 4a P3: manager_project.md is the generic template. A second project renders it with its
    own values (or the defaults), the install's command paths and none of AFClaude's own lines;
    AFClaude's text is pinned byte for byte in SameTextTest."""

    def setUp(self):
        self.db = testenv.fresh_db(self)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        self.repo = os.path.join(os.path.dirname(HERE), "someone-else")
        self.p = store.add_project(self.conn, "Other", path=self.repo)
        self.mine = seed_checkout(self.conn)
        self.afclaude_lines = [prompts.CHECKOUT_SEED[k] for k in
                               ("public_repo_rule", "worktree_rule", "project_notes", "deploy_rule")]

    def message(self, repo, **kw):
        return ka.session_message("continue", reason="r", progress="P.md", hooked=False, repo=repo, **kw)

    def test_a_second_project_gets_its_own_values_and_none_of_afclaudes(self):
        for k, v in (("public_repo_rule", "- The repo is public: run tools/guard.sh."),
                     ("goals_location", "docs/goals.md")):
            actions.perform(self.conn, "prompt_var.set", {"project": "Other", "name": k, "value": v},
                            actor="cli", via="cli")
        msg = self.message(self.repo)
        self.assertIn("The repo is public: run tools/guard.sh.", msg)
        self.assertIn("(docs/goals.md: one short line per goal", msg)
        self.assertIn("and OPEN_QUESTIONS.md (only things", msg)                # the default
        rules = self.rules()
        for name in ("PROGRESS.md", "GOALS.md"):       # (manager.md, a different template, still names PROGRESS.md)
            self.assertNotIn(name, rules)
        self.assertIn("docs/goals.md", rules)
        for line in self.afclaude_lines:
            self.assertNotIn(" ".join(line.split()), msg)
        for word in ("tools/check_public.py", "tools/deploy.py", "ka_resume.sh", "store.py once migrated"):
            self.assertNotIn(word, msg)
        # its commands are typed from its own directory: the install's, absolute
        self.assertIn(f"`python3 {prompts.HERE}/keepalive.py --decide`", msg)
        self.assertIn(f"python3 {prompts.HERE}/usage_report.py", msg)
        self.assertNotIn("`python3 keepalive.py", msg)
        self.assertNotIn("\n", msg)

    def rules(self):
        """manager_project.md as rendered for the second project (its stored values, else defaults)."""
        return prompts.render("manager_project", prompts.project_values(self.conn, self.p["id"]),
                              afclaude_dir=prompts.HERE, session_stop_pct=90, tools="")

    def test_the_defaults_name_none_of_afclaudes_files(self):
        plain = self.rules()
        for name in ("PROGRESS.md", "GOALS.md"):
            self.assertNotIn(name, plain)
        self.assertIn("(the project's goals doc: one short line per goal", plain)
        self.assertEqual(plain.count("(state in the project's progress doc)"), 2)
        self.assertEqual(self.message(self.repo).count("(state in the project's progress doc)"), 2)

    def test_afclaudes_own_commands_stay_relative(self):
        mine = self.message(None)
        self.assertIn("`python3 keepalive.py --decide`", mine)
        self.assertIn("python3 keepalive.py --run-stopped", mine)
        self.assertEqual(prompts.tool_vars(prompts.HERE)["tools"], "")
        self.assertEqual(prompts.tool_vars(None), {"afclaude_dir": prompts.HERE, "tools": ""})
        self.assertEqual(prompts.tool_vars(self.repo)["tools"], os.path.realpath(prompts.HERE) + "/")
        sub = os.path.join(prompts.HERE, ".claude", "worktrees", "w")     # a worktree is not the checkout
        self.assertEqual(prompts.tool_vars(sub)["tools"], os.path.realpath(prompts.HERE) + "/")

    def test_project_values_follow_the_directory(self):
        self.assertEqual(prompts.project_values_at(self.conn, self.repo)["public_repo_rule"], "")
        self.assertEqual(prompts.project_values_at(self.conn, os.path.join(self.repo, "sub"))["docs_rule"],
                         prompts.PROJECT_VARS["docs_rule"].default)           # the closest parent project
        self.assertEqual(prompts.project_values_at(self.conn)["deploy_rule"], prompts.CHECKOUT_SEED["deploy_rule"])
        unknown = prompts.project_values_at(self.conn, "/nowhere/at/all")      # no project: the defaults
        self.assertEqual(unknown, {k: v.default for k, v in prompts.PROJECT_VARS.items()})

    def test_an_empty_whole_line_variable_leaves_no_line(self):
        text = prompts.render("manager_project", prompts.project_values(self.conn, self.p["id"]),
                              session_stop_pct="95", **prompts.tool_vars(self.repo))
        self.assertNotIn("\n\n", text)
        self.assertEqual(text.count("\n"), 8)                  # 12 template lines minus the 4 empty ones
        self.assertTrue(all(line.startswith("- ") for line in text.splitlines()))
        full = prompts.render("manager_project", prompts.project_values(self.conn, self.mine["id"]),
                              session_stop_pct="95", **prompts.tool_vars(None))
        self.assertEqual(full.count("\n"), 12)

    def test_the_checkout_project_without_its_seed_is_an_error(self):
        conn = store.connect(testenv.fresh_db(self))
        self.addCleanup(conn.close)
        store.add_project(conn, "AFClaude", path=prompts.HERE)
        with self.assertRaisesRegex(prompts.NotMigrated, "run `python3 prompts.py migrate`"):
            prompts.project_values_at(conn)
        with self.assertRaises(prompts.NotMigrated):                # a message that needs the rules does not go out thin
            ka.session_message("continue", reason="r", progress="P.md")
        ka.session_message("continue", reason="r", progress="P.md", hooked=True)   # the hook has them: no read
        out = ka.session_rules_hook("afclaude", {"cwd": ka.HERE})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could not be built (NotMigrated", out)       # the hook's documented fallback
        self.assertIn("manager_project.md", out)
        actions.perform(conn, "prompt_var.migrate", {}, actor="runner:deploy", via="runner")
        ka.session_message("continue", reason="r", progress="P.md")
        with mock.patch("sys.stdout"):
            self.assertEqual(prompts.main(["migrate", "--dry-run"]), 0)

    def test_the_cli_renders_a_project(self):
        with mock.patch("sys.stdout") as out:
            self.assertEqual(prompts.main(["render", "manager_project", "--project", "Other"]), 0)
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn(f"python3 {os.path.realpath(prompts.HERE)}/keepalive.py --decide", text)
        self.assertNotIn("tools/check_public.py", text)
        with mock.patch("sys.stderr"):
            self.assertEqual(prompts.main(["render", "manager_project", "--set", "x=1"]), 1)   # still strict

    def test_the_template_and_the_seed_cover_each_other(self):
        self.assertEqual(set(prompts.PROMPTS["manager_project"].project), set(prompts.PROJECT_VARS))
        self.assertEqual(set(prompts.CHECKOUT_SEED), set(prompts.PROJECT_VARS))


if __name__ == "__main__":
    unittest.main()
