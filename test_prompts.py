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

    def test_readme_placeholders_match_the_registry(self):
        """Each row's placeholder cell lists the prompt's variables (a row may say "the project variables"
        instead of naming the project ones)."""
        with open(os.path.join(HERE, "prompts", "README.md")) as fh:
            rows = {}
            for line in fh:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if line.startswith("| `") and len(cells) >= 3 and cells[0].endswith(".md`"):
                    rows[cells[0].strip("`")[:-3]] = cells[-1]
        for name, spec in prompts.PROMPTS.items():
            with self.subTest(prompt=name):
                listed = set(re.findall(r"`\{(\w+)\}`", rows[name]))
                want = set(spec.system) | set(spec.project)
                self.assertTrue(listed == want or (listed == set(spec.system) and "project variables" in rows[name]),
                                f"README lists {sorted(listed)}, the registry has {sorted(want)}")

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
        self.assertNotIn(prompts.render("manager").strip(), hooked_manager)
        neutral = prompts.bundle("continue_foreign", manager=False, reason="r", context="")
        self.assertIn(prompts.default("guard_respect").strip(), neutral)
        self.assertNotIn(prompts.render("manager").strip(), neutral)

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
        self.assertIn(" ".join(prompts.render("manager").split()), text)   # the bundle is flat: squashed
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
        self.assertEqual(prompts.render("manager"), prompts.default("manager").format(
            progress_location=prompts.PROJECT_VARS["progress_location"].default))

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


# AFClaude's own rules as the senders must keep sending them, byte for byte (the P3 fixture, the old
# manager_afclaude.md rendered once, and manager.md as it was before P5 made its progress
# file a variable): committed expected renderings, so the texts can't drift silently. Changing a
# template or a seed value on purpose means changing these files in the same commit. The two
# sentinels stand for the checkout's directory and the session_stop_pct setting.
EXPECTED_MANAGER = os.path.join(HERE, "testdata", "afclaude_manager_rendered.md")
EXPECTED_RULES = os.path.join(HERE, "testdata", "afclaude_manager_project_rendered.md")


def expected_text(path, system=None):
    with open(path) as fh:
        text = fh.read()
    if system is not None:
        text = text.replace("<AFCLAUDE_DIR>", system["afclaude_dir"]).replace("<STOP_PCT>", system["session_stop_pct"])
    return text


def old_afclaude_rules(system):
    return expected_text(EXPECTED_RULES, system)


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
        real_render = prompts._render

        def legacy_render(name, project_values, system, ov):    # AFClaude's rules = the committed expected text
            if name == "manager_project":
                return old_afclaude_rules(system)
            if name == "manager":
                return expected_text(EXPECTED_MANAGER)
            return real_render(name, project_values, system, ov)
        with mock.patch.object(ka, "load_prompt", _legacy_ka), \
                mock.patch.object(nightly_tasks, "_prompt", _legacy_nightly), \
                mock.patch.object(inbox, "prompt", _legacy_inbox), \
                mock.patch.object(prompts, "_render", legacy_render):
            before = self.renders()
        self.assertEqual(sorted(before), sorted(after))
        self.assertGreater(len(after), 60)
        # the expected texts really are in play: the AFClaude rules are in the inline and the hook texts
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
        self.assertEqual(usage_sampler.haiku_prompt(), _legacy_nightly("usage_haiku").rstrip("\n"))

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
                     ("goals_location", "docs/goals.md"), ("progress_location", "docs/notes.md")):
            actions.perform(self.conn, "prompt_var.set", {"project": "Other", "name": k, "value": v},
                            actor="cli", via="cli")
        msg = self.message(self.repo)
        self.assertIn("The repo is public: run tools/guard.sh.", msg)
        self.assertIn("(docs/goals.md: one short line per goal", msg)
        self.assertIn("and OPEN_QUESTIONS.md (only things", msg)                # the default
        rules = self.rules()
        for name in ("PROGRESS.md", "GOALS.md"):
            self.assertNotIn(name, rules)
        self.assertNotIn("Keep PROGRESS.md as", msg)      # manager.md too (P5): the progress file is a variable
        self.assertNotIn("updating PROGRESS.md", msg)
        self.assertIn("Keep docs/notes.md as your external memory", msg)
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
        self.assertIn("Keep the project's progress doc as your external memory", self.message(self.repo))

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


