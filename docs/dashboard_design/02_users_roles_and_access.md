## 2. Users, roles and access (D-149, D-156, D-160, D-172, D-182–D-184, D-189, D-190, D-195, D-196, D-298)

User roles (people logged in to the dashboard, and machine users, below):

| role | who | can |
|---|---|---|
| **owner** | the holder of the setup token link (§9.3, D-293; supersedes the first-browser-registration race in part), plus every user an owner makes owner or an OIDC group grants it (§9.4); several owners allowed | everything, incl. making and removing owners; only owners see and change global settings (windows, budget, auth, users, backup) (D-149) |
| **manager** | granted per project | in that project, everything its task-manager can do (docs, goals, tasks, stages, priorities, order, question answers, stall decisions and rules for its sessions, proposing decisions, deciding agent proposals, messages to its task-manager), plus **forcing an immediate run or resume when usage doesn't allow it**: run-now / work-on-now (§5.4) and messages that wake the task-manager at once (F14) (D-184, D-186, D-190); never global settings |
| **editor** | granted per project | the same as a manager in that project, except that it can't force anything immediate: its run requests and messages wait for the next regular run (D-184, D-186, D-190) |
| **viewer** | granted per project | read-only: that project's stages, questions (seen in the inbox, but not answerable, D-196), decisions, hand-offs, effective prompts, its driven sessions, the session/week usage bars and "Holding until" (D-298) |
| *(none)* | every other registered login | nothing; sees "no access yet" (default for new users, D-149) |

**Machine users (D-195).** Interactive Claude sessions that use the MCP server on the user's request act as a separate user per **machine**: a host or VM with Claude Code installed. Every machine whose Claude Code has the AFClaude MCP server registered is one machine user (`users.kind = machine`), labelled with its hostname at registration and renamable by owners; all interactive sessions on that machine share it. A machine user has no dashboard login; it is identified by a per-machine token issued at registration (§9.7). It gets grants like any user (viewer, editor or manager per project) and then has exactly that role's rights through MCP. A newly registered machine user has **no access until an owner grants it** a role; the setup wizard offers to grant this host's machine user one (§10.4) (D-195). A machine user can be granted any role, including owner (D-198).

Session and component roles:

| role | who | can |
|---|---|---|
| **task-manager** | the project's one task-manager session (`projects.manager_session`, D-189), AFClaude's own project included | in its project: write the docs, goals and progress, add/edit/order tasks, ask questions (to the owners and the project's managers and editors; the project's viewers see them read-only, D-196), propose decisions, write its hand-offs, reply to messages (F14), review its agents' proposals and merge their branches (§7.9); never owner-only actions (D-160) |
| **agent** | every other AFClaude session working for a project (task and worker sessions, subagents told apart from their task-manager) | only the direct writes of §9.6 (own hand-offs, its task's status and notes, failure escalations, its own branch, D-185); everything else becomes a proposal to its project's task-manager (§7.9, D-182) |
| **runner** | the runner daemon's components | the state changes the runners own (task start/finish, run log, telemetry), audited as `runner:<component>` |

- **The only difference between manager and editor is forcing an immediate run** (D-190). Owners, managers and editors can all decide agent proposals in the dashboard (F9). Further differences, *proposals* for the owner:
  - Managers can grant and revoke **editor** and **viewer** access to their project (not manager or owner; those stay with owners) (D-192).
  - Managers and editors both **make** (confirm) project-scoped decisions; task-managers only **propose** them (§7.6, D-192). Install-wide decisions are confirmed by owners only.
- The last owner that isn't group-derived can't be removed or demoted, so a group change at the OIDC provider can't leave the install without an owner (D-183).
- Granting **editor** or **manager** on a project lets that user steer autonomous sessions that run with the guard hooks bypassed on the execution host (an answer, a stage or a message becomes a session's input). The grant dialog says so.
- **Choosing the work is algorithmic** (D-189): which project runs and which task or stage inside it is picked by the dispatcher, by project rank and stage priority (D-064); people steer it through the ranks, the priorities and run-now (§5.4). The project's task-manager then does the run and delegates to agents.
- **Questions (D-196):** a project's questions (incl. blocked tasks and decision proposals) go to the owners and the project's managers and editors, who can answer them; the project's viewers see them too, read-only, without an answer box.
- **Usage visibility (D-298):** the session/week usage bars and "Holding until" (F1) are visible to every grant on a project (manager, editor, viewer), not owner-only; the split between AFClaude's and the owner's own use, the window planner (F4) and the reserve-threshold numbers stay owner-only (D-149), since those are about the owner's own Claude account and budget, not a project's work.
- **Interactive sessions via MCP** (D-068, D-189, D-195): any interactive Claude session (one AFClaude didn't start) can add, remove and manage tasks through the MCP server, but only on the user's explicit request. Such a session acts as its machine's machine user (above), with that machine user's grants, never as an agent or a task-manager, and never as the first owner or another person.
- The audit actor is the user (`user:<id>`), the machine user (`machine:<id>`; the audit row also records the MCP session id when known), the task-manager session (`task-manager:<session>`), the agent session (`agent:<session>`) or the component (`runner:dispatcher`).
- v1 enforcement of the task-manager and agent roles is advisory (D-160, D-173): rejected attempts are audited and raise an alert; see §9.6 for its limits.

