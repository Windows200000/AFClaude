# AFClaude dashboard: design

Status: design only, nothing built yet (goal 7). Written 29.09.2026 by an AFClaude subagent, for the manager to turn into task-store stages. Host names are placeholders: `<manager-host>` runs the AFClaude manager container, `<web-host>` serves the existing login-protected `<private-host>/private` pages.

## 1. Goals and non-goals

Goals
- One place, usable from a phone, for everything AFClaude needs from the user or wants to show: blocked tasks, stalled sessions, the queue, the automation window, usage and budget, prompts, the sessions AFClaude drives.
- Built from scratch with its own architecture. It is not an extension of the quickview (`export_quickview.py` + `quickview/`), which stays a read-only snapshot until the dashboard replaces it.
- Every write goes through the same validated, audited code path as the CLI (`tasks.py`) and the MCP server, so the three can never disagree.
- The dashboard never executes anything itself. It records intent in the DB, and the existing runners (dispatcher, keepalive, cron) act on it under their usual window, budget and preflight rules.
- Generic code goes in the public repo. Host names, identities, keys, the backlog and all runtime data stay local.

Non-goals (v1)
- Multiple users or roles. There is one owner.
- Replacing the MCP server as the main way tasks get added. The dashboard is the second interface: view, reorder, answer, decide.
- Showing transcripts. Titles, cwd, the stall notice and short snippets are enough, and the Claude app is the place to read a session.
- Driving Remote Control (RC) or speaking its protocol (ruled out: internal protocol, and against the terms). The dashboard links to sessions, it doesn't host them.
- Tracking more than one host, and a shared experience store across users (goal 10).
- A budget-rule editor. v1 shows the rule and its decisions. The thresholds move into settings so editing them later is a small change.

## 2. Users and main flows (phone first)

There is one user, the owner. They mostly open the dashboard on a phone, often right after a push or in the morning. So: one column, large tap targets, no hover-only controls, no drag-and-drop as the only way to reorder, every page useful without scrolling past the first screen.

Top bar on every page: automation state (running / paused, with a toggle), tonight's window ("22:00–08:00, 2 × 5 h"), weekly usage and the projected end-of-week value, and a badge with the inbox count.

- **F1 Inbox (home).** Shows ONLY real questions: blocked tasks (question plus a text box) and open manager questions (§4.6). Never stalls, never untitled sessions. Answering moves the task from blocked to pending, and the card disappears immediately (the server returns the new inbox). An empty inbox says so in one line.
- **F2 Stalled sessions.** Only sessions whose last entry is a limit notice (`sessions.stalled = 1`), newest first, own AFClaude sessions hidden by default. Each card shows the title (or the cwd plus the first prompt when the session has no title), project, stall kind, reset time and the effective decision with its source. Buttons: Continue, Ignore, and "Always…" (a session rule or a project rule; the project rule preselects the most specific cwd). An undecided stall stays until the user decides it; it never expires. Decided or resumed stalls leave the undecided list right away. A "decided" filter shows the rest.
- **F3 Queue and projects.** Ranked projects, each expanded to its stages with a priority chip (tap cycles high → medium → low). Up/down buttons move projects and stages, plus a "move to position…" field (optional drag on desktop). A per-project menu has "Set whole project to medium/low", edit, and manage/unmanage. A second tab shows the flat execution order (`store.execution_order`: high by project rank then stage, then medium, then low), marks what runs next, and says why an item is skipped (managed project, skip list).
- **F4 Window planner.** Pick a start time and a number of session windows (§4.2), once for the whole week or per weekday (§4.2.1). The page shows the session-limit length, the resulting window as a timeline over the next 7 nights, and the weekly-reset marker. It also shows the measured ratios from `limit_ratio.py`: weekly % per full session window, windows per week, windows left this week, and the AFClaude vs user share (each with "insufficient data" when that is the honest answer). A hint compares planned windows with the windows needed to use the full weekly limit. Save applies from the next dispatcher pass.
  - Editing flow: the week is shown as seven rows (Mon..Sun), each with its window or "off"; identical windows carry the same link colour. "Set for the whole week" writes one window to all seven days as one link group. Tapping a day's window opens the editor with a choice: **change all linked** (every day in that link group moves together) or **only this day** (the day leaves the group and becomes individual; a day edited to match another group's window can be re-linked with one tap). The preview shows which days change before saving.
