# Goals (in order; the task-manager keeps this current)

Status: `[x]` done · `[~]` in progress · `[ ]` open · `[!]` blocked (see OPEN_QUESTIONS.md)

0a. [~] Compaction for long-running sessions (D-144, D-155, D-191): built as design phase 3c (needs 3a runner + 3b task-manager unification); store hand-offs in the DB.
0c. [x] Usage split fixed 08.10. (b783ff3): time-based (weekly rise during autonomous runs = AFClaude, all else = user); this week 0% AFClaude / 100% user; pacing spans no longer cut at the fire's own message (user share 51% → 47%).
0e. [x] Stage ETA prediction (D-213): built and live (stage_eta.py, ETAs on the quickview phase chips, daily stage_eta_log); its rare review runs with the monthly usage review (D-264; ran 23.10. and 29.10., next 26.11.). Open: the 29.10. review proposes counting cleaned design chats (~30%/week) instead of the flat mean in the ETA capacity (with Q-review-1029-design).
0b. [x] Architecture review with the owner done 04.10. (D-143..D-199); design v2 in docs/dashboard_design/ (index: docs/dashboard_design/README.md).
1. [x] Night starts verified and reworked 07.10.: the gate runs at each session-window start (23:00, 04:00; D-202/D-203), no continue after a session limit (D-204).
2. [x] Host-wide stalled-session detector + SQLite store (`stalled.py`, `store.py`), 29.09.
3. [x] Task store (`tasks.py`) with the project/stage priority model (ordered projects; stages high/medium/low, default high), 29.09.
4. [x] Local MCP server (`mcp_server.py`, 9 tools), 29.09. registered and verified (task 1 via MCP, 29.09.)
5. [x] Dispatcher (`dispatcher.py`), cron every 10 min: since 07.10. only verification, cleanup and the owner's approved stalled sessions at their reset, any time (D-205/D-206); no task sessions; project selection among task-managers = phase 3b.
6. [x] Usage model: forecast-driven night gate live 04.10. (D-141: a full session runs only if the predicted week end <= the reserve threshold, auto = one session left; no margin). New predictor 18.10., monthly review as a nightly task (D-254, ran 29.10., next 26.11.), ETA re-run 23.10. done; stale samples are filtered by the sampler. Left to the monthly review: the D-267 predictor ideas.
7. [~] Dashboard: phase 1 done; design v2 (§13 plan: 2a–2d, 3a–3c, 4a–4b, 5a–5e, 6a–6c, 7a–7b, gates 8a–8d; tasks #32–#50, #25–#28). 2a–2d done; 3a runner S1–S9 done (S9(a) legacy retired 31.10.); 3b S1–S6 done (S6 manager-session/ka- names retired 31.10.); 3c S1 (several task-managers) + S2 detector done 30.10.; 4a P1–P5 done 30.10. (generic task-manager template, prompt overrides, cleanup); 4b Q1 (decisions in the DB, schema v5) done 30.10.; 4b Q2 done 31.10. (owner-word evidence, seeding: the DB is the only decision source, DECISIONS.md retired); D-310 run end done 30.10.; 4b Q3 W1–W3 done 31.10. (questions in the DB: 23 imported, answered on the quickview; the owner's first quickview answer still to see). NEXT: Q3 done-when week, 3c S3 after the D-305 review (01.11.).
8. [x] Pre-public scrub: history squashed into one commit after an audit, with a pre-commit/pre-push guard (`tools/check_public.py`), 29.09.; host-specific values into config still to do
9. [ ] Usage optimisation (after everything else): use the full limits using the measured session/weekly ratios, AFClaude vs user share
9b. [ ] Use the monthly extra (API-billed) usage of Team plans too, capped by a buffer or a fixed limit (e.g. $200) (D-231, after the dashboard)
10. [ ] Shared review + experience store across AFClaude users (usage-limit experience, prediction functions); consider the design

11. [x] `claude rc` server mode: researched 22.10.; live test passed 30.10. (D-288); continue hook built and deployed 30.10. (approved continues of live rc threads are delivered into the thread; first real use is its live check)

12. [~] Usage split autonomous vs manual (plan docs/usage_split_plan.md; S1 done; S2 built 31.10. (branch usage-split-s2, Opus review and merge next); the pure usage_split.py core is merged): tag AFClaude-driven periods in user sessions; estimate other-device usage; a separate session→weekly ratio per side from single-side intervals

Side work:
- [x] Quickview status page (`export_quickview.py`, `quickview/`), 29.09. Follow-up: the export should read GOALS.md + OPEN_QUESTIONS.md + the task inbox instead of parsing PROGRESS.md
- [x] Limit-ratio monitoring (`limit_ratio.py`), 29.09.:: how fast session % vs weekly % rise, per user, split AFClaude vs user; surfaced on the quickview for window planning
- [x] Task-manager usage report (`usage_report.py`), 29.09.: per-subagent + session tokens, deltas via --record/--last
- [x] Container runs the schedule, host executes (whitelisted SSH bridge, host.py routes every host call). Done 01.10. 14:10: the `afclaude` container (docker/compose.yml) runs the schedule (supercronic: sampler, watcher + watchdog, window-start, usage review, quickview export, dispatcher, at shim); the host crontab has no AFClaude lines any more (backup data/host_crontab.bak)
