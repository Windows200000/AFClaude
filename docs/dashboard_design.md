# AFClaude dashboard and backend: target design (v2)

Status: target design for phases 2–9, rewritten on 04.10.2026 after the owner's architecture review (D-143/D-145; answers D-146–D-165). Phase 1 (schema v4, `actions.py`, `afclaude_config.py`) is built. This doc replaces v1 as a whole; where v1 and this doc differ, this doc holds. `D-NNN` ids point at the owner's local decision store; owner decisions are binding, everything marked *proposal* is the manager's and may be changed. Host names are placeholders.

## 1. Goals and non-goals

Goals
- One place, usable from a phone, for everything AFClaude needs from people or wants to show: questions, blocked tasks, stalled sessions, the queue, windows, usage and budget, decisions, prompts, the sessions AFClaude drives, hand-offs and the manager docs.
- Built from scratch with its own architecture (D-070). The temp dashboard (the read-only `/private` status page, `export_quickview.py` + `quickview/`) is NOT part of this design; it stays in use until the dashboard is live and is then retired as a separate side task (§10.7).
- **Everything lives in one DB** (D-161): config, auth, work, decisions, questions, hand-offs, prompt overrides, usage telemetry, runner state and the manager docs. A backup or a move to another host restores a complete, clean continue (§11). Only prompt *defaults* ship with the code.
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

## 2. Users, roles and access (D-149, D-156, D-160)