# ---------------------------------------------------------------- overrides (P4, D-193)

def set_override(conn, name, body, actor="owner", via="dashboard"):
    return actions.perform(conn, "prompt.set", {"name": name + ".md", "body": body}, actor=actor, via=via)


def marked(name):
    """The default plus a marker token: the same variables, so it is a valid override."""
    return prompts.default(name) + f" OVRD_{name}"


class OverrideSenderTest(SameTextTest):
    """An override reaches every sender of its prompt (and only those): each registered prompt gets a
    marked override in turn, and every sender path is rendered again (SameTextTest.renders)."""
    test_byte_identical_before_and_after = None      # SameTextTest's own, not repeated
    test_every_path_goes_through_the_registry = None

    def test_every_prompt_reaches_its_senders_and_only_them(self):
        base = self.renders()
        for name in sorted(prompts.PROMPTS):
            if name in ("task_start", "usage_haiku"):       # unused since D-205; the sampler: its own test
                continue
            marker = f"OVRD_{name}"
            conn = store.connect(self.db)
            try:
                set_override(conn, name, marked(name))
                got = self.renders()
                actions.perform(conn, "prompt.reset", {"name": name + ".md"}, actor="owner", via="dashboard")
            finally:
                conn.close()
            hit = {k for k, v in got.items() if marker in v}
            with self.subTest(prompt=name):
                self.assertTrue(hit, f"no sender path carries the override of {name}")
                self.assertEqual({k for k in got if got[k] != base[k]}, hit)    # nothing else moved
        self.assertEqual(self.renders(), base)                                  # all reset: the defaults again


