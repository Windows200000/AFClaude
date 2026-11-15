# Registering the AFClaude MCP server

The MCP server (`mcp_server.py`) is one server over the task store with two transports. In the container layout (phase 5b K5, `docs/phase5b_plan.md` §3.3) it runs in the `afclaude-mcp` container and a registration is one of two lines, both of which change your Claude Code config (`~/.claude.json`, user scope), so you run them yourself: AFClaude never edits it.

## This host, after the switch: stdio through the `afclaude` wrapper

The host kit (version 7 and later) installs the wrapper as `afclaude` (source `docker/host-kit/afclaude_host.py`, linked from `~/.local/bin`: put that on `PATH`). It runs every AFClaude command inside the container, `docker exec -i afclaude-mcp afclaude <arguments>`, and passes only the identity environment of `identity.py` plus this machine's token, read from its token file (`~/.config/afclaude/machine.json`, written by `python3 tasks.py machine register-host`, which runs in a plain shell on the host and so before the switch makes the container the only writer; mode 0600, the directory yours alone) and this host's name. The token is never in an argv or in a Claude config.

```sh
claude mcp remove --scope user afclaude      # the old venv registration, if you have it
claude mcp add --scope user afclaude -- afclaude mcp stdio
```

Needs: the kit installed (`docker/install_host_bridge.sh`, run by you), the container running (the compose profile `mcp`: `docker/compose.services.yml` header; it starts only once `roles_enforce` is `on`), and this host's machine user (`tasks.py machine register-host`). A session without a token file is the host owner (D-330); a session AFClaude drives is a task-manager or agent whatever file there is (D-198). The wrapper refuses (exit 2, nothing run) an `AFCLAUDE_*` variable in the environment other than `AFCLAUDE_AGENT_ID` and `AFCLAUDE_MACHINE_FILE`, and a token file that is a link, not yours, not 0600 or in a directory others can write.

Check: `claude mcp list` shows `afclaude … ✓ Connected`; `/mcp` inside a session lists the tools. Remove: `claude mcp remove --scope user afclaude`.

## Another machine: HTTP

A machine that is not this host registers its user on the host (`afclaude machine code`, then `afclaude machine register CODE` there) and gets the token copied over a channel you trust. Before 8d (the reverse-proxy route) the port is reachable only through your own tunnel: `docker/compose.tunnel.yml` publishes it on this host's loopback (`127.0.0.1:8000`), and you forward that port (`ssh -L`, a VPN). On that machine:

```sh
claude mcp add --scope user --transport http afclaude http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <the machine's token>" --header "X-AFClaude-Hostname: <this machine's host name>"
```

The name and the URL come **before** `--header` (the option takes several values and would swallow them). Both headers are required: a request without the host name header is refused (401 and an audit row, D-342); the header is the client's own claim and only feeds the alert when one token shows up from two host names (D-198), it never authorises. A request without a valid token, or from a revoked machine, is a 401 and an audited refusal. Over HTTP the caller is always the machine user, never the host owner; this host's own sessions use the stdio line above.

## Until the switch: the venv registration

Before the switch (the old single container, `meta.writers` not `container`) the server runs on the host in the project venv, because the MCP SDK needs Python >= 3.10 and the system `python3` is 3.9:

```sh
cd <the checkout>
python3.12 -m venv .venv
.venv/bin/pip install mcp          # official MCP Python SDK (2.x)
claude mcp add --scope user afclaude -- <the checkout>/.venv/bin/python <the checkout>/mcp_server.py
```

Check it: `claude mcp list` should show `afclaude … ✓ Connected`, and `/mcp` inside a session lists its tools. Sessions that are already running only pick up a registration after a restart or `/mcp` reconnect.

## Only on explicit request, no approval step

A task added through MCP counts exactly like one you add in the UI (today `tasks.py`, later the dashboard): it is `pending` right away, and the dispatcher starts it on its own in the nightly window. There is no approval step (owner decision, 29.09.2026). So the server tells every session to use these tools **only when you explicitly ask for AFClaude**, or when the AFClaude task prompt a session was started with tells it to report through them (`prompts/task_start.md`: done / blocked). It says this in two places, because some clients drop server instructions:

- the server's MCP `instructions` (sent at initialize: when to use the tools, never on the session's own initiative, no approval step, execution order);
- a short prefix on every tool description: "Only when the user explicitly asks for AFClaude (or an AFClaude task prompt says so)."