- **F5 Status.** Keep-alive state, the latest budget decisions (one line each, with the reason text from `budget_decision`), dispatcher activity, the cron entries, the next usage review, and the review results split into universal and user-specific findings.
- **F6 Driven sessions.** Every session AFClaude started or continued: tmux name, kind (keep-alive / task / stall / review), state, a link that opens it in the Claude app when an RC URL is known, and the RC caveat (§7.4).
- **F7 Prompts.** Every `prompts/*.md` file with its placeholders and where it is used. Shows the default, any override, and a diff. Edit, validate, save, reset to default.
- **F8 Add task** (secondary): title, description, project, priority.
- **F9 Audit** (settings page): the last N writes with actor, source and a before/after summary.

## 3. Architecture options

**A. A small Python web app next to the DB (recommended).** A separate ASGI service (Starlette + Jinja2 + uvicorn, htmx for partial updates, all static assets vendored so no CDN is needed). It runs in its own container on `<manager-host>`, from the manager image with a different command. It imports `store.py` and a new shared service layer, `actions.py`. It serves the HTML and a JSON API from the same handlers.
- Pro: one process owns the view and the write rules. No sync step: writes are direct SQLite transactions on the same DB the dispatcher uses. No JS build chain. Server-rendered pages work well on phones and degrade without JS. Straightforward to test with Starlette's TestClient on temp DBs.
- Con: a new long-running service. The DB is only reachable on that host, so the UI can't be fully static.

**B. A static SPA on `<web-host>` plus a JSON API on `<manager-host>`.** A JS app under `/private` calls a thin API through the web host's proxy.
- Pro: the UI is covered by the existing `/private` login, and UI and API are cleanly split.
- Con: two deploy targets, and every UI change is a deploy on the web host, which is outside AFClaude's autonomy boundary and needs the user each time. It also needs a JS toolchain, and CORS/proxy details on every call. The quickview's client-side rendering already shows the drawbacks (CDN dependencies, markdown sanitising in the browser).

**C. Add an HTTP transport to the MCP server and build the UI on it.**
- Pro: one process for every client.
- Con: it exposes the MCP write path over the network. That write path can trigger autonomous execution, and the plan wants it to stay local stdio. MCP tool calls are the wrong shape for a UI (no conditional requests, no pagination). MCP clients on other machines would then need credentials. It also mixes a protocol server with a web app, and the SDK's HTTP server is not built for browser security (CSRF, cookies, CSP).

**Recommendation: A.** The one real advantage of B (reusing the existing login) is kept by putting A behind the same `/private` proxy (§6.2). All three write paths (CLI, MCP, dashboard) call `actions.py`, which validates, runs one `store` transaction, writes the audit row and handles idempotency. The MCP server stays stdio (`docker exec` into the manager container), unchanged for its clients.

```
phone ─https─> <web-host> /private/afclaude/  (existing login, adds key + user header)
                    │ https, key-gated, allowlisted source IP
                    v
<manager-host> Traefik ─> afclaude-dashboard (ASGI) ─┐
                                                     │ actions.py → store.py (SQLite WAL, data/afclaude.db)
host sessions ─stdio/docker exec─> mcp_server.py ────┤
tasks.py CLI ────────────────────────────────────────┤
cron: dispatcher / keepalive / sampler / review ─────┘ (read settings, consume action_requests)
```

Code layout: `dashboard/` (app.py, routes/, templates/, static/, docker-compose.yml), `actions.py`, `schedule.py` (window maths), `prompts.py` (loader), `config.py` (local config), `docs/`. The existing flat modules stay where they are.

## 4. Data model on top of schema v3 (v4)

