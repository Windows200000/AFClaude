# Prompts

Every prompt AFClaude sends lives here, so the dashboard can show and edit them later. These files are the defaults.

| file | used for | placeholders |
|---|---|---|
| `continue.md` | continue message for a resumed session (keepalive.py) | `{reason}`, `{progress}` |
| `project_start.md` | first message of a new project session (`--work-on`) | `{title}`, `{desc}`, `{slug}` |
| `guard_respect.md` | appended to both: respect the guard hooks even when they're bypassed | — |
| `manager.md` | appended to both: act as project manager and delegate to subagents | — |
| `usage_review.md` | the periodic usage-model review (Opus, medium effort; usage_review.py): one run that tests hypotheses, sorts findings into universal (code, public) vs user-specific (data/user_model.json, local) and leaves an unread-review note in memory | `{date}`, `{since}` |
| `usage_haiku.md` | per-session usage classification by Haiku (usage_sampler.py) | `{name}`, `{n}`, `{prompts}`, `{gaps}`, `{totals}` |

Session messages are flattened to one line (tmux send-keys). Literal braces in a file with placeholders must be doubled (`{{ }}`).
