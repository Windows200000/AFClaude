## 10. Deployment

### 10.1 Containers (D-163)
One image, one compose file usable by plain docker and by Coolify (docker-compose deploy). `/data` in every service is the host directory `AFCLAUDE_DATA` (§10.2):

| service | command | mounts | port |
|---|---|---|---|
| `afclaude-web` | `afclaude web` | `/data` | 8080 (internal; proxied) |
| `afclaude-runner` | `afclaude runner` | `/data`, the bridge key and host key (read-only) | none |
| `afclaude-mcp` | `afclaude mcp` (stdio via `docker exec`; remote transport for other machines, §4, §9.7) | `/data` | internal only, for the remote transport via the proxy (D-198) |

**Multi-stage image build (D-209).** The Dockerfile has two stages, so Node exists only while the image is built:
1. **Frontend build stage** (a pinned Node LTS image, by digest): `npm ci --ignore-scripts` from the committed lockfile, `npm audit` (fails on high/critical), `tsc --noEmit` (strict), the frontend unit tests, then `esbuild` bundles and minifies into `dist/` (hashed file names, source maps not shipped).
2. **Runtime stage** (a pinned Python slim image, by digest): the Python code and its hash-pinned dependencies, plus only `dist/` copied from stage 1 into the web app's static directory. No Node, npm, `node_modules` or TS sources in the final image.

A failing typecheck, test or audit in stage 1 fails the image build, so an image with an unchecked frontend can't exist. The backend serves `dist/` itself (Starlette static files, the app shell for every non-API path).

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
In the repo: all code (incl. the TS frontend sources, `package.json`, `package-lock.json` and the generated API types; not the build output), default prompts, the Dockerfile and compose files with `${VARS}`, `env.example`, this doc. Local or in the DB only: the DB and its backups, the master key, `.env`, the bridge key, the backlog, the decision store, identities. `tools/check_public.py` guards every commit and push.

### 10.4 AFClaude's own working root and Claude config (D-165)
- **First-startup wizard** (in `/setup`, after the first owner registers; `afclaude setup` on the CLI until the dashboard exists; the data directory `AFCLAUDE_DATA` is chosen before the containers start, §10.2): host check (§10.5) → choose the **AFClaude working root** (the working folder) on the host → Claude config and login → trust and permission setup → register this host as a machine (its machine user for interactive sessions, §9.7) and offer to grant it a role (D-195) → time zone. Or "restore from a backup or export" instead (§11.3), which asks for the working root too.
- **Claude plan (D-229).** The wizard asks which Claude plan the account is on (Pro, Max 5x/20x, Team standard/premium seat, Enterprise, API) and stores it as the setting `claude_plan`. Plan-dependent numbers come from a per-plan table in code (defaults only, every one also overridable): the starting session-to-weekly ratio and fill rate until enough runs are measured, whether a weekly limit exists, which models are available for task-managers/subagents (e.g. Opus), and the session length. Measured values replace the plan defaults as data comes in. The host check (F13) compares the plan with what `/usage` reports and flags a mismatch.
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

### 10.9 Owner previews (D-211)
The owner wants to see and react to whatever the dashboard UI looks like as soon as it exists, without that review blocking or gating any work (unlike the putup gates of §13/8, which still need an explicit go/no-go as usual). The mechanism:

- **Preview build.** The TS frontend (§10.1) gets a second build target — `npm run build:preview` (an esbuild `define` flipping the API client's base implementation) — that swaps the real API client for a mock client serving typed fixture JSON matching the same generated schema (§8). The preview build makes no network requests to any backend; its CSP sets `connect-src 'none'`. Only dummy data ships in it, never a copy of real rows.
- **Where it lands.** The preview build's static output is deployed under the owner's existing `/private` area at a separate, dashboard-owned path (written generically here, e.g. `/private/AFClaude-preview/`), disconnected from the live backend, the data volume and the host bridge — the same way the manager already deploys the temp dashboard page (§10.7), just to its own path and container/route.
- **First possible from phase 6a** (§13): that's where the TS frontend scaffold, its own router and app shell first exist to build and serve. The temp dashboard's phase strip marks this (below).
- **Cadence.** After every UI-bearing step — 6a, 6b, 6c, 7a, 7b — the preview is rebuilt and redeployed, and a non-blocking review item (one line + the preview link) is added to "needs your input". The owner's feedback, if any, is folded into a later step; the subagent doesn't wait for a reply before continuing.
- **Fallback.** If a given feature genuinely can't be faked with static fixture data, it previews instead as a strictly isolated container with dummy data: no access to the live data volume, the host bridge, or Claude.

