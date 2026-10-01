# Alternatives research (before building more)

Written 01.10.2026 by an AFClaude subagent, per the project rule: check for viable
alternatives before building. Covers (1) tools that already do parts of AFClaude,
(2) whether an existing self-hosted PM tool could replace the planned dashboard
(`docs/dashboard_design.md`), (3) building blocks that would shrink the dashboard
build. Primary sources where possible; community project maturity read from the
repo page itself (stars/forks/commits/dates), not secondary blog posts.

## 1. Auto-resume after usage limits

AFClaude's `keepalive.py` already does more than any of these: a nightly window,
a weekly budget-projection rule, RC-thread-safety (never take over a `claude rc`
thread), project-manager-mode prompts, and dry-run-by-default. The community
projects below only do the first, simplest piece ("detect the limit notice,
resume at reset"):

| project | covers | lacks | license/maintenance |
|---|---|---|---|
| `terryso/claude-auto-resume` | shell script, detects limit, resumes at reset | no budget rule, no window, no RC-safety | small, unclear activity |
| `cheapestinference/claude-auto-retry` (npm `claude-auto-retry`) | tmux-based, waits for printed reset, backoff for 529/5xx too | same gaps; also retries transient errors (AFClaude treats these separately) | published npm pkg, 2026 |
| `henryaj/autoclaude` | TUI, watches tmux panes, sends "continue" on reset | no window/budget concept | small |
| `PHSM22/claude-code-auto-resume` | launchd/systemd timer, headless resume | marked "(draft)"; no window/budget | draft status |
| Anthropic Desktop "Auto-continue when limits reset" (Aug 2026) | official, built into the Desktop app | Desktop only (not CLI/headless), no window/budget rule, no task queue; an open CLI feature request (`anthropics/claude-code#35744`) is still unresolved | official but partial |

**Recommendation: BUILD (keep `keepalive.py` as is).** None of these cover the
window + budget rule + RC-safety + project-manager-mode combination, which is
the actual hard part. Worth a watch-item: if Anthropic ships native CLI
auto-continue (issue #35744), re-check whether it can replace the resume
mechanics under `keepalive.py` while AFClaude keeps the window/budget layer on
top — no action now. No impact on dashboard phases.

## 2. Usage monitors / forecasting

`ccusage` (and its forks `ccusage-monitor`, `cc-monitor-rs`, `cc-budget`) read
`/usage`-equivalent data and show burn rate / predictions in a terminal or
statusline. `usage_sampler.py` + `limit_ratio.py` already do this plus the
AFClaude-vs-user token-delta split and the weekly-cycle bookkeeping these
generic tools don't attempt (they don't know which session is "AFClaude's").
`mikhin/claude-quota-budget` is a hook that just paces 1/7 of quota per day —
simpler than the existing linear-extrapolation budget rule.

**Recommendation: BUILD (keep own sampler/forecaster).** Optionally cross-check
`usage_report.py` numbers against `ccusage`'s output once, as a sanity check,
not a dependency. No impact on dashboard phases (F5/§4.6 already designed
around the existing cache and sample files).

## 3. Session orchestrators / task-queue dashboards

This is the closest existing alternative to the planned dashboard itself, so it
got the most scrutiny. All are read from their own GitHub pages directly.

| project | covers | lacks (vs. `dashboard_design.md` §1-§2) | license | maturity |
|---|---|---|---|---|
| `akuks/Claude-Orchestrator` | web dashboard, task queue, cron schedules, Slack notify, approval inbox, MCP credential vault, cost caps, per-project budgets | no stalled-session/limit-reset concept, no RC-safety, no window/budget rule, no standing rules, no prompt-override system, headless `ANTHROPIC_API_KEY` auth only (no OIDC/local login, no per-user audit) | unstated on page | 116 commits, 1 star/0 forks, "phases 1-5 complete" but no license shown, light adoption |
| `phahadek/claude-orchestrator` | web dashboard, task dispatch from Notion/GitHub/Jira/YAML, automated PR review, lifecycle states, live cost/token view, staged "intents" needing human approval | same gaps as above; no auth/OIDC evidence at all (assumes local/trusted use) | MIT | ~1,700+ commits claimed, 1 star/0 forks |
| `kalepasch1/claude-orchestrator` | hosted control plane (Nuxt+Supabase+Vercel) + Mac runner, approvals, spend tracking | cloud-hosted (wrong shape: AFClaude's design explicitly keeps the DB local-only), fleet-of-runners model doesn't fit one owner/one host | unstated | unclear |
| `mattwwarren/claude-workspace` | multi-session workspace orchestrator, ticket queue, worker dispatch, gates | no web dashboard found; CLI/workspace-level | unstated | unclear |
| `dnvriend/claude-code-scheduler` | GUI + CLI scheduler, REST API, manual/interval/calendar/file-watch triggers | pure scheduler, no task store, no stalled-session or budget logic | unstated | unclear |
| `vasiliyk/claude-queue` | priority (0-100) + dependency queue, pauses at 95% of session/weekly limits, resumes on reset | JSON-file store (not transactional), **its usage monitoring reads Claude.ai's internal web endpoints — its own README flags this as a likely ToS violation** | MIT | 4 commits, 23 stars/4 forks |

None of these combine: ranked projects with per-stage priority and a *global*
execution order, a blocked-task Q/A inbox, stalled-session continue/ignore with
standing rules, per-weekday automation windows, a budget rule tied to Claude's
own two usage windows, RC-thread-safety, a prompt-override editor, and generic
OIDC + local login behind the owner's existing `/private` pattern. That
combination is exactly the project-specific glue `dashboard_design.md` is
built around; it is not a feature gap any of these would close with a plugin —
it's most of the application. Adopting one would also mean a second,
un-audited write path into task state (the exact risk Option C in
`dashboard_design.md` §3 was rejected for), or running a Node/React or
Nuxt/Supabase stack alongside the existing pure-Python flat-module codebase.

**Recommendation: BUILD (confirms Option A, already chosen in
`dashboard_design.md`).** Two implementation ideas worth lifting, no adoption
needed: `akuks`'s "staged intent requiring approval" pattern maps well onto
`action_requests` (§4.6); `dnvriend`'s REST-trigger shape is a reasonable
reference for `requests` POST (§5). No change to the phased plan.

## 4. MCP task/todo servers

`claude-task-mcp`, `todo-mcp-server`, `mcp-taskmanager`, `claude-todo-manager-mcp`
are all single-table SQLite/JSON todo stores exposed over MCP: create/list/
complete, sometimes filters. None have project ranking, stage priority,
execution order across projects, stalled-session decisions, or standing rules —
`mcp_server.py` already covers all of that against `store.py`. Adopting one of
these would mean re-deriving the AFClaude-specific semantics anyway; the generic
part (an MCP stdio server with typed tools) is a thin wrapper, not the hard
part.

**Recommendation: BUILD (keep `mcp_server.py`).** No impact on phases; phase 1
already routes it through `actions.py`.

## 5. Anthropic's own features

- **Routines** (cloud-hosted scheduled Claude Code tasks, triggered by
  schedule/API/webhook; announced ~April 2026, third-party coverage only, no
  single documentation page found as a primary source). Daily run caps (5/15/25
  by plan) are far too low for nightly dispatch of many task stages, and
  execution is on Anthropic's infrastructure, not the host AFClaude needs to run
  tasks on (tmux sessions, local cwd, host bridge). Not a fit for the
  dispatcher's job.
- **Remote Control**: already used (`ka_resume.sh --remote-control`); the design
  doc already rules out driving it directly (internal protocol, against the
  terms) and only links out to it (§7.4) — this research confirms that was the
  right call; nothing found changes it.
- **`/usage`**: already the data source (`keepalive.read_usage_cache()`); no new
  forecasting surface found beyond what third parties (ccusage et al., §2)
  reverse-engineer.
- **Desktop auto-continue**: see §1.

**Recommendation: INTEGRATE-watch, no action now.** Re-check at each dashboard
deploy gate (8a/8c) whether Anthropic has shipped anything that changes the
local-execution assumption; nothing today replaces dispatcher/keepalive's job.

## 6. Self-hosted PM tools as a dashboard replacement

Checked against the actual F1-F9 requirements in `dashboard_design.md` §2.

| tool | fit | gap vs. requirements | license | maintenance |
|---|---|---|---|---|
| **Vikunja** | best generic fit: clean API, webhooks, built-in OIDC (any provider), priorities/labels | no per-project-rank-then-stage-priority execution order, no stalled-session/limit concept, no window planner, no prompt editor, no budget/usage view | AGPL | active, 2.4.0 released 2026-07-19 |
| **Kanboard** | JSON-RPC API, OAuth2/LDAP/SAML | same functional gaps; adds a PHP runtime next to an all-Python codebase | MIT-ish | active |
| **Taiga** | priorities, Kanban/Scrum, webhooks | same gaps; heavy multi-service stack (Django+Angular+RabbitMQ+Redis) for one user | AGPL | active |
| **Focalboard** | — | **no API at all**; project effectively stalled | MIT | not actively maintained — reject |
| **OpenProject** (owner already runs one) | full work-package API, webhooks, custom fields, a `priorities` endpoint | priority is one global enum, not AFClaude's rank+3-tier model; no stalled-session, window, budget, or prompt concepts; heavy Rails+Postgres+memcached stack meant for multi-user teams | mixed (CE open-source core) | active |

Every candidate would need the *same* custom pages built anyway (stalls,
window planner, budget/usage, prompt editor, standing rules) plus a
synchronization/integration layer translating AFClaude's store into the tool's
task model — which directly conflicts with the design goal that "every write
goes through the same validated, audited code path" (§1). That doubles the
surface instead of shrinking it, for the ~30% of the UI (plain task/project
CRUD) that generic tools are actually good at.

**Recommendation: BUILD (no adoption).** This strengthens confidence in the
existing Option A choice; no change to phases 1-9. If the owner ever wants a
general personal task tool unrelated to AFClaude, Vikunja is the best generic
pick found (OIDC built in, single Go binary, active) — but that is a separate
decision from this dashboard.

## 7. Building blocks to shrink the build

| piece | candidate | verdict | reason |
|---|---|---|---|
| OIDC client (generic provider, Q2) | **Authlib** | ADOPT | mature, widely used Starlette/FastAPI OIDC+OAuth2 client, BSD license, active; saves hand-rolling discovery/token validation |
| Session cookie signing | **itsdangerous** | ADOPT | small, already the de-facto Starlette-ecosystem signer; avoids hand-rolled crypto |
| Password hashing (local login) | **argon2-cffi** or `passlib[argon2]` | ADOPT | standard, avoids a hand-rolled KDF for a security-critical path |
| Login routes, CSRF, session glue | bespoke (`starlette-login`/`Imia` considered) | BUILD | the surface is small and security-critical (§6 of the design doc already scopes exactly this); a generic login framework adds indirection for ~100 lines of code the team should own directly |
| htmx + component kit | plain vendored `htmx.min.js` + handwritten CSS | BUILD (no kit) | a kit like `htmui`/BasecoatUI needs a Tailwind build step, which the design doc explicitly avoids ("no JS build chain" is a stated Pro of Option A); the visual design is already specified (purple accent, quickview's tokens) and is a small, bespoke stylesheet |
| Charts (window timeline, usage ratios) | **uPlot**, vendored | ADOPT | MIT, ~50 KB, zero dependencies, canvas-based; fits "no CDN" rule as a single static file; the dashboard only needs simple line/bar views (F4/F5), not uPlot's streaming ceiling, but its small footprint still beats Chart.js (~60 KB) and avoids a framework |

**Impact on phases:** phase 4 ("Dashboard skeleton... auth middleware") gets
smaller — pull in Authlib + itsdangerous + argon2-cffi instead of writing OIDC
and crypto from scratch, but the login routes/CSRF/session glue and the CSS
stay bespoke as already scoped. Phase 5 ("Read views... window planner") can
vendor uPlot for the timeline and ratio charts. No other phase changes.

## Summary verdict

| area | verdict |
|---|---|
| Auto-resume / keep-alive | BUILD (own system already does more) |
| Usage monitoring / forecasting | BUILD (own sampler already does more) |
| Session orchestrator / dashboard | BUILD (confirms Option A; no close alternative exists) |
| MCP task server | BUILD (own semantics are the hard part) |
| Anthropic native features (Routines etc.) | INTEGRATE-watch (monitor, no action now) |
| Self-hosted PM tool as dashboard replacement | BUILD (every candidate leaves the same ~70% to build, plus a sync problem) |
| OIDC / crypto libraries | ADOPT (Authlib, itsdangerous, argon2-cffi) |
| htmx component kit | BUILD (plain htmx + bespoke CSS; avoid a Tailwind build step) |
| Charts | ADOPT (uPlot, vendored) |