The usual approach applies: `CREATE TABLE IF NOT EXISTS` plus the `COLUMNS` dict, with `SCHEMA_VERSION = 4`. No table rebuilds are needed. Values are validated in Python, not with CHECK constraints (the existing convention).

### 4.1 `settings`
`key TEXT PK, value TEXT (JSON), updated_at, updated_by`. Typed accessors live in `schedule.py` and `actions.py`, and code defaults apply when a key is missing, so an empty table reproduces today's behaviour exactly.
- `window.days` (per-weekday windows, §4.2.1), `window.tz` ("Europe/Berlin"), `window.session_hours` (5, the limit length; shown, overridable if it ever changes), `window.legacy_end` (today's 08:00, used only until the first GUI save, see §10 Q1)
- `budget.projection_threshold` (90), `budget.cutoff_after_window_h` (3: today's "11:00 after an 08:00 window end"), `budget.session_usage_stop` (85, from `dispatcher.json`)
- `automation.paused` (bool, checked by every runner in addition to the `PAUSED` file)

### 4.2 Window semantics (`schedule.py`)
- Window = [start, start + N × session_hours), per weekday (§4.2.1). The start is a wall-clock time in `window.tz`, and the length is in absolute hours, so a DST night still holds exactly N full session windows (it may end an hour earlier or later on the wall clock; the preview shows this).
- The grid is anchored at the chosen start, as decided earlier: session k runs from start + k × session_hours, so every session window inside the automation window is a full one, and the window ends on a session-limit boundary. The start picker moves in 30-min steps. It also offers "snap to the usual reset": from `data/samples.jsonl` it shows when a user-started session window was typically still running at the chosen start and when it reset (the automation window's first session can only begin after that). Picking that time aligns the grid with real limits.
- `in_window`, `current_window_end`, `next_window_start` and the budget cutoff (window end + cutoff hours) move from constants in `keepalive.py` to `schedule.py`. `keepalive.py`, `dispatcher.py`, `export_quickview.py` and `usage_review.py` read the settings once per pass or loop iteration.
- The fixed window-start cron (`0 22,23 * * *` UTC) is replaced by a window-start tick in the dispatcher pass: the first pass at or after the window start fires the keep-alive's window-start continue once per window (dedup key = window start date).

### 4.2.1 Per-weekday windows and link groups
- `window.days` = `{"mon": {"start": "23:00", "n": 2, "group": "g1"} | null, …, "sun": …}`. A window belongs to the weekday on which it **starts** (Mon 23:00–09:00 runs into Tuesday). `null` = no automation window that night.
- `group` is a link-group id. Days in one group have identical windows by construction: a "change all linked" edit rewrites every day in the group in one transaction; an "only this day" edit gives that day a fresh group id. Setting "the whole week" writes all seven days with one new group. Two days with identical windows but different groups stay separate until the user re-links them.
- Validation: start on the 30-min grid, n ≥ 1, and no overlap between consecutive days' windows (e.g. Mon 23:00 × 2 ends Tue 09:00, so Tue can't start before 09:00); a rejected edit names the conflicting day.
- Default (empty table): all seven days `{"start": "23:00", "n": 2, "group": "weekly"}` in Europe/Berlin (owner decision, 29.09.2026).
- The `settings/window` write (§5) takes `{day, start, n, mode: linked | individual | week}` and the settings version.

### 4.3 Prompt overrides
- `prompt_overrides(name PK, body, base_sha256, updated_at, updated_by)`. `name` is the file name under `prompts/`, and `base_sha256` is the hash of the default file when the edit was made. If the default changes later (a repo update), the dashboard flags "default changed since your edit" and shows a three-way view.
- `prompts.load(name)` returns the override if there is one, otherwise the file. Every sender uses it (keepalive, dispatcher, usage_review, usage_sampler, notify). Saving validates the placeholder set (the same `{placeholders}` as the default, doubled literal braces) and test-renders the prompt with dummy values. Old bodies stay in the audit log.

### 4.4 `audit_log` (append-only, triggers like `task_events`)
`id, ts, actor (owner | mcp:<session> | cli | dispatcher | keepalive), via (dashboard | mcp | cli | runner), action, target_type, target_id, before JSON, after JSON, request_id`. `task_events` stays the per-task history. The audit log covers everything else as well (rules, decisions, settings, prompts, projects).

### 4.5 Concurrency and idempotency
- A `version INTEGER NOT NULL DEFAULT 0` column on `projects`, `tasks`, `standing_rules` and `session_decisions`, bumped on every update. Writes carry the version the user saw, and a mismatch returns 409 with the current row.
- `idempotency_keys(key PK, actor, action, response JSON, created_at)`, pruned after 7 days. A replayed key returns the stored response without acting again.

### 4.6 Questions, runner decisions, driven sessions, requests
- **Manager questions into the store.** A new MCP tool `afclaude_ask(question, project)` creates a task with `kind='question'` that is born blocked. Answering it shows the answer to the asking session on its next continue (via the `{context}` of `continue_foreign.md`, or the managed project's continue). `OPEN_QUESTIONS.md` becomes a generated local export, so the inbox has a single source.
- **`run_log(id, ts, component, session_id, task_id, decision, reason)`.** keepalive and the dispatcher write one row per decision (FIRE, HOLD, WAIT_WINDOW, skip-RC-thread, start, cleanup) in addition to their log files, so the Status page doesn't parse logs.
- **`driven_sessions(session_id PK, tmux, kind, task_id, started_at, last_seen, ended_at, holder_kind, rc_url)`.** Replaces the session list inside `dispatcher_state.json` and `data/own_sessions.txt` (both kept as exports until nothing reads them).
- **`action_requests(id, ts, actor, kind, target, status, result, handled_at)`.** The dashboard's only way to ask for execution. Kinds in v1: `continue_now` (the dispatcher's `--now` for one approved stall; the budget rule still applies) and `review_now`. The dispatcher consumes them on its next pass under its flock and records the outcome.
- **Usage reviews.** `usage_review.md` also asks the review run to write `data/usage_reviews/<date>.json` (`universal[]`, `user_specific[]`, `commits[]`). The dashboard lists those files and tracks read/unread in `settings`.

## 5. API surface

All under `/api/v1`, JSON. The HTML pages call the same handlers (htmx gets HTML fragments, other clients get JSON). Times are ISO UTC with a local rendering. Reads are GET; writes are POST with an `Idempotency-Key` header (a UUID per user action, made in the page) and `version` in the body where one exists.

Reads: `overview` (top bar + inbox count), `inbox`, `projects` (with stages), `queue` (execution order + skip reasons), `tasks/{id}` (+ events), `stalls?state=undecided|decided|all&own=0|1`, `rules`, `window` (settings + next 7 windows + ratio snapshot + weekly reset), `usage` (cache values, projection, `limit_ratio` snapshot, AFClaude vs user share), `runs?component=&limit=` (run_log), `sessions/driven`, `reviews`, `prompts`, `prompts/{name}` (default, override, diff, placeholders), `audit?limit=`, `health` (DB reachable, last scan age, last dispatcher pass age, no secrets).

Writes, each a single `actions.py` call and a single transaction:

| endpoint | effect | idempotency / conflict |
|---|---|---|
| `projects` POST, `projects/{id}` PATCH | create / edit (name, description, path, manager_session) | version |
| `projects/{id}/move` | new rank (store keeps ranks contiguous) | no-op when unchanged |
| `projects/{id}/priority` | every open stage → level (`set_project_priority`) | naturally idempotent |
| `tasks` POST, `tasks/{id}` PATCH | create / edit title, description, project | version |
| `tasks/{id}/priority`, `tasks/{id}/move` | stage priority / stage position | idempotent / no-op when unchanged |
| `tasks/{id}/answer` | blocked → pending with the answer | 409 unless blocked; same answer replayed = 200 |
| `tasks/{id}/cancel`, `/reopen` | lifecycle | 409 on a wrong state |
| `stalls/{session}/decision` | continue / ignore / clear, with the `stall_ref` shown | 409 if the session has stalled again since (the user decided an older stall) |
| `rules` POST, `rules/{id}` DELETE | standing rule per session / project | unique (scope, match) → returns the existing rule |
| `settings/window` | day, start, N, mode (linked / individual / week; validated, snapped, overlap-checked) | version = settings.updated_at |
| `settings/automation` | pause / resume | idempotent |
| `prompts/{name}` PUT, DELETE | save override / reset to default | base_sha256 + updated_at |
| `requests` POST | queue `continue_now` / `review_now` | one open request per (kind, target) |

Concurrency with the runners:
- SQLite WAL with `BEGIN IMMEDIATE` and a 30 s busy timeout already serialises writers across processes. Dashboard transactions are short (no I/O inside), so they don't stall the dispatcher.
- The dispatcher reads the execution order and the settings once per pass. A reorder, a priority change or a window change applies from the next pass (at most 10 min later). `start_task` claims only pending tasks atomically. So "answer" and "start" can't both win, and "cancel" during a start returns 409 to whichever side comes second.
- Shrinking or moving the window never kills running sessions. It only stops new starts. Pausing does the same, and the page says so.
- The keepalive watcher is long-running, so it must re-read settings on each loop (a code change in phase 2).
- The dashboard never calls `/usage`, `claude`, tmux or the host bridge. Usage comes from the cache and sample files the sampler keeps fresh.

## 6. Security model

### 6.1 Threat model
The dashboard and the MCP write path can trigger autonomous execution: sessions running in auto mode with the guard hooks bypassed. So a write here is close to "run code on the host":
- an answer or a new task becomes the input of an autonomous session;
- a prompt override changes the instructions of every future autonomous session (the highest-impact write);
- a "continue" decision or an "always continue" project rule spends budget and resumes sessions;
- a window change or a pause changes when all of this happens.

Attackers to design against: anyone on the internet who reaches the route; a CSRF page in the owner's browser; a leaked proxy key; a compromised web host; and an autonomous session (prompt-injected by content it read) that writes back through MCP.

### 6.2 AuthN / authZ
- **Edge.** The dashboard is reached only through `<private-host>/private/afclaude/` on `<web-host>`, behind the existing login (Authelia). That host's nginx proxies to `<manager-host>` and adds, server-side, the `X-AFClaude-Key` header (the quickview pattern: the key never reaches the browser) and the authenticated user header. This reuses a proven path and adds no new login system.
- **Manager host.** The Traefik route matches only its path prefix and has an IP allowlist middleware for `<web-host>` (a local config value). The app has no published port.
- **App.** Every route except `health` requires (1) a key comparison in constant time against `~/.config/afclaude/dashboard.key` and (2) the forwarded user equal to the configured owner. There is deny-by-default middleware, and a test enumerates every route and asserts a 401 without credentials (§8).
- **CSRF.** Writes need POST/PUT/PATCH/DELETE with a JSON or htmx header, a same-origin `Origin`/`Sec-Fetch-Site` check, and a per-session CSRF token. There are no state-changing GETs.
- **High-impact writes** (prompt override, project-scope "always continue", un-pausing, window change) need an explicit confirm step in the UI. Each also appends a line to `ALERTS.md` and PROGRESS, so a change the owner didn't make gets noticed. An optional later step: require a fresh 2FA (Authelia's re-auth) on those paths.
- **Autonomous writers.** Tasks created through MCP by AFClaude's own autonomous sessions (the actor is in `own_sessions`/`driven_sessions`) land as `pending_approval` and are shown in the inbox. The exception is stages inside the managed project that session manages (see Q3). Autonomous sessions can never create rules, decisions or prompt overrides through MCP (tool-level check on the actor).

### 6.3 Hardening
Strict CSP (`default-src 'self'`, no inline script, vendored htmx), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on the API. Jinja autoescaping everywhere. Markdown (prompts, reviews) is rendered server-side with an allowlist sanitiser. Size limits (answer 8 KB, prompt 16 KB, title 200 chars). A write rate limit (30/min). Errors never echo internals.

The container runs as the same UID with only these mounts: code read-only, `data/` read-write, the key file read-only, `~/.claude.json` read-only (usage cache). It gets no `~/.claude` credentials, no host-bridge SSH key, no docker socket and no tmux socket. A compromised app can therefore corrupt the DB (restorable from the `.bak` and WAL copies), but it can't run anything directly. Key rotation works like the quickview's.

## 7. Deployment and coexistence

### 7.1 Hosting evaluation
- **In the manager container itself:** simplest, but the web process would share the container that holds the claude login, tmux and the host bridge key. A crash or restart would then also affect cron and tmux. Rejected.
- **A sidecar container on `<manager-host>` (recommended):** same image, own container `afclaude-dashboard`, same Docker network as Traefik, mounts as in §6.3. It shares the SQLite file with the manager container on the same filesystem (WAL across containers is fine on one kernel; never over a network filesystem). It can restart independently.
- **On `<web-host>`:** it would need a copy of the DB or remote writes, and every UI change would be a deploy there. Rejected. That host only keeps its one proxy location (a one-time change by the user).

### 7.2 Coexistence
- **Cron / supercronic** (sampler, dispatcher, keepalive watchdog, review, stall scan) is unchanged, except that the window-start tick moves into the dispatcher (§4.2) and all runners read `settings`. The dashboard is not in the cron.
- **MCP server:** stays stdio via `docker exec`. It switches to `actions.py` (audit, idempotency, the autonomous-writer rules), keeps its 9 tools and adds `afclaude_ask`.
- **Quickview:** keep it until the dashboard's read views reach parity (phase 6), run both for about a week, then retire it: stop its cron, remove its container and Traefik route, and replace the `/private` page with the dashboard location. `export_quickview.py` stays only as the generator of the `OPEN_QUESTIONS.md` export, or is deleted.
- **The container cut-over** (goal "container as manager") comes first. The dashboard is deployed only against the containerised manager, never against the host crontab.

### 7.3 Public vs local
In the repo: all code, templates, the vendored static assets, `config.example.toml`, the compose file with `${VARS}`, the default prompts, and this doc.

Local only (gitignored, and blocked by `tools/check_public.py`):
- `~/.config/afclaude/config.toml`: host names, the owner identity, repo root, manager session id, allowlisted proxy IP. This also absorbs today's hardcoded paths and the session UUID in `export_quickview.py`/`usage_report.py`.
- the key files and `dashboard/.env`
- `data/`: the DB with prompt overrides, settings, the backlog projects, reviews and `user_model.json`
- `BACKLOG.md`

The backlog is seeded into the DB by a generic `tools/seed_backlog.py` that reads the local `BACKLOG.md`: backlog projects ranked after AFClaude, their stages low priority and kind `backlog_project`. It runs once, in phase 8d, after the 8c pentest is clean.

### 7.4 Remote Control visibility
Sessions AFClaude drives already run as individual RC sessions (`ka_resume.sh` launches with `--remote-control --name`, and a resume keeps the RC link). The dashboard lists them in F6 with their RC link when one can be found (`claude agents --json` or the session registry, read by the sampler and stored in `driven_sessions.rc_url`; to verify in phase 5), and the tmux name otherwise.

Threads hosted by the user's `claude rc` server are never taken over: that forks the thread (two writers on one transcript, see README "Key findings"). The dashboard shows such a stall as "RC server thread: continue it from the app", with the dispatcher's skip reason from `run_log`, and the Continue button explains that the dispatcher will keep skipping it. Goal 11 (hooking into rc server mode) can lift this later without changing the UI.

## 8. Testing strategy
- **Unit (offline, temp DBs, like the existing suites):** `actions.py` (every write: validation, audit row, version conflict, idempotent replay, autonomous-writer rules); `schedule.py` (snapping, N × session_hours, DST nights in both directions, the budget cutoff relative to the window end, the legacy window, the window-start tick dedup); `prompts.py` (override precedence, placeholder validation, test render, default-changed detection); the v3 → v4 upgrade in place.
- **API (Starlette TestClient):** a route-table test that every route except `health` returns 401 without the key or with the wrong user, and 403 for a wrong `Origin` or a missing CSRF token; 409 paths; replayed idempotency keys; size limits; security headers and CSP present; no secret or key ever in a response or template.
- **Concurrency:** two processes hammering `answer`/`start_task`/`move` on one DB (exactly one winner, no `database is locked` beyond the timeout), and dashboard writes during a dispatcher dry-run pass.
- **Runner integration:** dispatcher and keepalive dry-runs with settings rows (a moved window, pause, `continue_now` request) using the existing stub-PATH fixtures in `test_dispatcher.py`/`test_keepalive.py`.
- **UI:** a smoke test of every page at 390 px width in a headless browser (screenshots attached to the phase report). htmx flows are checked with the same TestClient (fragment responses).
- **Deploy checks (curl, after each deploy):** direct access to `<manager-host>` without the key → 401, from a non-allowlisted IP → 403, through `/private` without login → login page, with login → 200; `health` fresh.
- **Every commit:** `python3 tools/check_public.py --tree HEAD` and the pre-commit/pre-push guard, never bypassed.

## 9. Phased build plan

Each phase is one subagent in a worktree with a clear definition of done (tests green, `check_public` clean). Phases 1–3 change runner code and must keep every existing suite green. Nothing is deployed before phase 8b, and nothing deployed can reach Claude before phase 8d.

1. **Config + v4 schema + `actions.py`.** `config.py` + `config.example.toml`; move hardcoded paths and session ids; v4 tables and columns (§4); `actions.py` with audit and idempotency; `tasks.py` and `mcp_server.py` routed through it; the autonomous-writer rules; `afclaude_ask` + `kind='question'`. Tests.
2. **`schedule.py` + settings in the runners.** Window and budget cutoff from settings; keepalive re-reads settings per loop; the dispatcher window-start tick replaces the fixed cron line; `automation.paused`; `run_log` writes; `action_requests` consumption (`continue_now`, `review_now`). DST and legacy-window tests.
3. **`prompts.py`.** Loader with overrides, placeholder validation and test render, used by every sender; the `usage_review.md` structured-output addition; `driven_sessions` filled by the dispatcher and keepalive (the state files are kept as exports).
4. **Dashboard skeleton.** Starlette app, auth middleware (key, user, CSRF), security headers, layout + top bar, `health`, vendored htmx, compose file and container command (not started), the route-table auth test.
5. **Read views.** Inbox, stalls (with filters and the RC caveat), projects and queue, status (keep-alive, budget decisions, cron, reviews), window planner (read-only preview + ratios), driven sessions (verify the RC URL source), prompts (read + diff), audit.
6. **Task and project writes.** Answer (the card disappears), add/edit/cancel/reopen, stage priority chip, project bulk priority, move up/down/position, manage/unmanage; 409 handling in the UI. Parity with the quickview is reached here.
7. **Stall, rule, window, prompt and automation writes.** Decide with `stall_ref`, session and project rules, window editor with snapping and a live preview, prompt editor (validate, save, reset, default-changed view), pause/resume, the `continue_now` button, confirm steps and the ALERTS lines for high-impact writes.
8. **Deploy gates, strictly in this order:**
   - **8a. Thorough review (Opus 5.5, ultracode/max effort).** The whole dashboard plus its integration (actions.py, schedule.py, prompts.py, the runner changes, auth incl. OIDC and local login, deploy files). Findings fixed and re-reviewed before 8b.
   - **8b. Deploy without the Claude connection (with the user's approval).** Sidecar on `<manager-host>` behind Traefik with the IP allowlist; the user adds the `/private/afclaude/` location on `<web-host>`; curl checks from §8. The app runs against a separate staging DB that no runner reads, so no write can trigger, continue or decide a session; the container has no route to `claude`, tmux or the host bridge anyway (§6.3).
   - **8c. Live pentest with full code access.** An agent with the source tests the running 8b deployment (auth bypass, OIDC flow and local-login brute force, CSRF, session handling, injection, IDOR, header/CSP checks, the proxy path). Findings fixed, re-tested, and 8a re-run on the fixes if they are non-trivial.
   - **8d. Attach the Claude connection + seed the backlog (with the user's approval).** Point the app at the live DB, so its writes (decisions, `continue_now`, windows) reach the runners; the manager container must already be cut over. Then run `tools/seed_backlog.py` locally with the owner's backlog.
9. **Retire the quickview.** About a week of overlap, then retire the quickview (§7.2); `OPEN_QUESTIONS.md` becomes an export; the README and PROGRESS updated.

Future (not a phase): **window recommendations.** The usage monitor recommends shifting windows when the user is usually active during an automation window, or usually inactive at some other time of the week. It aggregates activity only by **weekday + hour of day** (never the date or the day of the month), shows the evidence (weeks observed per slot), and proposes a concrete edit in the F4 terms (which day or link group, new start/N); applying it is a normal window write.

Preliminary status page: until phase 9, the quickview gets a small **phase visualiser** (phases 1–9 incl. 8a–8d as a strip, each coloured by its task-store state).

## 10. Open questions for the user
1. **Default window after the switch.** Today's 00:00–08:00 is 8 h, which is not a multiple of the 5 h session limit. Which should the default be: 22:00–08:00 (2 windows, same end), 00:00–10:00 (2 windows, same start), or 00:00–05:00 (1 window)? And does the "grid anchored at the start you pick" rule still hold, or should the start snap to your observed session resets (the planner can offer both)?
2. **Access path.** Reuse the `/private` login on the web host with a server-side key proxy (recommended; needs one nginx location change there, done by you), or put the dashboard directly behind the login on the manager host's Traefik (forward-auth), so the web host isn't involved?
3. **Tasks written by autonomous sessions.** Should tasks that AFClaude's own sessions add through MCP wait for your approval in the inbox (recommended; stages inside a session's own managed project excepted), or run like the tasks you add?

## Decisions by the owner (29.09.2026)

- **Q1 window:** the default automation window is **23:00–09:00 Europe/Berlin** (10 h = 2 session windows of 5 h). The weekly-reset cutoff of the budget rule stays "no later than 11:00 after the window".
- **Q2 login:** the dashboard implements **both** a general SSO (standard OpenID Connect, any provider, e.g. the existing Authelia) **and** a simple local username/password login (hashed passwords, rate limiting, secure session cookies). Either can be enabled via config. The reverse-proxy pattern stays as defence in depth.
- **Q3 MCP tasks:** tasks added through MCP are treated exactly like tasks created in the UI; **no approval step**. The MCP server's instructions and tool descriptions must say that the tools are only to be used when the user explicitly asks for AFClaude.

## Decisions by the owner (30.09.2026)

- **Windows:** settable once for the whole week **and** individually per weekday. When editing, the user chooses to change all identical (linked) windows together or only one day (§4.2.1, F4). Default stays one weekly window 23:00–09:00 Europe/Berlin.
- **Future: window recommendations** by the usage monitor, from weekday + hour only (§9, "Future").
- **Architecture option A** (a small Python web app) is confirmed; auth = generic OIDC SSO with any provider alongside local username/password (as in Q2).
- **Deploy gates:** 8a thorough review (Opus 5.5, ultracode effort) → 8b deploy without the Claude connection → 8c live pentest with full code access → 8d attach the Claude connection + seed the backlog (§9).
- **Quickview phase visualiser** on the preliminary status page (§9).

## Visual design (owner, 30.09.2026)

The owner likes the preliminary quickview's look and wants it kept for the main app:
- **Purple as the main accent**, and **vibrant colours that directly represent status** (done / in progress / pending / blocked / alert), used consistently everywhere a status appears.
- The feel: **rigid but sleek**. A strict grid, clear boxes and chips, compact and dense, no decorative fluff.
- Baseline: take the colour tokens, dark/light theming, typography and spacing from `quickview/AFClaude.html` as the starting design system; phone first.