| role | who | can |
|---|---|---|
| **owner** | the first browser registration (exactly one) | everything, incl. making/removing co-owners and transferring ownership |
| **co-owner** | granted by the owner (or by an OIDC group, §9.4) | everything except owner-only user actions (*proposal*: co-owners manage users and project grants but can't create or remove co-owners) |
| **project edit** | granted per project | that project's stages, priorities, order, question answers, stall decisions for its sessions; propose decisions |
| **project view** | granted per project | read that project (stages, questions, decisions, hand-offs, effective prompts, its driven sessions) |
| *(none)* | every other registered login | nothing; sees "no access yet" (default for new users, D-149) |
| **agent** | AFClaude's autonomous sessions (MCP/CLI) | add/edit tasks and ask questions in its project, write its own hand-offs and its project's manager docs, propose decisions; never owner-only actions (D-160) |
| **runner** | the runner daemon's components | the state changes the runners own (task start/finish, run log, telemetry), audited as `runner:<component>` |

- Only owner and co-owners see global settings (windows, budget, auth, users, backup) (D-149).
- *Proposal:* run-now, prompt overrides, project-scope "always continue" rules and decision confirmation are owner/co-owner only, because they spend the shared budget or bind autonomous sessions.
- Granting **edit** on a project lets that user steer autonomous sessions that run with the guard hooks bypassed on the execution host (an answer or a stage becomes a session's input). The grant dialog says so.
- The audit actor is the user (`user:<id>`), the session (`agent:<session>`) or the component (`runner:dispatcher`).
- v1 enforcement of the agent role is advisory (D-160): rejected attempts are audited and raise an alert; see §9.6 for its limits.

## 3. Main flows (phone first)

One column, large tap targets, no hover-only controls, no drag-and-drop as the only way to reorder, every page useful on its first screen. Top bar: automation state (running / paused, toggle), tonight's window ("23:00–09:00, 2 × 5 h"), weekly usage and the projected end of week, inbox badge. Every view is filtered by the viewer's grants.

- **F1 Inbox (home).** Only real questions (D-083): open questions (`kind='question'`) and blocked tasks, with a text box each; decision proposals waiting for confirmation. Never stalls, never untitled sessions. Answering removes the card at once (the server returns the new inbox). Empty inbox = one line.
- **F2 Stalled sessions.** Only sessions whose last entry is a limit notice (D-041), newest first, own AFClaude sessions hidden by default. Card: title (or cwd + first prompt), project, stall kind, reset time, the effective decision and its source. Buttons: Continue, Ignore, "Always…" (session rule or project rule, preselecting the most specific cwd), and **Run now** (§5.4). Undecided stalls never expire; decided ones move to a "decided" filter. RC-server threads show "continue it from the app" (§10.8).
- **F3 Queue and projects.** Ranked projects with their stages and a priority chip (tap cycles high → medium → low, D-064). Up/down buttons, "move to position…", optional drag on desktop. Per-project menu: whole project to medium/low, edit, manage/unmanage, **Work on now** (D-036, §5.4), members (grants). A second tab shows the flat execution order and why an item is skipped.
- **F4 Window planner.** Seven rows Mon..Sun with each night's window or "off"; linked days share a colour (D-034, §6). Editor: change all linked / only this day / whole week; preview over the next 7 nights incl. DST nights and the weekly-reset marker; the measured ratios (`limit_ratio.py`: weekly % per session window, windows per week and left, AFClaude vs user share, each with "insufficient data" when honest). Save applies at once (the runner is woken).
- **F5 Status.** Runner health (last tick per job), the latest budget decisions with their reason, run log, the next usage review, review results split into universal and user-specific, and the reserve-threshold numbers (§3.1).
- **F6 Driven sessions.** Every session AFClaude started or continued: readable tmux name (§5.3), kind, project, state, RC link when known, the latest hand-off.
- **F7 Decisions.** Per project (and install-wide): the current decisions (D-104, D-158), keyword filter, proposals with "confirm / reject / edit", "propose a change".
- **F8 Prompts.** Every prompt with its placeholders and the session kinds that use it; default, global override, project override, the effective bundle per session kind, diffs. Edit, validate, save, reset.
- **F9 Docs.** Per project: goals, progress, alerts, exceptions, reviews, the phase strip (D-084, D-085); the generated exports.
- **F10 Settings.** Grouped sections (§7.4), incl. auth and session lifetimes (§9.5), users and grants, backup and the full export with its reinstate guide (§11).
- **F11 Account.** My logins: passkeys (with the hardware / synced badge), TOTP, recovery codes, OIDC links, active sessions and remembered devices with "sign out", display time zone.
- **F12 Audit.** The last N writes with actor, via, before/after summary; undo where the action supports it (D-075).
- **F13 Host.** The host validation (D-164, §10.5): tmux, Claude Code, login, bridge, working root, permissions, MCP, each ok / warn / fail with a fix hint; "run all checks" and the end-to-end session test.
- **Add task** (secondary): title, description, project, priority.

First login: a popup offers the browser's time zone (`Intl.DateTimeFormat().resolvedOptions().timeZone`). For the owner it sets the global `window_tz`; for everyone it sets their display time zone. Both stay adjustable (D-148).

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
     ├ stall scan, sampler, usage review, exporters, backups
     └ session executor ──ssh bridge──> host: tmux + Claude CLI sessions (D-164)
```

- **One image, three services** (D-163): `afclaude-web` (the only one with an HTTP port), `afclaude-runner` (a resident daemon, §5.1), `afclaude-mcp` (the MCP server). All share the data volume. SQLite WAL across containers is fine on one kernel; never on a network filesystem, so all three run on one host.
- **Usage numbers** reach the dashboard through a `status_snapshot` row the runner writes each pass (usage %, resets, budget decision, `threshold_info()`, ratios). The web container imports no model code and never reads `~/.claude.json`, calls `/usage`, `claude`, tmux or the bridge. Pages show the snapshot's age.
- **Sessions stay on the host** (D-164): AFClaude itself (web, runner, MCP) is containerised; the Claude CLI sessions run in tmux on the host, started by the runner through the whitelisted SSH bridge (D-120, D-121), in AFClaude's own working root (§10.4). The dashboard validates the host setup (§10.5).
- **MCP transport:** the `afclaude-mcp` container, registered for host sessions as a stdio command (`docker exec -i afclaude-mcp afclaude mcp`), which works today. It is never exposed on the network.
- Code layout: `dashboard/` (app, routes, templates, static), `actions.py`, `schedule.py`, `prompts.py`, `runner.py` (daemon), `auth/` (sessions, password, TOTP, WebAuthn, OIDC), `backup.py`, `docker/`. The flat modules stay.

## 5. Runners

### 5.1 One resident runner daemon (*proposal*, follows from D-153)
`runner.py` replaces supercronic, the `at` shim and the keep-alive watcher. It is one process with an internal scheduler; every job runs in a child process with a timeout, so a crash doesn't stop the loop. Job intervals are settings.

| job | default |
|---|---|
| dispatcher pass (approved stalls, task starts, managed projects incl. the AFClaude manager, cleanup, verification) | every 10 min + on wake |
| window-start tick: the first pass at/after a window start fires each managed project's window-start continue once per window (dedup key = the window's end date); a POSTPONE defers it to last activity + 60 min (D-018) | in the dispatcher pass |
| stall scan (`stalled.scan`, own tick) | every 3 min |
| usage sampler + pre/post-reset samples | every 15 min + scheduled jobs |
| usage review | when due |
| status snapshot, exporters (§7.7), backups (§11) | each pass / on change / daily |

- **Wake:** after any write that the runner must act on (run-now, window, pause, answer, decision), `actions.py` sends one datagram to `/data/run/runner.sock` after the commit. The runner then reads `action_requests` and settings from the DB. The DB is the truth; a lost wake is caught by the next tick. No polling interval for run-now (D-153).
- Health: the runner writes its last tick per job to `runner_state`; the web container's health check and F5 read it; the container restarts on a stale heartbeat.
- Pause (`automation_paused`) stops automatic starts and continues; it never kills running sessions.

### 5.2 One continuation path (D-147)
- The AFClaude manager is an ordinary managed project (`projects.manager_session`), continued by the dispatcher like every other managed project: window-start continue, stall continue, last-stretch slots, compaction (§5.5). The keep-alive watcher's continuation path and the manager exclusion (`keepalive_sessions`) go away. `keepalive.py` keeps only reusable pieces (transcript parsing, preflight, budget wiring).
- Per-project session settings: model and effort (default for manager sessions: Opus 5.5, high, D-043), permission mode (auto; dontAsk for Haiku, D-047), guard bypass with the hook rules in context (D-044), no `--bg` (D-045), take-over-idle (D-046), archive at the end (D-050), trusted dirs (D-051).

### 5.3 Readable tmux names (D-147)
- `afc-<project-slug>-<role>`: `afc-afclaude-manager`, `afc-spending-task42`, `afc-stall-<title-slug>-<id4>`, `afc-review`. Charset `[a-z0-9-]`, ≤ 40 chars, a short suffix only on collision. The same string is the RC `--name`.
- Runners find their sessions through `driven_sessions` (tmux name ↔ session id), never by a name regex (today `ka-[0-9a-f]{8}`).

### 5.4 Run now (D-153, D-036)
- Kinds: `continue_now` (one stalled session), `work_on_now` (a project: its manager session, or its next ready stage), `review_now`.
- Run-now **skips the time window and the budget check** (incl. the session-usage stop) and starts **immediately**: the write queues the request and wakes the runner (§5.1). It still passes the safety preflight (never take over an RC-server thread or a live holder; never fork a session). *Proposal:* it also runs while automation is paused (pause is about the automatic schedule) and isn't limited by the automatic concurrency cap; the button warns when the session limit is nearly used up.
- Feedback: the request row moves queued → started (tmux name) / refused (reason), shown live on the card.

### 5.5 Compaction hand-offs (D-144, D-155)
For sessions whose total run is longer than one session length (the managers; others only if they run that long):
1. **Save prompt.** Before the session window ends (the earlier of `handoff_before_reset_min`, default 20 min before the session reset, and `handoff_session_pct`, default 90%, below the 95% night stop of D-014), the runner sends `session_end_save.md`: finish the current step, commit, write the hand-off, compaction follows at the next start.
2. **Structured hand-off.** The session writes it through MCP (`afclaude_handoff`) into `handoffs`: summary, done, in progress, next steps, open question ids, decision ids, files touched, notes (JSON, size-limited). One open hand-off per project; a new one replaces the open one.
3. **Compact on resume.** At the next continue the runner resumes the session, sends `/compact` first, waits for the compaction marker in the transcript, then sends the continue prompt with the hand-off in `{handoff}` and marks it consumed.
4. No hand-off (crash, missed save): the continue prompt says so and points at the latest progress entries and run log.
- The dashboard shows the latest hand-off per project (F6, F9). Hand-offs are in the backup (§11) and let a moved install resume cleanly even when a transcript can't be resumed.

## 6. Schedule (D-148, D-030, D-033, D-034)

- `schedule.py` is the **only** window code; the dispatcher, the window-start tick, pacing (nights left, the straight-line fallback, the last stretch), the stall rules, the F4 preview and window recommendations all use it. It replaces the three implementations in `keepalive.py`, `pacing.py` and the validation in `actions.py`.
- A window is `[start, start + N × session_hours)`: the start is a wall-clock time in `window_tz`, the length is absolute. So **every window is N whole 5-h sessions, also on DST nights**; such a night ends an hour earlier or later on the wall clock, and the preview shows it.
- The grid is anchored at the chosen start: session k runs from start + k × session_hours, so every session window inside the automation window is a full one. The start picker moves in 30-min steps and offers "snap to the usual reset" from the samples.
- Per weekday with link groups: `window_days = {mon: {start, n, group} | null, …}`. A window belongs to the weekday it starts on; `null` = no window that night. "Change all linked" rewrites the whole group in one transaction; "only this day" gives the day a fresh group; "whole week" writes all seven with one group. Validation: 30-min grid, n ≥ 1, no overlap with the next day (the error names the day). Built in phase 1 (`window.set`).
- Default: every day 23:00 × 2 (D-033), one weekly link group. `window_tz` defaults to Europe/Berlin (D-031) until the owner's first login sets it from the browser (§3).
- API: `windows(from, to)`, `in_window(t)`, `current_window(t)`, `next_window_start(t)`, `session_slots(window)`, `window_key(window)`, `nights_until(reset)`; all tz-aware, never a fixed UTC offset.

## 7. Data model: everything in the DB (D-161)

### 7.1 Conventions
- SQLite in WAL mode at `/data/afclaude.db`; `CREATE TABLE IF NOT EXISTS` plus the `COLUMNS` dict; validation in Python.
- **Schema guard (review A1):** code refuses to write when the DB's `schema_version` is newer than its own; non-additive changes (e.g. the `prompt_overrides` key, §7.6) go through an explicit migrate step after a backup.
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
| prompts | `prompt_overrides` (+ `project_id`) | built; scope new |
| decisions | `decisions`, `decision_links`, `decisions_fts` | new (§7.6) |
| hand-offs | `handoffs` | new (§5.5) |
| docs | `docs` (whole documents: goals, exceptions, backlog), `doc_entries` (append logs: progress, alerts, reviews, drift) — per project | new |
| auth | `users`, `grants`, `identities` (OIDC iss+sub), `credentials` (password, TOTP, WebAuthn), `recovery_codes`, `auth_sessions`, `remembered_devices`, `mds_cache` | new (§9) |
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
| `PROGRESS.md`, `GOALS.md`, `ALERTS.md`, `EXCEPTIONS.md`, `data/reviews/*`, the drift log | `docs` / `doc_entries` (exports, §7.7) |
| `OPEN_QUESTIONS.md`, `DECISIONS.md` | questions as tasks, `decisions` (§7.6) |
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
  - **Automation:** `automation_paused`, job intervals, concurrency cap, `handoff_before_reset_min`, `handoff_session_pct`, the `continue_now` safety options.
  - **Sessions:** model/effort/permission mode per session kind (overridable per project).
  - **Auth & sessions:** §9.5, OIDC config and the group map (§9.4).
  - **Host:** working root, config dir mode (separate / shared, §10.4), bridge user, host-check interval, tested Claude Code version, owner-session continuation on/off.
  - **Backup:** schedule, retention, target, include credentials in exports (off) (§11).
  - **Data:** telemetry and log retention; docs export per project (§7.7).
  - **Display:** per-user time zone, theme.

### 7.5 Per project vs global (D-152)
- Per project: decisions, questions, prompt overrides, hand-offs, docs (goals, progress, alerts, exceptions, reviews), session settings, grants.
- Global: windows, budget, automation, auth, users, backup, accounts.

### 7.6 Prompts and the decision store
**Prompts (D-110, D-111).** Defaults ship as `prompts/*.md`. `prompt_overrides` is keyed by `(name, project_id)` (`NULL` = global; the table is empty, so the key change is a trivial migrate). Resolution: project override > global override > default. `prompts.py` defines one **bundle per session kind** (manager continue, task start, stall continue, session-end save, resume after compact, usage review, Haiku judge), so F8 shows exactly what each kind receives. Saving validates the placeholder set and test-renders; `base_sha256` flags "default changed since your edit" with a three-way view.

**Decision store (D-104–D-107, D-151, D-158).**
- `decisions`: `id` (D-NNN), `project_id` (`NULL` = install-wide), `slug` (stable unique key per project, e.g. `budget/night-gate`), `title`, `summary`, `words` (verbatim quotes with date and source, JSON), `keywords` (JSON), `scope`, `status` (`proposed | active | done`), `author` (`owner | co-owner | manager`), `interpretation` (clearly non-binding), `updated_at/by`, `version`.
- **Only the current decision per entry** (D-158): a change overwrites the row. There are no superseded entries in the store; each change is in the audit log (old and new text), which is the history view. A decision that no longer applies is deleted (audited).
- Who writes: owner and co-owners edit or confirm directly. Managers, agents and project editors **propose**: a proposal is a question whose payload is the new or changed decision; the active entry stays in force until a human with the right role confirms (D-104). Manager decisions (D-105) are proposals with `author=manager` that briefs may follow but that never override an owner entry.
- Retrieval for briefs (D-107): MCP `afclaude_decisions(project, keywords, full=false)` matches keywords plus FTS5 over title/summary/words and returns summaries; full text only on request. Typed `decision_links` (`refines | requires | conflicts`) surface related and conflicting entries with each hit.
- Seeding (phase 4b): active owner entries import verbatim as `active`; superseded and drift entries are written to the audit log only; partially superseded entries are merged by the manager into one current entry with status `proposed` for the owner to confirm.
- Design note from the reference project the owner named (D-158): a stable unique key per entry with upsert-overwrite (no history in the store), typed links between entries with contradictions shown inline on retrieval, and deterministic full-text lookup that injects only a few high-confidence matches into a session.

### 7.7 Exports
- The runner regenerates files from the DB on change: `OPEN_QUESTIONS.md`, `DECISIONS.md`, `ALERTS.md` (local, gitignored) and per project the docs it opts into (`docs_export`: none | local | repo).
- For AFClaude, `PROGRESS.md`, `GOALS.md` and `EXCEPTIONS.md` stay the public build log: exported into the repo and committed by the manager, through the `check_public` hooks.
- Exports are read-only copies; nobody edits them. The CLAUDE.md rule "delete resolved items from OPEN_QUESTIONS.md first" becomes "close the question in the DB first" (phase 4b).

## 8. API surface

All under `/api/v1`, JSON; HTML pages call the same handlers (htmx gets fragments). Reads are GET; writes are POST/PUT/PATCH/DELETE with an `Idempotency-Key` and the `version` where one exists. Every write is one `actions.py` call. Every handler checks the role (§2) before anything else.

Reads: `overview`, `inbox`, `projects`, `queue`, `tasks/{id}`, `stalls`, `rules`, `window` (settings + next 7 windows + ratios), `usage` (the snapshot), `runs`, `sessions/driven`, `handoffs`, `decisions`, `prompts`, `prompts/{name}` (default, overrides, bundle, diff), `docs`, `reviews`, `audit`, `settings`, `users`, `me` (logins, sessions), `host` (latest checks), `backups`, `health` (no secrets).

| write | effect | conflict |
|---|---|---|
| `projects` / `tasks` create, edit, move, priority, cancel, reopen | as built (`project.*`, `task.*`) | version / no-op / 409 on state |
| `tasks/{id}/answer` | blocked → pending; a question → done; a decision proposal → applied or rejected | 409 unless open |
| `stalls/{session}/decision` | continue / ignore / clear with the `stall_ref` shown | 409 if it stalled again |
| `rules` | session / project rule | unique (scope, match) |
| `decisions` create, edit, confirm, delete | §7.6 | version |
| `settings/{key}` set / reset; `settings/reset` (section, all) | registry-validated; undo from audit | version |
| `settings/window` | day, start, n, mode | version |
| `prompts/{name}?project=` PUT / DELETE | override / reset | base_sha256 + version |
| `requests` | `continue_now`, `work_on_now`, `review_now` (wakes the runner) | one open per (kind, target) |
| `users`, `grants` | invite, role, project scope, remove | version |
| `auth/*` | login, factors, passkeys, OIDC, step-up, logout, sessions | §9 |
| `backup`, `export` create / download; restore (setup only) | §11 | one running at a time |
| `host/checks` | run all checks or one (and the end-to-end test) now | one run at a time |

Concurrency: WAL + `BEGIN IMMEDIATE` + 30 s busy timeout serialise writers across containers; dashboard transactions do no I/O. `start_task` claims only pending tasks atomically, so "answer" vs "start" and "cancel" vs "start" have exactly one winner. Shrinking a window or pausing never kills running sessions.

## 9. Security

### 9.1 Threat model
A write here is close to "run code on the execution host": answers, stages and prompts become input of autonomous sessions that run in auto mode with the guard hooks bypassed; continue/run-now spends the budget; window and pause decide when this happens. The final dashboard is **reachable from the internet behind its login** (D-157), with several users (D-149).

Attackers: anyone on the internet; a CSRF page in a user's browser; a stolen session cookie or device; a phished password; a compromised or misconfigured OIDC provider or a hostile group claim; a user escalating beyond their grants (IDOR across projects); an autonomous session, prompt-injected by content it read, writing back through MCP or the CLI; a stolen backup bundle.

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
- **First registration (D-149):** while `users` is empty, `/setup` registers the owner. *Proposal:* it requires a one-time setup code that the web container prints to its log on start, so a fresh install that happens to be reachable can't be claimed by a stranger. The owner registers locally (password + TOTP, or a hardware passkey); OIDC is configured afterwards. Setup can instead restore a backup (§11).
- Lockout recovery: `docker exec afclaude-web afclaude admin reset-auth` (host access = owner) or recovery codes.

### 9.4 Authorization and OIDC groups (D-149, D-150)
- Deny by default: a route-table test asserts that every route except `health` and the login pages needs a session, and an action-matrix test asserts each `actions.py` action × role (§2).
- Grants: `grants(user, scope = coowner | project:<id>, level = view | edit, source = manual | oidc:<group>)`.
- **Group map** (setting `auth_oidc_group_map`): `[{group, grant}]`, e.g. `afclaude-admins → coowner`, `team-x → project:spending:edit`. Group grants are recomputed at every OIDC login (removed when the group is gone); manual grants are separate. The owner role is never granted by a group. A login with no matching group = registered, no access.

### 9.5 Session lifetimes (D-162): settings with defaults
Defaults follow NIST SP 800-63B AAL2 (30 min idle, 12 h total) and keep friction low through one-tap passkeys and remembered devices.

| setting | default | range | effect |
|---|---|---|---|
| `auth_idle_timeout_min` | 30 | 5–480 | no request for this long → sign in again |
| `auth_absolute_lifetime_h` | 12 | 1–168 | one login lasts at most this long, active or not |
| `auth_remember_device_days` | 30 | 0–90 (0 = off) | on a remembered device a password login skips the second factor; never the first factor, never step-up; revoked on password or factor change |
| `auth_reauth_window_min` | 10 | 1–60 | high-impact writes need a full authentication within this window (step-up; OIDC with `max_age`) |
| `auth_max_sessions_per_user` | 10 | 1–50 | oldest session ends first |
| `auth_login_rate` | 5 failures / 15 min per account, 20 per IP | | then exponential backoff; repeated failures raise an alert |
| `auth_oidc_backchannel_logout` | on | | the provider's logout ends our sessions |

- Server-side session rows (revocable, listed in F11); cookie `__Host-` prefix, Secure, HttpOnly, SameSite=Lax; the session id rotates at login and step-up.
- **High-impact writes** (step-up + confirm dialog + an alert entry): prompt overrides, project-scope "always continue" rules, unpause, window changes, run-now, decision confirmation, users/grants/roles, auth settings, host settings, backup/export download and restore.

### 9.6 Agent role and its limits (D-160)
- MCP and CLI writes from AFClaude's own sessions (the session is in `driven_sessions`, or the CLI runs with `CLAUDE_GUARD_DISABLE=1`) act as `agent:<session>`. Allowed: tasks and questions in its project, its own hand-offs, its project's docs, decision proposals. Everything else is refused, audited and alerted.
- v1 is **advisory**: while sessions run on the same host as the same UID with a shell, a session can bypass MCP (write the DB, edit prompt files). Detection = audit + alerts + the 8a/8c checks. Sessions stay on the host (D-164), so enforcement would need a separate UID for the DB and prompts; that stays a later option.
- The MCP server's instructions keep saying its tools are used only when the user explicitly asks for AFClaude; MCP tasks count like UI tasks, no approval step (D-068).

### 9.7 Hardening
CSRF: no state-changing GETs; writes need a same-origin `Origin`/`Sec-Fetch-Site`, an htmx/JSON header and the per-session CSRF token. Strict CSP (`default-src 'self'`, no inline script), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS, `Cache-Control: no-store` on the API. Jinja autoescaping; markdown rendered server-side through an allowlist sanitiser. Size limits (answer 8 KB, prompt 16 KB, title 200 chars), write rate limit (30/min per user). Errors never echo internals.

Container isolation: `afclaude-web` gets only the data volume and its port; no Claude credentials, no bridge key, no docker socket, no tmux. Only the runner's session executor reaches Claude sessions. A compromised web app can corrupt the DB (restore from backup) but can't run anything directly. Read-only root fs, non-root UID, no privileges in every container.

## 10. Deployment

### 10.1 Containers (D-163)
One image, one compose file usable by plain docker and by Coolify (docker-compose deploy):

| service | command | mounts | port |
|---|---|---|---|
| `afclaude-web` | `afclaude web` | `/data` | 8080 (internal; proxied) |
| `afclaude-runner` | `afclaude runner` | `/data`, the bridge key and host key (read-only) | none |
| `afclaude-mcp` | `afclaude mcp` (stdio via `docker exec`) | `/data` | none |

### 10.2 Minimal env
| env | required | purpose |
|---|---|---|
| `AFCLAUDE_PUBLIC_URL` | yes | external URL: OIDC redirect URI and the WebAuthn RP ID (passkeys are bound to this domain) |
| `AFCLAUDE_MASTER_KEY` | no | encrypts secrets in the DB; if unset, generated once into `/data/master.key` and wrapped into every backup |
| `AFCLAUDE_DATA` | no | data path, default `/data` |
| `AFCLAUDE_BRIDGE_HOST` | no | the host's address for the SSH bridge, default `host.docker.internal`; the bridge key and host key are mounted files, the bridge user is a setting |

Everything else (windows, budget, auth, users, OIDC client, intervals, retention, session settings) is a DB setting edited in the dashboard. `docker/.env` and `data/afclaude.json` shrink to this list.

### 10.3 Public vs local
In the repo: all code, templates, vendored assets, default prompts, compose files with `${VARS}`, `env.example`, this doc. Local or in the DB only: the DB and its backups, the master key, `.env`, the bridge key, the backlog, the decision store, identities. `tools/check_public.py` guards every commit and push.

### 10.4 AFClaude's own working root and Claude config (D-165)
- **First-startup wizard** (in `/setup`, after the owner registers; `afclaude setup` on the CLI until the dashboard exists): host check (§10.5) → pick the **AFClaude working root** on the host → Claude config and login → trust and permission setup → time zone. Or "restore from backup" instead (§11.3).
- The working root is a dedicated directory, **not the default/home directory** and not inside a tree of non-AFClaude projects. The wizard refuses `$HOME` and `/`, and warns when the path already holds transcripts of sessions AFClaude didn't start, or has an ancestor `CLAUDE.md`. Every AFClaude-run session, the managers included, runs with its cwd under it (`<root>/<project-slug>/…`; project repos are cloned there), so their transcripts (`<config>/projects/<encoded path>/`) are separable from everything else.
- **A separate `CLAUDE_CONFIG_DIR` for AFClaude's sessions?**

| | separate config dir, e.g. `<afclaude-home>/claude` (**recommended**) | shared default `~/.claude` |
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
- The current manager container (D-120) and the host bridge (D-121) keep running while phases 2–5 land; each runner change is deployed like today (autonomous deploys within the gates, D-106).
- The temp dashboard keeps working through the transition as side work (not a dashboard phase); its stall scan moves to the runner's own tick in phase 3a.
- Phase 5b switches to the three-service layout; the host crontab lines and supercronic are retired. Phase 5e moves this installation into its own working root (§11.5).

### 10.7 Retiring the temp dashboard (side task after 8d, not a phase)
About a week of overlap once the dashboard is live, then stop the export, remove the temp dashboard's container and route, and replace the `/private` page with a link to the dashboard.

### 10.8 Remote Control visibility (D-042, D-052)
Driven sessions run as individual RC sessions (`--remote-control --name <tmux name>`); F6 links them when the RC URL is known (source to verify in phase 6c). Threads hosted by the owner's `claude rc` server are never taken over (two writers on one transcript); they show "continue it from the app" with the skip reason. Hooking into rc server mode stays on the roadmap.

## 11. Backup, export, restore, move (D-161, D-165)

### 11.1 DB backup bundle
`backup.py`, a runner job, daily by default; also on demand from F10 (step-up):
1. Consistent snapshot without stopping: SQLite online backup (`VACUUM INTO`).
2. `manifest.json`: installation id and epoch, app version, schema version, created at, row counts per table, sha256 of every file.
3. The master key, wrapped with the backup passphrase.
4. Encrypted as a whole (passphrase → argon2id → authenticated encryption). Stored in `/data/backups` (retention setting), optionally copied off-host (a mounted directory first, S3/SFTP later).

### 11.2 Full export: DB + the required Claude data (D-165)
The dashboard's **Export** (F10, step-up) = the §11.1 bundle + a `claude/` part read from the host through the bridge + a generated reinstate guide. Every item is in the manifest with its checksum, so a restore can prove it is complete.

| item | source | note |
|---|---|---|
| transcripts of AFClaude-run sessions, incl. subagent files and per-session side files (e.g. file history) | `<config>/projects/<encoded working-root paths>/` | selected by `driven_sessions` and the working root; the exact file list is fixed in phase 5d against the real layout |
| credentials | `<config>/.credentials.json` | **off by default**; including it shows a warning: whoever has the bundle and passphrase can use the Claude account, and the old host must stop using it |
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
2. Deploy the containers with the same `AFCLAUDE_PUBLIC_URL` (passkeys are bound to the domain; a new domain means re-registering passkeys and updating the OIDC redirect URI).
3. `/setup` → "restore from backup" (setup code + passphrase), or `afclaude restore <bundle>`: decrypt, verify checksums and manifest, refuse a schema newer than the code, migrate forward if older, bump the installation epoch, record `restored_from`.
4. Choose the working root: the same path as before (recommended: transcripts are keyed by the encoded cwd) or a new one with a path map (§11.5).
5. The runner writes the `claude/` part to the host through a whitelisted restore command: config dir files, trust flags, MCP registration, hooks, `CLAUDE.md`.
6. Sign Claude in for that config dir if the credentials weren't included.
7. Clone the project repos into the root (the guide lists them with their remotes).
8. Post-restore validation: every manifest item present with its checksum, plus all host checks (§10.5) incl. the end-to-end session.
9. The install **starts paused** (*proposal*, setting `restore_starts_paused`, default on) with a banner "restored from <backup time>: check and resume". One tap resumes; managed projects `--resume` their session where the transcript is present, otherwise they start fresh from the latest hand-off (§5.5).

### 11.4 Move to another host
1. Old host: pause automation; managed sessions get the session-end save prompt (§5.5), so their hand-offs are current and their work is pushed; wait for running sessions to finish or save.
2. Take a final full export marked "move": the old installation becomes read-only and stays paused (a fence against two installs driving the same account and repos).
3. New host: §11.3.
- A restore test (export → fresh install → equal row counts and checksums → a dry-run dispatcher pass decides the same) runs in CI and as part of gate 8a.

### 11.5 One-off migration of this installation (D-165)
Today AFClaude's sessions share a root directory and the default `~/.claude` with sessions and projects AFClaude doesn't manage. Moving this installation into its own working root (and its own config dir, §10.4) is **the validation that the export is complete**:
- **Only the export's content set.** The migration runs the export code itself, with one addition: a selection filter that picks AFClaude's sessions and projects out of the shared root (from `driven_sessions`, `projects.path`, the manager session and the own-sessions list; the owner confirms the list). It carries nothing the export wouldn't carry.
- **Path map.** Restore takes `old prefix → new root` and rewrites the encoded project-dir names and the cwd in the session metadata. This is a normal restore feature, not a migration-only one. Whether `--resume` accepts a moved transcript is verified in phase 5e; if not, those projects continue from their hand-offs.
- **Nothing by hand.** Whatever turns out missing or wrong on the new side is fixed by correcting or expanding the export (code + manifest); then export and restore run again from scratch, until the post-restore validation, the end-to-end session and one real manager continue pass.
- Cut-over: the old setup is paused and fenced, the new one unpaused; the old transcripts stay in place, untouched.

## 12. Testing strategy
- **Unit (offline, temp DBs):** every `actions.py` action (validation, audit, version, idempotent replay, role matrix incl. agent); `schedule.py` (DST nights both ways, N × session_hours, link groups, the window-start key, nights until the reset); `prompts.py` (project > global > default, placeholders, bundles); importers (file → DB, row counts equal); hand-off lifecycle; the schema guard.
- **Auth:** factor rules; passkeys with recorded attestation fixtures (hardware BE=0 + MDS-verified passes alone; BE=1 needs a second factor; revoked AAGUID refused; bad signature refused); TOTP windows and replay; recovery codes single-use; lifetimes and step-up with a fake clock; OIDC against a stub provider (state/nonce/PKCE, group map add/remove, no e-mail linking); setup code.
- **API (TestClient):** route-table auth test, CSRF/Origin, 409s, idempotency, size limits, headers/CSP, no secret in any response.
- **Runner:** dry-runs with settings rows (moved window, pause, run-now skipping window and budget, the manager as a managed project, readable tmux names, wake socket, compaction sequence) with the stub-PATH fixtures; two processes hammering the DB (one winner, no lock errors).
- **Backup and export:** round trip and a move rehearsal on temp volumes; wrong passphrase and tampered bundle refused; the export's selection (only AFClaude's sessions, nothing else) and the path map on fixture config dirs; the manifest check catches a missing item.
- **Host checks:** each check against stub host commands (ok / warn / fail), a stale bridge version, a logged-out `claude`.
- **Containers:** compose up with only `AFCLAUDE_PUBLIC_URL` → setup page; health checks; read-only fs.
- **UI:** every page at 390 px in a headless browser (screenshots in the phase report).
- **Every commit:** `python3 tools/check_public.py --tree HEAD` and the hooks, never bypassed.

## 13. Phased build plan

Each phase is one subagent in a worktree with a clear definition of done (tests green, `check_public` clean). Runner phases keep every existing suite green and are deployed autonomously within the gates (D-106). No dashboard deploy before 8b, no internet route before 8d, nothing from the dashboard reaches Claude before 8d (D-072, D-157).

1. **Config + v4 schema + `actions.py`.** Done (01.10.).
2. **Backend foundation**
   - **2a Settings in the DB (D-146):** every tunable into `SETTINGS` (afclaude.json, dispatcher.json), one-time import, runners and pacing read the DB, schema guard (A1).
   - **2b `schedule.py` (D-148):** the only window code, pacing included; DST tests; `window_tz` plumbing.
   - **2c Telemetry into the DB (D-161):** usage tables + importers; sampler, `limit_ratio`, `pacing`, `usage_review`, `usage_report` read/write the DB; `account_id`.
   - **2d Runner state + docs into the DB (D-161):** state files, own sessions, scheduled jobs, logs → DB; `docs`/`doc_entries` with exporters.
3. **Runners**
   - **3a Resident runner (§5.1):** daemon with the job table, stall-scan tick, status snapshot, wake socket, all runner writes via `actions.py` (D-154), `run_log`/`driven_sessions` filled.
   - **3b Manager unification + run-now (D-147, D-153, D-036):** the manager as a managed project in the dispatcher, keep-alive continuation retired, readable tmux names, `continue_now`/`work_on_now`/`review_now` skipping window and budget.
   - **3c Compaction hand-offs (D-144, D-155):** `handoffs`, save prompt trigger, MCP tool, `/compact` + resume prompt.
4. **Content stores**
   - **4a `prompts.py` (D-111, D-152):** project scope (migrate step), bundles per session kind, every sender uses it.
   - **4b Questions and decisions in the DB (D-151, D-158):** `decisions` + links + FTS, proposals as questions, MCP tools, seeding, exports, prompts and CLAUDE.md rules switched to the DB.
5. **Users, packaging, backup**
   - **5a Users, roles, agent role (D-149, D-160):** users/grants schema, actor = user, the role matrix in `actions.py`, agent detection, alerts on violations.
   - **5b Containers + host checks (D-163, D-164):** one image, web/runner/mcp services, compose for docker and Coolify, minimal env, master key, MCP in a container, cut-over from the current container and host crontab; sessions stay on the host via the bridge; the host checks (§10.5) as a runner job + CLI, with the bridge whitelist extension.
   - **5c DB backup / restore / move (D-161):** bundle, restore, `restore_starts_paused`, move fence, CI round trip.
   - **5d Claude data export + working root (D-165):** first-startup working root and config dir (CLI; the UI follows in 6a), verify `CLAUDE_CONFIG_DIR` on the host (RC, `/usage`, `claude agents`, login), the `claude/` export part, whitelisted restore command, path map, the generated reinstate guide, post-restore validation.
   - **5e One-off migration of this installation (§11.5):** the export with the AFClaude selection filter out of the shared root, restored into the dedicated root; every gap fixed in the export and the run repeated from scratch, until validation, the end-to-end session and a real manager continue pass.
6. **Dashboard**
   - **6a Skeleton + local auth:** Starlette app, layout + top bar (D-074), sessions and lifetimes (§9.5), password + TOTP + recovery codes, setup/first registration and the first-startup wizard (§10.4), CSRF, headers, route-table and matrix tests, `health`.
   - **6b Passkeys + OIDC (D-150, D-159):** python-fido2 with MDS3, BE rules and warnings, Authlib OIDC with the group map, step-up re-auth, F11.
   - **6c Read views:** F1–F9, F12, F13, the threshold panel, RC URL source verified.
7. **Writes**
   - **7a Work writes:** answers, tasks, projects, priorities, moves, decisions and proposals, 409 handling.
   - **7b Control writes:** stalls and rules, window editor, prompt editor, pause, run-now with live feedback, Settings page with reset/undo (D-075), users/grants, backup and export UI, confirm + step-up for high-impact writes.
8. **Deploy gates, strictly in this order (D-072, D-145, D-157):**
   - **8a Thorough review (Opus 5.5, ultracode/max effort).** The whole dashboard and its integration: `actions.py`, `schedule.py`, `prompts.py`, the runner, auth (OIDC + groups, password + 2FA, passkeys + MDS), backup, export and restore, the host checks, containers. It also checks that **the docs and every owner decision agree with the actual code**, not only with comments and explainer files; every discrepancy goes to the owner to decide, none is fixed silently. It also **validates the users and roles** (D-168): every role's effective permissions match its description in §2, the group→role map, the first-owner setup, and the user records themselves. Findings fixed and re-reviewed before 8b.
   - **8b Deploy without the Claude connection (with the owner's approval).** The three services on a staging volume that no live runner reads, the session executor disabled; **no internet route** (tunnel or VPN only). Curl checks: no session → 401/login, wrong Origin → 403, health fresh.
   - **8c Live pentest with full code access** against the 8b deployment: auth bypass, setup-code race, password/TOTP brute force, passkey policy bypass (BE flag, attestation, MDS), OIDC flow and group-claim injection, session fixation and lifetimes, CSRF, IDOR across projects and roles, **role-boundary tests (D-168): for each role, a test user tries to reach what the role description says it should not (other projects, global settings, owner-only actions, user management, other users' sessions)**, agent-role escalation via MCP, injection, headers/CSP, backup and export download, the bridge whitelist (restore and check commands). Fixed, re-tested, 8a re-run on non-trivial fixes.
   - **8d Attach the Claude connection, seed the backlog, open the route (with the owner's approval).** Point the services at the live volume and enable the executor; seed `BACKLOG.md` locally (D-065); then add the public route behind the login.

The temp dashboard's phase strip (D-085) shows these phases incl. the sub-phases and 8a–8d.

**Status display (D-166):** the status views show when the next run will take place (date/time, kind, one-line reason, from `pacing.next_run()`), not the budget rule itself; the rule details belong to the threshold/settings view.

## 14. Future (after phase 9, not phases)
- **Shared account pool (D-156).** Several Claude accounts as one pool, never assigned to projects; the runner picks an account with headroom. Needs installations talking to each other. Hooks left open now: `accounts` table and `account_id` on telemetry and runner rows; an `AccountPool` interface with one single-account implementation; `installation` id and epoch; a peer API (signed, per-installation keys) to be designed later. Build on the existing open-source `claude-accounts` project (owner decision D-167): preferably contribute a small API upstream that AFClaude calls, so it stays current, instead of copying its code; opening that PR needs the owner's go.
- **Window recommendations (D-035).** The usage monitor recommends shifting windows when the owner is usually active during a window or usually idle elsewhere; it aggregates by weekday + hour only, shows the evidence (weeks per slot) and proposes a concrete F4 edit, applied as a normal `window.set`.
- **Experience store across users (D-137)** and the usage split (D-138): universal vs user-specific data stay separate, with format versions.
- **Hooking into `claude rc` server mode (D-052).**
