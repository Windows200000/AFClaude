# Phase 6c: read views (implementation plan)

Plan for design §13 step 6c, with the parts of §3 (the top bar, F1–F9, F12, F13), §3.1 (the UX rules, the reserve-threshold UI, stage ETAs), §4 (the `status_snapshot`, a web container that imports no model code, uPlot), §8 (the read endpoints, request polling), §9.8 (`SanitizedHtml`, CSP), §10.8 (RC links), §10.9 (owner previews) and §12 (contract and UI tests) that it builds: one typed read endpoint per view with its pydantic models and contract tests, the TS view over it, the shared read functions and the status snapshot that the temp dashboard and the dashboard both use, server-sanitised markdown, uPlot charts and request-state polling. Each step in §5 is one worker branch; nothing is built yet.

Binding inputs: D-070 (the dashboard is built from scratch, not from the quickview's page), D-074 (purple accent, vibrant status colours, rigid but sleek), D-075 (only the key settings on the main views), D-083 (needs-input first, never stalls or untitled sessions), D-084 / D-085 (documents and the phase strip surface), D-137 / D-266 (the session/weekly ratio and the usage split, three-way: runs, design chats, other use), D-141 / D-258 (the threshold UI's numbers, computed in code), D-148 (display time zone), D-149 / D-196 (only owners see global settings; viewers see their project's questions read-only), D-161 (everything in the DB), D-166 / D-200 / D-201 / D-224 (show when the next run happens, assuming no more usage, labelled "Holding until"; a stopped run shows the next window), D-176 (decision summaries name the topic only), D-193 / D-197 (prompts system-wide, project variables), D-208 / D-209 (Python API, TS frontend, mypy --strict and TS strict), D-211 (non-blocking previews after every UI step), D-213 (cumulative stage ETAs), D-230 / D-249 (simple numbers, compact times and durations, instant tooltips with a "?" toggle for verbose ones, collapsed minor tasks, the lineup, no filler text), D-235 / D-239 (fail fast; display paths show the error and never pause), D-237 / D-238 (the task-manager's context and re-read cost), D-256 (no audit row per runner write), D-263 / D-279 (owner questions in plain words; questions in full in the UI, never a link to a file), D-268 / D-269 (Remote Control per category, maybe unneeded later), D-007 (existing libraries first).

## 0. Already decided