class OverrideTest(unittest.TestCase):
    def setUp(self):
        self.db = testenv.fresh_db(self)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        seed_checkout(self.conn)

    def stale(self, name, body):
        """An override that stopped fitting its prompt (the default moved on after the edit), written
        under prompt.set's checks, as a default that gained or lost a variable would leave it."""
        store.put_prompt_override(self.conn, name + ".md", body, "0" * 64, "owner")
        self.conn.commit()

    def test_text_is_override_then_default(self):
        self.assertEqual(prompts.text("compact"), prompts.default("compact"))
        set_override(self.conn, "compact", "Compact now. OVRD")
        self.assertEqual(prompts.text("compact"), "Compact now. OVRD")
        self.assertEqual(ka.compact_command(), "/compact Compact now. OVRD")
        self.assertEqual(prompts.default("compact"), _legacy_ka("compact"))      # the file itself is untouched
        actions.perform(self.conn, "prompt.reset", {"name": "compact.md"}, actor="owner", via="dashboard")
        self.assertEqual(prompts.text("compact"), prompts.default("compact"))

    def test_every_loader_sees_it(self):
        for name, loader in (("run_pace", ka.load_prompt), ("nightly_tasks_due", nightly_tasks._prompt),
                             ("inbox_held", inbox.prompt), ("run_pace", prompts.text)):
            set_override(self.conn, name, marked(name))
            self.assertTrue(loader(name).endswith(f"OVRD_{name}"), name)
        set_override(self.conn, "usage_haiku", marked("usage_haiku"))
        import usage_sampler
        self.assertTrue(usage_sampler.haiku_prompt().endswith("OVRD_usage_haiku"))
        set_override(self.conn, "inbox_triage", marked("inbox_triage"))
        self.assertIn("OVRD_inbox_triage", inbox.triage_prompt("t"))
        self.assertIn("OVRD_inbox_triage", prompts.bundle("inbox_triage", text="t"))

    def test_the_project_template_override_gets_the_project_values(self):
        set_override(self.conn, "manager_project", marked("manager_project"))
        other = store.add_project(self.conn, "Other", path="/srv/other")
        store.put_prompt_var(self.conn, other["id"], "project_notes", "- NOTE-OTHER", "owner")
        self.conn.commit()
        msg = ka.session_message("continue", repo="/srv/other", reason="r", progress="P")
        self.assertIn("NOTE-OTHER", msg)
        self.assertIn("OVRD_manager_project", msg)
        self.assertIn("OVRD_manager_project", ka.standing_rules("afclaude"))

    def test_set_validates_against_the_registry(self):
        with self.assertRaisesRegex(ValueError, r"missing \['deploy_rule'\]"):      # a project variable
            set_override(self.conn, "manager_project",
                         prompts.default("manager_project").replace("{deploy_rule}\n", ""))
        with self.assertRaisesRegex(ValueError, r"unknown \['nope'\]"):
            set_override(self.conn, "manager_project", prompts.default("manager_project") + " {nope}")
        with self.assertRaises(store.NotFound):
            set_override(self.conn, "zz_unknown", "x")
        self.assertEqual(store.list_prompt_overrides(self.conn), [])

    def test_set_test_renders_every_bundle_that_uses_the_prompt(self):
        seen = []
        real = prompts._bundle

        def spy(kind, pv, insert, hooked, manager, system, ov):
            seen.append(kind)
            return real(kind, pv, insert, hooked, manager, system, ov)
        with mock.patch.object(prompts, "_bundle", spy):
            set_override(self.conn, "guard_respect", marked("guard_respect"))
        self.assertEqual(sorted(set(seen)), prompts.bundles_using("guard_respect"))
        self.assertIn("continue", seen)           # through the rules tail
        self.assertIn("usage_review_oneoff", seen)
        # a body that does not render anywhere it is sent is refused, and never written
        with self.assertRaisesRegex(ValueError, "does not render in prompt continue"):
            prompts.try_render("continue", "no variables")
        with mock.patch.object(prompts, "try_render", side_effect=ValueError("does not render in bundle x")):
            with self.assertRaisesRegex(ValueError, "bundle x"):
                set_override(self.conn, "compact", "Another compaction text")
        self.assertEqual(prompts.text("compact"), prompts.default("compact"))

    def test_a_stale_override_is_an_error_not_a_text_with_missing_variables(self):
        set_override(self.conn, "continue", marked("continue"))
        self.stale("continue", "Go on ({reason}).")                    # the default has {progress} too
        with self.assertRaisesRegex(prompts.StaleOverride, r"continue.*missing \['progress'\]"):
            ka.session_message("continue", reason="r", progress="P")
        with self.assertRaises(prompts.StaleOverride):
            ka.load_prompt("continue")
        st = {r["name"]: r for r in prompts.override_status(self.conn)}
        self.assertFalse(st["continue"]["fits"])
        # the hook never fails a session start: its fallback text, and the cause named in it
        self.stale("manager", "{gone}")
        hook = ka.session_rules_hook("manager", {"cwd": prompts.HERE})
        self.assertIn("could not be built (StaleOverride", hook["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(prompts.check_name("manager"), "manager")

    def test_base_outdated_is_reported_and_the_fitting_override_still_used(self):
        set_override(self.conn, "compact", "Compact the way I said. OVRD")
        self.assertEqual(prompts.override_status(self.conn)[0]["base_outdated"], False)
        # the default moves on (wording only): the deploy's sync flags the edit, it keeps being used
        self.conn.execute("UPDATE prompt_overrides SET base_sha256=? WHERE name='compact.md'", ("1" * 64,))
        self.conn.commit()
        actions.perform(self.conn, "prompt.defaults_sync", {}, actor="runner:deploy", via="runner")
        row = prompts.override_status(self.conn)[0]
        self.assertEqual((row["base_outdated"], row["default_changed"], row["fits"]), (True, True, True))
        self.assertEqual(prompts.text("compact"), "Compact the way I said. OVRD")
        diff = prompts.override_diff(self.conn, "compact")
        self.assertIn("base_outdated=True", diff)
        self.assertIn("+Compact the way I said. OVRD", diff)
        # a fresh edit is made against the current default again
        set_override(self.conn, "compact", "Compact once more. OVRD")
        self.assertEqual(prompts.override_status(self.conn)[0]["base_outdated"], False)
        self.assertEqual(store.get_prompt_override(self.conn, "compact.md")["version"], 4)

    def test_a_db_error_fails_the_send(self):
        set_override(self.conn, "run_pace", marked("run_pace"))
        boom = store.DBUnavailable("database is locked", "locked")
        senders = {
            "load_prompt": lambda: ka.load_prompt("run_pace"),
            "text": lambda: prompts.text("compact"),
            "render": lambda: prompts.render("compact"),
            "bundle": lambda: prompts.bundle("compact"),
            "compact_command": ka.compact_command,
            "save_text": lambda: ka.save_text(SID, "manual"),
            "nightly": lambda: nightly_tasks._prompt("nightly_tasks_due"),
            "inbox": lambda: inbox.triage_prompt("t"),
            "haiku": lambda: __import__("usage_sampler").haiku_prompt(),
            "rules": lambda: ka.standing_rules("manager"),
        }
        for what, send in senders.items():
            with self.subTest(sender=what), mock.patch.object(store, "connect", side_effect=boom), \
                    mock.patch.object(store, "_sleep"):
                with self.assertRaises(store.DBError):
                    send()
        with mock.patch.object(store, "connect", side_effect=boom):     # the hook alone keeps its fallback
            hook = ka.session_rules_hook("manager", {"cwd": prompts.HERE})
        self.assertIn("could not be built", hook["hookSpecificOutput"]["additionalContext"])

    def test_no_override_changes_nothing(self):
        before = ka.session_message("continue", reason="r", progress="P")
        set_override(self.conn, "continue", marked("continue"))
        actions.perform(self.conn, "prompt.reset", {"name": "continue.md"}, actor="owner", via="dashboard")
        self.assertEqual(ka.session_message("continue", reason="r", progress="P"), before)

    def test_autonomous_sessions_cannot_edit(self):
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "prompt.set", {"name": "compact.md", "body": "x"}, actor="mcp:" + SID,
                            via="mcp", autonomous=True)

    def test_cli(self):
        path = os.path.join(os.path.dirname(self.db), "body.md")
        with open(path, "w") as fh:
            fh.write("Compact it. OVRD")

        def run(*argv):
            with mock.patch("sys.stdout") as out:
                rc = prompts.main(list(argv))
            return rc, "".join(c.args[0] for c in out.write.call_args_list)
        rc, text = run("override", "set", "compact", path)
        self.assertEqual(rc, 0)
        self.assertIn('"base_outdated": 0', text)
        self.assertEqual(prompts.text("compact"), "Compact it. OVRD")
        rc, text = run("override", "list")
        self.assertIn('"name": "compact"', text)
        self.assertIn('"base_outdated": false', text)
        rc, text = run("override", "diff", "compact.md")
        self.assertIn("+Compact it. OVRD", text)
        self.assertIn("-" + prompts.default("compact").splitlines()[0], text)
        rc, text = run("render", "compact")
        self.assertEqual(text, "Compact it. OVRD")                   # render shows what a sender sends
        rc, text = run("override", "reset", "compact")
        self.assertEqual(prompts.text("compact"), prompts.default("compact"))
        self.assertIn("no override", run("override", "diff", "compact")[1])
        with mock.patch("sys.stderr"):
            self.assertEqual(run("override", "set", "nope", path)[0], 1)
            self.assertEqual(run("override", "reset", "nope")[0], 1)


if __name__ == "__main__":
    unittest.main()
