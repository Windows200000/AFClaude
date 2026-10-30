# Phase 4: prompts, decisions, questions and messages (implementation plan)

Plan for design §7.6 / §13 steps 4a and 4b: every prompt comes from one registry (`prompts.py`) with per-project variable values and one bundle per session kind; decisions, questions and the owner's messages to a task-manager live in the DB, and `DECISIONS.md` / `OPEN_QUESTIONS.md` are retired. Each step in §5 is one worker branch; P1 is built on branch `phase4`.

Binding inputs: D-110 (the task-manager/delegation prompt for every project), D-111 (the UI shows every prompt; ours are the defaults), D-169 (defaults ship with the code), D-193 (prompts are system-wide, overrides owner-only, no per-project overrides; supersedes D-152's prompt part), D-197 (templates with system and per-project variables; managers and editors set a project's values), D-225 (standing rules by the SessionStart hook), D-227 (deployed defaults copied into `prompt_defaults`), D-104/D-105 (the owner's decisions are kept and need the owner to change them; the task-manager may decide), D-107/D-242 (one row per decision with keywords; TLDR after a compaction, then lookup by keyword), D-151/D-181 (questions and decisions in the DB; no file export), D-158 (only the current decision, changes overwrite), D-176 (expand a summary before acting on or pasting it), D-178/D-186 (message box; owners and managers wake the task-manager, editors wait), D-192/D-196 (who decides and who sees questions), D-241/D-244/D-246/D-248/D-250/D-252 (the inbox as built), D-263/D-279 (questions in plain words, readable in full in the UI), D-278 (one-offs are not decisions), D-154 (one write path with audit), D-161 (everything in the DB), D-209 (mypy --strict), D-235 (fail fast), D-284 (titles stay in lists; topics are free keywords, "all" allowed, several per entry, kept low by prompts), D-285 (owner chat statements recorded active at once; only the task-manager's own entries/rewordings wait for confirmation), D-286 (expired one-offs move to the change history).

## 0. Already decided

- **"Project scope" means per-project variable values, not per-project prompts.** §13 still cites D-152 ("prompt edits per project"); D-193 superseded that part and D-197 replaced it with variables. So `prompt_overrides` stays keyed by name only, and a project only fills variables. The design text that still shows project overrides (§3 F8 "project override", §8 `prompts/{name}?project=`, §12 "project > global > default") is corrected in P5.
- **Defaults stay files** (D-169); the DB keeps a copy (D-227, built) and the owner's edits (`prompt_overrides`, built, no live rows today).
- **Byte-identical first.** Every 4a step that moves code keeps what each sender sends byte for byte; a test renders every prompt kind through the old and the new path. Only P3 changes a template, and even there AFClaude's rendered text stays identical.
- **The inbox is the message store.** F14's `chat_threads`/`chat_messages` (§7.2) are built on the existing `inbox_messages` (D-241 ff.), not beside it: one message store, one delivery path.
- **Expired one-off decisions move to the change history** (the audit log, D-158's "a decision that no longer applies is deleted, audited"), like replaced decisions (D-286, later than D-278's 21.10 preference for keeping them marked `expired`). No `expired` state is kept in the table.
- **Decision lists and search keep showing titles.** D-284 answers question 1: the topic-label-instead-of-title option is dropped. Each entry also gets one or more free-keyword topics ("all" allowed where an entry applies to everything); topics aren't predetermined and the total stays low, loosely enforced by prompts, not a hard cap.
- **Owner chat statements are recorded active immediately.** D-285 answers question 2: what the owner says is written in with their exact words and the date, active at once; only the task-manager's own entries, and its rewordings or merges of the owner's, wait for the owner's confirmation on the dashboard.

## 1. Today (as-is inventory)