- **One main read endpoint per view**, plus detail endpoints for drill-downs and pages of long lists. F1 has two: `inbox` (what 7a's answer write returns, §3 F1) and `overview`; the top bar has its own `topbar`, fetched by the app shell. Design §8's `messages` (7a, with F14 and D-246/D-250), `settings`, `users`, `backups` (7b) and `me` (6a W5) are not 6c.
- **Two read sources, nothing else** (§4). (a) DB reads in a new module `read_views.py` (mypy --strict; imports only `store`, `schedule`, `project_docs`, `decisions`, `prompts`, `failstop` and `inbox` read helpers). (b) Everything that needs model code or host state (`pacing`, `limit_ratio`, `stage_eta`, `keepalive`, `compact_timing`, `run_metrics`, `fillup`, `~/.claude.json`, transcripts, tmux, the process list) comes from the `status_snapshot`, written on the runner side. A test walks the imports of `dashboard/*` and `read_views.py` and fails on any model module.
- **The quickview's numbers move, they are not copied.** `export_quickview.py`'s blocks become calls into `read_views` and the snapshot builder, and its `status.json` stays byte-identical (a golden test over a fixture DB and a fixed clock). Until §10.7 retires the quickview, both pages show each number from the same function. D-070 forbids building the dashboard *from the quickview page*; sharing the computation in modules is not that.
- **The snapshot is 3a S9(b)'s `status_snapshot` item, built here** (R1): one row per section, written by the job that runs the quickview export (a runner job since 3a S3, so every 3 min; the design's "at most once a minute" once 3a S9 runs it each loop). S9(b) keeps only `run_log` and `driven_sessions`.
- **Errors (D-235, D-239(3)).** A DB read that fails answers 503 `unavailable` (6a's path). A snapshot section whose build raised is stored as an error section and shown as that error in its place: never the previous value, never a default. Every section carries its age; older than 3× its cadence it is marked stale, never shown as current. Display paths raise no alert and no pause of their own; a snapshot that stops being written is the runner heartbeat's alert (3a, D-257).
- **Section models are concrete pydantic models** (no generics, so the generated TS names stay readable), validated when the web reads them. A payload that fails validation is an error section (logged as a bug), not a 500 for the whole view.
- **Times are ISO 8601 UTC in the API** (§8). The client formats them in the user's display time zone (D-148): `today 23:00` / `tomorrow 04:00` / `Tue 23:00`, minutes as a superscript only when not :00 (D-230's addendum), durations `3w` / `3d` / `2h` / `<1h`, `~` only when estimated. These are pure TS functions with unit tests; the server never sends formatted strings.
- **No explanatory text in pages** (D-230). Labels are nouns or numbers. Explanations live in a TS tooltip registry with a short and a verbose text per element (D-249's "?" toggle). §3.1's "one line explains the rule" of the threshold UI moves into its tooltip, because D-230 is the later decision.
- **"Holding until"** (D-166/D-200/D-201/D-224): `pacing.next_run()`'s time assuming no more usage, with the forecast-based time as a smaller second line. It reads "Running" only while a run is going; after a run stops it shows the next window. While automation or the shown project is paused it reads "Paused" (the reason in the tooltip). The budget rule's details are only in the threshold panel.
- **Read access** (*proposal*; 8a checks it against §2):
  - Every list shows only projects the user holds a grant on (5a's `roles.visible_projects`). Viewers get the same cards without controls (D-196).
  - F4 (the window planner), F5 (status), F13 (host) and the threshold panel are owner-only (D-149: global settings, budget, host).
  - The top bar and F1's usage bars and "Holding until": question 2.
  - F12: owners see every row; everyone else sees the rows whose target resolves to a project they hold a grant on (5a's scope resolvers), never auth rows.
  - Stalled sessions outside any project: owners only.
- **Markdown is rendered on the server and then allowlist-sanitised** (§2), typed `SanitizedHtml` in the schema. That is a named string schema; codegen turns it into a branded TS type that only the API client produces, and the one raw-HTML sink renders it under a Trusted Types policy (§9.8). Rendered as markdown: questions (in full, D-279), docs, hand-off text, review reports. Shown as plain text: prompts, decision words.
- **uPlot** is the fifth runtime package §4 already lists. R2 adds it with an exact version. Charts get numbers only; axis and series labels are set as text (8c tests "uPlot labels").
- **F8 follows D-193/D-197:** default, override, the project's variable values and the prompt rendered for that project; no project override (4a P5 corrects §3 F8 and §8's `?project=` meaning).
- **The pink preview marker (D-211) stays a quickview feature.** The final phase strip has no preview marker.
- **F4's share is three-way (D-266):** runs and design chats (both AFClaude's) and other use. R12 updates §3 F4's "AFClaude vs user".
- **Repository documents** (D-084, which the task-manager applied to the future dashboard; *proposal*): the web container can't read repositories (§9.8: data volume only). So the 10-min docs import also mirrors each project's Markdown files, read-only, into `docs` (kind `file`, named by repo path, with the quickview's `all_docs` exclusions). F9 lists them under the project docs.
- **Paging:** lists take `limit` (capped per endpoint) and `before` (an id cursor); log docs page by entry, newest first. Each response is capped (256 KiB); a doc beyond it pages.

## 1. Today (as-is inventory)

| view | data today | quickview code | missing | needs first |
|---|---|---|---|---|
| API layer | `dashboard/` (6a W1/W2, merged): endpoint table, error shape, schema + TS codegen, a contract-validating test client | — | path and query parameters (every query parameter is refused today); sessions (W5), TS scaffold (W3), preview build (W4) | 6a W3–W5 |
| top bar | `failstop.pauses()`; `schedule` window; pacing's predicted week end; inbox count | `automation_block`, `window_info`, `next_run_block`, `needs_input` | runner dot: today `watcher_running` = `pgrep` on the host | 3a S2 (runner heartbeat) |
| F1 inbox | `OPEN_QUESTIONS.md` (a file); `tasks` kind `question` and blocked tasks (`store.pending_user_input`) | `needs_input`, `_split_open_questions` (D-279 continuation lines) | questions and decision proposals in the DB; blocked tasks only once forwarded | 4b Q3, 5a A4 |
| F1 overview | usage from the `~/.claude.json` cache (`ka.read_usage_cache`); `pacing.next_run` with `ka.run_active` / `pending_deferral` / `last_mile_next_slot`; `build_stages` over `store.list_tasks`; `stage_eta.gather` | `keepalive_and_usage`, `next_run_block`, `stages`, `eta_result` | the lineup (no function computes it), the D-230 important-task rule, "the current project" | R1, 3b S4 |
| F2 stalls | `sessions`, `limit_hits`, `session_decisions`, `standing_rules`; `store.stalled_sessions` / `effective_decision`; `stalled.scan` | none (D-083 keeps stalls off the quickview) | a first-prompt snippet; the RC-held reason (dispatcher state) | 3a S2 (`stall_scan` job) |
| F3 queue | `projects` (rank, priority), `tasks` (stage_seq, priority, estimated_sessions), `store.execution_order` | `stages()` (one project) | the flat order across projects with skip reasons; members | 3b S4, 5a |
| F4 window | `window_days` / `window_tz` / `session_hours` through `schedule.py`; the limit_ratio snapshot on the latest sample; `usage_session_windows`, `usage_weekly_series` | `window_info`, `limits_block` | 7-night preview with DST nights and the weekly reset; chart series | R1 |
| F5 status | `runner_jobs` / `runner_runs` (3a S1); `run_log` (empty: nothing writes it yet); keepalive lines in `app_log`; `usage_reviews`; `scheduled_jobs`, nightly tasks; `stage_eta_log`; `pacing.threshold_info`; `compact_timing.context_block`; `run_metrics`; the fill-up plan | `latest_decision`, `next_review_block`, `threshold_block`, `context_block`, `fill_block`, `fillup_block`, `cron_entries` | `run_log` rows; the review's structured results (§7.7: "not built … with F5"); the ETA review score as data | 3a S2–S6, S9(b); R6 |
| threshold panel | `pacing.threshold_info()` | `threshold_block` | — | R1 |
| F6 driven | `driven_sessions` (6 rows: kind, tmux, project; `rc_url` never written), `handoffs` (9 rows); tmux has-session on the host | `tmux_block` (task-manager only) | the RC URL's source; live state | 3b S2, 3c; R7 |
| F7 decisions | `decisions` (245 rows, D-242 mirror, FTS) | none (`DECISIONS.md` as a doc) | `project_id`, topic, links, proposals | 4b Q1–Q3 |
| F8 prompts | `prompt_defaults`, `prompt_overrides`, `prompt_vars`, `prompts.render` (4a P1) | copies of `prompts/*.md` | bundles per session kind | 4a P2 |
| F9 docs | `docs` / `doc_entries` (progress: 623 entries, ~124 KB; goals, alerts, exceptions, `review:*`); `build_stages` | `all_docs`, `doc_source`, `parse_goals` | `agent_proposals` / `agent_runs`; repository Markdown in the DB | 5a A4; R9 |
| F12 audit | `audit_log` (~2.3k rows; actors owner / cli / mcp / runner) | none | a project per row; `user:<id>` actors | 5a A1/A3 |
| F13 host | none (`host_checks` not built) | none | everything | 5b |
| polling | `action_requests` (`store.get_request`) | none | — | 3b S5 (rows) |

## 2. Library research (D-007)

| need | chosen | also checked |
|---|---|---|
| markdown → HTML on the server | markdown-it-py (CommonMark; raw HTML off with `html=False`; link schemes checked by `validateLink`; pure Python, maintained) | Python-Markdown (passes raw HTML through by default), mistune (looser CommonMark), commonmark.py (unmaintained) |
| HTML allowlist sanitiser | nh3 (bindings to the Rust ammonia sanitiser; maintained; wheels for arm64 and amd64) | bleach (deprecated since 2023), html-sanitizer (pulls lxml), an own allowlist over markdown-it's tokens (possible, but §9.8 wants the sanitiser as its own second line) |
| charts | uPlot (design §4) | — |
| request polling | own (`setTimeout` with backoff in the client) | server-sent events (§8: only if polling proves too slow) |

Both Python packages are new direct dependencies, pinned with hashes next to 6a W10's `requirements.txt` and covered by `pip-audit`. R2 first confirms maintained wheels for Python 3.12 on both architectures. They are web-side only (`dashboard/markdown.py`); the quickview keeps its client-side marked + DOMPurify.

## 3. Design details

### 3.1 Endpoints
| name | path | view | who (§0) | content |
|---|---|---|---|---|
| `topbar` | `/api/v1/topbar` | shell | any grant | automation state (install + visible project pauses), runner dot state + detail, tonight's window, predicted week end, inbox count, snapshot age |
| `inbox` | `/api/v1/inbox` | F1 | per project | questions, forwarded blocked tasks, decision proposals: id, project, kind, title, text (`SanitizedHtml`, in full), asked at, `version`, `can_answer` |
| `overview` | `/api/v1/overview` | F1 | any grant (usage parts: question 2) | session/week bars with reset times, Holding until, the current project's phase strip with ETAs, the lineup, each visible project's important tasks + the count of the rest |
| `queue` | `/api/v1/queue` | F3 | per project | ranked projects with priority, stages by priority with ETA, important tasks + count, the flat execution order with each skip reason, members (5a) |
| `project_tasks` | `/api/v1/projects/{project}/tasks` | F3 "+N more" | per project | every open task, paged |
| `task` | `/api/v1/tasks/{id}` | F3 detail | per project | the task, its events, `version` |
| `stalls` | `/api/v1/stalls?state=&own=` | F2 | per project; owners for the rest | stalled sessions: title or cwd + first prompt, project, kind, reset time, the effective decision and its source; the standing rules |
| `window` | `/api/v1/window` | F4 | owner | `window_days`, the next 7 windows (start, end, sessions, DST shift), the weekly reset, the measured ratios with their status, chart series |
| `usage` | `/api/v1/usage` | threshold panel (F5; F10 in 7b) | owner | `threshold_info`, the limits section, the next run with kind, reason and predicted end |
| `status` | `/api/v1/status` | F5 | owner | each runner job (state, last ok, next due), the latest budget decisions, next usage review, the latest review's results, ETA review score, the task-manager's context (D-237/D-238), fill and fill-up, each snapshot section's age, the proposal queue length and oldest (§7.9) |
| `runs` | `/api/v1/runs?component=&before=` | F5 | owner | a page of `run_log` |
| `reviews` | `/api/v1/reviews?before=` | F5 | owner | usage review records with their structured results; the report via `doc` |
| `driven` | `/api/v1/sessions/driven?ended=` | F6 | per project | tmux name, kind, project, state (live from the snapshot), RC link id, the latest hand-off's summary |
| `handoffs` | `/api/v1/handoffs?session=&before=` | F6 | per project | hand-offs, rendered |
| `decisions` | `/api/v1/decisions?project=&q=` | F7 | per project; install-wide: any grant | id, title, state, keywords, links |
| `decision` | `/api/v1/decisions/{id}` | F7 | as above | the full entry |
| `prompts` | `/api/v1/prompts` | F8 | any grant | names, the session kinds that use each, override state, "default changed since your edit" |
| `prompt` | `/api/v1/prompts/{name}?project=` | F8 | any grant; `project` per project | default, override, diff, variables with the project's values, the rendered prompt |
| `docs` | `/api/v1/docs?project=` | F9 | per project | the doc list (name, kind, updated), the phase strip, the lineup slice, the proposal queue summary |
| `doc` | `/api/v1/docs/{project}/{name}?before=` | F9 | per project | a body doc rendered, or a page of a log doc's entries |
| `proposals` | `/api/v1/proposals?project=&status=` | F9 | per project | agent proposals and run summaries (deciding: 7a) |
| `audit` | `/api/v1/audit?project=&before=` | F12 | §0 | actor, via, action, target, changed fields with a short before/after |
| `host` | `/api/v1/host` | F13 | owner | each check: status, last run, fix hint |
| `request` | `/api/v1/requests/{id}` | polling | the requester; viewers of the target project | the request's state and result |

### 3.2 Parameters (R0)
`Endpoint.params: type[Model] | None` is one model for the path and query parameters (`extra="forbid"`; strings coerced to int/bool only here). It is validated before the handler (422 with field paths), and a repeated key is refused. Codegen emits OpenAPI `parameters`, and the TS client function takes a typed params object. An endpoint without `params` still refuses every query parameter, as in 6a.

### 3.3 The snapshot (R1)
- Table `status_snapshot(section, account_id, computed_at, state ok|error, payload JSON, error, builder, version, PRIMARY KEY (section, account_id))`, added through `store.py`'s migrations. `builder` is the payload format's version.
- Sections: `usage`, `next_run`, `threshold`, `limits`, `eta` (per stage id + assumptions), `fill`, `fillup`, `context`, `next_review`, `runtime` (tmux alive per driven session; the watcher process until 3a S8), `charts` (bounded series: ≤ 1000 points each).
- `status_snapshot.py` (not on the strict list: display code over untyped model modules) builds every section once: `build(now) -> {section: (state, payload | error)}`. `write()` stores them through one registered `actions.py` action (`snapshot.put`, runner actors only, one transaction, no audit row, like D-256's runner rows). The quickview export calls `build` once, writes, then derives `status.json` from the same values. The once-a-day `stage_eta` log stays a side effect of building the `eta` section.
- Size cap: 64 KiB per section (over it = an error section).
- `dashboard/snapshot_models.py` holds one pydantic model per section. A contract test builds every section from fixtures and validates it.

### 3.4 Shared read functions (R1)
`read_views.py` returns plain typed dicts and dataclasses (no pydantic: `test_export_quickview` also runs on the host's Python 3.9). The handlers map them to the response models.

| function | moves from | used by |
|---|---|---|
| `automation(conn)` | `automation_block` | top bar, quickview |
| `phase_strip(conn, project, eta)` + `PHASE_RE` | `build_stages`, `_phase_status` | F1, F9, quickview |
| `important(tasks)` | new (D-230: blocked or needs input, in progress, or the project's top stage priority) | F1, F3, F9 |
| `lineup(conn, eta, limit=10)` | new (§3.5) | F1, F9 |
| `inbox_cards(conn, projects)` | `needs_input`'s DB part (the `OPEN_QUESTIONS.md` parse stays in the quickview until 4b Q3) | F1, quickview |
| `window_preview(conn, now, nights=7)` | `window_info` | F4, top bar, quickview |
| `runner_health(conn, now)` | new, over `runner_jobs` / `runner_runs` (replaces `cron_entries` after 3a) | F5, top bar |
| `snapshot(conn, now)` | new: rows + age + stale flag | every view that shows model numbers |
| `stalls`, `queue`, `driven`, `handoffs`, `decisions`, `docs`, `doc_page`, `audit`, `request` | new | their views |

The snapshot side gets `keepalive_and_usage`, `next_run_block`, `runner_state`, `threshold_block`, `limits_block`, `limits_from_snapshot`, `eta_result`, `eta_summary`, `fill_block`, `fillup_block`, `context_block`, `next_review_block`, `tmux_block` and `watcher_running`.

### 3.5 Lineup, current project, skip reasons
- **Order (D-064):** all high stages in project rank order, then medium, then low. This is one store function, shared with 3b S4's pick and `stage_eta.open_stages`. R1 checks they agree and makes them one function if they don't.
- **Current project:** the project of the run that is going (3b S4 puts it on the start's `run_log` line and the task-manager's `driven_sessions` row); otherwise the next pick.
- **Lineup:** the ordered open stages after the current project's current phase or stage: position, project, stage, cumulative ETA (D-213). At most 10 items; no text.
- **Skip reasons (F3's flat order):**
  - from the DB: install paused, project paused (`project_pauses`), no ready task, a higher priority tier still open;
  - from the host: at the limit, the owner holds the session; read from the last start's `run_log` line.

### 3.6 View details
- **F2:** `stalled.scan` stores a first-prompt snippet (`sessions.first_prompt`, additive, first 200 characters; §1's "short snippets"). The RC-held skip comes from the dispatcher's `handled` state and shows "continue it from the app" (§10.8).
- **F5 review results:** `python3 usage_review.py results set <review> --file F` lets the review session write its structured results through an audited action. They are stored as a telemetry record next to `usage_reviews`: universal vs user-specific findings as `{text, kind}` items, plus the backtest numbers (bias, sd, rmse, n). The review prompt asks for them; the report doc stays.
- **F6 RC link (§10.8):** see R7. The page stores and sends the session id only; the TS builds the link. A session launched without RC (D-269's setting off) shows none.
- **F12:** before/after shows the changed field names and values cut to 80 characters; secrets are never in audit rows (5a/6a's rule), and the endpoint drops any field named in a deny list as a second line.

### 3.7 Frontend
- One file per view under `dashboard/web/src/views/`. Shared components:
  - `Card`, `Chip` (D-074 status colours), `UsageBar` (the only bar, D-230), `HoldingUntil`, `PhaseStrip`, `Lineup`, `TaskList` (important tasks + "+N more");
  - `SectionError`, `Stale`, `SanitizedHtml`, `Chart` (the uPlot wrapper, collapsed by default per question 1);
  - 6a's `Tooltip`.
- `src/tips.ts`: one entry `{short, verbose}` per tooltip key. A test fails on a missing key, and lists every static text node of each view (render-to-string) for gate 8a's no-filler review.
- Refresh: a view fetches when it opens. The top bar and F1 re-fetch every 60 s while the page is visible. No client cache of truth (§4).
- `pollRequest(id)`: every 2 s, backing off to 10 s, until a terminal state. After 10 min it shows an error, never "pending" forever. 7a and 7b's writes use it.
- Preview (D-211): typed dummy fixtures per endpoint in `src/preview/fixtures/`, so a schema change breaks the build, not the preview. The task-manager redeploys the preview after each view step.

## 4. Order and dependencies

- **Before the TS views:** 6a W3 (scaffold) and W4 (preview build). The Python endpoints can be built after W2 (merged) with the test authenticator. Real filtering needs 6a W5 (the principal) and 5a A3 (`roles.visible_projects`). Endpoints declare their minimum role from the start, so W9's matrix covers them.
- **R0 first:** 6a code under `dashboard/` only; nothing live imports it.
- **R1 after the 3a merge:** S2–S6 edit `export_quickview.py` and `actions.py`. R1 edits `export_quickview.py`, `store.py` and `actions.py`, which every live process imports. So it is built in a worktree and merged alone, with no other step editing `store.py` or `actions.py` at the same time. Deploy: files only (the next export run uses them); the table is created on connect.
- **R2** after R0 and W3.
- **R3** after R1 and R2. The full inbox needs 4b Q3 and 5a A4; the lineup needs 3b S4. Before those, the cards show what the DB has (`kind='question'` tasks, blocked tasks), never a pointer to a file (D-279).
- **R4** after R1, 3b S4 and 5a A3 (members).
- **R5** after R2; its `stalled.py` change is live code (worktree).
- **R6** after R1 and R2. The run log needs 3a S9(b). The review results edit `usage_review.py` (live) and the review prompt.
- **R7:** the spike can run any time. The writer edits the SessionStart hook path in `keepalive.py` (live), so it comes after 3a S6's watcher refactor.
- **R8:** decisions on today's table after R2; topic, links and proposals after 4b Q1/Q3; the per-kind bundles after 4a P2.
- **R9** after R2; proposals after 5a A4; the mirror edits `project_docs.py`'s import (live).
- **R10:** the audit after 5a A3; host after 5b.
- **R11** with R3, or any time after R0.
- **R12** last.
- No dashboard deploy before 8b. Only R1, R5, R6, R7 and R9's runner-side parts reach the live system (and the quickview), each deployed like any runner change.

## 5. Steps

**R0: endpoint parameters and read conventions.**
- **Build:** `Endpoint.params` (§3.2); the cursor/limit convention; a `visible_projects` hook on `Call` (5a fills it; until then tests inject it); codegen for `parameters` and typed client arguments; the `SanitizedHtml` named string schema and its branded TS type.
- **Tests:** a bad, unknown or repeated parameter gives 422 with no input echo; path ints coerced, nothing else; the schema's `parameters` match the models; the staleness check; an endpoint without `params` still refuses a query.
- **Done when:** `test_dashboard`, `test_typing` and `check_public` are green. Deploy: none.
- **Risk:** query coercion weakening `strict=True`. It is limited to the params model, and a test passes `"1.5"` and `"true"` where an int is expected.

**R1: shared read functions and the status snapshot.**
- **Build:** §3.3 and §3.4: `read_views.py` (strict list), `status_snapshot.py`, the table and `snapshot.put`, `dashboard/snapshot_models.py`; `export_quickview.py` on both; the one ordering function (§3.5).
- **Tests:**
  - `status.json` byte-identical before and after (fixture DB, fixed clock, stubbed usage cache and transcript);
  - a section that raises is stored as an error and shown as one; size cap; stale after 3× cadence;
  - the import boundary of `dashboard/*` and `read_views.py`;
  - every section validates against its model;
  - `test_export_quickview`, `test_stage_eta`, `test_pacing`, `test_actions` green.
- **Done when:** suites green; one live export run writes every section (the task-manager checks the rows and the page).
- **Risk:** a slower export from writing the snapshot (one transaction of ~12 rows; measured in the report). A section that silently drifts from the quickview: the golden test covers every block.

**R2: rendering and frontend kit.**
- **Build:** `dashboard/markdown.py` (markdown-it-py with raw HTML off, links limited to http(s), mailto and relative; then nh3 with a fixed tag/attribute allowlist, `rel="noopener noreferrer"`); the `SanitizedHtml` component with its Trusted Types policy; the TS formatters (§0); the colour tokens per status; `tips.ts`; uPlot in `package.json` and the `Chart` wrapper (text labels only).
- **Tests:**
  - an XSS corpus (script, event handlers, `javascript:` and `data:` links, SVG, nested markdown/HTML tricks) → nothing executable survives;
  - the source check still finds no other raw-HTML sink;
  - formatter tables (DST days, :00 vs :30, every duration unit, `~`);
  - the chart renders from fixtures with zero CSP violations in a headless browser.
- **Done when:** as R0 plus the TS suite; `npm audit` and `pip-audit` clean.
- **Risk:** uPlot under Trusted Types, if it writes HTML strings anywhere (its legend is the likely place). The headless test checks it; if it trips, its legend is turned off and our wrapper draws the labels as text.

**R3: top bar and F1.**
- **Build:** `topbar`, `inbox`, `overview` (§3.1); the shell's top bar; the F1 view: inbox cards first (D-083), read-only until 7a; then the bars, Holding until, phase strip, lineup and per-project important tasks.
- **Tests:**
  - contract tests per endpoint and role (a viewer sees cards without `can_answer`; another project's cards are absent);
  - a question shows in full, multi-line (D-279);
  - Holding until's states (running, stopped → next window, paused, error, stale);
  - the important/collapsed rule;
  - the lineup order on a fixture with two projects and mixed priorities.
- **Done when:** the view at 390 px in a headless browser with zero CSP violations (screenshot in the report); preview fixtures added.
- **Risk:** F1 getting crowded on a phone. Everything below the cards is numbers and chips, and the per-project inputs (7a) take their space later.

**R4: F3 queue.**
- **Build:** `queue`, `project_tasks`, `task`; the view with two tabs (projects, flat order); ETAs with their assumptions one tap away (D-213).
- **Tests:** skip reasons per kind; "+N more" counts; paging; the ETA "?" when unknown.
- **Done when / Risk:** as R3. Host skip reasons can be old (the last start's): each shows its time.

**R5: F2 stalls.**
- **Build:** `stalls`; `sessions.first_prompt` in `stalled.scan`; the view (newest first, own sessions hidden by default, "decided" filter, RC-held note).
- **Tests:** `test_stalled` for the snippet (length cap, only the first user entry); the effective decision and source per rule kind; owner-only for sessions outside projects.
- **Done when:** as R3, and the live scan fills the snippet for new stalls.
- **Risk:** the snippet holding something private from the owner's own sessions. Those are owner-only, as the transcript itself is.

**R6: F4, F5 and the threshold panel.**
- **Build:** `window`, `usage`, `status`, `runs`, `reviews`; `usage_review.py results set` and the review prompt's instruction; the views; the two charts per question 1.
- **Tests:**
  - the 7-night preview over both DST nights;
  - `insufficient data` passed through, never a number;
  - the three-way split (D-266);
  - the threshold panel's fields equal `threshold_info()` on a fixture;
  - review results round trip;
  - `test_usage_review` green.
- **Done when:** as R3, and the next real review writes its results.
- **Risk:** the review session not writing results. A review without them shows "no results" as an error on the record (D-235), not an empty list.

**R7: F6 and the RC link.**
- **Build:**
  - (a) a spike: does a per-session `--remote-control` session expose its bridge session id, through `~/.claude/sessions/<pid>.json` (undocumented; preflight already reads it) or `CLAUDE_CODE_BRIDGE_SESSION_ID` in a SessionStart hook (documented for server threads)? Prefer the documented hook if it fires in per-session mode; result in `docs/rc_server_mode.md`.
  - (b) the existing SessionStart hook writes it through an audited `driven.rc_url` action.
  - (c) `driven` and `handoffs`, and the view.
- **Tests:** the hook path with a fake env; a session without RC shows none; hand-offs rendered and paged.
- **Done when:** a live task-manager start fills `rc_url`, and the link opens the session (the owner taps it once; non-blocking).
- **Risk:** the id source changing with a Claude Code update. The host checks (5b) gain a check that a fresh RC session reports one.

**R8: F7 and F8.**
- **Build:** `decisions`, `decision`, `prompts`, `prompt`; keyword and FTS filter; per-project rendering with variable values; the diff for "default changed since your edit".
- **Tests:** visibility (install-wide vs project); FTS and LIKE fallback; rendered prompt equals `prompts.render` for the project; unknown names give 404.
- **Done when:** as R3 for what 4a/4b have built; the rest of the view waits for them, with no stand-in data.

**R9: F9.**
- **Build:** `docs`, `doc`, `proposals`; the repository-Markdown mirror in the docs import (§0); the view (phase strip, lineup, docs, proposal queue read-only).
- **Tests:** log paging by entry; a body doc rendered once; the mirror's exclusions and size cap; proposals filtered by project and status.
- **Done when:** as R3, and the live import mirrors AFClaude's files.
- **Risk:** a large repository bloating the DB. Each file is capped at 512 KiB, and a project's files at 400 (over the cap = an alert, not a silent cut).

**R10: F12 and F13.**
- **Build:** `audit` and `host` with their views.
- **Tests:** per-role row filtering; the deny list; the before/after cut; each host check state, a never-run check shown as such.
- **Done when:** as R3 (F13 once 5b's `host_checks` exist).

**R11: request polling.** `request` and `pollRequest` (§3.7). **Tests:** terminal states stop polling; backoff; 10-min error; a request of another project → 404 (not 403, no existence leak). **Done when:** used by the fixtures' run-now card in the preview.

**R12: docs.** Design §3 (F4's three-way split, F8 without project overrides, the threshold line as a tooltip), §4 (snapshot as built), §8 (the endpoint list and `params`), §13 6c status; README file table (`read_views.py`, `status_snapshot.py`, `dashboard/markdown.py`); the mypy list; 3a plan S9(b) pointing here.

## 6. Questions for the owner

1. **Charts on the final dashboard.** The design plans two small charts. One shows, session by session, how much of the weekly limit a full 5-hour session uses. The other shows this week's usage so far, split into AFClaude and your own use, with the forecast up to the weekly reset. You asked for very simple numbers. Options: (a) the numbers stay the main content, and each chart sits folded under its number, opened with a tap; (b) no charts, numbers only; (c) the charts always visible. Recommendation: (a).
2. **What the people you add see of the usage.** The Claude account is yours. Managers, editors and viewers you add see their own projects. Should they also see the session and week usage bars and "Holding until" (when AFClaude next works)? Options: (a) yes, the bars and "Holding until" only, while the split between AFClaude and your own use, the window planner and the threshold details stay owner-only; (b) no usage or timing at all for them. Recommendation: (a); without it they can't tell when their project will be worked on.
