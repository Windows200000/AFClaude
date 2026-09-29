# Rule exceptions log (AFK session 2026-09-26)

**No CLAUDE.md or guard rule was broken on purpose tonight.** For transparency, these are the state-touching actions I took. All of them were inside this project's own scope:

| time (Berlin) | action | why | undo |
|---|---|---|---|
| 00:01, recurring | `claude -p [--no-session-persistence] /usage` | get fresh usage numbers (local slash command, no model call); it rewrites the `cachedUsageUtilization` cache in `~/.claude.json`, which the CLI does on its own anyway | none needed |
| 00:04 | launched bg probe session `d39ad479` (haiku, cwd work/AFClaude/probe) | test env inheritance for resumed sessions | stopped by me at 00:05; `claude rm d39ad479` to delete it from the list |
| 00:05 | launched bg probe session `c0b93373` (haiku) | same (first probe hung on a permission prompt) | stopped by me at 00:06; `claude rm c0b93373` |
| 00:06 | a bare `--resume` of probe c0b93373 | prove wake-in-place + read effective env | **denied by the auto-mode classifier [Create Unsafe Agents]**; not retried, no workaround |
| 00:11 | started `keepalive.py` in **DRY-RUN**, detached (pid 3056629), watching my own session | live detection/decision test; it never resumes anything | `touch work/AFClaude/STOP` |

Not touched: the `claude daemon` (pid 3051692, which carries `CLAUDE_GUARD_DISABLE=1`, see PROGRESS.md 00:04), the guard hooks, settings.json, and all other sessions.
| 26.09. 14:21 | added `CLAUDE_GUARD_DISABLE=1` to the env line of `ka_resume.sh` | user asked explicitly ("setup hook bypass, that's a core part of this project"); only sessions launched by AFClaude get it, and their continue message tells them to respect the hooks anyway | remove `CLAUDE_GUARD_DISABLE=1` from that printf line (commit ac73533) |
