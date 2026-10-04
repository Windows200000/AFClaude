# AFClaude dashboard and backend: target design (v2)

Status: target design for phases 2–9, rewritten on 04.10.2026 after the owner's architecture review (D-143/D-145; answers D-146–D-191). Phase 1 (schema v4, `actions.py`, `afclaude_config.py`) is built. This doc replaces v1 as a whole; where v1 and this doc differ, this doc holds. `D-NNN` ids point at the owner's local decision store; owner decisions are binding, everything marked *proposal* is the task-manager's and may be changed. Host names are placeholders.

**Terms (D-189).** A **task-manager** is the Claude session that runs a project: exactly one per project, frequently compacted (§5.5); there is no global task-manager, and AFClaude's own project simply has its task-manager like any other (`projects.manager_session` holds the task-manager's session id). A **manager** is a user role (§2). **Agents** are the worker sessions and subagents that work for a task-manager.

## 1. Goals and non-goals

Goals
- One place, usable from a phone, for everything AFClaude needs from people or wants to show: questions, blocked tasks, stalled sessions, the queue, windows, usage and budget, decisions, prompts, the sessions AFClaude drives, hand-offs and the project docs.
- Built from scratch with its own architecture (D-070). The temp dashboard (the read-only `/private` status page, `export_quickview.py` + `quickview/`) is NOT part of this design; it stays in use until the dashboard is live and is then retired as a separate side task (§10.7).
- **Everything lives in one DB** (D-161): config, auth, work, decisions, questions, hand-offs, prompt overrides, usage telemetry, runner state and the project docs. A backup or a move to another host restores a complete, clean continue (§11). Only prompt *defaults* ship with the code.
- **Everything the dashboard can change is a DB setting** that the runners read there (D-146). Env vars only bootstrap a container (D-163).
- **One write path**: dashboard, runners, MCP and CLI all write through `actions.py`, which validates and audits every write (D-154).
- **AFClaude runs in containers**, deployable on Coolify or plain docker with minimal env vars (D-163). The Claude CLI sessions stay on the host, in AFClaude's own working root, and the dashboard validates the host setup (D-164, D-165).
- The dashboard never executes anything itself. It records intent in the DB and wakes the runner, which acts (§5).
- Generic code in the public repo; identities, keys, the backlog and all runtime data stay in the DB or local.

Non-goals (v1)
- Several Claude accounts. One account per installation, shared by all users (D-156); the shared pool is future (§14).
- Showing transcripts. Titles, cwd, the stall notice and short snippets are enough; the Claude app is where a session is read.
- Driving Remote Control (RC) or speaking its protocol (internal protocol, against the terms). The dashboard links to sessions, it doesn't host them.
- Replacing the MCP server as the main way tasks get added.
- A budget-rule editor. The thresholds are settings; the rule itself is code (`pacing.py`, D-141).

## 2. Users, roles and access (D-149, D-156, D-160, D-172, D-182–D-184, D-189, D-190, D-195, D-196)

User roles (people logged in to the dashboard, and machine users, below):

| role | who | can |
|---|---|---|
| **owner** | the first browser registration (guarded by the one-time setup code, §9.3), plus every user an owner makes owner or an OIDC group grants it (§9.4); several owners allowed | everything, incl. making and removing owners; only owners see and change global settings (windows, budget, auth, users, backup) (D-149) |
| **manager** | granted per project | in that project, everything its task-manager can do (docs, goals, tasks, stages, priorities, order, question answers, stall decisions and rules for its sessions, proposing decisions, deciding agent proposals, messages to its task-manager), plus **forcing an immediate run or resume when usage doesn't allow it**: run-now / work-on-now (§5.4) and messages that wake the task-manager at once (F14) (D-184, D-186, D-190); never global settings |
| **editor** | granted per project | the same as a manager in that project, except that it can't force anything immediate: its run requests and messages wait for the next regular run (D-184, D-186, D-190) |
| **viewer** | granted per project | read-only: that project's stages, questions (seen in the inbox, but not answerable, D-196), decisions, hand-offs, effective prompts, its driven sessions |
| *(none)* | every other registered login | nothing; sees "no access yet" (default for new users, D-149) |

