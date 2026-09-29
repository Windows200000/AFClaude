# Prompts

Every prompt AFClaude sends lives here, so the dashboard can show and edit them later. These files are the defaults.

| file | used for | placeholders |
|---|---|---|
| `continue.md` | continue message for a resumed AFClaude session (keepalive.py, dispatcher.py) | `{reason}`, `{progress}` |
| `project_start.md` | first message of a new project session (`--work-on`) | `{title}`, `{desc}`, `{slug}` |
| `continue_foreign.md` | neutral continue message for an approved stalled session that is not an AFClaude session (dispatcher.py; the user's own sessions get only this + `guard_respect.md`, and resume on their own model). With a `{context}` it also continues dispatcher task sessions and managed-project manager sessions | `{reason}`, `{context}` |
| `task_start.md` | first message of a task session started by the dispatcher (also used to resume a blocked task's session with the answer) | `{id}`, `{title}`, `{project}`, `{description}`, `{qa}` |
| `guard_respect.md` | appended to every session message: respect the guard hooks even when they're bypassed | — |
| `manager.md` | appended to all of them except the user's own sessions: act as project manager and delegate to subagents | — |
| `manager_afclaude.md` | appended after `manager.md` only for sessions working on AFClaude itself: usage report, GOALS/OPEN_QUESTIONS, public-repo rule, worktrees | — |
| `usage_review.md` | the periodic usage-model review (Opus, medium effort; usage_review.py): one run that tests hypotheses, sorts findings into universal (code, public) vs user-specific (data/user_model.json, local) and leaves an unread-review note in memory | `{date}`, `{since}` |
| `usage_haiku.md` | per-session usage classification by Haiku (usage_sampler.py) | `{name}`, `{n}`, `{prompts}`, `{gaps}`, `{totals}` |

Session messages are flattened to one line (tmux send-keys). Literal braces in a file with placeholders must be doubled (`{{ }}`).
