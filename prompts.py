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
only until the roles of phase 5a give it to the project's managers and editors). No prompt uses a
project variable yet: the generic task-manager template is step P3 of the plan.

Migrate step: `prompts.py migrate` writes the seed values (CHECKOUT_SEED: what the AFClaude-repo
rules hard-code today) for the project at this checkout's path, each only where that project has
never had a row for it (a value someone set or reset is never overwritten). Idempotent and audited
(prompt_var.migrate); a project-less checkout has nothing to seed.

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
                                   [--hooked] [--no-manager] [--no-afclaude]
    python3 prompts.py vars [--project P]           # the project variables and their values
    python3 prompts.py set-var PROJECT NAME VALUE | reset-var PROJECT NAME
    python3 prompts.py migrate [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
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
    "manager": Prompt(used_by="standing rule: task-manager sessions, the hook part manager"),
    "manager_afclaude": Prompt(("session_stop_pct",), used_by="standing rule: AFClaude-repo sessions, "
                               "the hook part afclaude"),
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

# D-197: what a project fills. Values are text; the design's list: repo facts, the public-repo guard,
# the live files that need worktrees, where goals and questions live, a short free-text note.
PROJECT_VARS: dict[str, ProjectVar] = {
    "public_guard": ProjectVar("the public-repo guard the commit hooks run (empty: the repo is not public)"),
    "worktree_files": ProjectVar("files the live system imports: change them only in a git worktree"),
    "goals_location": ProjectVar("where the project's goal list lives"),
    "questions_location": ProjectVar("where the project's open questions for the user live"),
    "project_notes": ProjectVar("a short free-text note for the project's task-manager", max_len=2000),
}

# The migrate step's values for the project at this checkout's path (AFClaude itself): what
# manager_afclaude.md hard-codes today, so the generic template (plan P3) renders the same text.
CHECKOUT_SEED: dict[str, str] = {
    "public_guard": "tools/check_public.py",
    "worktree_files": "keepalive.py, store.py, stalled.py, usage_sampler.py, export_quickview.py, ka_resume.sh",
    "goals_location": "GOALS.md",
    "questions_location": "OPEN_QUESTIONS.md",
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
              manager_afclaude (if afclaude=True) and afk_rules when manager=True and not hooked;
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
    "afclaude_rules": Bundle((Part(prompt="session_rules"), Part(prompt="manager_afclaude"),
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


def render(name: str, project_values: Optional[Mapping[str, str]] = None, /, **system: object) -> str:
    """The default of `name` filled in: exactly its system variables from `system`, its project
    variables from project_values (a project's values, e.g. project_values()) or their default.
    A missing or an unknown system variable is a ValueError (no partial text goes out, D-235).
    `name` and `project_values` are positional-only (P2): a prompt can declare a system variable
    literally called "name" (nightly_task.md does) or "project_values" without colliding with
    render()'s own parameters -- callers that pass a bundle's **system straight through are common
    (nightly_tasks.brief)."""
    spec = PROMPTS[check_name(name)]
    missing, unknown = set(spec.system) - set(system), set(system) - set(spec.system)
    if missing or unknown:
        raise ValueError(f"prompt {name}: missing variable(s) {sorted(missing)}, unknown {sorted(unknown)}")
    values: dict[str, object] = dict(system)
    for v in spec.project:
        given = (project_values or {}).get(v)
        values[v] = PROJECT_VARS[v].default if given is None else given
    return default(name).format(**values)


# ---------------------------------------------------------------- bundles (P2, §3.2)

def check_bundle(kind: object) -> str:
    if not isinstance(kind, str) or kind not in BUNDLES:
        raise UnknownBundle(f"no bundle {kind!r} (the registry: prompts.BUNDLES)")
    return kind


def _rules_tail(hooked: bool, manager: bool, afclaude: bool, project_values: Optional[Mapping[str, str]],
                system: Mapping[str, object]) -> list[str]:
    """The standing-rule tail of a session message (Part(rules=True)): see Part's docstring."""
    if hooked and manager:
        return [default("rules_pointer")]
    if not manager:
        return [default("guard_respect")]
    tail = [default("guard_respect"), default("manager")]
    if afclaude:
        fields = {k: system[k] for k in variables("manager_afclaude") if k in system}
        tail.append(render("manager_afclaude", project_values, **fields))
    tail.append(default("afk_rules"))
    return tail


def bundle(kind: str, project_values: Optional[Mapping[str, str]] = None, /, insert: Optional[str] = None,
          hooked: bool = False, manager: bool = True, afclaude: bool = True, **system: object) -> str:
    """BUNDLES[kind] rendered: each prompt part gets exactly its own declared system variables out
    of **system (one **system serves every part of a bundle; render()'s strict check still applies
    per part; `kind` and `project_values` are positional-only, render()'s own reason), the insert
    part gets `insert` if given (skipped if empty), the rules part gets the standing-rule tail
    (_rules_tail; a bundle without one ignores hooked/manager/afclaude). Joined
    per the bundle's own `flat` (BUNDLES[kind].flat): one line (tmux send-keys), or a blank line
    between parts, each stripped (hook context)."""
    b = BUNDLES[check_bundle(kind)]
    chunks: list[str] = []
    for part in b.parts:
        if part.prompt:
            fields = {k: system[k] for k in variables(part.prompt) if k in system}
            chunks.append(render(part.prompt, project_values, **fields))
        elif part.rules:
            chunks.extend(_rules_tail(hooked, manager, afclaude, project_values, system))
        elif insert:
            chunks.append(str(insert))
    if b.flat:
        return " ".join(" ".join(chunks).split())
    return "\n\n".join(c.strip() for c in chunks)


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


def checkout_project(conn: sqlite3.Connection) -> store.Row | None:
    """The project whose path is this checkout (exactly: a worktree is not the project)."""
    import store
    return store.get_project(conn, HERE, nearest=False)


def migrate_plan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """What the migrate step would write: [{project_id, project, name, value}], the seeds of the
    project at this checkout that it never had a row for. Read-only."""
    import store
    p = checkout_project(conn)
    if p is None:
        return []
    have = {r["name"] for r in store.list_prompt_vars(conn, p["id"])}
    return [{"project_id": p["id"], "project": p["name"], "name": k, "value": v}
            for k, v in CHECKOUT_SEED.items() if k not in have]


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


def _perform(name: str, params: dict[str, Any]) -> Any:
    import actions
    import store
    conn = store.connect()
    try:
        return actions.perform(conn, name, params, actor="cli", via="cli")
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="The prompt registry (design §7.6, phase 4a).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every prompt, its variables and senders")
    s = sub.add_parser("show", help="a prompt's default text")
    s.add_argument("name")
    r = sub.add_parser("render", help="a prompt filled in (system variables with --set)")
    r.add_argument("name")
    r.add_argument("--project", help="fill the project variables with this project's values")
    r.add_argument("--set", type=_parse_set, action="append", default=[], metavar="KEY=VALUE")
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
    bu.add_argument("--no-afclaude", dest="afclaude", action="store_false", default=True)
    m = sub.add_parser("migrate", help="seed the project at this checkout (idempotent, audited)")
    m.add_argument("--dry-run", action="store_true")
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
            values = None
            if a.project:
                conn = store.connect()
                try:
                    values = project_values(conn, _project(conn, a.project)["id"])
                finally:
                    conn.close()
            sys.stdout.write(render(a.name, values, **dict(a.set)))
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
            values = None
            if a.project:
                conn = store.connect()
                try:
                    values = project_values(conn, _project(conn, a.project)["id"])
                finally:
                    conn.close()
            print()
            sys.stdout.write(bundle(a.kind, values, insert=a.insert, hooked=a.hooked, manager=a.manager,
                                    afclaude=a.afclaude, **dict(a.set)))
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
                print(json.dumps(_perform("prompt_var.migrate", {}), ensure_ascii=False))
    except (ValueError, LookupError, OSError, sqlite3.Error, store.DBError) as e:
        print(f"prompts.py: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
