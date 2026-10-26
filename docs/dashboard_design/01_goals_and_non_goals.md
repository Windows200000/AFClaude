## 1. Goals and non-goals

Goals
- One place, usable from a phone, for everything AFClaude needs from people or wants to show: questions, blocked tasks, stalled sessions, the queue, windows, usage and budget, decisions, prompts, the sessions AFClaude drives, hand-offs and the project docs.
- Built from scratch with its own architecture (D-070). The temp dashboard (the read-only `/private` status page, `export_quickview.py` + `quickview/`) is NOT part of this design; it stays in use until the dashboard is live and is then retired as a separate side task (§10.7).
- **Everything lives in one DB** (D-161): config, auth, work, decisions, questions, hand-offs, prompt overrides, usage telemetry, runner state and the project docs. A backup or a move to another host restores a complete, clean continue (§11). Only prompt *defaults* ship with the code.
- **Everything the dashboard can change is a DB setting** that the runners read there (D-146). Env vars only bootstrap a container (D-163).
- **One write path**: dashboard, runners, MCP and CLI all write through `actions.py`, which validates and audits every write (D-154).
- **AFClaude runs in containers**, deployable on Coolify or plain docker with minimal env vars (D-163). The Claude CLI sessions stay on the host, in AFClaude's own working root, and the dashboard validates the host setup (D-164, D-165).
- The dashboard never executes anything itself. It records intent in the DB and wakes the runner, which acts (§5).
- Generic code in the public repo; identities, keys, the backlog and all runtime data stay in the DB or local.

Non-goals (v1)
- Several Claude accounts. One account per installation, shared by all users (D-156); the shared pool is future (§14).
- Showing transcripts. Titles, cwd, the stall notice and short snippets are enough; the Claude app is where a session is read.
- Driving Remote Control (RC) or speaking its protocol (internal protocol, against the terms). The dashboard links to sessions, it doesn't host them.
- Replacing the MCP server as the main way tasks get added.
- A budget-rule editor. The thresholds are settings; the rule itself is code (`pacing.py`, D-141).