| what | where | state |
|---|---|---|
| 31 default prompts | `prompts/*.md`, table in `prompts/README.md` | files; placeholders are `str.format` fields |
| loaders | `keepalive.load_prompt` (also used by `usage_review`, `dispatcher`, `inbox_worker`), `nightly_tasks._prompt`, `inbox.prompt` → `actions.prompt_default`, `usage_sampler.HAIKU_PROMPT` (read at import) | four copies of "read `prompts/<name>.md`"; nothing reads overrides |
| bundles | `keepalive.session_message` (prompt + insert + hook pointer, or guard + manager + manager_afclaude + afk inline; one line), `keepalive.standing_rules` (the two hook parts), `usage_review` one-off (brief + guard + manager), `nightly_tasks.brief` | assembled in code, per sender |
| per-project parts | `manager_afclaude.md` for AFClaude sessions only (`afclaude=True`); `{progress}` = this checkout's PROGRESS.md | implicit; 3b S4 sends other projects the rules inline without it |
| `prompt_overrides`, `prompt.set/reset`, `actions.prompt_text` | store/actions (v4) | built, owner-only, unused by senders |
| `prompt_defaults`, `prompt.defaults_sync` | `store.py import-prompts` at every container start (D-227) | built |
| decision store, first version | `decisions` + `decisions_fts` (LIKE without FTS5), `decisions.py` (importer from `DECISIONS.md`, every 10 min and at the hook), `decision.import` / `decision.keywords` (audited), `tasks.py decision tldr\|find\|show\|keywords\|import`, MCP `afclaude_decisions` (find/show, `full=false` lists ids + keywords), SessionStart TLDR in 3 parts (compact/startup/clear, D-242) | built; `DECISIONS.md` is still the source; states `active\|partial\|done\|superseded`, "expired …" and "active until <date>" Status lines (D-278) |
| not yet | `slug`, `project_id`, `author`, `topics` (free keyword list per entry, D-284), `decision_links`, proposals, writes other than import/keywords | — |
| questions | `tasks` kind `question` (born blocked, closed by `task.answer`), MCP `afclaude_ask`; `OPEN_QUESTIONS.md` (local) read by the quickview in full (D-279) | two sources |
| messages | `inbox_messages` (verbatim, triage, hold, delivery, `tasks.py inbox reply`), `inbox_api`/`inbox_worker`, quickview box | built for the AFClaude task-manager; no threads, no MCP reply tool |

## 2. What changes

- **4a:** one module, `prompts.py`, knows every prompt (file, system variables, project variables, which session kinds use it), renders strictly (an unknown or missing variable is an error, D-235), resolves override > default, and defines the bundles. Every sender calls it. `manager_afclaude.md` becomes the generic task-manager rules template; AFClaude's specifics become the AFClaude project's values.
- **4b:** the DB becomes the source for decisions and questions; the task-manager writes through `actions.py` (CLI/MCP); the two files are read one last time by the seeding and then retired (no export, D-181). The inbox gets threads and an MCP reply tool.
- **Not in phase 4:** the role matrix (managers/editors as actors, 5a: until then only the owner and the CLI write, autonomous MCP writes to prompts and variables are refused like `prompt.set`); the F8/F1/F14 UI (6c/7a/7b).

## 3. Design details

### 3.1 Registry (`prompts.py`)
- `PROMPTS: {name: Prompt(system, project, used_by, flat)}`, one entry per file. A test pins it to `prompts/`: every file registered, every entry has its file, the placeholder set equals `system ∪ project`, `prompts/README.md` names each file.
- `PROJECT_VARS: {name: ProjectVar(help, default, max_len)}`, the variables a project fills (D-197: repo facts, the public-repo guard, the files that need worktrees, where goals/questions live, `project_notes`). Values are text; a project without a value gets the variable's declared default (part of the registry, like a setting's default; not a fallback for a failed read).
- `prompts.default(name)`: the file (D-169), the only loader. `prompts.render(name, project_values=…, **system)`: `str.format` with exactly the declared names.
- Project values: table `prompt_vars(project_id, name, value, version, updated_at, updated_by)`, PK `(project_id, name)`, version trigger; written only by `prompt_var.set` / `prompt_var.reset` (audited, `owner_only` until 5a; the value checked against `PROJECT_VARS`). Additive (no schema bump).
- **Migrate step:** `python3 prompts.py migrate [--dry-run]` writes each project's seed values (today only AFClaude's: the project at this checkout's path) where none is set. Idempotent, audited (`prompt_var.migrate`, actor `runner:deploy` from the entrypoint, `cli` by hand); a re-run changes nothing; a value someone set is never overwritten.

