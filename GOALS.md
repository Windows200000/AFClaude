# Goals (in order; the manager keeps this current)

Status: `[x]` done · `[~]` in progress · `[ ]` open · `[!]` blocked (see OPEN_QUESTIONS.md)

1. [ ] Check that the 00:00 window-start fire of 30.09. acted (plan = send-keys into tmux ka-f2897285) and log the result
2. [x] Host-wide stalled-session detector + SQLite store (`stalled.py`, `store.py`), 29.09.
3. [~] Task store (`tasks.py`), done 29.09.; the project/stage priority model (high/medium/low, ordered projects) is being reworked
4. [~] Local MCP server (`mcp_server.py`)
5. [ ] Dispatcher: continue all approved stalled sessions, same window + budget rule, one tmux session each, cleanup when finished
6. [ ] Usage model: first review Thu 01.10. 17:00 (`usage_review.py`), after that from the collected data
7. [ ] Dashboard (built from scratch), then seed BACKLOG.md into the DB so backlog projects run
8. [~] Pre-public scrub: history rewritten to one initial commit after an audit; a guard keeps problematic content out
9. [ ] Usage optimisation (after everything else): use the full limits using the measured session/weekly ratios, AFClaude vs user share
10. [ ] Shared review + experience store across AFClaude users (usage-limit experience, prediction functions); consider the design

Side work:
- [x] Quickview status page (`export_quickview.py`, `quickview/`), 29.09. Follow-up: the export should read GOALS.md + OPEN_QUESTIONS.md + the task inbox instead of parsing PROGRESS.md
- [~] Limit-ratio monitoring: how fast session % vs weekly % rise, per user, split AFClaude vs user; surfaced on the quickview for window planning
- [x] Manager usage report (`usage_report.py`), 29.09.: per-subagent + session tokens, deltas via --record/--last
- [~] Container as manager, host executes (user chose the whitelisted SSH bridge). `docker/host_exec.py` + `install_host_bridge.sh` done and whitelist-tested; bridge installed by the user 29.09.; next: image (bundle all non-machine-specific code, supercronic, MCP via docker exec), route host calls through a `host.py` helper, test with the scheduler off, then cut over
