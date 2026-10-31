#!/usr/bin/env python3
"""
prompts.py: the prompt registry (design §7.6, phase 4a; docs/phase4_plan.md P1 + P2).

Every prompt AFClaude sends is a file prompts/<name>.md (the defaults ship with the code, D-169;
`store.py import-prompts` copies them into prompt_defaults at each deploy, D-227). This module
knows each of them:

  - PROMPTS: one entry per file: the variables the sender fills (`system`: the session, the
    reason, the date, ...), the per-project variables it uses (`project`), who sends it. test_prompts
    pins the registry to the files (every file registered, the placeholder sets equal).
  - default(name): the file's text, the one loader every sender uses (keepalive.load_prompt,
    nightly_tasks, inbox, usage_sampler). An unregistered name is UnknownPrompt.
  - render(name, project_values, **system): strict str.format: exactly the declared system
    variables (a missing or an unknown one is a ValueError, D-235), the project ones from
    project_values or their declared default.

Project scope (D-193, D-197): prompts are system-wide (an override is keyed by name only, owner
only); a project only fills the variables PROJECT_VARS declares. Its values are rows of
store.prompt_vars, written through actions.py (prompt_var.set / prompt_var.reset, audited; owner
only until the roles of phase 5a give it to the project's managers and editors). Two prompts use
them: manager.md (P5: where the progress notes are) and manager_project.md (P3), the task-manager rules
of every project: a few words (the goal list's,
the progress notes' and the questions' place) and whole lines (the public-repo rule, the worktree rule, a note, the docs
rule, the deploy rule; an empty one leaves no line). A session's text is built from its project's
values: keepalive.session_message(repo=...) reads them (project_values_at), the SessionStart hook
reads AFClaude's own.

Migrate step: `prompts.py migrate` writes the seed values (CHECKOUT_SEED: the lines AFClaude's own
rules had before P3) for the project at this checkout's path, each only where that project has
never had a row for it, and moves a value that still equals the previous seed (OLD_CHECKOUT_SEED:
the questions moved into the DB, phase 4b Q3) to the new one (a value someone set or reset is never
overwritten). Idempotent and audited (prompt_var.migrate); a project-less checkout has nothing to seed.

Bundles (P2, §3.2): one session kind of message is more than one prompt -- a head prompt, the
sender's own free-text insert (a hand-off, a pacing line, held/noted inbox messages, the due
nightly tasks -- whichever the kind carries), and, for a session message, the standing-rule tail
(the SessionStart hook's one-line pointer if the session already has the rules, D-225; otherwise
the rules themselves; just guard_respect for a neutral, non-manager message). BUNDLES names every
kind once (F8's view, D-111); `bundle(kind, ...)` renders it -- flat (one tmux line) or not, per
the bundle, not per prompt (a prompt like manager.md is sent both ways in different bundles, so
P1's per-prompt `flat` was the wrong place for it and is gone). `session_message`, `standing_rules`,
`usage_review.run_once` and `nightly_tasks.brief` are calls into it now.

    python3 prompts.py list                         # every prompt, its variables and senders
    python3 prompts.py show NAME                    # the default text
    python3 prompts.py render NAME [--project P] [--set KEY=VALUE ...]
    python3 prompts.py bundles                       # every bundle kind and its parts
    python3 prompts.py bundle KIND [--project P] [--set KEY=VALUE ...] [--insert TEXT]
                                   [--hooked] [--no-manager]
    python3 prompts.py vars [--project P]           # the project variables and their values
    python3 prompts.py set-var PROJECT NAME VALUE | reset-var PROJECT NAME
    python3 prompts.py override list | diff NAME | set NAME FILE | reset NAME   # the owner's edits (P4)
    python3 prompts.py migrate [--dry-run] [--deploy]

Overrides (P4, D-193, D-169): the DB's prompt_overrides holds the owner's edit of a prompt (one per
prompt, system-wide; the default stays the file). text(name) = override > default is what every
sender sends: render / bundle / keepalive.load_prompt / nightly_tasks / inbox / usage_sampler all go
through it, and read the DB on every send, so a DB error fails the send (D-235) and a reset takes
effect at once. prompt.set (actions.py) validates the body against the registry (the prompt's
system + project variables) and test-renders it in the prompt and every bundle that uses it
(try_render) before it writes. An override that no longer fits (the default changed its variables:
base_outdated) is a StaleOverride error at the send, never a text with missing variables; one whose
default only changed in wording is used and flagged (override_status, `override list|diff`, MCP
afclaude_prompts). The SessionStart hook still never fails a session start: its fallback text applies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import string
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if TYPE_CHECKING:
    import store   # the DB parts import it when called: the senders' import stays file-only

PROMPTS_DIR = os.path.join(HERE, "prompts")


@dataclass(frozen=True)
class Prompt:
    system: tuple[str, ...] = ()     # filled by the sender
    project: tuple[str, ...] = ()    # PROJECT_VARS names: the project's values
    used_by: str = ""                # who sends it (F8 shows it, D-111)


@dataclass(frozen=True)
class ProjectVar:
    help: str
    default: str = ""                # a project without a value gets this (part of the registry)
    max_len: int = 500


PROMPTS: dict[str, Prompt] = {
    "continue": Prompt(("progress", "reason"), used_by="keepalive start / slot; dispatcher stall continue"),
    "fillup": Prompt(("progress", "reason", "remaining", "reset"), used_by="keepalive fill-up run"),
    "last_mile": Prompt(("progress", "reason"), used_by="keepalive last-stretch slot"),
    "run_pace": Prompt(("end", "last_hour"), used_by="keepalive.run_pace_text, inserted in every start"),
    "run_chain": Prompt(("next", "until"), used_by="keepalive.run_pace_text, a chained start"),
    "session_end_save": Prompt(("session", "tasks", "why"), used_by="keepalive save prompt before a compaction"),
    "session_end_save_retry": Prompt(("newest", "session", "tasks"),
                                     used_by="keepalive: the save prompt re-sent once after a reply without a hand-off"),
    "session_end_save_refresh": Prompt(("handoff", "session", "tasks", "what"),
                                       used_by="keepalive: the hand-off asked for again when new input reached the "
                                               "session after it, before /compact (D-307)"),
    "compact": Prompt(used_by="keepalive /compact instruction"),
    "compact_resume": Prompt(("handoff", "progress", "when"), used_by="keepalive, inside compact_after"),
    "compact_after": Prompt(("resume",), used_by="keepalive message after a compaction"),
    "compact_after_run": Prompt(("budget", "pace", "reason", "resume", "started", "threshold"),
                                used_by="keepalive message after a mid-run compaction (D-307): the run goes on"),
    "project_start": Prompt(("desc", "slug", "title"), used_by="unused since keepalive --work-on was retired (3b S5); S6 removes it"),
    "manager_first_start": Prompt(("desc", "path", "progress", "project"),
                                  used_by="keepalive.create_task_manager: a project's first start (3b S4, D-281)"),
    "run_project": Prompt(("project",), used_by="keepalive start / slot / fill-up: the project's name (D-301)"),
    "continue_foreign": Prompt(("context", "reason"), used_by="dispatcher: the user's own stalled session"),
    "task_start": Prompt(("description", "id", "project", "qa", "title"), used_by="unused since D-205"),
    "guard_respect": Prompt(used_by="standing rule: every session message, the hook part afclaude"),
    "manager": Prompt((), ("progress_location",),
                      used_by="standing rule: task-manager sessions, the hook part manager"),
    "manager_project": Prompt(("afclaude_dir", "session_stop_pct", "tools"),
                              ("goals_location", "progress_location", "questions_location", "public_repo_rule", "worktree_rule",
                               "project_notes", "docs_rule", "deploy_rule"),
                              used_by="standing rule: the project's own rules after manager.md (D-197); the "
                                      "hook part afclaude for AFClaude itself"),
    "afk_rules": Prompt(used_by="standing rule: unattended runs, the hook part afclaude"),
    "session_rules": Prompt(("part", "what"), used_by="SessionStart hook header (keepalive --session-rules)"),
    "rules_pointer": Prompt(used_by="session messages to a hooked session (D-225)"),
    "usage_review": Prompt(("date", "since"), used_by="monthly nightly task brief; one-off usage review"),
    "stage_eta_review": Prompt(("date",), used_by="monthly nightly task brief"),
    "stage_eta_review_once": Prompt(("date", "report"), used_by="one-off ETA review; nightly task brief"),
    "stage_eta_review_once_after_usage": Prompt(("report",), used_by="the chained one-off ETA review"),
    "stage_eta_review_rerun": Prompt(used_by="nightly task brief (the ETA review re-run)"),
    "compaction_handoff_review": Prompt(("date", "report"), used_by="one-off compaction vs. hand-off review "
                                        "(D-305); nightly task brief"),
    "inbox_triage": Prompt(("text",), used_by="inbox_worker Haiku triage"),
    "inbox_now": Prompt(("at", "id", "notes", "text"), used_by="inbox_worker: a 'now' message"),
    "inbox_notes": Prompt(("notes",), used_by="inbox notes inserted in the next message"),
    "inbox_held": Prompt(("held",), used_by="held inbox messages inserted in a start"),
    "nightly_tasks_due": Prompt(("tasks",), used_by="a night start's continue (nightly_tasks.start_insert)"),
    "nightly_task": Prompt(("name", "report", "title"), used_by="head of a nightly task brief"),
    "usage_haiku": Prompt(("gaps", "n", "name", "prompts", "totals"), used_by="usage_sampler Haiku judge"),
}

# D-197: what a project fills. Values are text. goals_location / progress_location / questions_location are words inside
# a line of the template; the rest are WHOLE-LINE variables (the value carries its own "- "): a line
# that can't be phrased with a value (the public-repo rule, the worktree story) is a variable whose
# default is empty, and render() drops an empty one's line, so a project without it gets no gap.
PROJECT_VARS: dict[str, ProjectVar] = {
    # defaults fit a project whose docs live in the AFClaude DB (docs_rule's default); read as "(the
    # project's goals doc: one short line ...)" and "stop cleanly (state in the project's progress doc)"
    "goals_location": ProjectVar("where the project's goal list lives", "the project's goals doc"),
    "progress_location": ProjectVar("where the project's progress notes (the state of a stopped run) live",
                                    "the project's progress doc"),
    "questions_location": ProjectVar("where the project's open questions for the user live",
                                    "the project's open questions in the AFClaude DB (afclaude_ask)"),
    "public_repo_rule": ProjectVar("whole line: the public-repo rule and the guard the commit hooks run "
                                   "(empty: the repo is not public)", max_len=2000),
    "worktree_rule": ProjectVar("whole line: the files the live system imports, which change only in a git "
                                "worktree (empty: none)", max_len=2000),
    "project_notes": ProjectVar("whole line: a short free-text note for the project's task-manager", max_len=2000),
    "docs_rule": ProjectVar("whole line: how the project docs are read and written",
                            "- The project docs (progress, goals, exceptions, alerts, review reports) live in the "
                            "AFClaude DB: read and write them with the afclaude_doc tool (actions list, show, "
                            "append, set; it knows the calling session's project).", max_len=3000),
    "deploy_rule": ProjectVar("whole line: how the project is deployed (empty: not covered)", max_len=2000),
}

# The migrate step's values for the project at this checkout's path (AFClaude itself): the lines the
# old manager_afclaude.md hard-coded, so the generic template manager_project.md renders the same
# text for it (test_prompts, against testdata/afclaude_manager_project_rendered.md).
CHECKOUT_SEED: dict[str, str] = {
    "goals_location": "GOALS.md",
    "progress_location": "PROGRESS.md",
    "questions_location": (
        "the open questions in the AFClaude DB, kept with `python3 tasks.py question ask` and `question close`"),
    "public_repo_rule": (
        '- The repo will be PUBLIC. Keep personal data, secrets, IP addresses, domains and user-specific '
        'host details (host inventory, personal projects, usage habits) out of committed files. '
        'User-specific questions go into the AFClaude DB (`python3 tasks.py question ask`), never into a '
        'committed file. The pre-commit/pre-push guard `tools/check_public.py` blocks the obvious cases; '
        'never bypass it with `--no-verify`, rephrase instead.'),
    "worktree_rule": (
        '- Subagents that change code the live system imports (keepalive.py, store.py, stalled.py, '
        'usage_sampler.py, export_quickview.py, ka_resume.sh) work in a separate git worktree (`git '
        'worktree add ../AFClaude-wt-<name>`); you review and merge. Cron and the watcher import the live'
        ' files, so a half-edited store.py once migrated the live DB.'),
    "project_notes": (
        '- In this repo the open questions for the user are in the AFClaude DB (`python3 tasks.py question '
        'ask` / `question close`) and the goal list is GOALS.md.'),
    "docs_rule": (
        '- The project docs live in the AFClaude DB (design §7.7): progress, goals, exceptions, alerts '
        'and the review reports (review:<date>). Write through the write path where a command is natural:'
        ' `python3 tasks.py doc append progress "- HH:MM …"` (or the MCP tool afclaude_doc), `python3 '
        'tasks.py doc set goals - < file`; read with `python3 tasks.py doc show progress --entries 20`, '
        "`doc show goals`, `doc show alerts` (the runners' notes and every alert are only in the DB: "
        'ALERTS.md is no longer written). Editing PROGRESS.md, GOALS.md or EXCEPTIONS.md directly still '
        "works: an import every 10 minutes takes the edits in, merged with the runners' notes. Before "
        'each commit that includes PROGRESS.md, GOALS.md or EXCEPTIONS.md, run `python3 tasks.py doc '
        'export` (it imports your edits first, then regenerates the three files from the DB, public-repo '
        'checked) and commit what it wrote. If it reports a conflict (the file and the DB changed the '
        'same lines), merge by hand: put the merged text in the file (`python3 tasks.py doc show NAME` '
        'has the DB side), run `python3 tasks.py doc import --force`, then export again.'),
    "deploy_rule": (
        '- To deploy (rebuild the manager container after a merge to main), run `python3 '
        'tools/deploy.py`, never the bare `docker compose -f docker/compose.yml up -d --build`: it drains'
        ' the runner and waits for the container to go quiet (no sampler / usage-review / unittest / '
        'bridge `claude` call in flight) before rebuilding with the right `AFCLAUDE_CODE_VERSION`, so the'
        " rebuild can't cut an in-flight usage read the way the 21.10. incident did (D-234/D-235/D-257). "
        'It refuses within 30 min before or after a session-window start (a restart during a start alerts'
        " + pauses) and prints the next safe time; don't pass `--force` to get around that."),
}

# What the migrate step wrote before the questions moved into the DB (phase 4b Q3 W3): a project value that
# still equals one of these was never changed by anyone, so `prompts.py migrate` moves it to the new seed.
OLD_CHECKOUT_SEED: dict[str, str] = {
    "questions_location": "OPEN_QUESTIONS.md",
    "public_repo_rule": (
        '- The repo will be PUBLIC. Keep personal data, secrets, IP addresses, domains and user-specific '
        'host details (host inventory, personal projects, usage habits) out of committed files. '
        'User-specific questions go to OPEN_QUESTIONS.md, which is local only (gitignored). The '
        'pre-commit/pre-push guard `tools/check_public.py` blocks the obvious cases; never bypass it with'
        ' `--no-verify`, rephrase instead.'),
    "project_notes": (
        '- In this repo the list of open questions for the user is OPEN_QUESTIONS.md (local only) and the'
        ' goal list is GOALS.md.'),
}


# ---------------------------------------------------------------- bundles (P2, §3.2)

@dataclass(frozen=True)
class Part:
    """One piece of a bundle. Exactly one of the three:
      prompt  a PROMPTS name: rendered (its own system variables, filtered out of the bundle
              call's **system by name, and its project variables) and appended.
      insert  the sender's own free-text goes here if it has any (skipped if empty); the label
              says what that text usually is, for F8 (D-111) -- the mechanism itself (one slot)
              does not care what is actually passed.
      rules   the standing-rule tail of a session message: rules_pointer alone when the session
              already has the rules (hooked=True and manager=True, D-225); guard_respect, manager,
              manager_project (the project's own values) and afk_rules when manager=True and not hooked;
              guard_respect alone when manager=False (a neutral message, e.g. the user's own
              session)."""
    prompt: str = ""
    insert: str = ""
    rules: bool = False


@dataclass(frozen=True)
class Bundle:
    parts: tuple[Part, ...]
    flat: bool = True     # one tmux line (send-keys) vs. "\n\n"-joined (hook context); per bundle,
                          # not per prompt -- the same prompt (e.g. manager.md) is sent both ways
    used_by: str = ""


BUNDLES: dict[str, Bundle] = {
    # task-manager continue / stall continue (session_message, keepalive._fire_start, D-274/D-275)
    "continue": Bundle((Part(prompt="continue"),
                        Part(insert="the run-pace line, held inbox messages, inbox notes, due nightly tasks"),
                        Part(rules=True)),
                       used_by="keepalive _fire_start: a session-window start, a postponed one, or a "
                               "last-stretch slot's continue"),
    "fillup": Bundle((Part(prompt="fillup"), Part(insert="the run-pace line, held inbox messages, inbox notes"),
                      Part(rules=True)), used_by="keepalive _fire_start: a fill-up run"),
    "last_mile": Bundle((Part(prompt="last_mile"), Part(insert="the run-pace line, held inbox messages, inbox notes"),
                         Part(rules=True)), used_by="keepalive _fire_start: a last-stretch slot"),
    "continue_foreign": Bundle((Part(prompt="continue_foreign"), Part(insert="(rarely used)"), Part(rules=True)),
                               used_by="dispatcher: the user's own stalled session, not the task-manager's"),
    "project_start": Bundle((Part(prompt="project_start"), Part(insert="(rarely used)"), Part(rules=True)),
                            used_by="unused since keepalive --work-on was retired (3b S5); S6 removes it"),
    "manager_first_start": Bundle((Part(prompt="manager_first_start"),
                                   Part(insert="the run-pace line, held inbox messages, inbox notes"),
                                   Part(rules=True)),
                                  used_by="keepalive.create_task_manager: a project's first start (3b S4, D-281)"),
    "task_start": Bundle((Part(prompt="task_start"), Part(insert="(rarely used)"), Part(rules=True)),
                         used_by="unused since D-205"),
    "compact_after": Bundle((Part(prompt="compact_after"), Part(insert="the hand-off read back"), Part(rules=True)),
                            used_by="keepalive message after a compaction"),
    "compact_after_run": Bundle((Part(prompt="compact_after_run"), Part(insert="inbox notes"), Part(rules=True)),
                                used_by="keepalive message after a mid-run compaction (D-307): the run goes on"),
    "inbox_now": Bundle((Part(prompt="inbox_now"), Part(insert="(rarely used)"), Part(rules=True)),
                        used_by="inbox_worker: a 'now' message"),
    # compaction, sent as is (no rules tail: inside an already-running session)
    "session_end_save": Bundle((Part(prompt="session_end_save"),), used_by="keepalive save prompt before a "
                               "compaction"),
    "session_end_save_retry": Bundle((Part(prompt="session_end_save_retry"),),
                                     used_by="keepalive: the save prompt re-sent once after a reply without a "
                                             "hand-off"),
    "session_end_save_refresh": Bundle((Part(prompt="session_end_save_refresh"),),
                                       used_by="keepalive: the hand-off asked for again (D-307)"),
    "compact": Bundle((Part(prompt="compact"),), used_by="keepalive /compact instruction"),
    # the standing rules the SessionStart hook injects, in two parts (D-225)
    "manager_rules": Bundle((Part(prompt="session_rules"), Part(prompt="manager")), flat=False,
                            used_by="SessionStart hook, part 1: the task-manager rules"),
    "afclaude_rules": Bundle((Part(prompt="session_rules"), Part(prompt="manager_project"),
                              Part(prompt="guard_respect"), Part(prompt="afk_rules")), flat=False,
                             used_by="SessionStart hook, part 2: the AFClaude-repo, guard and AFK rules"),
    # usage review: a one-off, in its own fresh session (the monthly brief goes through nightly_task below)
    "usage_review_oneoff": Bundle((Part(prompt="usage_review"),
                                   Part(insert="the chained ETA review's pointer, D-228"),
                                   Part(prompt="guard_respect"), Part(prompt="manager")),
                                  used_by="usage_review.run_once: a one-off usage review"),
    "stage_eta_review_oneoff": Bundle((Part(prompt="stage_eta_review_once"),
                                       Part(insert="the chained ETA review's pointer, D-228"),
                                       Part(prompt="guard_respect"), Part(prompt="manager")),
                                      used_by="usage_review.run_once: a one-off ETA review"),
    # nightly task brief (D-254/D-261): the head; the task's own --prompt list is rendered alongside it
    "nightly_task": Bundle((Part(prompt="nightly_task"),), flat=False,
                           used_by="nightly_tasks.brief: the head of a nightly task's brief"),
    # Haiku judges: a single prompt, no tail
    "usage_haiku": Bundle((Part(prompt="usage_haiku"),), flat=False, used_by="usage_sampler Haiku judge"),
    "inbox_triage": Bundle((Part(prompt="inbox_triage"),), flat=False, used_by="inbox_worker Haiku triage"),
}


class UnknownPrompt(FileNotFoundError):
    """A name the registry doesn't have (a FileNotFoundError: callers treat it as a missing file)."""


class UnknownBundle(LookupError):
    """A kind the registry doesn't have (the registry: prompts.BUNDLES)."""


class StaleOverride(RuntimeError):
    """An override that no longer fits its prompt (the default changed its variables after the edit):
    every send of that prompt fails with this, naming it, until the owner resets or edits it
    (D-235: no send with missing or unknown variables, no silent fall back to the default)."""


class NotMigrated(RuntimeError):
    """The project at this checkout (AFClaude) misses seed values: its rules would render without
    its own lines (D-235: an error, not a thinner text). `prompts.py migrate` writes them."""


def check_name(name: object) -> str:
    if not isinstance(name, str) or name not in PROMPTS:
        raise UnknownPrompt(f"no prompt {name!r} (the registry: prompts.PROMPTS)")
    return name


def path(name: str) -> str:
    return os.path.join(PROMPTS_DIR, check_name(name) + ".md")


def default(name: str) -> str:
    """prompts/<name>.md, the default text (D-169), byte for byte."""
    with open(path(name)) as fh:
        return fh.read()


def override_key(name: str) -> str:
    """prompt_overrides.name: the file name (the key the DB schema and prompt.set use)."""
    return check_name(name) + ".md"


def load_overrides(conn: Optional[sqlite3.Connection] = None) -> dict[str, str]:
    """{registry name: body} of every prompt that has an override now (D-193: one per prompt,
    system-wide). Read on every call (a sender never caches one: a reset takes effect at the next
    send). A DB error is raised, never answered with the defaults (D-235): the send fails. An
    override row of a name the registry doesn't know is not loadable (override_status lists it)."""
    import store
    own = conn is None
    c = store.connect() if conn is None else conn
    try:
        rows = store.retrying(lambda: store.list_prompt_overrides(c), c)
    finally:
        if own:
            c.close()
    return {r["name"][:-3]: r["body"] for r in rows if r["name"].endswith(".md") and r["name"][:-3] in PROMPTS}


def _text(name: str, ov: Mapping[str, str]) -> str:
    """Override > default. An override must still fit its prompt's variables: the default may have
    changed since the edit (base_outdated), so it is checked at every use, not trusted from
    prompt.set (StaleOverride)."""
    body = ov.get(check_name(name))
    if body is None:
        return default(name)
    want, have = variables(name), placeholders(body)
    if have != want:
        detail = ("unbalanced braces" if have is None
                  else f"missing {sorted(want - have)}, unknown {sorted(have - want)}")
        raise StaleOverride(f"the override of prompt {name} no longer fits its variables ({detail}); the default "
                            f"changed since the edit: `python3 prompts.py override diff {name}`, then edit or "
                            f"`override reset {name}`")
    return body


def text(name: str, conn: Optional[sqlite3.Connection] = None) -> str:
    """What a sender sends: the owner's override of `name` if there is one, else the default (D-169,
    D-193; P4). The senders' one loader; `default` stays the file, for the diff and the import."""
    return _text(name, load_overrides(conn))


def placeholders(text: str) -> set[str] | None:
    """The {placeholder} names of a str.format template; None if it isn't one (unbalanced braces)."""
    try:
        return {f.split(".")[0].split("[")[0]
                for _literal, f, _spec, _conv in string.Formatter().parse(text) if f is not None}
    except ValueError:
        return None


def variables(name: str) -> set[str]:
    spec = PROMPTS[check_name(name)]
    return set(spec.system) | set(spec.project)


def _render(name: str, project_values: Optional[Mapping[str, str]], system: Mapping[str, object],
            ov: Mapping[str, str]) -> str:
    spec = PROMPTS[check_name(name)]
    missing, unknown = set(spec.system) - set(system), set(system) - set(spec.system)
    if missing or unknown:
        raise ValueError(f"prompt {name}: missing variable(s) {sorted(missing)}, unknown {sorted(unknown)}")
    values: dict[str, object] = dict(system)
    body = _text(name, ov)
    for v in spec.project:
        given = (project_values or {}).get(v)
        values[v] = PROJECT_VARS[v].default if given is None else given
        if values[v] == "":          # a whole-line variable without a value leaves no empty line
            body = re.sub(rf"(?m)^\{{{v}\}}\n", "", body)
    return body.format(**values)


def render(name: str, project_values: Optional[Mapping[str, str]] = None, /, **system: object) -> str:
    """`name` (the override, else the default) filled in: exactly its system variables from
    `system`, its project variables from project_values (a project's values, e.g. project_values())
    or their default. A missing or an unknown system variable is a ValueError (no partial text goes
    out, D-235). `name` and `project_values` are positional-only (P2): a prompt can declare a system
    variable literally called "name" (nightly_task.md does) or "project_values" without colliding
    with render()'s own parameters -- callers that pass a bundle's **system straight through are
    common (nightly_tasks.brief)."""
    return _render(name, project_values, system, load_overrides())


# ---------------------------------------------------------------- bundles (P2, §3.2)

def check_bundle(kind: object) -> str:
    if not isinstance(kind, str) or kind not in BUNDLES:
        raise UnknownBundle(f"no bundle {kind!r} (the registry: prompts.BUNDLES)")
    return kind


RULES_PROMPTS = ("rules_pointer", "guard_respect", "manager", "manager_project", "afk_rules")   # the tail's


def _rules_tail(hooked: bool, manager: bool, project_values: Optional[Mapping[str, str]],
                system: Mapping[str, object], ov: Mapping[str, str]) -> list[str]:
    """The standing-rule tail of a session message (Part(rules=True)): see Part's docstring."""
    if hooked and manager:
        return [_text("rules_pointer", ov)]
    if not manager:
        return [_text("guard_respect", ov)]
    fields = {k: system[k] for k in variables("manager_project") if k in system}
    return [_text("guard_respect", ov), _render("manager", project_values, {}, ov),
            _render("manager_project", project_values, fields, ov), _text("afk_rules", ov)]


def bundle(kind: str, project_values: Optional[Mapping[str, str]] = None, /, insert: Optional[str] = None,
          hooked: bool = False, manager: bool = True, **system: object) -> str:
    """BUNDLES[kind] rendered: each prompt part gets exactly its own declared system variables out
    of **system (one **system serves every part of a bundle; render()'s strict check still applies
    per part; `kind` and `project_values` are positional-only, render()'s own reason; the overrides
    are read once for the whole bundle), the insert
    part gets `insert` if given (skipped if empty), the rules part gets the standing-rule tail
    (_rules_tail; a bundle without one ignores hooked/manager). Joined
    per the bundle's own `flat` (BUNDLES[kind].flat): one line (tmux send-keys), or a blank line
    between parts, each stripped (hook context)."""
    return _bundle(kind, project_values, insert, hooked, manager, system, load_overrides())


def _bundle(kind: str, project_values: Optional[Mapping[str, str]], insert: Optional[str], hooked: bool,
            manager: bool, system: Mapping[str, object], ov: Mapping[str, str]) -> str:
    b = BUNDLES[check_bundle(kind)]
    chunks: list[str] = []
    for part in b.parts:
        if part.prompt:
            fields = {k: system[k] for k in variables(part.prompt) if k in system}
            chunks.append(_render(part.prompt, project_values, fields, ov))
        elif part.rules:
            chunks.extend(_rules_tail(hooked, manager, project_values, system, ov))
        elif insert:
            chunks.append(str(insert))
    if b.flat:
        return " ".join(" ".join(chunks).split())
    return "\n\n".join(c.strip() for c in chunks)


# ---------------------------------------------------------------- overrides (P4, D-193)

def bundles_using(name: str) -> list[str]:
    """The bundle kinds that send `name`: as a part, or as one of the standing-rule tail's prompts."""
    check_name(name)
    return sorted(k for k, b in BUNDLES.items()
                  if any(p.prompt == name or (p.rules and name in RULES_PROMPTS) for p in b.parts))


def try_render(name: str, body: str) -> None:
    """Render `body` as the override of `name`, with dummy values, everywhere it would be sent: the
    prompt itself and every bundle that uses it (the rules tail in all its hooked / manager
    variants). The first failure is a ValueError naming where: a bad override must not reach a
    live session (prompt.set runs this before it writes). A bundle typed into tmux must still start with
    an AFClaude prefix (owner_words.KEEPALIVE_PREFIXES): the transcript can't tell it from the owner's
    typing otherwise (D-198)."""
    import owner_words
    dummy = {v: "x" for spec in PROMPTS.values() for v in spec.system}
    ov = {check_name(name): body}
    runs: list[tuple[str, Any, bool]] = [(f"prompt {name}", lambda: _render(
        name, None, {v: "x" for v in PROMPTS[name].system}, ov), False)]
    for kind in bundles_using(name):
        for hooked, manager in ((False, True), (True, True), (False, False)):
            runs.append((f"bundle {kind} (hooked={hooked}, manager={manager})",
                         lambda kind=kind, hooked=hooked, manager=manager:
                         _bundle(kind, None, "x", hooked, manager, dummy, ov),
                         BUNDLES[kind].flat and kind != "compact"))   # typed into tmux (/compact is put in front by code)
    for where, fn, typed in runs:
        try:
            text = fn()
        except (KeyError, IndexError, ValueError, AttributeError, StaleOverride) as e:
            raise ValueError(f"the override does not render in {where}: {type(e).__name__}: {e}") from None
        if typed and not text.lstrip().startswith(owner_words.KEEPALIVE_PREFIXES):
            raise ValueError(f"the override does not start with an AFClaude prefix in {where} (one of "
                             f"{', '.join(owner_words.KEEPALIVE_PREFIXES)}): a text typed into tmux that does "
                             "not would pass for the owner's words (D-198)")


def bundle_system(kind: str) -> set[str]:
    """Every system variable a bundle's prompts declare (the rules tail's included): what a sender
    of this kind fills."""
    b = BUNDLES[check_bundle(kind)]
    names: set[str] = set()
    for part in b.parts:
        if part.prompt:
            names |= set(PROMPTS[part.prompt].system)
        elif part.rules:
            for r in RULES_PROMPTS:
                names |= set(PROMPTS[r].system)
    return names


def render_in(conn: sqlite3.Connection, name: str, project_values: Optional[Mapping[str, str]],
              system: Mapping[str, object]) -> str:
    """render() with the overrides read through `conn` (a caller that has its own database open)."""
    return _render(name, project_values, system, load_overrides(conn))


def bundle_in(conn: sqlite3.Connection, kind: str, project_values: Optional[Mapping[str, str]],
              system: Mapping[str, object], insert: Optional[str] = None, hooked: bool = False,
              manager: bool = True) -> str:
    """bundle() with the overrides read through `conn`."""
    return _bundle(kind, project_values, insert, hooked, manager, system, load_overrides(conn))


def override_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every current override: name, version, who/when, `base_outdated` (the stored flag, refreshed at
    each deploy's prompt.defaults_sync: None = unknown), `default_changed` (live: the default file is not
    the one the edit was made against), `fits` (the override still has exactly its prompt's variables;
    False means every send of it fails, StaleOverride). Read-only."""
    import hashlib
    import store
    out: list[dict[str, Any]] = []
    for r in store.list_prompt_overrides(conn):
        reg = r["name"][:-3] if r["name"].endswith(".md") else r["name"]
        known = reg in PROMPTS
        row: dict[str, Any] = {"name": reg, "version": r["version"], "updated_at": r["updated_at"],
                               "updated_by": r["updated_by"], "base_outdated": None if r["base_outdated"] is None
                               else bool(r["base_outdated"]), "known": known}
        if known:
            row["default_changed"] = (r["base_sha256"] is not None and r["base_sha256"]
                                      != hashlib.sha256(default(reg).encode()).hexdigest())
            row["fits"] = placeholders(r["body"]) == variables(reg)
        out.append(row)
    return out


def override_diff(conn: sqlite3.Connection, name: str) -> str:
    """A unified diff default -> override of `name`, headed by its status; '' when it has none."""
    import difflib
    import store
    key = override_key(name)
    row = store.get_prompt_override(conn, key)
    if row is None or row["body"] is None:
        return ""
    st = next(x for x in override_status(conn) if x["name"] == name)
    head = (f"# override of {name} v{row['version']} by {row['updated_by']} at {row['updated_at']}: "
            f"base_outdated={st['base_outdated']} default_changed={st['default_changed']} fits={st['fits']}\n")
    return head + "".join(difflib.unified_diff(default(name).splitlines(keepends=True),
                                               row["body"].splitlines(keepends=True),
                                               f"default/{name}.md", f"override/{name}.md"))


def preview_system(project_path: Optional[str], given: Mapping[str, object], declared: set[str]) -> dict[str, Any]:
    """The system variables a preview (the CLI, the MCP tool) fills itself where `declared` has them:
    the tool paths for the project's directory, the session_stop_pct setting; `given` overrides
    them, and anything else stays for the strict render."""
    import importlib
    config: Any = importlib.import_module("afclaude_config")
    auto: dict[str, Any] = dict(tool_vars(project_path))
    if "session_stop_pct" in declared:
        auto["session_stop_pct"] = f"{float(config.setting('session_stop_pct')):g}"
    out = {k: v for k, v in auto.items() if k in declared}
    out.update(given)
    return out


# ---------------------------------------------------------------- project values (D-197)

def check_var(name: object, value: object = None) -> str:
    """A PROJECT_VARS name (and, if given, a value it accepts: text within its max_len)."""
    if not isinstance(name, str) or name not in PROJECT_VARS:
        raise ValueError(f"unknown prompt variable {name!r} (one of {', '.join(PROJECT_VARS)})")
    if value is not None:
        if not isinstance(value, str):
            raise ValueError(f"the value of {name} must be text")
        if len(value) > PROJECT_VARS[name].max_len:
            raise ValueError(f"the value of {name} is too long ({len(value)} > {PROJECT_VARS[name].max_len})")
    return name


def project_values(conn: sqlite3.Connection, project_id: int) -> dict[str, str]:
    """Every PROJECT_VARS variable for the project: its stored value, else the declared default."""
    import store
    out = {k: v.default for k, v in PROJECT_VARS.items()}
    for r in store.list_prompt_vars(conn, project_id):
        if r["name"] in out and r["value"] is not None:
            out[r["name"]] = r["value"]
    return out


def tool_vars(repo: Optional[str] = None) -> dict[str, str]:
    """The system variables of manager_project that say where AFClaude's own tools live, for a
    session working in `repo` (None: this checkout). afclaude_dir: the install directory.
    tools: the prefix of a command (`python3 {tools}keepalive.py`): empty in this checkout's root,
    where the commands run as typed (what AFClaude's rules always said), else the install
    directory and a slash, so another project's task-manager can run them from its own."""
    here = os.path.realpath(HERE)
    own = repo is None or os.path.realpath(repo) == here
    return {"afclaude_dir": HERE, "tools": "" if own else here + "/"}


def project_values_at(conn: sqlite3.Connection, repo: Optional[str] = None) -> dict[str, str]:
    """project_values of the project that owns the directory `repo` (the closest parent project;
    None: this checkout's), or the declared defaults when no project does. The project at this
    checkout must have every CHECKOUT_SEED row (the migrate step wrote them): otherwise its
    rules would go out without its own lines, so NotMigrated (D-235)."""
    import store
    p = store.get_project(conn, os.path.abspath(repo or HERE), nearest=True)
    if p is None:
        return {k: v.default for k, v in PROJECT_VARS.items()}
    if os.path.realpath(p["path"]) == os.path.realpath(HERE):
        missing = sorted(set(CHECKOUT_SEED) - {r["name"] for r in store.list_prompt_vars(conn, p["id"])})
        if missing:
            raise NotMigrated(f"project {p['name']!r} (this checkout) has no value for {', '.join(missing)}: "
                              "run `python3 prompts.py migrate`")
    return project_values(conn, p["id"])


def checkout_project(conn: sqlite3.Connection) -> store.Row | None:
    """The project whose path is this checkout (exactly: a worktree is not the project)."""
    import store
    return store.get_project(conn, HERE, nearest=False)


def migrate_plan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """What the migrate step would write: [{project_id, project, name, value}], the seeds of the
    project at this checkout that it never had a row for, and (with "was": the old value) a row that still
    equals the old seed (OLD_CHECKOUT_SEED, written by an earlier migrate and never changed since), which
    moves to the new one. A value someone set or reset is kept. Read-only."""
    import store
    p = checkout_project(conn)
    if p is None:
        return []
    have = {r["name"]: r["value"] for r in store.list_prompt_vars(conn, p["id"])}
    out: list[dict[str, Any]] = []
    for k, v in CHECKOUT_SEED.items():
        item: dict[str, Any] = {"project_id": p["id"], "project": p["name"], "name": k, "value": v}
        if k not in have:
            out.append(item)
        elif k in OLD_CHECKOUT_SEED and have[k] == OLD_CHECKOUT_SEED[k] != v:
            out.append(dict(item, was=have[k]))
    return out


# ---------------------------------------------------------------- CLI

def _parse_set(s: str) -> tuple[str, str]:
    k, sep, v = s.partition("=")
    if not sep or not k:
        raise argparse.ArgumentTypeError(f"KEY=VALUE expected, got {s!r}")
    return k, v


def _project(conn: sqlite3.Connection, ref: str) -> store.Row:
    import store
    p = store.get_project(conn, int(ref) if ref.isdigit() else ref)
    if p is None:
        raise ValueError(f"no project {ref!r}")
    return p


def _perform(name: str, params: dict[str, Any], deploy: bool = False) -> Any:
    import actions
    import store
    conn = store.connect()
    try:
        return actions.perform(conn, name, params, actor="runner:deploy" if deploy else "cli",
                               via="runner" if deploy else "cli")
    finally:
        conn.close()


def _override_cli(a: argparse.Namespace) -> None:
    import store
    if a.ocmd == "list":
        conn = store.connect()
        try:
            rows = override_status(conn)
        finally:
            conn.close()
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return
    name = str(a.name).removesuffix(".md")
    key = override_key(name)
    if a.ocmd == "diff":
        conn = store.connect()
        try:
            d = override_diff(conn, name)
        finally:
            conn.close()
        sys.stdout.write(d or f"{name}: no override\n")
    elif a.ocmd == "set":
        if a.file == "-":
            body = sys.stdin.read()
        else:
            with open(a.file) as fh:
                body = fh.read()
        r = _perform("prompt.set", {"name": key, "body": body})
        print(json.dumps({k: r[k] for k in ("name", "version", "updated_by", "base_outdated") if k in r}))
    else:
        r = _perform("prompt.reset", {"name": key})
        print(json.dumps({k: r[k] for k in ("name", "version", "updated_by") if k in r}))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="The prompt registry (design §7.6, phase 4a).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every prompt, its variables and senders")
    s = sub.add_parser("show", help="a prompt's default text")
    s.add_argument("name")
    r = sub.add_parser("render", help="a prompt filled in (system variables with --set)")
    r.add_argument("name")
    r.add_argument("--project", help="fill the project variables with this project's values")
    r.add_argument("--set", type=_parse_set, action="append", default=[], metavar="KEY=VALUE",
                   help="a system variable (the tool paths and session_stop_pct are filled in)")
    v = sub.add_parser("vars", help="the project variables and their values")
    v.add_argument("--project")
    sv = sub.add_parser("set-var", help="set a project's variable (owner, audited)")
    sv.add_argument("project")
    sv.add_argument("name")
    sv.add_argument("value")
    rv = sub.add_parser("reset-var", help="back to the variable's default (owner, audited)")
    rv.add_argument("project")
    rv.add_argument("name")
    sub.add_parser("bundles", help="every bundle kind and its parts (F8's view, D-111)")
    bu = sub.add_parser("bundle", help="a bundle's parts and its rendered text")
    bu.add_argument("kind")
    bu.add_argument("--project", help="fill the project variables with this project's values")
    bu.add_argument("--set", type=_parse_set, action="append", default=[], metavar="KEY=VALUE")
    bu.add_argument("--insert", default=None, help="the sender's own text for the insert part, if any")
    bu.add_argument("--hooked", action="store_true", help="the session already has the standing rules")
    bu.add_argument("--no-manager", dest="manager", action="store_false", default=True)
    ov = sub.add_parser("override", help="the owner's system-wide edit of a prompt (audited, D-193)")
    ovs = ov.add_subparsers(dest="ocmd", required=True)
    ovs.add_parser("list", help="every override with its base_outdated / default_changed / fits flags")
    o1 = ovs.add_parser("set", help="set the override from FILE (- for stdin); validated, test-rendered everywhere")
    o1.add_argument("name")
    o1.add_argument("file")
    o2 = ovs.add_parser("reset", help="back to the default")
    o2.add_argument("name")
    o3 = ovs.add_parser("diff", help="default -> override, with its flags")
    o3.add_argument("name")
    m = sub.add_parser("migrate", help="seed the project at this checkout (idempotent, audited)")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--deploy", action="store_true", help="the entrypoint's run: audited as runner:deploy")
    a = ap.parse_args(argv)
    import store
    try:
        if a.cmd == "list":
            for n, p in sorted(PROMPTS.items()):
                vs = ", ".join(sorted(p.system)) or "-"
                pv = f" | project: {', '.join(p.project)}" if p.project else ""
                print(f"{n:36} {vs}{pv}  [{p.used_by}]")
        elif a.cmd == "show":
            sys.stdout.write(default(a.name))
        elif a.cmd == "render":
            values, proj = None, None
            if a.project:
                conn = store.connect()
                try:
                    proj = _project(conn, a.project)
                    values = project_values(conn, proj["id"])
                finally:
                    conn.close()
            sys.stdout.write(render(a.name, values, **preview_system(proj["path"] if proj and proj["path"] else None, dict(a.set), set(PROMPTS[check_name(a.name)].system))))
        elif a.cmd == "vars":
            conn = store.connect()
            try:
                if a.project:
                    print(json.dumps(project_values(conn, _project(conn, a.project)["id"]), indent=2,
                                     ensure_ascii=False))
                else:
                    for k, var in PROJECT_VARS.items():
                        print(f"{k:20} default {var.default!r}: {var.help}")
                    for row in store.list_prompt_vars(conn):
                        print(f"  project {row['project_id']}: {row['name']} = {row['value']!r} "
                              f"(v{row['version']}, {row['updated_by']})")
            finally:
                conn.close()
        elif a.cmd == "bundles":
            for k, b in sorted(BUNDLES.items()):
                labels = ", ".join(part.prompt or (f"insert[{part.insert}]" if part.insert else "rules")
                                   for part in b.parts)
                print(f"{k:28} flat={b.flat!s:5} {labels}  [{b.used_by}]")
        elif a.cmd == "bundle":
            check_bundle(a.kind)
            for part in BUNDLES[a.kind].parts:
                print(f"  - {part.prompt or (f'insert: {part.insert}' if part.insert else 'rules')}")
            values, proj = None, None
            if a.project:
                conn = store.connect()
                try:
                    proj = _project(conn, a.project)
                    values = project_values(conn, proj["id"])
                finally:
                    conn.close()
            print()
            sys.stdout.write(bundle(a.kind, values, insert=a.insert, hooked=a.hooked, manager=a.manager,
                                    **preview_system(proj["path"] if proj and proj["path"] else None, dict(a.set),
                                                   {"afclaude_dir", "tools", "session_stop_pct"})))
        elif a.cmd == "override":
            _override_cli(a)
        elif a.cmd == "set-var":
            print(json.dumps(_perform("prompt_var.set", {"project": a.project, "name": a.name, "value": a.value}),
                             ensure_ascii=False))
        elif a.cmd == "reset-var":
            print(json.dumps(_perform("prompt_var.reset", {"project": a.project, "name": a.name}),
                             ensure_ascii=False))
        elif a.cmd == "migrate":
            if a.dry_run:
                conn = store.connect()
                try:
                    plan = migrate_plan(conn)
                finally:
                    conn.close()
                print(json.dumps({"would_seed": plan}, ensure_ascii=False))
            else:
                print(json.dumps(_perform("prompt_var.migrate", {}, deploy=a.deploy), ensure_ascii=False))
    except (ValueError, LookupError, OSError, sqlite3.Error, store.DBError) as e:
        print(f"prompts.py: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