**Machine users (D-195).** Interactive Claude sessions that use the MCP server on the user's request act as a separate user per **machine**: a host or VM with Claude Code installed. Every machine whose Claude Code has the AFClaude MCP server registered is one machine user (`users.kind = machine`), labelled with its hostname at registration and renamable by owners; all interactive sessions on that machine share it. A machine user has no dashboard login; it is identified by a per-machine token issued at registration (§9.7). It gets grants like any user (viewer, editor or manager per project) and then has exactly that role's rights through MCP. A newly registered machine user has **no access until an owner grants it** a role; the setup wizard offers to grant this host's machine user one (§10.4) (D-195). *Proposal:* the owner role can't be granted to a machine user (MCP exposes no owner-only actions, and a token stored on a machine is a weaker credential than an owner's login).

Session and component roles:

| role | who | can |
|---|---|---|
| **task-manager** | the project's one task-manager session (`projects.manager_session`, D-189), AFClaude's own project included | in its project: write the docs, goals and progress, add/edit/order tasks, ask questions (to the owners and the project's managers and editors; the project's viewers see them read-only, D-196), propose decisions, write its hand-offs, reply to messages (F14), review its agents' proposals and merge their branches (§7.9); never owner-only actions (D-160) |
| **agent** | every other AFClaude session working for a project (task and worker sessions, subagents told apart from their task-manager) | only the direct writes of §9.6 (own hand-offs, its task's status and notes, failure escalations, its own branch, D-185); everything else becomes a proposal to its project's task-manager (§7.9, D-182) |
| **runner** | the runner daemon's components | the state changes the runners own (task start/finish, run log, telemetry), audited as `runner:<component>` |

- **The only difference between manager and editor is forcing an immediate run** (D-190). Owners, managers and editors can all decide agent proposals in the dashboard (F9). Further differences, *proposals* for the owner:
  - Managers can grant and revoke **editor** and **viewer** access to their project (not manager or owner; those stay with owners) (D-192).
  - Managers and editors both **make** (confirm) project-scoped decisions; task-managers only **propose** them (§7.6, D-192). Install-wide decisions are confirmed by owners only.
- The last owner that isn't group-derived can't be removed or demoted, so a group change at the OIDC provider can't leave the install without an owner (D-183).
- Granting **editor** or **manager** on a project lets that user steer autonomous sessions that run with the guard hooks bypassed on the execution host (an answer, a stage or a message becomes a session's input). The grant dialog says so.
- **Choosing the work is algorithmic** (D-189): which project runs and which task or stage inside it is picked by the dispatcher, by project rank and stage priority (D-064); people steer it through the ranks, the priorities and run-now (§5.4). The project's task-manager then does the run and delegates to agents.
- **Questions (D-196):** a project's questions (incl. blocked tasks and decision proposals) go to the owners and the project's managers and editors, who can answer them; the project's viewers see them too, read-only, without an answer box.
- **Interactive sessions via MCP** (D-068, D-189, D-195): any interactive Claude session (one AFClaude didn't start) can add, remove and manage tasks through the MCP server, but only on the user's explicit request. Such a session acts as its machine's machine user (above), with that machine user's grants, never as an agent or a task-manager, and never as the first owner or another person.
- The audit actor is the user (`user:<id>`), the machine user (`machine:<id>`; the audit row also records the MCP session id when known), the task-manager session (`task-manager:<session>`), the agent session (`agent:<session>`) or the component (`runner:dispatcher`).
- v1 enforcement of the task-manager and agent roles is advisory (D-160, D-173): rejected attempts are audited and raise an alert; see §9.6 for its limits.

## 3. Main flows (phone first)

One column, large tap targets, no hover-only controls, no drag-and-drop as the only way to reorder, every page useful on its first screen. Top bar: automation state (running / paused, toggle), tonight's window ("23:00–09:00, 2 × 5 h"), weekly usage and the projected end of week, inbox badge. Every view is filtered by the viewer's grants.

- **F1 Inbox (home).** Only real questions (D-083): open questions (`kind='question'`) and blocked tasks (from agents only once their task-manager forwarded them, §7.9); decision proposals waiting for confirmation. A project's questions go to the owners and that project's managers and editors, each card with a text box; the project's viewers see the same cards read-only, without a box (D-196). Never stalls, never untitled sessions. Answering removes the card at once (the server returns the new inbox). Empty inbox = one line.
- **F14 Message the task-manager (D-178, D-186), on the inbox page next to the answers.** A general box for anything that isn't an answer to an open question, mainly questions to a project's task-manager or new goals; a minimal embedded chat. The box addresses one project's task-manager (a project picker over the projects the user is editor, manager or owner of). It starts as one text box; after sending, it shows the message and, once it arrives, the task-manager's one reply. It grows into a chat (the thread with a new box under it) only when the user sends a follow-up. After `ui_chat_reset_hours` (default 5 h) without a new message in the thread, the thread is closed and the page shows an empty box again (closed threads stay in the DB, reachable from "earlier messages"). Mechanics: each message is a row in `chat_messages` (§7.2); sending queues a `task_manager_message` request. **Who wakes it (D-186):** a message from an owner or from a manager of that project wakes the runner, which delivers it to the task-manager immediately, like run-now (D-153, §5.4: no window or budget check, works while paused, preflight applies): a running task-manager gets a short notice at its next idle point, an idle one is resumed with it. A message from an editor waits for the task-manager's next regular run (its continue prompt carries it; a task-manager that is running anyway gets it at its next idle point). The task-manager reads the thread and replies through MCP (`afclaude_messages`, `afclaude_reply`; the reply is a task-manager write, not an agent write); goals it accepts it records through its normal tools (tasks, docs) and says so in the reply. While waiting, the card shows the request state (queued → delivered → answered).
- **F2 Stalled sessions.** Only sessions whose last entry is a limit notice (D-041), newest first, own AFClaude sessions hidden by default. Card: title (or cwd + first prompt), project, stall kind, reset time, the effective decision and its source. Buttons: Continue, Ignore, "Always…" (session rule or project rule, preselecting the most specific cwd), and **Run now** (§5.4). Undecided stalls never expire; decided ones move to a "decided" filter. RC-server threads show "continue it from the app" (§10.8).
- **F3 Queue and projects.** Ranked projects with their stages and a priority chip (tap cycles high → medium → low, D-064). Up/down buttons, "move to position…", optional drag on desktop. Per-project menu: whole project to medium/low, edit, its task-manager session, **Work on now** (owners and the project's managers, D-036, §5.4), members (grants). A second tab shows the flat execution order and why an item is skipped.
- **F4 Window planner.** Seven rows Mon..Sun with each night's window or "off"; linked days share a colour (D-034, §6). Editor: change all linked / only this day / whole week; preview over the next 7 nights incl. DST nights and the weekly-reset marker; the measured ratios (`limit_ratio.py`: weekly % per session window, windows per week and left, AFClaude vs user share, each with "insufficient data" when honest). Save applies at once (the runner is woken).
- **F5 Status.** Runner health (last tick per job), the latest budget decisions with their reason, run log, the next usage review, review results split into universal and user-specific, and the reserve-threshold numbers (§3.1).
- **F6 Driven sessions.** Every session AFClaude started or continued: readable tmux name (§5.3), kind, project, state, RC link when known, the latest hand-off.
- **F7 Decisions.** Per project (and install-wide): the current decisions (D-104, D-158), keyword filter, proposals with "confirm / reject / edit" (for those who may confirm, §7.6), "propose a change".
- **F8 Prompts.** Every prompt with its placeholders and the session kinds that use it; default, global override, project override, the effective bundle per session kind, diffs. Edit, validate, save, reset.
- **F9 Docs.** Per project: goals, progress, alerts, exceptions, reviews, the phase strip (D-084, D-085); the agent-proposal queue and the run summaries with their review state (§7.9), where owners and the project's managers and editors can decide proposals (D-190).
- **F10 Settings.** Grouped sections (§7.4), incl. auth and session lifetimes (§9.5), users and grants (incl. machine users: rename, revoke and re-issue their tokens, §9.7), backup and the full export with its reinstate guide (§11).
- **F11 Account.** My logins: passkeys (with the hardware / synced badge), TOTP, recovery codes, OIDC links, active sessions and remembered devices with "sign out", display time zone.
- **F12 Audit.** The last N writes with actor, via, before/after summary; undo where the action supports it (D-075).
- **F13 Host.** The host validation (D-164, §10.5): tmux, Claude Code, login, bridge, working root, permissions, MCP, each ok / warn / fail with a fix hint; "run all checks" and the end-to-end session test.
- **Add task** (secondary): title, description, project, priority.

First login: a popup offers the browser's time zone (`Intl.DateTimeFormat().resolvedOptions().timeZone`). For the first owner it sets the global `window_tz` (Europe/Berlin until then, D-173); for everyone it sets their display time zone. Both stay adjustable (D-148).

### 3.1 UX rules (owner, kept)
- **Visual design (D-074):** purple main accent; vibrant colours that directly represent status (done / in progress / pending / blocked / alert), used the same everywhere; rigid but sleek: a strict grid, clear boxes and chips, compact, no decorative fluff.
- **Settings UX (D-075):** only the most important settings on the main views (pause, tonight's window, `last_mile_hours`, the budget mode); everything else on the Settings page in sections. Every setting has a default, its own reset, a short explanation of what it does and what it can affect (e.g. "can make AFClaude run during your daytime"). "Reset this section" and "Reset all" show a warning listing what changes, plus one-tap UNDO from the audit log. The registry (§7.4) is the single source; the UI renders from it.
- **Reserve threshold UI (D-141):** next to `reserve_threshold` ("auto" or a %) show, all computed in code by `pacing.threshold_info()`: the session→weekly ratio ± spread (n); the weekly cost of one full session window ± uncertainty and the dynamic default (100 − that cost); the model's back-calculated inaccuracy (`forecast_backtest()`: bias, sd, rmse, n, points reaching the reset, preliminary/ok); the predicted week end's uncertainty and its slack to the threshold; the active threshold and its source. One line explains the rule: a night runs a FULL session window only if the week is then predicted to end at or below the threshold; the model has no margin, the threshold is the only spare.

## 4. Architecture

Option A (D-071): a small Python web app next to the DB. Starlette + Jinja2 + uvicorn, htmx for partial updates, vendored uPlot for charts, all assets vendored (no CDN, no JS build). HTML and the JSON API come from the same handlers.

```
browser ──https──> reverse proxy (Traefik / Coolify)
                      │  (no internet route until gates 8a–8c pass, D-157)
                      v
   afclaude-web ──────┐ actions.py ─> store.py ─> SQLite WAL (volume /data)
   afclaude-mcp ──────┤      ^                         ^
   afclaude-runner ───┘      │ wake (unix socket in /data/run)
     ├ dispatcher tick, window-start tick, run-now
     ├ stall scan, sampler, usage review, build-log export, backups
     └ session executor ──ssh bridge──> host: tmux + Claude CLI sessions (D-164)
```

- **One image, three services** (D-163): `afclaude-web` (the dashboard's HTTP port), `afclaude-runner` (a resident daemon, §5.1), `afclaude-mcp` (the MCP server; its only port is the remote MCP transport, §4). All share the data volume. SQLite WAL across containers is fine on one kernel; never on a network filesystem, so all three run on one host.
- **Usage numbers** reach the dashboard through a `status_snapshot` row the runner writes each pass (usage %, resets, budget decision, `threshold_info()`, ratios). The web container imports no model code and never reads `~/.claude.json`, calls `/usage`, `claude`, tmux or the bridge. Pages show the snapshot's age.
- **Sessions stay on the host** (D-164): AFClaude itself (web, runner, MCP) is containerised; the Claude CLI sessions run in tmux on the host, started by the runner through the whitelisted SSH bridge (D-120, D-121), in AFClaude's own working root (§10.4). The dashboard validates the host setup (§10.5).
- **MCP transport:** the `afclaude-mcp` container. On the AFClaude host it is registered as a stdio command (`docker exec -i afclaude-mcp afclaude mcp`), which works today. Other machines reach it through a remote MCP transport (*proposal:* streamable HTTP on an internal port of `afclaude-mcp`, routed by the reverse proxy at `<AFCLAUDE_PUBLIC_URL>/mcp` with TLS, so it follows the exposure stages of §9.2: tunnel or VPN only until gate 8d). Every interactive connection authenticates with its machine's token (§9.7).
- Code layout: `dashboard/` (app, routes, templates, static), `actions.py`, `schedule.py`, `prompts.py`, `runner.py` (daemon), `auth/` (sessions, password, TOTP, WebAuthn, OIDC), `backup.py`, `docker/`. The flat modules stay.

## 5. Runners

### 5.1 One resident runner daemon (D-153, D-173)
`runner.py` replaces supercronic, the `at` shim and the keep-alive watcher. It is one process with an internal scheduler; every job runs in a child process with a timeout, so a crash doesn't stop the loop. Job intervals are settings.

| job | default |
|---|---|
| dispatcher pass (approved stalls, picking the next project and task by rank and priority, every project's task-manager incl. AFClaude's own, cleanup, verification) | every 10 min + on wake |
| window-start tick: the first pass at/after a window start fires each project's task-manager window-start continue once per window (dedup key = the window's end date); a POSTPONE defers it to last activity + 60 min (D-018) | in the dispatcher pass |
| stall scan (`stalled.scan`, own tick) | every 3 min |
| usage sampler + pre/post-reset samples | every 15 min + scheduled jobs |
| usage review | when due |
| status snapshot, AFClaude build-log export (§7.7), backups (§11) | each pass / on change / daily |
| WAL archiver (Litestream, §11.1), a supervised child process; its replication lag is part of the health | continuous |

- **Wake:** after any write that the runner must act on (run-now, a waking message to a task-manager (F14), an agent proposal for a task-manager that isn't running (§7.9), window, pause, answer, decision), `actions.py` sends one datagram to `/data/run/runner.sock` after the commit. The runner then reads `action_requests` and settings from the DB. The DB is the truth; a lost wake is caught by the next tick. No polling interval for run-now (D-153).
- Health: the runner writes its last tick per job to `runner_state`; the web container's health check and F5 read it; the container restarts on a stale heartbeat.
- Pause (`automation_paused`) stops automatic starts and continues; it never kills running sessions.

### 5.2 One continuation path (D-147)
- Every project has exactly one task-manager (D-189), and AFClaude's own task-manager is just the task-manager of the AFClaude project (`projects.manager_session` holds its session), continued by the dispatcher like every other project's: window-start continue, stall continue, last-stretch slots, compaction (§5.5). There is no global task-manager. The keep-alive watcher's continuation path and its exclusion list (`keepalive_sessions`) go away. `keepalive.py` keeps only reusable pieces (transcript parsing, preflight, budget wiring).
- Per-project session settings: model and effort (default for task-manager sessions: Opus 5.5, high, D-043), permission mode (auto; dontAsk for Haiku, D-047), guard bypass with the hook rules in context (D-044), no `--bg` (D-045), take-over-idle (D-046), archive at the end (D-050), trusted dirs (D-051).

### 5.3 Readable tmux names (D-147)
- `afc-<project-slug>-<role>`: `afc-afclaude-task-manager`, `afc-spending-task42`, `afc-stall-<title-slug>-<id4>`, `afc-review`. Charset `[a-z0-9-]`, ≤ 40 chars, a short suffix only on collision. The same string is the RC `--name`.
- Runners find their sessions through `driven_sessions` (tmux name ↔ session id), never by a name regex (today `ka-[0-9a-f]{8}`).

### 5.4 Run now (D-153, D-036, D-173)
- Kinds: `continue_now` (one stalled session), `work_on_now` (a project: its task-manager, with its next ready stage), `review_now`; a waking `task_manager_message` (F14) is delivered the same way.
- Who: owners, and managers for their own project (D-184, D-190). Editors can't force an immediate run; viewers can't request anything. `review_now` (the usage review, global) is owner only.
- Run-now **skips the time window and the budget check** (incl. the session-usage stop) and starts **immediately**: the write queues the request and wakes the runner (§5.1). It still passes the safety preflight (never take over an RC-server thread or a live holder; never fork a session). It also runs while automation is paused (pause is about the automatic schedule) and isn't limited by the automatic concurrency cap; the button warns when the session limit is nearly used up.
- Feedback: the request row moves queued → started (tmux name) / refused (reason), shown live on the card.

### 5.5 Compaction hand-offs (D-144, D-155, D-191)
Hand-offs and compaction are **per tmux session** (one `driven_sessions` row). A session compacts only when it has run **longer than one session length in total**, not merely across a session-window boundary (D-191). The rule, checked at the end of each session window: **compact if this tmux session already existed during the previous session window.** So the first compaction comes after 1 to 2 session lengths, depending on when the session started; a session that started during this session window just runs on across the boundary. In practice this is the task-managers; few agents run that long.
1. **Save prompt.** For a session that meets the rule, before the session window ends (the earlier of `handoff_before_reset_min`, default 20 min before the session reset, and `handoff_session_pct`, default 90%, below the 95% night stop of D-014), the runner sends `session_end_save.md`: finish the current step, commit, write the hand-off, compaction follows at the next start.
2. **Structured hand-off.** The session writes it through MCP (`afclaude_handoff`) into `handoffs`: summary, done, in progress, next steps, open question ids, decision ids, files touched, notes (JSON, size-limited). One open hand-off per tmux session; a new one replaces that session's open one. The task-manager's is the project's hand-off; an agent's hand-off is one of its direct writes and is listed in its run summary (§7.9).
3. **Compact on resume.** At the next continue the runner resumes the session, sends `/compact` first, waits for the compaction marker in the transcript, then sends the continue prompt with the hand-off in `{handoff}` and marks it consumed. From then on the rule applies at every session-window end, so a long-running task-manager is compacted at each one.
4. No hand-off (crash, missed save): the continue prompt says so and points at the latest progress entries and run log.
- The dashboard shows the latest hand-off per session (F6) and the task-manager's per project (F9). Hand-offs are in the backup (§11) and let a moved install resume cleanly even when a transcript can't be resumed.

## 6. Schedule (D-148, D-030, D-033, D-034)

- `schedule.py` is the **only** window code; the dispatcher, the window-start tick, pacing (nights left, the straight-line fallback, the last stretch), the stall rules, the F4 preview and window recommendations all use it. It replaces the three implementations in `keepalive.py`, `pacing.py` and the validation in `actions.py`.
- A window is `[start, start + N × session_hours)`: the start is a wall-clock time in `window_tz`, the length is absolute. So **every window is N whole 5-h sessions, also on DST nights**; such a night ends an hour earlier or later on the wall clock, and the preview shows it.
- The grid is anchored at the chosen start: session k runs from start + k × session_hours, so every session window inside the automation window is a full one. The start picker moves in 30-min steps and offers "snap to the usual reset" from the samples.
- Per weekday with link groups: `window_days = {mon: {start, n, group} | null, …}`. A window belongs to the weekday it starts on; `null` = no window that night. "Change all linked" rewrites the whole group in one transaction; "only this day" gives the day a fresh group; "whole week" writes all seven with one group. Validation: 30-min grid, n ≥ 1, no overlap with the next day (the error names the day). Built in phase 1 (`window.set`).
- Default: every day 23:00 × 2 (D-033), one weekly link group. `window_tz` defaults to Europe/Berlin (D-031) until the owner's first login sets it from the browser (§3).
- API: `windows(from, to)`, `in_window(t)`, `current_window(t)`, `next_window_start(t)`, `session_slots(window)`, `window_key(window)`, `nights_until(reset)`; all tz-aware, never a fixed UTC offset.

## 7. Data model: everything in the DB (D-161)

### 7.1 Conventions
- SQLite in WAL mode at `/data/afclaude.db`; `CREATE TABLE IF NOT EXISTS` plus the `COLUMNS` dict; validation in Python. The WAL is archived for `db_wal_retention_days` (default 7) instead of being discarded at checkpoint, so the DB can be rolled back to any moment in that window (D-175, §11.1).
- **Schema guard (review A1):** code refuses to write when the DB's `schema_version` is newer than its own; non-additive changes go through an explicit migrate step after a backup.
- `version` column (trigger-bumped) on every owner-editable table; writes carry the version they saw, a mismatch is a 409. Idempotency keys for 7 days. Both built.
- Telemetry and runner tables carry `account_id` (one row in `accounts` now, D-156), so the future pool needs no migration.
- Secrets in the DB (TOTP seeds, OIDC client secret, recovery-code hashes' pepper, the bridge key if stored) are encrypted with the master key (§10.2).

### 7.2 Table catalogue

| area | tables | status |
|---|---|---|
| work | `projects`, `tasks` (incl. `kind='question'`), `task_events`, `session_decisions`, `standing_rules` | built |
| observation | `sessions`, `limit_hits`, `run_log`, `driven_sessions`, `action_requests` | built (runners write them from phase 3a) |
| config | `settings`, `meta`, `installation` (id, epoch, restored_from), `accounts` | settings built; rest new |
| audit | `audit_log` (append-only), `idempotency_keys` | built |
| prompts | `prompt_overrides` (system-wide, D-193) | built |
| decisions | `decisions`, `decision_links`, `decisions_fts` | new (§7.6) |
| hand-offs | `handoffs` | new (§5.5) |
| agent proposals | `agent_proposals` (project, session, run, kind, payload, summary, status `proposed`/`approved`/`edited`/`rejected`, reviewer, note, ts), `agent_runs` (session, project, task, started/ended, summary, reviewed by/at) | new (§7.9, D-182) |
| messages | `chat_threads` (user, project, opened, last activity, closed), `chat_messages` (thread, author `user:<id>` or `task-manager:<session>`, text, ts, request id) | new (F14, D-178) |
| docs | `docs` (whole documents: goals, exceptions, backlog), `doc_entries` (append logs: progress, alerts, reviews, drift) — per project | new |
| auth | `users` (`kind` = `person` / `machine`), `grants`, `identities` (OIDC iss+sub), `credentials` (password, TOTP, WebAuthn), `recovery_codes`, `auth_sessions`, `remembered_devices`, `mds_cache`, `machine_tokens` (machine user, token hash, label, hostname at registration, last seen at / hostname / transport, created, revoked at), `machine_registrations` (one-time registration codes: hash, created by, expires, used at) | new (§9, §9.7) |
| telemetry | `usage_samples`, `usage_weekly_series`, `usage_session_windows`, `forecast_log`, `haiku_judgements`, `weekly_cycles`, `usage_reports`, `user_model`, `usage_reviews` | new (replace the JSONL/JSON files) |
| runner | `runner_state` (component, key, JSON), `scheduled_jobs` (the pre/post-reset samples), `status_snapshot`, `host_checks` (latest result per check, §10.5), `app_log` (retention setting) | new (replace the state files and logs) |
| backup | `backups` (metadata of made bundles and exports, with their manifests) | new |

What moves from files (each with a one-time importer that checks row counts, then the file is retired):

| today | into |
|---|---|
| `data/afclaude.json` tunables, `data/dispatcher.json` | `settings` (the file keeps nothing; machine identity becomes env, §10.2) |
| `samples.jsonl`, `weekly_series.jsonl`, `session_windows.jsonl`, `forecast_log.jsonl`, `haiku.jsonl`, `weekly_cycles.json`, `usage_reports.jsonl`, `user_model.json` | telemetry tables (raw samples get a retention setting; session windows are never pruned) |
| `dispatcher_state.json`, `own_sessions.txt`, `keepalive_state.json`, `keepalive_deferred.json`, `sampler_state.json`, `usage_review_state.json`, `at_spool/` | `runner_state`, `driven_sessions`, `scheduled_jobs` |
| `*.log` | `run_log` (decisions) + `app_log` (lines, retention), also on container stdout |
| `PROGRESS.md`, `GOALS.md`, `ALERTS.md`, `EXCEPTIONS.md`, `data/reviews/*`, the drift log | `docs` / `doc_entries`; only AFClaude's public build log (PROGRESS, GOALS, EXCEPTIONS) is still written out as files (§7.7) |
| `OPEN_QUESTIONS.md`, `DECISIONS.md` | questions as tasks, `decisions` (§7.6); the files are retired, not exported |
| `BACKLOG.md` | seeded at gate 8d (D-065), then `docs` + backlog projects |

### 7.3 Write path (D-154)
- `actions.py` is the only module that writes. Each action: validate → one transaction → audit → idempotency → version check, as built in phase 1.
- Runners call it too (`via="runner"`): task start/finish, auto rules, reopen, request handling, settings imports.
- Append-only observation and telemetry rows (samples, run log, app log) are written through registered `actions.py` appenders. Each row carries ts, actor and via and is immutable by trigger, so the row is its own audit record; an `audit_log` row is written for every other change. (*Interpretation, 8a checks it.*)

### 7.4 Settings registry (D-146, D-075)
- `actions.SETTINGS` is the single source for key, type, default, validation, explanation, "important" flag, section and required role. Effective value = DB row, else the code default; env never overrides a setting.
- One-time import of the file tunables in phase 2a, then they are removed from the files; `afclaude_config.py` shrinks to bootstrap identity.
- Sections and keys (flat names as built):
  - **Schedule:** `window_days`, `window_tz`, `session_hours`.
  - **Budget:** `reserve_threshold`, `last_mile_hours`, `session_usage_stop`, `usage_model`, the pacing model parameters, the linear fallback's `projection_threshold` and `cutoff_after_window_hours`.
  - **Automation:** `automation_paused`, job intervals, concurrency cap, `handoff_before_reset_min`, `handoff_session_pct`, the `continue_now` safety options, `proposal_alert_hours` (default 24: oldest unreviewed agent proposal older than this raises an alert, §7.9).
  - **Sessions:** model/effort/permission mode per session kind (overridable per project).
  - **Auth & sessions:** §9.5, OIDC config and the group map (§9.4).
  - **Host:** working root, config dir mode (separate / shared, §10.4), bridge user, host-check interval, tested Claude Code version, owner-session continuation on/off.
  - **Backup:** schedule, retention, target, include credentials in exports (on, shown in red, D-174), `db_wal_retention_days` (default 7, range 1–30, D-175) (§11).
  - **Data:** telemetry and log retention.
  - **Display:** per-user time zone, theme, `ui_chat_reset_hours` (default 5, range 1–168: idle time after which the task-manager message box starts empty again, D-178).

### 7.5 Per project vs global (D-152)
- Per project: decisions, questions, hand-offs, docs (goals, progress, alerts, exceptions, reviews), agent proposals and run summaries, session settings, grants.
- Global: windows, budget, automation, auth, users, backup, accounts.

### 7.6 Prompts and the decision store
**Prompts (D-110, D-111).** Defaults ship as `prompts/*.md`. Prompts are **system-wide** (D-193): `prompt_overrides` is keyed by `name` only, owner-only. Resolution: override > default. Prompts are **templates with variables** (D-197): system variables filled by the runner (session kind, project, date, budget for this run, reason, hand-off, …) and **per-project variables** whose values are project settings in the DB (e.g. repo path, whether the repo is public and its guard command, the live files that need worktrees, the goal/question locations, a short free-text `project_notes`). Every prompt stays system-wide; a project only fills its variables. `manager_afclaude.md` becomes the generic task-manager rules template with these variables; AFClaude's specifics become its variable values. Saving a template validates that it only uses known variables; F8 shows the rendered prompt per project. Project variables are set by the project's managers and editors (D-197). `prompts.py` defines one **bundle per session kind** (task-manager continue, task start, stall continue, session-end save, resume after compact, usage review, Haiku judge), so F8 shows exactly what each kind receives. Saving validates the placeholder set and test-renders; `base_sha256` flags "default changed since your edit" with a three-way view.

**Decision store (D-104–D-107, D-151, D-158).**
- `decisions`: `id` (D-NNN), `project_id` (`NULL` = install-wide), `slug` (stable unique key per project, e.g. `budget/night-gate`), `title`, `summary` (relevance-only, see retrieval), `words` (verbatim quotes with date and source, JSON), `keywords` (JSON), `scope`, `status` (`proposed | active | done`), `author` (`owner | task-manager`), `interpretation` (clearly non-binding), `updated_at/by`, `version`.
- **Only the current decision per entry** (D-158): a change overwrites the row. There are no superseded entries in the store; each change is in the audit log (old and new text), which is the history view. A decision that no longer applies is deleted (audited).
- Who writes: owners edit or confirm directly. Task-managers **propose**: a proposal is a question whose payload is the new or changed decision; the active entry stays in force until a human with the right role confirms (D-104). The project's managers and editors make and confirm project-scoped decisions (§2, D-192); install-wide entries are confirmed by owners only. Task-manager decisions (D-105) are proposals with `author=task-manager` that briefs may follow but that never override an owner entry. Agents don't propose to the humans: their decision proposals go to their task-manager (§7.9), which forwards the ones it supports as its own proposal (the agent named as origin) or rejects them.
- Retrieval for briefs (D-107, D-176): MCP `afclaude_decisions(project, keywords, full=false)` matches keywords plus FTS5 over title/summary/words. With `full=false` it returns per hit only the id, slug and a **very brief summary: a few words naming the topic** (e.g. "night gate: when a full window may run"), deliberately **not enough to act on**, only to decide whether the entry is relevant. To read the rule itself the agent **must expand it** (`full=true`, or the entry by id), which returns the title, the owner's words, scope and interpretation. The MCP tool description says this explicitly, and every `full=false` result carries the same line ("summaries name the topic only; expand an entry before acting on it"). The `summary` field is constrained on write: a short length cap (e.g. 80 chars) and no rule content (no values, thresholds, do/don't instructions). `actions.py` enforces the cap; the content rule is stated in the proposal prompts and the seeding step, and the confirm dialog shows the summary on its own so the owner can see it carries only the topic. Typed `decision_links` (`refines | requires | conflicts`) surface related and conflicting entries with each hit.
- Seeding (phase 4b): active owner entries import verbatim as `active`; superseded and drift entries are written to the audit log only; partially superseded entries are merged by the AFClaude task-manager into one current entry with status `proposed` for the owner to confirm.
- Design note from the reference project the owner named (D-158): a stable unique key per entry with upsert-overwrite (no history in the store), typed links between entries with contradictions shown inline on retrieval, and deterministic full-text lookup that injects only a few high-confidence matches into a session.

### 7.7 File output (D-181)
- **No local file exports of DB content.** Questions, decisions, alerts and project docs are read in the dashboard or through MCP; there is no `OPEN_QUESTIONS.md`, `DECISIONS.md`, `ALERTS.md` or per-project doc file generated from the DB.
- **The only file output is AFClaude's public build log:** `PROGRESS.md`, `GOALS.md` and `EXCEPTIONS.md`, regenerated from the DB into the AFClaude repo and committed by the AFClaude task-manager through the `check_public` hooks. They are read-only copies; nobody edits them.
- Beyond that, the DB content leaves the DB only in backups and exports (§11).
- `data/ALERTS.fallback.md` (§7.8) is not an export: it is the out-of-DB escalation path written only while the DB is broken, and imported into the DB once it is healthy again.
- The CLAUDE.md rule "delete resolved items from OPEN_QUESTIONS.md first" becomes "close the question in the DB first" (phase 4b).

### 7.8 DB error handling and escalation (D-171)
With everything in the DB, a DB failure is the single biggest failure mode, so every DB access goes through one error path in `store.py`/`actions.py`:

| class | examples | handling |
|---|---|---|
| transient | `SQLITE_BUSY`/locked, short I/O hiccup | retry with backoff (bounded, e.g. 5 tries / ~10 s), then treat as persistent |
| caller error | constraint violation, version conflict (409), validation | no retry; a structured error back to the caller |
| persistent | disk full, read-only FS, corruption (`PRAGMA quick_check` fails), schema newer than code, DB missing | stop writing; runners pause automation (no autonomous starts on a broken DB); escalate |

- **Escalate only if retrying doesn't fix it (owner, D-171):** transient errors are retried automatically first; a session that still gets an error retries the action itself once more (after a short wait) before escalating. Escalation to the user is the last step, never the first.
- **Hand-back to Claude:** every error reaches the calling session as a structured result, never a silent failure or a raw traceback: MCP tools return an error object (class, message, what was not saved, suggested next step); the CLI exits non-zero with the same text. The session prompts (`prompts/*.md`) tell Claude: on a persistent DB error, stop the affected work, don't work around the DB (no hand-edited files as a substitute), and escalate to the user.
- **Escalation can't depend on the DB:** if the DB itself is broken, a question can't be stored in it. Escalation therefore has an out-of-DB path: an append to a fallback alert file in the data volume (`data/ALERTS.fallback.md`, imported into the DB once it's healthy again), the notifier, and a red banner from the web container's health check. The runner re-checks the DB each tick and resumes on its own once it's healthy (an alert says so).
- **Never lose the write:** an action that failed persistently is recorded in the fallback file (actor, action, payload hash, time), so it can be replayed or consciously dropped by the owner.
- Tests: fault injection for each class (locked DB, read-only file, corrupted file, newer schema), covering the runner, MCP, CLI and web paths; gate 8a checks no DB call bypasses the error path.

### 7.9 Agent proposals and the task-manager review (D-182, D-185, D-188, D-194)
The project's task-manager keeps the project's docs, goals, progress and tasks, so it stays up to date and decides with its broader context. Agents therefore don't write those themselves: their writes become **proposals** the task-manager reviews, and the task-manager gets one summary of everything each agent run wrote.
- **Writing as an agent.** Agents use the same MCP tools and CLI as today. `actions.py` checks the actor (§9.6): a write that is one of the agent's direct writes (§9.6) is applied as usual; any other write is not applied but stored as an `agent_proposals` row (kind = the action, payload = its arguments incl. the `version` the agent saw, a one-line summary, status `proposed`), and the tool returns "proposed as P-n, waiting for the task-manager's review". Questions to the humans and decision proposals go the same way.
- **Which task-manager.** The project's task-manager. Every project has exactly one (D-189), so there is no fallback reviewer.
- **Run summary.** Each agent run (a session from start to end, or a subagent run where it can be told apart) gets an `agent_runs` row. When the run ends (done, blocked, saved for a hand-off, killed or stalled) the runner builds **one summary of all the run's writes**, direct ones included, from the proposals and the audit log (actor = `agent:<session>`): proposals with their payload, direct task status changes and notes, hand-offs, escalations, branches pushed. The task-manager reviews the run as one item.
- **Review.** The task-manager lists and decides through MCP (`afclaude_proposals(project)`, `afclaude_review(id, approve | edit | reject, note, payload?)`), one by one or the whole run at once. Approve or edit applies the (edited) payload through `actions.py` with the task-manager as the actor (`task-manager:<session>`, `via=proposal`; the audit row names the proposal and the agent). A version mismatch is a 409: the task-manager edits it onto the current state or rejects it. Direct writes in the summary are acknowledged, or corrected by the task-manager's normal writes (reopen the task, undo where the action supports it). The run is marked reviewed when nothing in it is still `proposed`.
- **Branches (D-185).** Before merging an agent's finished branch, the task-manager reviews the branch's actual changes itself (the diff, not only the agent's report or run summary) to validate them; only then it merges.
- **Questions from agents.** The task-manager answers itself if it can (the answer goes back to the agent: into its task's notes and, while the agent runs, its next MCP result); otherwise it forwards the question to the humans (approve creates the question for the owners and the project's managers and editors, actor task-manager, the agent named as origin). A task an agent set `blocked` (a direct write, §9.6) reaches the inbox only when the task-manager forwards it.
- **Delivery (D-188).** Every proposal reaches the project's task-manager **immediately**; there is no urgent flag and no window or budget gate on the delivery. Agents normally run inside the task-manager's own run, so a proposal is delivered into its current turn (a short notice at its next idle point, like F14). If the task-manager isn't running when a proposal arrives (an agent session that outlived its run), the runner wakes it with the proposal (D-194). The task-manager then decides whether to act now or to postpone the proposal to its next session because more work is needed; postponed proposals are listed in its next continue prompt.
- **Task-manager down.** If the task-manager can't take the proposal (stalled at a limit, crashed, the DB broken), proposals wait; **nothing auto-applies** and nothing is approved by timeout. F9 and F5 show the queue length and the oldest pending proposal; past `proposal_alert_hours` an alert tells the owners. Owners and the project's managers and editors can decide a proposal from F9 as `user:<id>` (D-190); the task-manager sees that decision in its next turn.

## 8. API surface

All under `/api/v1`, JSON; HTML pages call the same handlers (htmx gets fragments). Reads are GET; writes are POST/PUT/PATCH/DELETE with an `Idempotency-Key` and the `version` where one exists. Every write is one `actions.py` call. Every handler checks the role (§2) before anything else.

Reads: `overview`, `inbox`, `projects`, `queue`, `tasks/{id}`, `stalls`, `rules`, `window` (settings + next 7 windows + ratios), `usage` (the snapshot), `runs`, `sessions/driven`, `handoffs`, `messages` (the open thread, or a closed one by id), `proposals` (per project, with the run summaries), `decisions`, `prompts`, `prompts/{name}` (default, overrides, bundle, diff), `docs`, `reviews`, `audit`, `settings`, `users` (incl. machine users with their token metadata, never the token), `me` (logins, sessions), `host` (latest checks), `backups`, `health` (no secrets).

| write | effect | conflict |
|---|---|---|
| `projects` / `tasks` create, edit, move, priority, cancel, reopen | as built (`project.*`, `task.*`) | version / no-op / 409 on state |
| `tasks/{id}/answer` | blocked → pending; a question → done; a decision proposal → applied or rejected | 409 unless open |
| `stalls/{session}/decision` | continue / ignore / clear with the `stall_ref` shown | 409 if it stalled again |
| `rules` | session / project rule | unique (scope, match) |
| `decisions` create, edit, confirm, delete | §7.6 | version |
| `proposals/{id}` approve / edit / reject (owners, and the project's managers and editors, D-190; task-managers do it through MCP) | applies or drops an agent proposal (§7.9) | 409 unless `proposed`; the payload's version |
| `settings/{key}` set / reset; `settings/reset` (section, all) | registry-validated; undo from audit | version |
| `settings/window` | day, start, n, mode | version |
| `prompts/{name}?project=` PUT / DELETE | override / reset | base_sha256 + version |
| `requests` | `continue_now`, `work_on_now` (owners, the project's managers), `review_now` (owners) (wakes the runner, §5.4) | one open per (kind, target) |
| `messages` | send a message to a project's task-manager (F14): appends to the open thread or opens one, queues `task_manager_message`; wakes the runner when the sender is an owner or a manager of the project, otherwise it waits for the next regular run (D-186); the reply arrives through MCP `afclaude_reply` | thread closed by the reset → a new thread |
| `users`, `grants` | invite, role, project scope, remove (owners; managers grant and revoke editor and viewer on their project, D-192); machine users take grants the same way | version |
| `machines` | create a one-time registration code; rename (label), revoke, re-issue a machine's token (owners, D-195, §9.7) | version |
| `machines/register` | exchanges a one-time registration code for a new machine user's id and token (or a re-issued token); called by `afclaude machine register` on the machine; no session, guarded by the code, rate-limited | code used or expired → 410 |
| `auth/*` | login, factors, passkeys, OIDC, step-up, logout, sessions | §9 |
| `backup`, `export` create / download; restore (setup only); point-in-time rollback to a moment in the WAL window | §11 | one running at a time |
| `host/checks` | run all checks or one (and the end-to-end test) now | one run at a time |

Concurrency: WAL + `BEGIN IMMEDIATE` + 30 s busy timeout serialise writers across containers; dashboard transactions do no I/O. `start_task` claims only pending tasks atomically, so "answer" vs "start" and "cancel" vs "start" have exactly one winner. Shrinking a window or pausing never kills running sessions.

## 9. Security

### 9.1 Threat model
A write here is close to "run code on the execution host": answers, stages and prompts become input of autonomous sessions that run in auto mode with the guard hooks bypassed; continue/run-now spends the budget; window and pause decide when this happens. The final dashboard is **reachable from the internet behind its login** (D-157), with several users (D-149).

Attackers: anyone on the internet; a CSRF page in a user's browser; a stolen session cookie or device; a phished password; a compromised or misconfigured OIDC provider or a hostile group claim; a user escalating beyond their grants (IDOR across projects); an autonomous session, prompt-injected by content it read, writing back through MCP or the CLI; a stolen backup bundle; a stolen or replayed machine token, or a session posing as another machine or as an AFClaude-driven session (§9.7).

### 9.2 Exposure stages (D-073, D-157)
- Until gates 8a–8c pass: **no internet route.** The app runs on the internal docker network or localhost, reached through an SSH tunnel or VPN. The temp dashboard (`/private`) stays in use meanwhile.
- From gate 8d: a public route through the reverse proxy (Traefik or Coolify's proxy) with TLS. The old key-header + forwarded-user model of v1 is gone; the app does its own login. A proxy-level login or IP allowlist may stay as optional defence in depth.

### 9.3 Login methods (D-150, D-159)
- **OIDC/OAuth** with any provider (Authelia, Authentik, …), via Authlib: discovery, authorization code + PKCE (S256), state and nonce, iss/aud/exp checks. Identity = (issuer, sub); e-mail is display only. An OIDC identity is linked to an existing user only by that signed-in user, never by e-mail match. The provider's 2FA is the provider's job; an optional `auth_oidc_required_amr` (e.g. `mfa`) can demand it.
- **Password** (argon2id) **always with a second factor**: TOTP (RFC 6238) or a passkey; 10 one-time recovery codes as fallback.
- **Passkeys (WebAuthn)**, via python-fido2 (it verifies attestation against FIDO MDS3):
  - **Hardware-bound** = backup-eligible flag **BE = 0** and an attestation statement that verifies against the **FIDO Metadata Service** (MDS3 blob fetched daily, signature checked to the FIDO root, cached in `mds_cache`) for an authenticator that isn't revoked or compromised; user verification (PIN/biometric) required. A hardware passkey **suffices on its own**.
  - **Synced** (BE = 1) or unverifiable attestation: allowed only after a warning that it isn't hardware-bound, and it counts as one factor, so a second factor is still required (D-159).
  - Sign counters are checked (clone detection).
  - **Phone caveat:** iOS and Android create synced passkeys by default (iCloud Keychain, Google Password Manager; BE = 1). On a phone use a hardware security key over NFC or USB-C, or the OIDC route.
- **Factor rule:** a login is complete with (a) OIDC, (b) a hardware passkey with UV, or (c) two different factors from {password, TOTP, synced passkey, recovery code}.
- **First registration (D-149, D-172, D-173):** while `users` is empty, `/setup` registers the first owner. It requires a one-time setup code that the web container prints to its log on start, so a fresh install that happens to be reachable can't be claimed by a stranger. The first owner registers locally (password + TOTP, or a hardware passkey); OIDC and further owners come afterwards. Setup can instead restore a backup or export (§11.3).
- Lockout recovery: `docker exec afclaude-web afclaude admin reset-auth` (host access = owner) or recovery codes.

### 9.4 Authorization and OIDC groups (D-149, D-150, D-172)
- Deny by default: a route-table test asserts that every route except `health`, the login pages and `machines/register` (guarded by its one-time code) needs a session (the remote MCP endpoint needs a machine token instead, §9.7), and an action-matrix test asserts each `actions.py` action × role (§2).
- Grants: `grants(user, scope = owner | project:<id>, level = viewer | editor | manager, source = manual | oidc:<group>)`.
- **Group map** (setting `auth_oidc_group_map`): `[{group, grant}]`, e.g. `afclaude-admins → owner`, `team-x → project:spending:editor`. Group grants are recomputed at every OIDC login (removed when the group is gone); manual grants are separate. The owner role can be granted by a group (D-172): whoever controls that group at the provider then controls the install, so mapping a group to `owner` is a high-impact write with a warning saying so. A login with no matching group = registered, no access.

### 9.5 Session lifetimes (D-162): settings with defaults
Defaults follow NIST SP 800-63B AAL2 (30 min idle, 12 h total) and keep friction low through one-tap passkeys and remembered devices.

| setting | default | range | effect |
|---|---|---|---|
| `auth_idle_timeout_min` | 30 | 5–480 | no request for this long → sign in again |
| `auth_absolute_lifetime_h` | 12 | 1–168 | one login lasts at most this long, active or not |
| `auth_remember_device_days` | 7 (D-179) | 0–90 (0 = off) | on a remembered device a password login skips the second factor; never the first factor, never step-up; revoked on password or factor change |
| `auth_reauth_window_min` | 10 | 1–60 | high-impact writes need a full authentication within this window (step-up; OIDC with `max_age`) |
| `auth_max_sessions_per_user` | 10 | 1–50 | oldest session ends first |
| `auth_login_rate` | 5 failures / 15 min per account, 20 per IP | | then exponential backoff; repeated failures raise an alert |
| `auth_oidc_backchannel_logout` | on | | the provider's logout ends our sessions |

- Server-side session rows (revocable, listed in F11); cookie `__Host-` prefix, Secure, HttpOnly, SameSite=Lax; the session id rotates at login and step-up.
- **High-impact writes** (step-up + confirm dialog + an alert entry): prompt overrides, project-scope "always continue" rules, unpause, window changes, run-now, decision confirmation, users/grants/roles, machine registration codes and token re-issue or revocation, auth settings, host settings, backup/export download, restore and point-in-time rollback.

### 9.6 Task-manager and agent roles and their limits (D-160, D-182, D-185, D-189)
- **Who is who.** MCP and CLI writes from AFClaude's own sessions (the session is in `driven_sessions`, or the CLI runs with `CLAUDE_GUARD_DISABLE=1`) act as `task-manager:<session>` when the session is a project's task-manager (`projects.manager_session`), otherwise as `agent:<session>`. A session that can't be identified counts as an agent (least privilege). Subagents a task-manager starts inside its own session report to it in that session; their MCP writes carry the task-manager's session id and so count as the task-manager's unless Claude Code exposes a subagent id to the MCP server (checked in phase 5a; if it does, they are agent writes). Interactive sessions AFClaude didn't start act as their machine's machine user (`machine:<id>`, §2, §9.7), never as agents. A session found in `driven_sessions` is always the task-manager or an agent, whatever token its MCP connection carries; a connection with neither a driven session nor a valid machine token counts as an agent.
- **Task-manager:** the writes of its row in §2, in its own project only. Owner-only actions and forcing an immediate run are refused, audited and alerted.
- **Agent, direct writes** (owner-approved, D-182, D-185; applied at once, all listed in the run summary for the task-manager, §7.9):
  1. hand-offs of its own tmux session (§5.5);
  2. status and progress notes of the task it is assigned to (start, notes, blocked with the reason, done with a result summary), because the runner schedules on them; the task-manager sees them in the summary and can reopen the task;
  3. failure escalations and alerts (DB errors per §7.8 incl. its fallback file, security and safety alerts), because the task-manager may itself be affected;
  4. code and files in its own branch or worktree; merging is the task-manager's, after it reviewed the branch's changes itself (§7.9).
- Automatic telemetry and observation rows (run log, usage report) are runner writes (`runner:<component>`), not agent writes, so they need no exception.
- **Agent, everything else is a proposal to its task-manager** (§7.9): project docs, goals and progress, new tasks, edits to other tasks, questions to the humans, decision proposals. Owner-only actions are refused outright, audited and alerted.
- v1 is **advisory**: while sessions run on the same host as the same UID with a shell, a session can bypass MCP (write the DB, edit prompt files). Detection = audit + alerts + the 8a/8c checks. Sessions stay on the host (D-164), so enforcement would need a separate UID for the DB and prompts; that stays a later option.
- The MCP server's instructions keep saying its tools are used only when the user explicitly asks for AFClaude; MCP tasks count like UI tasks, no approval step (D-068).

### 9.7 Machine users and MCP identity (D-195)
- **Registration.** An owner creates a one-time registration code in F10 (short-lived, single-use, step-up); on the machine, `afclaude machine register <AFClaude URL> <code>` (on the AFClaude host the setup wizard does it for this host, §10.4) exchanges it at `machines/register` for a new machine user: a random id and a random high-entropy secret token, generated by AFClaude at registration. The label defaults to the machine's hostname.
- **Storage on the machine.** The id and token go into that machine's Claude Code configuration: the AFClaude MCP server is registered at user scope in the interactive Claude config (not in AFClaude's own config dir, §10.4), with the token passed to the server (an env entry for stdio, an `Authorization: Bearer` header for the remote transport) and kept in a file only that user can read (mode 0600). It survives reboots and hostname changes, because nothing is derived from the hostname or `/etc/machine-id`; VMs cloned from one image before registration get distinct ids because each registers on its own.
- **Storage in AFClaude.** `machine_tokens` holds only a hash of the token (shown once, at issue), with the label, the hostname at registration and the last-seen time, hostname and transport. Tokens are compared in constant time. A token used from two different hostnames within a short time (e.g. a VM cloned after registration) raises an alert, so the owner can re-issue one of them.
- **Authentication.** Every interactive MCP connection presents its machine's token. Stdio on the AFClaude host (`docker exec -i -e AFCLAUDE_MACHINE_TOKEN afclaude-mcp afclaude mcp`): host access already means owner-level control (§9.3 lockout recovery), so the token there identifies the machine user rather than defending against the host. The remote transport (§4) requires the token as a bearer header over TLS, rate-limits failures and is reachable only within the exposure stage (§9.2). Revoked or unknown tokens get no session and an audited refusal.
- **Owner controls (F10, users and grants):** rename (label), revoke (the machine user keeps its audit history, its connections are refused), re-issue (a new registration code bound to that machine user; using it replaces the old token, which stops working at once). Grants of a machine user are managed like any user's (§2).
- **Rights.** A machine user has its grants' rights through MCP and nothing else: no dashboard login, never owner-only actions, never the task-manager or agent role. Writes are audited as `machine:<id>`, with the MCP session id when the client sends one.

### 9.8 Hardening
CSRF: no state-changing GETs; writes need a same-origin `Origin`/`Sec-Fetch-Site`, an htmx/JSON header and the per-session CSRF token. Strict CSP (`default-src 'self'`, no inline script), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS, `Cache-Control: no-store` on the API. Jinja autoescaping; markdown rendered server-side through an allowlist sanitiser. Size limits (answer 8 KB, prompt 16 KB, title 200 chars), write rate limit (30/min per user). Errors never echo internals.

Container isolation: `afclaude-web` gets only the data volume and its port; no Claude credentials, no bridge key, no docker socket, no tmux. Only the runner's session executor reaches Claude sessions. A compromised web app can corrupt the DB (restore from backup) but can't run anything directly. Read-only root fs, non-root UID, no privileges in every container.

## 10. Deployment

### 10.1 Containers (D-163)
One image, one compose file usable by plain docker and by Coolify (docker-compose deploy). `/data` in every service is the host directory `AFCLAUDE_DATA` (§10.2):

| service | command | mounts | port |
|---|---|---|---|
| `afclaude-web` | `afclaude web` | `/data` | 8080 (internal; proxied) |
| `afclaude-runner` | `afclaude runner` | `/data`, the bridge key and host key (read-only) | none |
| `afclaude-mcp` | `afclaude mcp` (stdio via `docker exec`; remote transport for other machines, §4, §9.7) | `/data` | internal only, for the remote transport via the proxy (*proposal*) |

### 10.2 Minimal env
| env | required | purpose |
|---|---|---|
| `AFCLAUDE_PUBLIC_URL` | yes | external URL: OIDC redirect URI and the WebAuthn RP ID (passkeys are bound to this domain) |
| `AFCLAUDE_MASTER_KEY` | no | encrypts secrets in the DB; if unset, generated once into `/data/master.key` and wrapped into every backup |
| `AFCLAUDE_DATA` | yes, **no default** (D-180, D-187) | the host directory that holds the DB, its WAL archive, backups and the runner socket; mounted as `/data` in all three services |
| `AFCLAUDE_BRIDGE_HOST` | no | the host's address for the SSH bridge, default `host.docker.internal`; the bridge key and host key are mounted files, the bridge user is a setting |

- **`AFCLAUDE_DATA` unset → nothing starts.** The compose file uses `${AFCLAUDE_DATA:?…}`, so compose (and Coolify's deploy) stops with "AFCLAUDE_DATA is not set: choose the data directory (see setup)"; the entrypoint also refuses to start when `/data` isn't a mounted volume, so no install ever writes into a silent default location.
- **Set during setup, with an optional restore (D-180, D-187).** The setup step (`afclaude setup` on the host, or the env form in Coolify) asks for the data directory. Like the working folder (§10.4), it has **no default**: both are chosen at setup. An empty directory gives `/setup` two choices: a new install (first owner, §9.3) or **restore from a backup or export** into it (§11.3). A directory that already holds an AFClaude DB is used as it is.

Everything else (windows, budget, auth, users, OIDC client, intervals, retention, session settings) is a DB setting edited in the dashboard. `docker/.env` and `data/afclaude.json` shrink to this list.

### 10.3 Public vs local
In the repo: all code, templates, vendored assets, default prompts, compose files with `${VARS}`, `env.example`, this doc. Local or in the DB only: the DB and its backups, the master key, `.env`, the bridge key, the backlog, the decision store, identities. `tools/check_public.py` guards every commit and push.

### 10.4 AFClaude's own working root and Claude config (D-165)
- **First-startup wizard** (in `/setup`, after the first owner registers; `afclaude setup` on the CLI until the dashboard exists; the data directory `AFCLAUDE_DATA` is chosen before the containers start, §10.2): host check (§10.5) → choose the **AFClaude working root** (the working folder) on the host → Claude config and login → trust and permission setup → register this host as a machine (its machine user for interactive sessions, §9.7) and offer to grant it a role (D-195) → time zone. Or "restore from a backup or export" instead (§11.3), which asks for the working root too.
- **No default (D-187).** Like the data folder (§10.2), the working root has no default and no prefilled path: setup (new install or restore) doesn't continue until one is chosen, and no AFClaude session starts before that. The setting stores the choice; changing it later is a deliberate move with a path map (§11.5).
- The working root is a dedicated directory, **not the default/home directory** and not inside a tree of non-AFClaude projects. The wizard refuses `$HOME` and `/`, and warns when the path already holds transcripts of sessions AFClaude didn't start, or has an ancestor `CLAUDE.md`. Every AFClaude-run session, the task-managers included, runs with its cwd under it (`<root>/<project-slug>/…`; project repos are cloned there), so their transcripts (`<config>/projects/<encoded path>/`) are separable from everything else.
- **AFClaude's sessions get their own `CLAUDE_CONFIG_DIR`** (D-173), with the shared default dir as the fallback if a check in phase 5d fails:

| | separate config dir, e.g. `<afclaude-home>/claude` (**chosen**) | shared default `~/.claude` (fallback) |
|---|---|---|
| settings, hooks, MCP registration, trust flags | AFClaude's own `settings.json` and `.claude.json`; nothing leaks into or from the owner's interactive setup | mixed with the owner's; the export must pick keys out of shared files |
| transcripts | the whole `projects/` of that dir is AFClaude's | separable only by the working-root path prefix |
| login | a second `claude` login in that dir (same subscription, D-156) | one login |
| user-level `CLAUDE.md` | AFClaude's own (its autonomy rules); no carve-out in the owner's files needed | the owner's user-level file applies; a carve-out like D-103 may be needed |
| export / move | a filtered copy of one directory (caches excluded) | a selection out of shared files |
| owner's own stalled sessions (F2, D-040/D-041) | still resumed with the default dir (`driven_sessions.config_dir` says which) | same dir |
| unknowns | RC listing, `/usage`, `claude agents` and the login flow under `CLAUDE_CONFIG_DIR`: verified on the host in phase 5d; if one fails, fall back to the shared dir | none |

- **CLAUDE.md workaround:** today AFClaude runs under a parent directory whose `CLAUDE.md` forbids unasked changes, with a carve-out for autonomous AFClaude agents (D-103). In its own root (outside that tree) with its own config dir, no workaround is needed: the root's `CLAUDE.md` carries AFClaude's rules. The host check warns if a restricting ancestor `CLAUDE.md` exists.
- The guard hooks stay in AFClaude's `settings.json` so their rules are in context while sessions run with the bypass env (D-044); the per-launch allow rules and permission modes (D-047) come from DB settings.

### 10.5 Host validation (D-164)
A **Host** page (F13), the setup wizard, the post-restore check and a runner job (hourly and before each window start) run the same checks through the bridge. Each shows ok / warn / fail, when it last ran and a fix hint; a fail raises an alert and holds the automatic starts that depend on it.

| check | how |
|---|---|
| bridge reachable, host key matches, `host_exec` whitelist version = what the image expects | ssh handshake + a version command |
| tmux installed (version), can create and kill a test session `afc-check` | whitelisted commands |
| Claude Code installed, version (warn if newer than the tested one) | `claude --version` |
| logged in with a subscription, per config dir in use (AFClaude's, and the default one while owner-session continuation is on) | `claude -p --no-session-persistence /usage` (no model call) |
| usage readable and fresh | the same call vs the snapshot |
| working root exists, writable, trusted; project dirs trusted | stat + trust flags in `.claude.json` |
| permission setup: allow rules, permission modes, guard hooks present, bypass env honoured | read the config dir's `settings.json`, hook dry-run |
| MCP registration points at `afclaude-mcp` and answers `list_tools` | MCP round trip |
| `CLAUDE.md` in the root present; no restricting ancestor or user-level `CLAUDE.md` | file scan |
| host disk space, clock drift between container and host | `df`, time compare |
| end-to-end (on demand, and after a restore): a Haiku session in tmux: start → prompt → reply → kill | like `test_e2e_tmux.sh` |

The new check commands extend `host_exec.py`'s whitelist. The owner installs the bridge (D-121), so a whitelist update is a one-time owner step per bridge version; the version check flags a stale bridge.

### 10.6 Transition from today
- The current AFClaude container (D-120, the one that runs today's schedule) and the host bridge (D-121) keep running while phases 2–5 land; each runner change is deployed like today (autonomous deploys within the gates, D-106).
- The temp dashboard keeps working through the transition as side work (not a dashboard phase); its stall scan moves to the runner's own tick in phase 3a.
- Phase 5b switches to the three-service layout; the host crontab lines and supercronic are retired. Phase 5e sets this installation up again under a new data directory and its own working root, through backup → export → restore (§11.5).

### 10.7 Retiring the temp dashboard (side task after 8d, not a phase)
About a week of overlap once the dashboard is live, then stop the quickview export, remove the temp dashboard's container and route, and replace the `/private` page with a link to the dashboard.

### 10.8 Remote Control visibility (D-042, D-052)
Driven sessions run as individual RC sessions (`--remote-control --name <tmux name>`); F6 links them when the RC URL is known (source to verify in phase 6c). Threads hosted by the owner's `claude rc` server are never taken over (two writers on one transcript); they show "continue it from the app" with the skip reason. Hooking into rc server mode stays on the roadmap.

## 11. Backup, export, restore, move (D-161, D-165)

### 11.1 DB backup bundle
`backup.py`, a runner job, daily by default; also on demand from F10 (step-up):
1. Consistent snapshot without stopping: SQLite online backup (`VACUUM INTO`).
2. `manifest.json`: installation id and epoch, app version, schema version, created at, row counts per table, sha256 of every file.
3. The master key, wrapped with the backup passphrase.
4. Encrypted as a whole (passphrase → argon2id → authenticated encryption). Stored in `/data/backups` (retention setting), optionally copied off-host (a mounted directory first, S3/SFTP later).
5. The WAL archive of the retention window (below) goes into the bundle, so a restored or moved install can still roll back within that window.

**Point-in-time recovery: the WAL archive (D-175).** The DB keeps its WAL for a reasonable timeframe: WAL changes are archived instead of being discarded at checkpoint, for `db_wal_retention_days` (default 7, range 1–30). The DB can then be rolled back to any moment in that window, e.g. to just before a bad write, a corrupting bug or a wrong bulk change, which a daily bundle alone can't do.
- **Tool: Litestream, not custom code.** Existing options checked first: Litestream (streams SQLite WAL changes to a file, S3 or SFTP target; since v0.5 in the LTX format with compaction and `litestream restore -timestamp` for point-in-time restore, retention configurable), `sqlite3_rsync` (snapshots only, no point in time), LiteFS (FUSE-based replication with a lease, built for multi-node, far heavier than needed), frequent `VACUUM INTO` snapshots (coarse and costly on disk), the SQLite session extension (app-level changesets, much custom code). Litestream fits: it already solves the hard parts (copying WAL frames before a checkpoint can drop them, detecting a break in WAL continuity and re-snapshotting, timestamp restore). Copying the WAL ourselves across three writing containers would repeat that with more risk.
- **How it runs:** the pinned Litestream binary (checksum-verified) is in the image; the runner runs it as a supervised child process (§5.1) with a config generated from the settings, replicating `/data/afclaude.db` to a file replica in `/data/wal-archive` (an off-host target later, like the backups). Per Litestream's guidance every AFClaude connection sets `PRAGMA wal_autocheckpoint = 0` and Litestream does the checkpoints. If Litestream is down, the WAL grows: the runner alerts on replication lag, and past a WAL size limit it checkpoints itself (Litestream re-snapshots when it's back; the gap in the window is shown). The cross-container setup and restore times are verified in phase 5c.
- **Disk cost:** the archive holds the snapshots plus the compressed changes of the window, roughly the DB size plus what was written in `db_wal_retention_days` (for AFClaude's tens-of-MB DB, telemetry appends dominate; expected tens to a few hundred MB for 7 days, measured in 5c). F10 shows the archive's current size and the oldest restorable moment next to the setting; the bundles grow by the archive size.
- **Rollback** (F10 → "roll back to…", step-up; or `afclaude restore --at <time>`): automation pauses and all services stop writing (writes get a clear "maintenance" error), Litestream restores the chosen moment into a new file, the integrity check runs, the current DB is kept as `afclaude.db.before-<time>`, the new file is swapped in, the installation epoch is bumped and the install starts paused like any restore (§11.3). A rollback only rewinds the DB: sessions that ran and commits that were pushed in the meantime stay; the banner says so.

### 11.2 Full export: DB + the required Claude data (D-165)
The dashboard's **Export** (F10, step-up) = the §11.1 bundle + a `claude/` part read from the host through the bridge + a generated reinstate guide. Every item is in the manifest with its checksum, so a restore can prove it is complete.

| item | source | note |
|---|---|---|
| transcripts of AFClaude-run sessions, incl. subagent files and per-session side files (e.g. file history) | `<config>/projects/<encoded working-root paths>/` | selected by `driven_sessions` and the working root; the exact file list is fixed in phase 5d against the real layout |
| credentials | `<config>/.credentials.json` | **included by default** (D-174); the option is shown ticked and in red with the warning that whoever has the bundle and passphrase can use the Claude account and that the old host must stop using it; untick it to leave the credentials out |
| permission setup for the autonomous modes | `settings.json` (allow rules, default permission mode, env) | the bypass env and per-launch flags are code + DB settings, already in the bundle |
| hooks | the guard and reminder hook scripts that `settings.json` references | |
| MCP registration | AFClaude's `mcpServers` entry in `.claude.json` | |
| trust flags | `hasTrustDialogAccepted` of the root and project dirs in `.claude.json` | |
| `CLAUDE.md` files | the working root's, and the config dir's user-level one | project repos carry their own in git |
| project repos | not copied: re-cloned from their remotes (the save step pushed); a project without a remote goes in as a `git bundle` | |

Excluded: caches, the owner's other sessions and projects, anything outside the selection. With the shared default dir, the same items are picked out of shared files (by key and path prefix).

### 11.3 Restore and the reinstate guide
The export carries a step-by-step guide generated for its content (also shown in the dashboard):
1. New host: install docker, tmux, Claude Code (the manifest's version or newer), sshd and the bridge (`install_host_bridge.sh`).
2. Deploy the containers with the same `AFCLAUDE_PUBLIC_URL` (passkeys are bound to the domain; a new domain means re-registering passkeys and updating the OIDC redirect URI) and `AFCLAUDE_DATA` pointing at a new, empty data directory (§10.2).
3. `/setup` → "restore from a backup or export" (setup code + passphrase), or `afclaude restore <bundle>`: decrypt, verify checksums and manifest, refuse a schema newer than the code, migrate forward if older, bump the installation epoch, record `restored_from`. The bundle's WAL archive comes along, so the restored DB can be taken at the bundle's latest moment or any earlier one in its window (§11.1).
4. Choose the working root (no default, D-187; the bundle's old path is shown for reference, not prefilled): the same path as before (transcripts are keyed by the encoded cwd, so this needs no path map) or a new one with a path map (§11.5).
5. The runner writes the `claude/` part to the host through a whitelisted restore command: config dir files, trust flags, MCP registration, hooks, `CLAUDE.md`.
6. Sign Claude in for that config dir if the credentials weren't included.
7. Clone the project repos into the root (the guide lists them with their remotes).
8. Post-restore validation: every manifest item present with its checksum, plus all host checks (§10.5) incl. the end-to-end session.
9. The install **starts paused** (D-173, setting `restore_starts_paused`, default on) with a banner "restored from <backup time>: check and resume". One tap resumes; the projects' task-managers `--resume` their session where the transcript is present, otherwise they start fresh from the latest hand-off (§5.5).

### 11.4 Move to another host
1. Old host: pause automation; the task-managers (and any long-running agents) get the session-end save prompt (§5.5), so their hand-offs are current and their work is pushed; wait for running sessions to finish or save.
2. Take a final full export marked "move": the old installation becomes read-only and stays paused (a fence against two installs driving the same account and repos).
3. New host: §11.3.
- A restore test (export → fresh install → equal row counts and checksums → a dry-run dispatcher pass decides the same) runs in CI and as part of gate 8a.

### 11.5 One-off migration of this installation (D-165, D-180)
Today AFClaude's sessions share a root directory and the default `~/.claude` with sessions and projects AFClaude doesn't manage. This installation is set up again **on this host, under a new data directory** (`AFCLAUDE_DATA`) **and a new working root**, with its own config dir (§10.4), through the normal setup with a restore (§10.2, §11.3). Both folders are chosen at that setup, with no defaults (D-187). That run is **the end-to-end test of the whole backup → export → restore path, covering both the data folder and the working folder**, and the validation that the export is complete:
- **One-off flow derived from the normal backup and export.** The migration runs the normal backup (§11.1) and export (§11.2) code, with one addition: a selection filter that picks only AFClaude's sessions and projects out of the shared root and shared config dir, never other sessions (from `driven_sessions`, `projects.path`, the task-manager sessions and the own-sessions list; the owner confirms the list). It carries nothing the export wouldn't carry.
- **Restored at setup.** The new install starts with `AFCLAUDE_DATA` set to the new, empty directory, takes "restore from a backup or export" with that bundle and chooses the new working root in the same setup, so setup, restore, the `claude/` part and the post-restore validation all run exactly as on a new host.
- **Path map.** Restore takes `old prefix → new root` and rewrites the encoded project-dir names and the cwd in the session metadata. This is a normal restore feature, not a migration-only one. Whether `--resume` accepts a moved transcript is verified in phase 5e; if not, those projects continue from their hand-offs.
- **Nothing by hand.** Whatever turns out missing or wrong on the new side is fixed by correcting or expanding the export (code + manifest); then export and restore run again from scratch, until the post-restore validation, the end-to-end session and one real task-manager continue pass.
- Cut-over: the old setup is paused and fenced, the new one unpaused; the old data directory and the old transcripts stay in place, untouched.

## 12. Testing strategy
- **Unit (offline, temp DBs):** every `actions.py` action (validation, audit, version, idempotent replay, role matrix incl. owner, manager, editor, viewer, machine user, task-manager and agent: a machine user without grants can do nothing and with a grant exactly that role, viewers see a project's questions but can't answer them, only managers and owners can force an immediate run, each of the agent's direct writes applies, every other agent write becomes a proposal and changes nothing); agent proposals (approve / edit / reject apply through `actions.py` with the task-manager as actor, a stale version gives a 409, nothing applies while the task-manager is down, the run summary lists every write of the run incl. the direct ones, a proposal reaches the task-manager at once whether it is running or idle, with no window or budget gate on the delivery); `schedule.py` (DST nights both ways, N × session_hours, link groups, the window-start key, nights until the reset); `prompts.py` (project > global > default, placeholders, bundles); importers (file → DB, row counts equal); hand-off lifecycle and the compaction rule (per tmux session, only if it existed during the previous session window); the schema guard; decision summaries (length cap; `full=false` returns no rule text and carries the expand notice); task-manager messages (thread open/append, reset after `ui_chat_reset_hours`, reply via MCP, owner and manager messages wake at once, editor messages wait).
- **Auth:** factor rules; passkeys with recorded attestation fixtures (hardware BE=0 + MDS-verified passes alone; BE=1 needs a second factor; revoked AAGUID refused; bad signature refused); TOTP windows and replay; recovery codes single-use; lifetimes and step-up with a fake clock; OIDC against a stub provider (state/nonce/PKCE, group map add/remove incl. a group granting `owner`, no e-mail linking); several owners and the last-owner guard; setup code; machine tokens (registration code single-use and expiring, only the hash stored, revoked and replaced tokens refused, constant-time compare, stdio and remote transport both authenticate, a driven session with a machine token still counts as task-manager or agent, an unknown connection counts as an agent, audit actor `machine:<id>`, the two-hostname alert).
- **API (TestClient):** route-table auth test, CSRF/Origin, 409s, idempotency, size limits, headers/CSP, no secret in any response.
- **Runner:** dry-runs with settings rows (moved window, pause, run-now skipping window and budget, the AFClaude task-manager as an ordinary project's task-manager, readable tmux names, wake socket, compaction sequence) with the stub-PATH fixtures; two processes hammering the DB (one winner, no lock errors).
- **Backup and export:** round trip and a move rehearsal on temp volumes; point-in-time rollback to a moment inside the WAL window (three writing processes, a checkpoint gap while Litestream is down); wrong passphrase and tampered bundle refused; the export's selection (only AFClaude's sessions, nothing else) and the path map on fixture config dirs; the manifest check catches a missing item.
- **Host checks:** each check against stub host commands (ok / warn / fail), a stale bridge version, a logged-out `claude`.
- **Containers:** compose up with only `AFCLAUDE_PUBLIC_URL` and `AFCLAUDE_DATA` → setup page, which doesn't finish without a chosen working root; without `AFCLAUDE_DATA` → refuses to start with the clear message; health checks; read-only fs.
- **UI:** every page at 390 px in a headless browser (screenshots in the phase report).
- **Every commit:** `python3 tools/check_public.py --tree HEAD` and the hooks, never bypassed.

## 13. Phased build plan

Each phase is one subagent in a worktree with a clear definition of done (tests green, `check_public` clean). Runner phases keep every existing suite green and are deployed autonomously within the gates (D-106). No dashboard deploy before 8b, no internet route before 8d, nothing from the dashboard reaches Claude before 8d (D-072, D-157).

1. **Config + v4 schema + `actions.py`.** Done (01.10.).
2. **Backend foundation**
   - **2a Settings in the DB (D-146):** every tunable into `SETTINGS` (afclaude.json, dispatcher.json), one-time import, runners and pacing read the DB, schema guard (A1), the DB error path and escalation (§7.8, D-171).
   - **2b `schedule.py` (D-148):** the only window code, pacing included; DST tests; `window_tz` plumbing.
   - **2c Telemetry into the DB (D-161):** usage tables + importers; sampler, `limit_ratio`, `pacing`, `usage_review`, `usage_report` read/write the DB; `account_id`.
   - **2d Runner state + docs into the DB (D-161, D-181):** state files, own sessions, scheduled jobs, logs → DB; `docs`/`doc_entries`, with the AFClaude build-log export (§7.7) as the only file output.
3. **Runners**
   - **3a Resident runner (§5.1):** daemon with the job table, stall-scan tick, status snapshot, wake socket, all runner writes via `actions.py` (D-154), `run_log`/`driven_sessions` filled.
   - **3b Task-manager unification + run-now (D-147, D-153, D-036, D-189):** the AFClaude task-manager as an ordinary project's task-manager in the dispatcher (one per project, no global one), keep-alive continuation retired, readable tmux names, `continue_now`/`work_on_now`/`review_now` skipping window and budget (owners and managers only).
   - **3c Compaction hand-offs (D-144, D-155, D-191):** `handoffs` per tmux session, the save prompt trigger (only for a tmux session that already existed during the previous session window), MCP tool, `/compact` + resume prompt.
4. **Content stores**
   - **4a `prompts.py` (D-111, D-152):** project scope (migrate step), bundles per session kind, every sender uses it.
   - **4b Questions, decisions and task-manager messages in the DB (D-151, D-158, D-176, D-178, D-181):** `decisions` + links + FTS, proposals as questions, MCP tools (relevance-only summaries, expand before acting), seeding, `OPEN_QUESTIONS.md`/`DECISIONS.md` retired (no export, D-181), prompts and CLAUDE.md rules switched to the DB; `chat_threads`/`chat_messages`, the `task_manager_message` request (waking the task-manager for owners and managers, waiting for editors, D-186), MCP `afclaude_messages`/`afclaude_reply` and the task-manager prompt's instructions for them.
5. **Users, packaging, backup**
   - **5a Users, roles, task-manager and agent roles (D-149, D-160, D-182–D-190):** users/grants schema (owner, manager, editor, viewer), actor = user, the role matrix in `actions.py`, task-manager/agent detection (incl. whether subagents can be told apart), `agent_proposals`/`agent_runs`, run summaries, MCP `afclaude_proposals`/`afclaude_review`, immediate delivery of proposals to the task-manager (D-188), the task-manager's own review of finished branches before merging (D-185), the task-manager and agent prompts' instructions, alerts on violations.
   - **5b Containers + host checks (D-163, D-164):** one image, web/runner/mcp services, compose for docker and Coolify, minimal env, master key, MCP in a container, cut-over from the current container and host crontab; sessions stay on the host via the bridge; the host checks (§10.5) as a runner job + CLI, with the bridge whitelist extension.
   - **5c DB backup / restore / move (D-161, D-175, D-180, D-187):** bundle, restore, `restore_starts_paused`, move fence, CI round trip; the WAL archive with Litestream (`db_wal_retention_days`, rollback to a moment, archive in the bundle, disk use measured); `AFCLAUDE_DATA` required with no default, set at setup together with the optional restore (the working root likewise, 5d).
   - **5d Claude data export + working root (D-165, D-187):** first-startup working root (no default, chosen at setup and at restore) and config dir (CLI; the UI follows in 6a), verify `CLAUDE_CONFIG_DIR` on the host (RC, `/usage`, `claude agents`, login), the `claude/` export part, whitelisted restore command, path map, the generated reinstate guide, post-restore validation.
   - **5e One-off migration of this installation (§11.5, D-180, D-187):** the end-to-end test of backup → export → restore, covering both folders: the normal backup and export with the AFClaude selection filter out of the shared root and config dir, restored at setup into a new data directory and a new working root on this host (both chosen at that setup, no defaults), with the dedicated config dir; every gap fixed in the export and the run repeated from scratch, until validation, the end-to-end session and a real task-manager continue pass.
6. **Dashboard**
   - **6a Skeleton + local auth:** Starlette app, layout + top bar (D-074), sessions and lifetimes (§9.5), password + TOTP + recovery codes, setup/first registration and the first-startup wizard (§10.4), CSRF, headers, route-table and matrix tests, `health`.
   - **6b Passkeys + OIDC (D-150, D-159):** python-fido2 with MDS3, BE rules and warnings, Authlib OIDC with the group map, step-up re-auth, F11.
   - **6c Read views:** F1–F9, F12, F13, the threshold panel, RC URL source verified.
7. **Writes**
   - **7a Work writes:** answers, the task-manager message box (F14, D-178, D-186), tasks, projects, priorities, moves, decisions and proposals, 409 handling.
   - **7b Control writes:** stalls and rules, window editor, prompt editor, pause, run-now with live feedback, Settings page with reset/undo (D-075), users/grants, backup and export UI, confirm + step-up for high-impact writes.
8. **Deploy gates, strictly in this order (D-072, D-145, D-157):**
   - **8a Thorough review (Opus 5.5, ultracode/max effort).** The whole dashboard and its integration: `actions.py`, `schedule.py`, `prompts.py`, the runner, auth (OIDC + groups, password + 2FA, passkeys + MDS), backup, export and restore, the host checks, containers. It also checks that **the docs and every owner decision agree with the actual code**, not only with comments and explainer files; every discrepancy goes to the owner to decide, none is fixed silently. It also **validates the users and roles** (D-168): every role's effective permissions match its description in §2, the group→role map, the first-owner setup, and the user records themselves. Findings fixed and re-reviewed before 8b.
   - **8b Deploy without the Claude connection (with the owner's approval).** The three services on a staging volume that no live runner reads, the session executor disabled; **no internet route** (tunnel or VPN only). Curl checks: no session → 401/login, wrong Origin → 403, health fresh.
   - **8c Live pentest with full code access** against the 8b deployment: auth bypass, setup-code race, password/TOTP brute force, passkey policy bypass (BE flag, attestation, MDS), OIDC flow and group-claim injection, session fixation and lifetimes, CSRF, IDOR across projects and roles, **role-boundary tests (D-168): for each role, a test user tries to reach what the role description says it should not (other projects, global settings, owner-only actions, user management, other users' sessions)**, agent-role escalation via MCP (an agent posing as a task-manager, a direct write outside the §9.6 list, a tampered proposal payload), injection, headers/CSP, backup and export download, the bridge whitelist (restore and check commands). Fixed, re-tested, 8a re-run on non-trivial fixes.
   - **8d Attach the Claude connection, seed the backlog, open the route (with the owner's approval).** Point the services at the live volume and enable the executor; seed `BACKLOG.md` locally (D-065); then add the public route behind the login.

The temp dashboard's phase strip (D-085) shows these phases incl. the sub-phases and 8a–8d.

**Status display (D-166):** the status views show when the next run will take place (date/time, kind, one-line reason, from `pacing.next_run()`), not the budget rule itself; the rule details belong to the threshold/settings view.

## 14. Future (after phase 9, not phases)
- **Shared account pool (D-156).** Several Claude accounts as one pool, never assigned to projects; the runner picks an account with headroom. Needs installations talking to each other. Hooks left open now: `accounts` table and `account_id` on telemetry and runner rows; an `AccountPool` interface with one single-account implementation; `installation` id and epoch; a peer API (signed, per-installation keys) to be designed later. Build on the existing open-source `claude-accounts` project (owner decision D-167): preferably contribute a small API upstream that AFClaude calls, so it stays current, instead of copying its code; opening that PR needs the owner's go.
- **Window recommendations (D-035).** The usage monitor recommends shifting windows when the owner is usually active during a window or usually idle elsewhere; it aggregates by weekday + hour only, shows the evidence (weeks per slot) and proposes a concrete F4 edit, applied as a normal `window.set`.
- **Experience store across users (D-137)** and the usage split (D-138): universal vs user-specific data stay separate, with format versions.
- **Hooking into `claude rc` server mode (D-052).**