`test_mcp_server.py` checks both over a real stdio round trip. Sessions that already run pick up the new texts only after a restart or `/mcp` reconnect.

## Tools

| tool | what |
|---|---|
| `afclaude_add_task` | new task (a stage at the end of a project); project defaults to the calling session's directory |
| `afclaude_list_tasks` | tasks in execution order; status `open` (default), `ready` (the run queue), `all`, or one status |
| `afclaude_get_task` | one task with description, Q&A and event history |
| `afclaude_update_task` | title, description, priority, project, stage position, and status (done, cancelled, pending = reopen, in_progress, blocked + question), all in one atomic call |
| `afclaude_answer_task` | answer a blocked task, which makes it pending again |
| `afclaude_project` | the ranked project list: `list`, `add`, `move`, `prio` (every open stage at once), `edit` (incl. `manager_session`: a managed project is worked by that session, the dispatcher starts no task sessions for it) |
| `afclaude_inbox` | everything waiting for you: blocked tasks and undecided stalled sessions (runs the stalled scan first) |
| `afclaude_decide_session` | continue, ignore or clear for one stalled session (id or prefix) |
| `afclaude_rule` | standing continue/ignore rules: `list`, `add`, `rm` |
| `afclaude_ask` | a question for you from a (task-manager) session: a blocked task of kind `question` in your inbox; your answer closes it |
| `afclaude_handoff` | a session's compaction hand-off (D-144): summary, done, in progress, next steps, the owner's messages (verbatim), in flight, lessons, working notes, open question ids, notes (D-305); the keep-alive injects it after compacting that session at its next start |
| `afclaude_doc` | a project's docs in the DB (phase 2d): `list`, `show`, `append` (progress, exceptions, alerts), `set` (goals, review:<name>); during the dual-write period the files stay the source (see `project_docs.py`) |

Every write goes through `actions.py` (one transaction, an `audit_log` row with the caller's principal as actor and its session in the `session` column: `machine:<id>` for a registered machine's session, `task-manager:<session>`, `agent:<session>[:<subagent id>]`; `identity.py`, phase 5a A2, `tasks.py whoami` shows what a process resolves to; the `PreToolUse` hook `identity_hook.py` in the repo's `.claude/settings.json` stamps each call with the session id and subagent id). A session AFClaude itself runs (`CLAUDE_GUARD_DISABLE=1`, `driven_sessions` incl. the own sessions of `python3 runner_state.py own add`, a task-manager session) may add tasks and projects, but `afclaude_decide_session` and `afclaude_rule add/rm` refuse it: deciding sessions and rules stays with you.

## How "project" defaults

The server takes the caller's directory from these sources, in order. Every `afclaude_add_task` result names the one it used as `project_source`.

1. **MCP roots** (`roots`): the first `file://` root, if the client declares the roots capability. The SDK marks roots deprecated as of protocol 2026-07-28, but they still work on the handshake protocol that stdio clients use. Tested with the SDK client.
2. **`$CLAUDE_PROJECT_DIR`** (`env`): used if it is set in the server's environment.
3. **The server's cwd** (`cwd`): Claude Code starts one stdio server process per session, in that session's working directory. Tested over a real stdio spawn.

`/` and `$HOME` count as "no project" (`project_source: none`). A directory maps to the project whose `path` is that directory or its closest parent. An unknown directory creates a new project at the bottom of the list, named after the directory.

`created_by_session` comes from `$CLAUDE_CODE_SESSION_ID`, if Claude Code passes it to the server. The server process starts with the session, so the value can be stale after `/clear` or `/resume` inside the same process.

## What Claude Code supplies to a stdio server

Checked on 14.11. (Claude Code 2.1.325, a headless run with a stub server that wrote its environment): the server starts with `CLAUDE_CODE_SESSION_ID` (the session's id at its start; after `/clear` the hook's `_afc` stamp carries the new one), `CLAUDECODE=1` and `CLAUDE_PROJECT_DIR` (the session's directory), so the second source of "How project defaults" works, and through the wrapper too (it passes these variables into the container and sets `CLAUDE_PROJECT_DIR` from its own cwd when the caller has none). Over HTTP no session id or directory reaches the server: the machine comes from the token, the session from the stamp, and a call has no project default.