### 3.2 Bundles (P2)
`BUNDLES: {kind: parts}` for: task-manager continue (session-window start, slot, fill-up, last mile), stall continue (AFClaude / foreign), project start, task start, compaction (save, `/compact`, after + resume), inbox now / held / notes, the standing-rules hook parts, usage review (monthly brief, one-off, ETA variants), nightly task brief, Haiku judges (`usage_haiku`, `inbox_triage`). A part is a prompt, an insert slot (hand-off, pacing, inbox, nightly tasks) or a rule block that the hook supplies when `hooked`. `session_message`, `standing_rules`, the one-off review and `nightly_tasks.brief` become calls into it. `prompts.py bundle KIND [--project P]` prints the parts and the rendered text (F8's view, D-111).

### 3.3 Decisions (4b)
- Columns added to `decisions`: `slug`, `project_id` (NULL = install-wide), `author` (`owner|task-manager`). A new table `decision_topics(decision_id, topic)` holds the free keywords (D-284): one or more per entry, "all" allowed where it applies to everything, not a predetermined list; the prompts that write a decision encourage a low count without enforcing one. The state set gains `proposed`; the CHECK constraint makes that a table rebuild, so it goes through `store.MIGRATIONS` and `store.py migrate` (backup first, design §7.1). No `expired` state: an expired one-off is deleted like any decision that no longer applies, audited (D-286).
- Writes: `decision.add`, `decision.edit` (overwrite, old and new in the audit row, D-158), `decision.expire` (deletes with the reason "expired" in the audit row, D-286), `decision.delete`, `decision.confirm` (a proposal becomes active), `decision.link` (`refines|requires|conflicts`, shown with every hit), `decision.topics` (add/remove keywords). Who may write what: question 2 (below; unrelated to the now-answered D-284/285/286).
- The TLDR and `find` without `full` list id + **title** (D-284: titles stay; topics are keywords used to find entries, not a label that replaces the title in listings); `show` / `full=true` gives the entry, its topics and D-176's expand-before-acting notice. Keywords as built, reused before a new topic is added.
- Seeding (§7.6): active owner entries verbatim as `active`; superseded and drift (`DR-NN`) entries into the audit log only; partly superseded entries merged by the task-manager into one `proposed` entry; expired one-offs into the audit log only too (D-286, not kept as rows).

### 3.4 Questions and proposals (4b)
- A question stays a `tasks` row of kind `question` (§7.2: "questions as tasks"), with `project_id`, the full text (D-279) and an optional `payload` (a decision draft) for a proposal. Answering a proposal approve / edit / reject applies `decision.add|edit|confirm` in the same transaction, actor = the answering human (D-104).
- `OPEN_QUESTIONS.md` items are imported once as questions (one item = one question, full text); the quickview's needs-your-input reads only the DB.
- CLAUDE.md and the manager prompts: "delete resolved items from OPEN_QUESTIONS.md first" becomes "close the question in the DB first" (`tasks.py answer` / MCP).

### 3.5 Messages (4b)
- `chat_threads(id, project_id, opened_by, opened_at, last_at, closed_at)` and `inbox_messages.thread_id` (additive). A message opens a thread or appends to the open one; a thread idle for `ui_chat_reset_hours` (built setting, D-178) closes.
- The task-manager's answer (`tasks.py inbox reply`, new MCP `afclaude_reply`) is a message in the thread; MCP `afclaude_messages` lists the open thread. Triage, hold and delivery stay as built (D-241/D-244/D-252).
- Waking: an owner's or manager's message is a `task_manager_message` request through 3b's run-now path (D-186); an editor's waits for the next start (only the owner exists until 5a).

## 4. Order

- P1 has no dependency (built now). P2 after P1. P3 needs the start to know its project (3b S4); until then 3b sends other projects the rules inline without `manager_afclaude.md`, and P3 replaces that with the generic template. P4 any time after P2. P5 last.
- 4b Q1–Q3 need no 3b step. Q4's waking needs 3b S5 (run-now). Q5 after Q1–Q4 have run one week.
- Code the live system imports (`keepalive.py`, `usage_sampler.py`, `dispatcher.py`, `inbox_worker.py`) changes only in worktrees; P2 and P3 touch `keepalive.session_message`, so they are not run in parallel with a 3b step that edits it.

## 5. Steps

**P1: registry, project variables, migrate step (built on `phase4`).**
- **Build:** `prompts.py` (§3.1; mypy --strict); `prompt_vars` + store functions; `prompt_var.set/reset/migrate`; CLI `prompts.py list|show|render|vars|set-var|reset-var|migrate`; the four loaders call `prompts.default` (`keepalive.load_prompt`, `nightly_tasks._prompt`, `inbox.prompt`, `usage_sampler.HAIKU_PROMPT`). No template uses a project variable yet; the entrypoint does not run `migrate` yet.
- **Tests:** the registry against the files; strict render; the actions (validation, audit, owner-only for autonomous MCP, version); migrate twice (second a no-op, a set value kept); a before/after test that renders every prompt kind and every sender path (session messages with every flag, hook parts, compaction texts, run pacing, inbox texts, nightly briefs, usage review one-offs, Haiku prompts) through a frozen copy of the old loaders and through the new code, byte-identical.
- **Done when:** suites, `test_typing`, `test_mcp_server`, `check_public` green. Deploy: imports only (no restart needed beyond the usual).
- **Risk:** a sender that builds a prompt outside these paths. The test enumerates the call sites by `grep`; any new one fails it.

**P2: bundles per session kind.**
- **Build:** §3.2; senders call `prompts.bundle`. `prompts.py bundle KIND`.
- **Tests:** byte-identical for every kind and flag (the P1 harness); one bundle per kind used by each sender.
- **Done when:** as P1, plus one night's starts, compaction and inbox delivery with unchanged texts in the transcripts.
- **Risk:** `session_message`'s one-line flattening and the hook's newlines differ per kind; the bundle carries `flat` explicitly.

**P3: the generic task-manager template (D-197).**
- **Build:** `manager_afclaude.md` → `manager_project.md` with project variables (§3.1); lines that cannot be phrased with a value (the public-repo rule) are whole-line variables with an empty default; `{progress}` becomes the project's progress location; senders pass the start's project (3b S4); the entrypoint runs `prompts.py migrate` after `import-prompts`; the hook renders the AFClaude project's values.
- **Tests:** AFClaude's rendered text byte-identical to the old file's (fixture kept until P5); a second project gets the template with its own values and no AFClaude paths; the inline rules for a hook-less project fit one tmux line (3b S4's length check).
- **Done when:** a night of AFClaude starts unchanged; `prompts.py render manager_project --project <test>` reviewed by the task-manager.
- **Risk:** the SessionStart hook now reads `prompt_vars`; a DB error there gives the hook's existing "read these files" text, which names the template, not the values. Kept as is (a hook must not fail a session start); flagged to the 8a review.
- **Built (branch `4a-p3`):** `manager_project.md` (system variables `session_stop_pct`, `afclaude_dir`, `tools` = the command prefix, empty in this checkout, else the install directory; project variables `goals_location`, `progress_location` (rework `p3-progress`: the old `{progress}` variable was never added; defaults "the project's goals doc" / "the project's progress doc", AFClaude's seeds GOALS.md / PROGRESS.md), `questions_location` and the whole-line `public_repo_rule`, `worktree_rule`, `project_notes`, `docs_rule`, `deploy_rule`, AFClaude's values seeded by `migrate`); `session_message(repo=...)` replaces the `afclaude` flag and reads the values of the project that owns `repo`; a checkout project without its seed rows is `NotMigrated` (an error for a send, the hook's fallback text for the hook); the entrypoint runs `migrate --deploy`. A hook-less project's inline rules now carry the template (about 3.4 KB), so the first start's description fits only up to about 800 characters inside the 15,000-byte tmux line (the real fix is a hook for every project).

**P4: overrides in every sender.**
- **Build:** `prompts.text(name)` = `prompt_overrides` > default (D-193) in every bundle; `prompt.set` validates against the registry (system + project variables); `prompts.py override set|reset|diff` (owner, the existing actions, the `base_outdated` flag shown); a read-only MCP `afclaude_prompts` (list, bundle, render for a project).
- **Tests:** an override reaches every sender of its prompt; a stale override (`base_outdated`) is reported, not silently used with missing variables; a DB error fails the send (D-235).
- **Done when:** a test override on a throwaway prompt is seen in a dry-run start, then reset.
- **Risk:** a bad override reaches a live session; `prompt.set` test-renders every bundle that uses it.
- **Built (branch `4a-p4`):** `prompts.text(name, conn=None)` = override > default, read from the DB at every send (`load_overrides`, `store.retrying`; a DB error propagates, no fallback); `render` / `bundle` and `keepalive.load_prompt`, `nightly_tasks._prompt`, `inbox.prompt`, `usage_sampler.haiku_prompt()` (replaces the import-time `HAIKU_PROMPT`) go through it, a bundle reads the overrides once. The override key stays the file name (`continue.md`, the DB's existing key). A use-time fit check (`StaleOverride`: placeholders no longer equal the prompt's system + project variables) fails the send; `prompt.set` checks against the registry's variables and `try_render`s the body in the prompt and every bundle that sends it (the rules tail in its hooked / manager variants). `store.put_prompt_override` now sets `base_outdated=0` in the same statement (a fresh edit is against the current default; before, a re-set after an outdated one kept the 1) and a reset clears it. `prompts.py override list|diff|set|reset` (`override_status`: `base_outdated` as stored, `default_changed` live, `fits`); MCP `afclaude_prompts` (list / bundle / render, read-only; the tool-list size bound in `test_mcp_server` 15000 -> 16500). The SessionStart hook keeps its fallback text (a stale override or a DB error there gives it, with the cause named). Deploy: no migration; MCP sessions pick up the new tool at their next start.

**P5: cleanup.**
- **Build:** design §3 F8, §8, §12 and §13 4a corrected to D-193/D-197; `prompts/README.md` checked against the registry; README file table; the P3 fixture removed.
- **Done when:** the docs and the code agree (the 8a check, D-145).

**Q1: decisions as the source.**
- **Build:** §3.3 (migration, actions, `decision_topics`, links); `tasks.py decision add|edit|expire|link|confirm|topics`; MCP `afclaude_decisions` gains `propose`; TLDR and find list by title, `find` also matches topics.
- **Tests:** the migration on a copy (backup made, rows equal); each action (audit before/after, version, overwrite); a low-topic-count nudge in the prompts (not enforced); TLDR under the hook cap; `full=false` carries the expand notice.
- **Done when:** suites green; `store.py migrate` run on a copy of the live DB and checked by the task-manager before the live run.
- **Risk:** the live DB is read-only between deploy and `migrate` (A1); the deploy runs `migrate` right after the backup, outside a run.

**Q2: seeding.**
- **Build:** the one-off import per §3.3 (task-manager writes each entry's topics; partly superseded entries merged as `proposed`); the 10-min `DECISIONS.md` import stops.
- **Tests:** synthetic sources: every active entry once, verbatim; superseded/drift/expired-one-off only in the audit log; counts match.
- **Done when:** the owner has the merged `proposed` entries as questions; `decision tldr` lists every current entry.

**Q3: questions and proposals.**
- **Build:** §3.4; the `OPEN_QUESTIONS.md` import; quickview from the DB only; prompts, CLAUDE.md and the worker agent switched.
- **Tests:** a proposal's approve/edit/reject applies the decision change atomically; import counts; the quickview shows each question in full (D-279).
- **Done when:** one week with questions asked and closed only through the DB.

**Q4: message threads.** §3.5. **Tests:** thread open/append/reset; reply via MCP; waking by role (owner now; editors wait, simulated). **Done when:** a quickview message answered through `afclaude_reply`. **Risk:** depends on 3b S5.

**Q5: retire the files.** `DECISIONS.md` and `OPEN_QUESTIONS.md` are no longer read or written (no export, D-181); they stay on disk untouched for the owner to archive. **Done when:** `grep` finds no reader outside tests and the seeding.

## 6. Questions for the owner

None open. The three questions this plan raised (what the decision list shows, recording chat decisions, expired one-offs) are answered by D-284, D-285 and D-286 respectively; see §0 and §3.3.
