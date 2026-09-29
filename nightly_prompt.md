You are the long-running build session for the persistent cross-session task-manager project. This session was launched automatically at 00:00 Europe/Berlin by an `at` job, with no human present (AFK). It is SHOWTIME: this is no longer a "wait for the window" brief — start planning, writing and testing now.

## AFK rules for this session (read first)

- This session was started with `CLAUDE_GUARD_DISABLE=1`, so the project's guard hooks (guard-destructive-bash.sh, guard-sensitive-edit.sh) are OFF. That is only so an unattended session doesn't hang forever on an approval prompt nobody can answer.
- Still behave as if the guards were on. Avoid state-changing or destructive actions wherever there is any other way. Never destroy anything: no deleting user data, no stopping/restarting services or containers you didn't start yourself, no force-pushes, no edits to files outside your own working dir unless unavoidable.
- Only break a CLAUDE.md or guard rule when it is truly necessary for the overall goal and there is no way around it. Log every such exception (what, why, how to undo it) in `/mnt/BlockVolume/Claude/work/AFClaude/EXCEPTIONS.md` so the user can review it in the morning.
- Work in `/mnt/BlockVolume/Claude/work/AFClaude/` (or subdirs). Keep a running progress log at `/mnt/BlockVolume/Claude/work/AFClaude/PROGRESS.md`.
- All times are Europe/Berlin (the user confirmed this). The host clock and cron run on UTC, so convert.

## Your task (from the user's original brief)

I've been brainstorming a persistent, cross-session task-manager project with Sonnet. Full context, all research findings and live-tested proof-of-concept results are in the memory file `/home/opc/.claude/projects/-mnt-BlockVolume-Claude/memory/task_manager_mcp_plan.md` (project memory). Read it first, in full. It has the architecture, every feasibility finding (what's confirmed working vs. ruled out), the live PoC results from testing on this exact host, and the hand-off instructions at the bottom.

Your first deliverable is not the dashboard. Build a small, dashboard-less prototype whose only job is to keep this project's own long-running session alive across session-limit resets, using the validated `claude --bg --resume <full-session-uuid> "..."` mechanism from the PoC section. It must follow the budget rule in that memory file's "Hand-off: first prototype, no dashboard" section:
- projected end-of-week usage below 90% → continue
- otherwise, continue only if the weekly reset is no later than 11:00 after the current window
- otherwise, hold back
- only inside the daily window 00:00–08:00 **Europe/Berlin** (confirmed, so hardcode it with a tz-aware implementation, not a fixed UTC offset).

**The session to keep alive is THIS session — resume yourself.** Your own full session UUID is in `$CLAUDE_CODE_SESSION_ID`. Wire the prototype to watch your own transcript. When you hit a session limit, it should resume you (budget rule and window permitting) with a continue message that tells you to pick up from PROGRESS.md.

There is also a still-running proof-of-concept watcher at `/mnt/BlockVolume/Claude/work/AFClaude-poc/watch_and_continue.py` (log: `/mnt/BlockVolume/Claude/work/AFClaude-poc/watch.log`), which the user left active. It waits for the next session on this host to hit its usage limit, then auto-resumes it as a live test of the exact mechanism you're building on. Check its status early (log + `ps aux | grep watch_and_continue`) and let its real outcome inform your prototype. Note: it had a hard 6h timeout from 14:51 Berlin on 2026-09-25, so it has most likely timed out by now; check the log.

Before writing anything else (dashboard, MCP server, task queue, priority system), get the keep-alive prototype working and prove it end-to-end on this host, the same way the earlier PoC work did.

When the prototype is done and proven, update the memory file with the results. Per the memory, any new GitHub repo needs its name from the user first, so don't create one; leave that as a morning question in PROGRESS.md.

## Findings from the setup session (2026-09-25, 17:20–17:40 Berlin)

1. **Guard bypass verified live.** Fed the guard hook the harmless command `echo "reboot keyword test"`: without the variable it returns `permissionDecision: "ask"`; with `CLAUDE_GUARD_DISABLE=1` it exits 0 silently. A test session launched as `CLAUDE_GUARD_DISABLE=1 claude --bg --remote-control --name ... --model claude-opus-5-5 --permission-mode auto "..."` (id 48976867-eaaf-4636-b8b2-6ef28294a027) had `CLAUDE_GUARD_DISABLE=1` in its `/proc/<pid>/environ`. It ran the guard-matching command without hanging and replied `GUARD=1 | reboot keyword test` on model `claude-opus-5-5`. So `--bg` sessions DO inherit the launching shell's environment.
2. **Env is fixed per process. This matters for resuming yourself.** `claude --bg --resume <uuid>` on a session whose process is still alive just wakes that process with its original environment. If your process has exited and the resume spawns a new one, the environment comes from whatever invoked the resume. So your keep-alive must launch its resume with `CLAUDE_GUARD_DISABLE=1` (and `--effort`/model are saved launch options, but verify they survive a resume) or the resumed you will have guards back on and could hang on the first "ask". Verify this explicitly.
3. **Auto-mode classifier pushback.** In the setup session (auto permission mode), the classifier denied follow-up actions around launching a guard-disabled `claude --bg` agent, labelled "Create Unsafe Agents", until the user explicitly authorized it. You are also in auto mode. Your keep-alive firing `CLAUDE_GUARD_DISABLE=1 claude --bg --resume ...` from your own Bash tool may get the same denial. The user has explicitly authorized this project's self-resume with the bypass. Prefer a design where one standalone watcher script (nohup/at/cron, outside any session) does the resuming, rather than per-fire tool calls. If you get denied anyway, don't hunt for workarounds: log it in PROGRESS.md as a blocker for the morning.
4. **Scheduling env hygiene.** `at` copies the environment of whoever scheduled the job. A Claude session's env carries `CLAUDECODE=1`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION=1`, a messaging socket and so on, which could confuse a new top-level session. The launcher for this session scrubs those with `env -i` and passes model/effort explicitly. Do the same in the keep-alive's resume launcher.
5. **Model/effort.** This session was launched with `--model claude-opus-5-5 --effort high`, at the user's explicit request. Keep it that way for resumes.
6. **Watcher status at setup time.** It was alive (python pid 2919865) and had found no stall yet (baseline: none) as of 17:26 Berlin.
7. (Removed before publishing: a host-specific note about the guard hooks' coverage.)
