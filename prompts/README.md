# Prompts

Every prompt AFClaude sends lives here, so the dashboard can show and edit them later. These files are the defaults.

| file | used for | placeholders |
|---|---|---|
| `continue.md` | continue message for a resumed AFClaude session: the task-manager at a session-window start or last-stretch slot start (keepalive.py), an approved stalled session in the AFClaude repo (dispatcher.py) | `{reason}`, `{progress}` |
| `fillup.md` | the fill-up run (D-212): continue message for the task-manager shortly before the session reset of a session window whose run stopped early (keepalive.py fillup_pass) | `{reason}`, `{progress}`, `{remaining}`, `{reset}` |
| `session_end_save.md` | the save prompt before a compaction (D-144/D-191, design §5.5): typed into the task-manager near the end of its session window when it will be compacted at the next start (keepalive.py handoff_save_pass), or appended to the fill-up message when a fill-up is planned for that window | `{reset}`, `{session}`, `{tasks}` |
| `compact.md` | the instruction after `/compact` at the start that compacts the task-manager (keepalive.py compact_session) | — |
| `compact_resume.md` | inserted into the continue message after a compaction: the session's latest hand-off from the DB (`handoffs`, rendered by store.render_handoff) | `{handoff}`, `{progress}` |
| `compact_resume_none.md` | the same when there is no hand-off (none saved, or the DB could not be read) | `{why}`, `{progress}` |
| `project_start.md` | first message of a new project session (`--work-on`) | `{title}`, `{desc}`, `{slug}` |
| `continue_foreign.md` | neutral continue message for an approved stalled session that is not an AFClaude session (dispatcher.py; the user's own sessions get only this + `guard_respect.md`, and resume on their own model). (`{context}` is empty since D-205: task sessions and task-managers are never continued at a limit reset, D-204) | `{reason}`, `{context}` |
| `task_start.md` | first message of a task session; unused since D-205 (the dispatcher starts no task sessions, a project's task-manager works its tasks), kept for design phase 3b | `{id}`, `{title}`, `{project}`, `{description}`, `{qa}` |
| `guard_respect.md` | appended to every session message: respect the guard hooks even when they're bypassed | — |
| `manager.md` | appended to all of them except the user's own sessions: act as the project's task-manager and delegate to subagents | — |
| `manager_afclaude.md` | appended after `manager.md` only for sessions working on AFClaude itself: usage report, the clean session stop, GOALS/OPEN_QUESTIONS, public-repo rule, worktrees | `{session_stop_pct}` (the setting, D-014) |
| `usage_review.md` | the periodic usage-model review (Opus, medium effort; usage_review.py): one run that tests hypotheses, sorts findings into universal (code, public) vs user-specific (data/user_model.json, local) and leaves an unread-review note in memory | `{date}`, `{since}` |
| `stage_eta_review.md` | appended to `usage_review.md` in the same monthly run (usage_review.py): the rare stage-ETA review (D-213) that scores the logged stage ETAs (`stage_eta.py --score`) and tunes the stages' session estimates only (D-142 analogue) | `{date}` |
| `usage_haiku.md` | per-session usage classification by Haiku (usage_sampler.py) | `{name}`, `{n}`, `{prompts}`, `{gaps}`, `{totals}` |

Session messages are flattened to one line (tmux send-keys). Literal braces in a file with placeholders must be doubled (`{{ }}`).

**Planned switch of the project docs (dashboard phase 2d part 2, design §7.7; not done yet).** PROGRESS.md, GOALS.md, EXCEPTIONS.md, ALERTS.md and the review reports are now also in the DB (`project_docs.py`, dual-write: the files stay the source and are imported). These prompts still tell sessions to edit the files; at the switch `manager.md`, `manager_afclaude.md`, `continue.md`, `fillup.md`, `compact_resume*.md`, `task_start.md`, `project_start.md` and the review prompts change to writing through the write path (`tasks.py doc append progress "…"`, `tasks.py doc set goals -`, MCP `afclaude_doc`), the `{progress}` placeholder points at `tasks.py doc show progress --entries N` instead of a file, and the AFClaude task-manager runs `tasks.py doc export` (the only file output, D-181) before it commits the build log.
