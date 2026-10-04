# Goals (in order; the task-manager keeps this current)

Status: `[x]` done · `[~]` in progress · `[ ]` open · `[!]` blocked (see OPEN_QUESTIONS.md)

0a. [ ] NEXT RUN, do first: compaction for long-running sessions (D-144, D-191), per tmux session: a session-end script prompts "save your work, compaction follows"; a structured hand-off path; at the next start /compact then resume from the hand-off. Only for a tmux session that ran longer than one session length in total: at the end of a session window, compact if it already existed during the previous session window (in practice the task-manager).
0b. [!] Before ANY dashboard build work (phase 2+): prepare the list of cascading architecture decisions of the dashboard + backend (D-143; skip small/easily changed ones), put it in OPEN_QUESTIONS.md, go through it with the owner. Phase 2 starts only after that.
1. [ ] Check that the 00:00 window-start fire of 30.09. acted (plan = send-keys into tmux ka-f2897285) and log the result
2. [x] Host-wide stalled-session detector + SQLite store (`stalled.py`, `store.py`), 29.09.
3. [x] Task store (`tasks.py`) with the project/stage priority model (ordered projects; stages high/medium/low, default high), 29.09.
4. [x] Local MCP server (`mcp_server.py`, 9 tools), 29.09. registered and verified (task 1 via MCP, 29.09.)
5. [x] Dispatcher (`dispatcher.py`), 29.09.: armed via cron every 10 min; approved stalled sessions + queued tasks, never takes over RC-server threads
6. [~] Usage model: forecast-driven night gate live 04.10. (D-141: a full session runs only if the predicted week end ≤ the reserve threshold, auto = one session left; no margin; threshold_info). Follow-ups: stale filter in usage_report.py/usage_review.py; the model error fills in once a clean week closes; next review 29.10. (prediction model only, D-142)
7. [!] Dashboard (blocked by 0b): phase 1 merged 01.10.; design done (docs/dashboard_design.md, 0d4b076); 9 build phases in the task store (1: config + schema v4 + actions.py … 9: retire the quickview + seed the backlog)
8. [x] Pre-public scrub: history squashed into one commit after an audit, with a pre-commit/pre-push guard (`tools/check_public.py`), 29.09.; host-specific values into config still to do
9. [ ] Usage optimisation (after everything else): use the full limits using the measured session/weekly ratios, AFClaude vs user share
10. [ ] Shared review + experience store across AFClaude users (usage-limit experience, prediction functions); consider the design

11. [ ] Investigate hooking into `claude rc` server mode (more convenient for the user; server sessions don't expire, unlike per-session Remote Control)

12. [ ] Usage split autonomous vs manual: tag AFClaude-driven periods in user sessions; estimate other-device usage; a separate session→weekly ratio per side from single-side intervals

Side work:
- [x] Quickview status page (`export_quickview.py`, `quickview/`), 29.09. Follow-up: the export should read GOALS.md + OPEN_QUESTIONS.md + the task inbox instead of parsing PROGRESS.md
- [x] Limit-ratio monitoring (`limit_ratio.py`), 29.09.:: how fast session % vs weekly % rise, per user, split AFClaude vs user; surfaced on the quickview for window planning
- [x] Task-manager usage report (`usage_report.py`), 29.09.: per-subagent + session tokens, deltas via --record/--last
- [x] Container runs the schedule, host executes (whitelisted SSH bridge, host.py routes every host call). Done 01.10. 14:10: the `afclaude` container (docker/compose.yml) runs the schedule (supercronic: sampler, watcher + watchdog, window-start, usage review, quickview export, dispatcher, at shim); the host crontab has no AFClaude lines any more (backup data/host_crontab.bak)
