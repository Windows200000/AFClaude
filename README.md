# AFClaude

> **⚠️ AI-generated.** All code and docs in this repository were written by Claude (Anthropic's Claude Code, model Claude Opus 5.5), running mostly unattended ("AFK") on the owner's server. A human set the goals and the rules and reviews the results, but did not write this code. Treat it as a prototype.

AFClaude keeps long-running Claude Code sessions going while nobody is at the keyboard. When a session hits its usage limit, AFClaude waits for the reset. Then, inside a nightly window and only if the weekly budget allows, it continues that same session. It is the first building block of a planned cross-session task manager.

## Rules

- **The core AFClaude session always runs on Opus 5.5 (`claude-opus-5-5`) with `--effort high`.** Subagents it spawns may use other models (e.g. Haiku for cheap classification). `keepalive.py` and `ka_resume.sh` default to exactly that.
- Permission modes for unattended runs (nothing may hang waiting for a human): main sessions `--permission-mode auto`, plus narrow per-launch allow rules (`ka_resume.sh`, `claude agents`) that are checked before the classifier. Haiku and other models without auto mode use `dontAsk`; so do all `claude -p` helper calls.
- Sessions launched by AFClaude get the guard-hook bypass (`CLAUDE_GUARD_DISABLE=1`) so they never hang unattended. They are told to read the guard hooks and respect them anyway.

## Pieces

| file | what |
|---|---|
| `keepalive.py` | Watches one session's transcript. Detects the synthetic "You've hit your … limit" notice and parses the reset time. Applies the window (default 23:00–09:00 Europe/Berlin, spanning midnight; `window_start`/`window_hours` in the optional `data/afclaude.json`, the weekly default of the dashboard's per-weekday windows) and the budget rule, then continues the session. Dry-run unless `--arm`. `--window-start` is a one-shot "continue at window start": cron runs it at `0 21,22 * * *` (UTC) and only the run at 23:xx Berlin acts (21:00 UTC in CEST, 22:00 UTC in CET), once per window. `--decide` prints the current decision. `--now` skips the window/start-hour gate (budget rule still applies). `--work-on` starts a backlog project. |
| `dispatcher.py` | Goal 5: runs everything else AFClaude should run, one pass per invocation (cron), same window and budget rule as `keepalive.py`, reusing its detection, `evaluate()`, `preflight()` and `fire()`. (1) Continues approved stalled sessions (`store.stalled_decisions()` = continue, plus the manager sessions of managed projects) after their reset; skips sessions `keepalive.py` itself keeps alive (config `keepalive_sessions` + any running watcher's `--session`); continues at most one session per fork family (copies share their first message uuid): the explicitly decided one, else the one with the most recent own activity. AFClaude sessions get `continue.md`; the user's own sessions get the neutral `continue_foreign.md` + `guard_respect.md` and resume on their own model (effort is always `high`: transcripts don't record a session's effort). Holders: an idle plain interactive holder (a terminal, `claude --resume` in a tty) is taken over (`take_over_idle`, default true; the resume keeps the same RC link); a session held by a `claude rc` server (the server or its per-thread child) is never taken over, because that forks the user's RC thread: it is skipped, logged once per stall, and retried on later passes. (2) Starts pending tasks in execution order in new tmux sessions (`task_start.md`, Opus 5.5 high, cwd = project path, else the creating session's cwd), marks them `in_progress`, adds a session rule `continue` and the id to `data/own_sessions.txt`; a blocked task that got an answer resumes its old session. Stages of managed projects (`projects.manager_session`) and `--skip-task` ids/titles are never started. (3) At most `max_concurrent` (2) dispatcher sessions; before each start: window, budget rule, session usage < 85%; per-night start cap. (4) Kills only the tmux sessions it started once they finished (task done/blocked/cancelled and idle 10 min, or idle > 2 h after an end_turn; never stalled ones, never the manager's). Dry-run unless `--arm`; flock; state `data/dispatcher_state.json`, log `data/dispatcher.log`, optional config `data/dispatcher.json`; alerts via `keepalive.alert()`. |
| `ka_resume.sh` | The only thing that starts or continues a session. It runs it as a normal interactive, Remote-Control-visible process in a detached tmux session `ka-<uuid8>`, **not `--bg`**. If the tmux session is alive, it types the message in (send-keys); otherwise it runs `claude --resume <uuid> …`. |
| `BACKLOG.md` | **Local only, not in the repo.** The owner's future projects. Once the dashboard/DB exists, they get seeded into the task store and run as filler work. `keepalive.py --work-on <n|title> --arm` can start one by hand in its own tmux session. |
| `start_keepalive.sh` | Starts the watcher detached (`setsid nohup`) with a clean env. Stop it with `touch STOP`. Cron restarts it after reboots and every 15 min if it died; `touch PAUSED` keeps it stopped. |
| `usage_sampler.py` | Cron, every 15 min. Records fresh `/usage` numbers; per-session token deltas from transcripts (own sessions vs the owner's other work); `claude agents` snapshot; weekly-cycle bookkeeping (pre-reset and post-reset samples via `at`); and an hourly Haiku judgement of whether current session *names* look usage-heavy. Output in `data/` (not committed). |
| `limit_ratio.py` | How fast session % vs weekly % rise (weekly % per full session window, from consecutive 15-min samples within one window, resets skipped), how many session windows fit into a week and are left this week, and the AFClaude vs user share, estimated from token deltas using clean single-side intervals only. It reports "insufficient data" rather than guessing. The sampler stores a snapshot in every row, and the quickview shows it in the keep-alive + usage section. |
| `stalled.py` | Host-wide limit detector. Scans every top-level transcript under `~/.claude/projects` incrementally: it keeps a byte offset per file and rescans a file from 0 if it shrank. It records every limit notice and marks a session stalled while its last user/assistant entry is one, using keepalive.py's detection. `scan`, `list [--all] [--json]` (reset times in Berlin), `history <id-prefix>`. |
| `store.py` | SQLite store `data/afclaude.db` (not committed). Idempotent schema: `sessions`, `limit_hits`, `meta` (v1); `tasks`, `task_events` (append-only history), `session_decisions` and `standing_rules` (v2); `projects` (v3). Older databases are upgraded in place on connect. For v2 → v3 the `tasks` table is rebuilt after a `.bak` copy of the database, and every task gets a `migrated` event. The task API used by the CLI, the MCP server and the dispatcher: **projects are a ranked list (rank 1 = top) and tasks are their stages** (`stage_seq`), each with priority `high` (the default), `medium` or `low`. Execution order: all high stages by project rank, then stage; then all medium; then all low. Only pending tasks are ready. A project with a `manager_session` is *managed*: that session works through its stages itself, and the dispatcher only keeps it alive. `set_project_priority` sets every open stage of a project at once. Also covers the lifecycle (pending → in_progress → done, blocked ↔ pending via question/answer, cancel, reopen) and continue/ignore decisions for stalled sessions. Decision precedence: one-off decision for the current stall > session rule > most specific project rule > undecided. `pending_user_input()` returns blocked tasks plus undecided stalls. |
| `tasks.py` | CLI for the task store: `add`, `list`, `order` (the run queue), `show`, `edit`, `prio <id> high\|medium\|low`, `move <id> <stage>`, `project add\|list\|move\|prio\|edit` (`edit --manager <session>` makes a project managed, `--manager ""` undoes it), `block`, `answer`, `start`, `done`, `cancel`, `reopen`, `decide <session> continue\|ignore\|clear`, `rule add\|list\|rm`, `inbox` (everything waiting for you). `--json` on every command; times in Berlin. |
| `mcp_server.py` | Local stdio MCP server over the task store, so any Claude Code session on this host can add, list, prioritize and answer tasks and decide stalled sessions when you ask it to. 9 tools (`afclaude_*`), compact JSON results, Berlin times, and clean tool errors. `project` defaults to the calling session's directory (MCP roots > `$CLAUDE_PROJECT_DIR` > the server's cwd). Runs in `.venv/` (MCP SDK 2.x, Python 3.12). |
| `mcp_register.md` | The `claude mcp add --scope user …` command you run to register the server for all your sessions, plus the tool list and how the project default works. |
| `usage_report.py` | Per-session usage table for the manager: one row per subagent (from `<session>/subagents/agent-*.jsonl` + its `.meta.json`) plus the main session, combining transcripts across project dirs if the cwd changed. Turns, output(+thinking)/input+cache-write/cache-read tokens, running/done, first/last activity in Berlin, each subagent's share of session output, then session/weekly % and reset times (`keepalive.read_usage_cache()`, or fresh via `--fresh`). `--since ISO`/`--last` for deltas, `--record` appends to `data/usage_reports.jsonl` (not committed), `--json`. |
| `notify.py` | Failure alerts: always `ALERTS.md` + `PROGRESS.md`, plus a best-effort first-party Claude push via a tiny Haiku run. Claude suppresses the push while you're at a terminal. |
| `test_e2e_tmux.sh` | Real end-to-end test of the tmux path on a Haiku session (new → send-keys → kill → resume, one transcript, bypass env). About 15 s. |
| `test_keepalive.py` | Offline tests: reset parsing, the DST-aware window, budget thresholds, transcript detection, and a sandboxed dry-run. |
| `test_stalled.py` | Offline tests for `stalled.py` and `store.py` (temp dirs and DBs), plus read-only checks against this host's transcripts. |
| `test_tasks.py` | Offline tests for the task store and `tasks.py` (temp DBs): lifecycle, execution order across projects and priorities, bulk project priority, project and stage moves, event log, validation, decision precedence, inbox, and the in-place upgrades of goal-2 (v1) and goal-3 (v2, integer priorities) databases. |
| `test_dispatcher.py` | Offline tests for the dispatcher (temp DB, fixture transcripts, stub `ka_resume.sh`/`claude`/`tmux` on PATH): approved vs undecided vs ignored, window/reset waits, keepalive targets, fork families, task order + start marking, skip list, managed projects, answered-task resume, launch failures, concurrency cap, budget/usage gate before each start, cleanup of only its own finished sessions, verification alerts, dry-run changing nothing, the `manager_session` column upgrade. |
| `test_mcp_server.py` | The MCP tools called directly on temp DBs, plus real stdio round trips through the SDK client (spawn, initialize, list_tools, add → list → inbox, errors, roots vs cwd). Run with `.venv/bin/python -m unittest test_mcp_server`. |
| `lastmsg.py` | Debug helper that prints a session's last messages. |
| `PROGRESS.md`, `EXCEPTIONS.md` | The build log (findings, dead ends) and the log of rule exceptions. |
| `launch_nightly.sh`, `nightly_prompt.md` | Legacy: the `at` launcher of the first AFK build session (still `--bg`). |

## Dispatcher cron (suggested, not installed)

Run a dry-run by hand first (`python3 dispatcher.py`, add `--now` to preview a night pass by day), then:

```
# */10 * * * * /usr/bin/python3 /mnt/BlockVolume/Claude/work/AFClaude/dispatcher.py --arm --once >/dev/null 2>>/mnt/BlockVolume/Claude/work/AFClaude/data/dispatcher.log # AFClaude dispatcher
```

The dispatcher logs to `data/dispatcher.log` itself and echoes each line to stdout, unless stdout already is that file (the installed cron line uses `>> data/dispatcher.log 2>&1`): then it skips the echo, so every line lands once. Crash tracebacks still reach the file via the redirect (or via the `CRASH` log line when stderr is that file). It acts only inside the window, but cleans up and verifies at any time.

## Budget rule

At each point where a continue could fire:
1. projected end-of-week usage < 90% → continue
2. otherwise, continue only if the weekly reset is ≤ 11:00 Berlin after the current window (on the day the window ends at 09:00)
3. otherwise, hold

The current forecast is a linear extrapolation over the elapsed week (elapsed floored at 24 h). `usage_sampler.py` collects data for a better model.

## Project-manager mode

Every continue/start message ends with `prompts/manager.md`: the controlling Opus session acts as a project manager and hands concrete work to subagents. That keeps its context for decisions and state, so it can hold more of the project at once. All AFClaude-run projects use the same file. It's based on the Claude Code docs: best-practices "Use subagents for investigation" ("Since context is your fundamental constraint, use subagents to keep research out of it…") and workflows "When to use a workflow" (Claude as the orchestrator that only receives final results).

## Trust prompt

Unattended launches must never stop at "trust this folder?". `ka_resume.sh` marks the launch cwd `hasTrustDialogAccepted` in `~/.claude.json`, only inside `/mnt/BlockVolume/Claude` (override with `KA_TRUST_ROOT`). Not yet validated through a real launch. One observation: a launch in an untracked subfolder (`probe/`) did not prompt, so trust may already be inherited from the parent folder.

## Quickview status page

A small read-only overview, not the future dashboard. Focus: progress and what needs the user's input.

- `export_quickview.py` (cron every 3 min, flock) writes `data/quickview/` (gitignored): `status.json` (latest progress lines, "Open for the user"/morning-action/question items, goal list with done state, `ALERTS.md`, stalled sessions from `stalled.py`, latest keep-alive log lines, usage + budget rule from the `~/.claude.json` cache (no `/usage` call), next usage review, AFClaude cron entries) and `docs/` (PROGRESS, ALERTS, EXCEPTIONS, BACKLOG, README, the design doc, `prompts/*.md`). `data/samples.jsonl` and `data/haiku.jsonl` are never exported, only aggregates.
- `quickview/docker-compose.yml` + `quickview/nginx.conf`: an `nginx:alpine` container behind the host's Traefik, serving only that folder at `https://$AFCLAUDE_QUICKVIEW_HOST/afclaude/` (host name set in `quickview/.env`, gitignored). Deny by default: every request needs the `X-AFClaude-Key` header, otherwise 401. The key is not in the repo: `~/.config/afclaude/quickview.key` plus the nginx map include `~/.config/afclaude/quickview-nginx-keymap.conf` (both mode 600). Start/update with `docker compose up -d` in `quickview/`.
- `quickview/AFClaude.html`: the page, deployed as `/private/AFClaude.html` on the website host. Its nginx proxies `/private/AFClaude-data/` to the ovm1 export and adds the key header server-side, so the key never reaches the browser, and the page is covered by the site's existing `/private` login. Markdown is rendered client-side (marked + DOMPurify from cdnjs, with SRI).
- Rotating the key: write a new one to both files on ovm1, `docker compose restart` in `quickview/`, and update the include file on the website host (see that host's nginx comment), then reload its nginx.

## Public repo guard

This repo is meant to be public, so nothing personal or host-secret may be committed. `tools/check_public.py` backs two hooks in `.githooks/`; enable them once per clone with `git config core.hooksPath .githooks`.

- `pre-commit` scans the staged change (added lines and paths), `pre-push` scans the full tree of every pushed commit plus the author, committer and message of new commits.
- Blocked: local-only and runtime paths (`BACKLOG.md`, `ALERTS.md`, `OPEN_QUESTIONS.md`, `data/`, `run/`, `probe/`, logs, `*.db`, state files), secrets (`docker/secrets/`, `.env`, `*.key`, `*.pem`, ssh keys), private key headers, common token formats (GitHub, `sk-`, Slack, AWS, Google), long high-entropy hex/base64 strings, and public IPv4 addresses (private and documentation ranges pass).
- The owner's identity is blocked without being written down: the e-mail only as a SHA-256 hash, and the account/org UUIDs, e-mail and names are read from `~/.claude.json` and the global git identity on each run. Extra local terms (domains, host names) go in `~/.config/afclaude/public_denylist.txt` (or `$AFCLAUDE_PUBLIC_DENYLIST`), which is not in the repo.
- A generic secret/IP false positive can be exempted with `public-check: allow` on that line. Don't use `--no-verify`.
- `python3 tools/check_public.py --tree HEAD` audits the whole tree; `python3 -m unittest test_check_public` tests the guard.
- Local only, read from disk: `BACKLOG.md` (the owner's future projects), `OPEN_QUESTIONS.md` (questions for the owner; the quickview reads it) and `ALERTS.md`. `GOALS.md`, `PROGRESS.md` and `EXCEPTIONS.md` are the public build log and must stay free of personal and host-secret details.
- The history was squashed into a single initial commit before publishing (29.09.2026).

## Key findings (details in PROGRESS.md)

- `claude -p --no-session-persistence /usage` gives fresh session/week usage without a model call. The `~/.claude.json` cache alone can be days stale.
- The limit notice is a synthetic assistant entry (`error: "rate_limit"`). Metadata lines follow it, so check the last user/assistant entry, not the last line.
- `--bg` sessions are forked from the `claude daemon`: they inherit the **daemon's** env, and they are retired after 60 min idle. That's why AFClaude uses tmux instead.
- A non-bg `claude --resume <uuid>` (flags allowed) continues in place and reconnects Remote Control to the same RC session. It only forks if another live process holds the session.
- `--allowedTools` and `--tools` are variadic and swallow a positional prompt. Use `--flag=value` or stdin.
- Taking over a `claude rc` server's per-thread child and resuming does NOT move the thread: the original stays usable in the app and a second RC thread with the same uuid appears (two writers on one transcript). `preflight()` refuses such holders; plain interactive holders are safe to take over.
- Never dry-run with a faked clock on the real date: `handle_fire` stores a dry-run marker under the real window-start key and blocks that night's fire.
