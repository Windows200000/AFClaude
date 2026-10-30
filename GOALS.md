# Goals (in order; the task-manager keeps this current)

Status: `[x]` done · `[~]` in progress · `[ ]` open · `[!]` blocked (see OPEN_QUESTIONS.md)

0a. [~] Compaction for long-running sessions (D-144, D-155, D-191): built as design phase 3c (needs 3a runner + 3b task-manager unification); store hand-offs in the DB.
0c. [x] Usage split fixed 08.10. (b783ff3): time-based (weekly rise during autonomous runs = AFClaude, all else = user); this week 0% AFClaude / 100% user; pacing spans no longer cut at the fire's own message (user share 51% → 47%).
0e. [ ] NEXT RUN: stage ETA prediction (D-213): cumulative ETA per project stage (done in 3 d, next in 4, next in 6, …) = sessions required per stage (the only new estimate) / (total available − predicted user usage, from the usage prediction), cumulative; shown on the temp dashboard; plus a rare review like the usage review. Research existing approaches first.
0b. [x] Architecture review with the owner done 04.10. (D-143..D-199); design v2 in docs/dashboard_design/ (index: docs/dashboard_design/README.md).
1. [x] Night starts verified and reworked 07.10.: the gate runs at each session-window start (23:00, 04:00; D-202/D-203), no continue after a session limit (D-204).
2. [x] Host-wide stalled-session detector + SQLite store (`stalled.py`, `store.py`), 29.09.
3. [x] Task store (`tasks.py`) with the project/stage priority model (ordered projects; stages high/medium/low, default high), 29.09.
4. [x] Local MCP server (`mcp_server.py`, 9 tools), 29.09. registered and verified (task 1 via MCP, 29.09.)
5. [x] Dispatcher (`dispatcher.py`), cron every 10 min: since 07.10. only verification, cleanup and the owner's approved stalled sessions at their reset, any time (D-205/D-206); no task sessions; project selection among task-managers = phase 3b.
6. [~] Usage model: forecast-driven night gate live 04.10. (D-141: a full session runs only if the predicted week end ≤ the reserve threshold, auto = one session left; no margin; threshold_info). Follow-ups: stale filter in usage_report.py/usage_review.py; the model error fills in once a clean week closes; new predictor merged 18.10. (rmse 7); monthly review is a nightly task from 29.10. (D-254); ETA re-run nightly task due 23.10. (D-261)
7. [~] Dashboard: phase 1 done; design v2 (§13 plan: 2a–2d, 3a–3c, 4a–4b, 5a–5e, 6a–6c, 7a–7b, gates 8a–8d; tasks #32–#50, #25–#28). 2a–2d done; 3a runner S1–S8 + S9(b) done 29.10. (S9(a) cleanup after calm days); 3b S1–S5 done 29.10. (S4 project pick, S5 run-now); 3c S1 (several task-managers) + S2 detector done 30.10.; 4a P1–P5 done 30.10. (generic task-manager template, prompt overrides, cleanup); 4b Q1 (decisions in the DB) in progress; D-310 run end done 30.10. NEXT: 3a S9a + 3b S6 retire (~31.10.), 3c S3 after the D-305 review (01.11.).
8. [x] Pre-public scrub: history squashed into one commit after an audit, with a pre-commit/pre-push guard (`tools/check_public.py`), 29.09.; host-specific values into config still to do
9. [ ] Usage optimisation (after everything else): use the full limits using the measured session/weekly ratios, AFClaude vs user share
9b. [ ] Use the monthly extra (API-billed) usage of Team plans too, capped by a buffer or a fixed limit (e.g. $200) (D-231, after the dashboard)
10. [ ] Shared review + experience store across AFClaude users (usage-limit experience, prediction functions); consider the design

11. [x] `claude rc` server mode: researched 22.10.; live test passed 30.10. (D-288); continue hook built and deployed 30.10. (approved continues of live rc threads are delivered into the thread; first real use is its live check)

12. [~] Usage split autonomous vs manual (plan docs/usage_split_plan.md; S1 in progress; S2+ after 3a S6): tag AFClaude-driven periods in user sessions; estimate other-device usage; a separate session→weekly ratio per side from single-side intervals

Side work:
- [x] Quickview status page (`export_quickview.py`, `quickview/`), 29.09. Follow-up: the export should read GOALS.md + OPEN_QUESTIONS.md + the task inbox instead of parsing PROGRESS.md
- [x] Limit-ratio monitoring (`limit_ratio.py`), 29.09.:: how fast session % vs weekly % rise, per user, split AFClaude vs user; surfaced on the quickview for window planning
- [x] Task-manager usage report (`usage_report.py`), 29.09.: per-subagent + session tokens, deltas via --record/--last
- [x] Container runs the schedule, host executes (whitelisted SSH bridge, host.py routes every host call). Done 01.10. 14:10: the `afclaude` container (docker/compose.yml) runs the schedule (supercronic: sampler, watcher + watchdog, window-start, usage review, quickview export, dispatcher, at shim); the host crontab has no AFClaude lines any more (backup data/host_crontab.bak)
