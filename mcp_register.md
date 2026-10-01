# Registering the AFClaude MCP server

`mcp_server.py` is a local stdio MCP server over the task store (`data/afclaude.db`, same DB as `tasks.py`). It runs in the project venv, because the MCP SDK needs Python ≥ 3.10 and the system `python3` is 3.9.

## One-time setup (already done if `.venv/` exists)

```sh
cd /mnt/BlockVolume/Claude/work/AFClaude
python3.12 -m venv .venv
.venv/bin/pip install mcp          # official MCP Python SDK (2.x)
```

## Register for all your sessions on this host

This changes your Claude Code config (`~/.claude.json`, user scope), so run it yourself:

```sh
claude mcp add --scope user afclaude -- /mnt/BlockVolume/Claude/work/AFClaude/.venv/bin/python /mnt/BlockVolume/Claude/work/AFClaude/mcp_server.py
```

Check it: `claude mcp list` should show `afclaude … ✓ Connected`, and `/mcp` inside a session lists its 9 tools. Sessions that are already running only pick it up after a restart or `/mcp` reconnect.

Remove it: `claude mcp remove --scope user afclaude`.

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

## How "project" defaults

The server takes the caller's directory from these sources, in order. Every `afclaude_add_task` result names the one it used as `project_source`.

1. **MCP roots** (`roots`): the first `file://` root, if the client declares the roots capability. The SDK marks roots deprecated as of protocol 2026-07-28, but they still work on the handshake protocol that stdio clients use. Tested with the SDK client.
2. **`$CLAUDE_PROJECT_DIR`** (`env`): used if it is set in the server's environment.
3. **The server's cwd** (`cwd`): Claude Code starts one stdio server process per session, in that session's working directory. Tested over a real stdio spawn.

`/` and `$HOME` count as "no project" (`project_source: none`). A directory maps to the project whose `path` is that directory or its closest parent. An unknown directory creates a new project at the bottom of the list, named after the directory.

`created_by_session` comes from `$CLAUDE_CODE_SESSION_ID`, if Claude Code passes it to the server. The server process starts with the session, so the value can be stale after `/clear` or `/resume` inside the same process.

## Not verified yet

Nobody has yet checked which of the three sources Claude Code actually supplies. A headless probe run (`claude -p` with a probe MCP config) was blocked in the build session. After you register the server, add one task from a normal session and look at `project_source` and `created_by_session`.
