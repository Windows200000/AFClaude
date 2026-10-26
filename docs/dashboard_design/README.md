# AFClaude dashboard and backend: target design (v2)

Status: target design for phases 2–9, rewritten on 04.10.2026 after the owner's architecture review (D-143/D-145; answers D-146–D-199), updated through D-230 (08.10.2026: Python backend with a TypeScript dashboard frontend, strict type checking for security, D-208/D-209; non-blocking owner previews of the dashboard UI from phase 6a on, D-211; 13.10.2026: the final UI's content rules — simple numbers, instant tooltips, collapsed task lists, the lineup, two project inputs, no filler text, D-230). Phase 1 (schema v4, `actions.py`, `afclaude_config.py`) is built. This doc replaces v1 as a whole; where v1 and this doc differ, this doc holds. `D-NNN` ids point at the owner's local decision store; owner decisions are binding, everything marked *proposal* is the task-manager's and may be changed. Host names are placeholders.

**Terms (D-189).** A **task-manager** is the Claude session that runs a project: exactly one per project, frequently compacted (§5.5); there is no global task-manager, and AFClaude's own project simply has its task-manager like any other (`projects.manager_session` holds the task-manager's session id). A **manager** is a user role (§2). **Agents** are the worker sessions and subagents that work for a task-manager.

## Sections

Each section is its own file; section numbers and anchors are unchanged, so references like "design §7.7" resolve to §7.7 inside `07_data_model.md`.

| § | Title | File |
|---|---|---|
| 1 | Goals and non-goals | [`01_goals_and_non_goals.md`](01_goals_and_non_goals.md) |
| 2 | Users, roles and access | [`02_users_roles_and_access.md`](02_users_roles_and_access.md) |
| 3 | Main flows | [`03_main_flows.md`](03_main_flows.md) |
| 4 | Architecture | [`04_architecture.md`](04_architecture.md) |
| 5 | Runners | [`05_runners.md`](05_runners.md) |
| 6 | Schedule | [`06_schedule.md`](06_schedule.md) |
| 7 | Data model: everything in the DB | [`07_data_model.md`](07_data_model.md) |
| 8 | API surface | [`08_api_surface.md`](08_api_surface.md) |
| 9 | Security | [`09_security.md`](09_security.md) |
| 10 | Deployment | [`10_deployment.md`](10_deployment.md) |
| 11 | Backup, export, restore, move | [`11_backup_export_restore_move.md`](11_backup_export_restore_move.md) |
| 12 | Testing strategy | [`12_testing_strategy.md`](12_testing_strategy.md) |
| 13 | Phased build plan | [`13_phased_build_plan.md`](13_phased_build_plan.md) |
| 14 | Future | [`14_future.md`](14_future.md) |
