# Goals (in order; the manager keeps this current)

Status: `[x]` done · `[~]` in progress · `[ ]` open · `[!]` blocked (see OPEN_QUESTIONS.md)

1. [ ] Check that the 00:00 window-start fire of 30.09. acted (plan = send-keys into tmux ka-f2897285) and log the result
2. [x] Host-wide stalled-session detector + SQLite store (`stalled.py`, `store.py`), 29.09.
3. [x] Task store (`tasks.py`) with the project/stage priority model (ordered projects; stages high/medium/low, default high), 29.09.
4. [x] Local MCP server (`mcp_server.py`, 9 tools), 29.09. The user still has to register it (OPEN_QUESTIONS)
5. [x] Dispatcher (`dispatcher.py`), 29.09.: armed via cron every 10 min; approved stalled sessions + queued tasks, never takes over RC-server threads
6. [ ] Usage model: first review Thu 01.10. 17:00 (`usage_review.py`), after that from the collected data
7. [~] Dashboard: design done (docs/dashboard_design.md, 0d4b076); 9 build phases in the task store (1: config + schema v4 + actions.py … 9: retire the quickview + seed the backlog)
8. [x] Pre-public scrub: history squashed into one commit after an audit, with a pre-commit/pre-push guard (`tools/check_public.py`), 29.09.; host-specific values into config still to do
9. [ ] Usage optimisation (after everything else): use the full limits using the measured session/weekly ratios, AFClaude vs user share
10. [ ] Shared review + experience store across AFClaude users (usage-limit experience, prediction functions); consider the design

11. [ ] Investigate hooking into `claude rc` server mode (more convenient for the user; server sessions don't expire, unlike per-session Remote Control)

Side work:
- [x] Quickview status page (`export_quickview.py`, `quickview/`), 29.09. Follow-up: the export should read GOALS.md + OPEN_QUESTIONS.md + the task inbox instead of parsing PROGRESS.md
- [x] Limit-ratio monitoring (`limit_ratio.py`), 29.09.:: how fast session % vs weekly % rise, per user, split AFClaude vs user; surfaced on the quickview for window planning
- [x] Manager usage report (`usage_report.py`), 29.09.: per-subagent + session tokens, deltas via --record/--last
- [~] Container as manager, host executes (user chose the whitelisted SSH bridge). `docker/host_exec.py` + `install_host_bridge.sh` done and whitelist-tested; bridge installed by the user 29.09.; next: image (bundle all non-machine-specific code, supercronic, MCP via docker exec), route host calls through a `host.py` helper, test with the scheduler off, then cut over
