# Phase 7: writes from the dashboard (implementation plan)

Plan for design §13 steps 7a (work writes) and 7b (control writes), with the parts of §2 (roles), §3 (F1–F4, F7–F10, F12–F14 and the UX rules of §3.1), §5.1 (wake), §5.4 (run-now), §6 (windows), §7.3 (one write path), §7.4 (the settings registry), §7.6 (prompts, decisions), §7.9 (agent proposals), §8 (the write table, errors, idempotency, live state), §9.5 (high-impact writes) and §9.8 (CSRF, size and rate limits) that they build. Every write is one typed endpoint in 6a's endpoint table calling one audited action in `actions.py` (D-154). Plan only; each step in §5 is one worker branch.

Binding inputs: D-154 (one write path with audit), D-184 / D-190 / D-192 (manager = the task-manager's project rights plus forcing; editor = the same without forcing; managers grant editor/viewer; editors and managers make project decisions), D-186 (owners' and managers' messages wake the task-manager, editors' wait), D-241 / D-252 (owner messages are triaged by Haiku; no triage while the task-manager is working), D-244 (a held "now" message waits until the next session-window start; inbox failures pause that project), D-246 / D-248 / D-250 (the box: loading → checkmark or chat; cleared once included; queued messages in a collapsed drop-up list), D-178 (the box grows into a chat, resets after `ui_chat_reset_hours`), D-196 (viewers see questions without an answer box), D-153 / D-036 / D-173 (run-now skips window and budget, starts at once, works while paused), D-282 (a failed run-now alerts and pauses until the pre-release step), D-283 (the task-manager may clear a pause), D-034 (windows per week or per day, linked editing), D-075 / D-146 (every setting in the DB registry with default, reset per setting / section / all with a warning and undo, an explanation), D-152 / D-193 / D-197 / D-169 (prompts system-wide with owner-only overrides, per-project variables set by managers and editors, defaults in code), D-104 / D-158 / D-176 (decisions need a human; only the current entry), D-182 / D-185 / D-188 (agent proposals reviewed by the task-manager; humans may decide them from F9, D-190), D-064 (project rank, stage priorities, whole project to medium), D-230 / D-249 (simple numbers, tooltips, two inputs per project, no filler text), D-211 (preview after every UI step), D-209 (typed API, mypy --strict and TS strict), D-235 (fail fast), D-263 / D-279 (owner questions in plain words), D-299 (stalled sessions belong to no project and are owner-only; a convenience on top of AFClaude, not a project feature).

## 0. Already decided

- **One endpoint, one action, one transaction** (D-154, §8). Where a write needs two effects (an instruction that also runs now, a section reset), it is one new action, never two `perform()` calls in a handler. Reads the handler does afterwards to return "the new state the view needs" are outside the transaction.
- **Request rows are the only live channel.** Every write the runner must act on records intent and wakes the runner (§5.1); the dashboard never executes anything. Run-now, backups, exports, the point-in-time rollback and on-demand host checks are all `action_requests` kinds, so the client has one polling path (`GET requests/{id}`, 6c) and one card (queued → started / refused / failed). Messages are polled through their thread (`GET messages`).
- **Polling, not server-sent events** (§8 *proposal*, kept): every 3 s while a request or message is open, stopping at a final state or when the view is hidden (`visibilitychange`); a 60-s cap after which the card says "still waiting" and offers a manual refresh.
- **Two inputs per project, two actions** (D-230 / D-178): the instruction box writes a task of the new kind `instruction` (new action `task.instruct`, §3.3); the question box writes an inbox message (`inbox.post`). Never one merged box.
- **An editor's message never wakes the task-manager** (D-186) and skips the Haiku triage: it is noted for the next start, or goes straight in when the task-manager is working anyway (D-252, design §3 F14 "a task-manager that is running anyway gets it at its next idle point"). Owners' and managers' messages are triaged as built (D-241). *Interpretation, 8a checks it.*
- **The high-impact list is §9.5's**, nothing more: prompt overrides, project-scope "always continue" rules, unpause, window changes, run-now, decision confirmation (incl. a human's direct add or edit of a decision, which makes it active the same way), users/grants/roles, machine codes and token revocation, auth settings, host settings, backup/export download, restore and rollback. Each gets step-up (6b P7's `Endpoint.step_up`), a confirm dialog and an alert entry. Answers, instructions and messages are session input too (§9.1) but stay without step-up as the design lists them; a waking message is triaged (D-241), run-now is not. *Interpretation, 8a checks it.*
- **Project scope per write comes from 5a's resolvers** (task → project, request → its target's project). Global writes (settings, windows, prompts, project creation and rank, `review_now`, users beyond D-192, backup) are owner-only (D-149, 5a §0). `project.manager_launch` stays owner-only as built (D-260: the task-manager itself can't change it, so D-184 doesn't give it to managers).
- **Stalled sessions have no project and are owner-only** (D-299, answers question 1): a stalled session is not resolved to "the project whose path contains its cwd" — it belongs to no project at all, "like random other sessions, just a convenience slapped on top of AFClaude" in the owner's words, confirming D-041/D-206 rather than the design text's "every project's managers and editors decide the stalls of sessions inside their project's folder". Every stall write (decide, clear, standing rules) and a stalled session's `continue_now` are owner-only, full stop; §2.2 and §3 below reflect that. **This conflicts with two other plans' text, flagged, not resolved here:** phase5a_plan.md §3.4 still lists "request → target project or the stalled session's project by cwd" as a project-scope resolver, and phase6c_plan.md's `stalls` endpoint (§3.1) still reads "per project; owners for the rest" and includes `project` in the row payload. Both assume stalled sessions can resolve to a project, which D-299 rules out.
- **Prompt overrides are system-wide** (D-193 over D-152's prompt part): §8's `prompts/{name}?project=` is not built; a project only fills variables (D-197). Phase 4 P5 corrects the design text.
- **No direct doc edits from the dashboard.** §8's write table has none, and goals reach a project through the instruction box (D-230); the task-manager keeps the docs (D-182's reason). Adding a goals editor later is one endpoint on the built `doc.set`.
- **Restore stays a setup-only path** (§11.3, 5c/5d); 7b offers backup, export, download and the rollback to a moment, not a restore into a running install.

## 1. Today (as-is inventory)

| write | today | entry points | role check |
|---|---|---|---|
| answer a question / blocked task | `task.answer` (dashboard double-submit is a no-op) | `tasks.py answer`, MCP `afclaude_answer_task` | none (5a) |
| tasks: add, edit, priority, move, block, cancel, reopen, start, finish, update | `task.*` | `tasks.py`, MCP `afclaude_add_task` / `afclaude_update_task` | none |
| questions | `task.ask` | MCP `afclaude_ask` | none |
| projects: add, edit, rank, whole-project priority, launch model | `project.add/edit/move/priority`, `project.manager_launch` (owner-only) | `tasks.py project …`, MCP `afclaude_project` | launch only |
| stalls and rules | `session.decide/clear`, `rule.add/remove` (owner-only) | `tasks.py decide|rule`, MCP `afclaude_decide_session` / `afclaude_rule` | owner-only refuses autonomous MCP |
| messages | `inbox.post` (owner-only, target = the AFClaude task-manager session), `inbox.retry`, `inbox.reply`; runner steps `triage/hold/deliver/include/fail` | temp dashboard box (`inbox_api.py`: `POST /afclaude/api/inbox`, `…/<id>/retry`, key header), `tasks.py inbox reply|retry` | key header = owner |
| pause | `automation.set`, `automation.set_project` (owner-only) | `tasks.py automation pause|resume [--project]` | owner-only |
| settings, window | `setting.set/reset/import`, `window.set` (owner-only); registry `actions.SETTINGS` with section, type, important, doc, bounds | no CLI or MCP (only the importer) | owner-only |
| prompts | `prompt.set/reset`, `prompt_var.set/reset/migrate` (owner-only) | `prompts.py set-var|reset-var|migrate`; no override CLI yet (4a P4) | owner-only |
| run-now | `request.add` (owner-only; kinds `continue_now`, `review_now`, `work_on_now`; one open per kind+target), `request.start/finish` (runner) | none yet: the consumer, `tasks.py run-now` and MCP `afclaude_request` are 3b S5 | owner-only |
| decisions | `decision.import`, `decision.keywords` only | `tasks.py decision keywords`, MCP `afclaude_decisions` (read) | none |
| agent proposals, users, grants, machines | not built (5a A1/A4) | — | — |
| undo, section reset | not built (`setting.reset` is per key; audit rows keep the full before/after of settings, windows, prompts and rules) | — | — |
| dashboard API | 6a W1/W2: endpoint table, write guard (JSON, `Origin`, `Sec-Fetch-Site`), 64 KiB body cap, `Idempotency-Key` on `idempotent` endpoints, the error shape incl. `step_up_required` / `conflict`, `health`, `schema.json`; no session, no role, no write endpoint | `dashboard/` | every non-public route 401s |
| runner wake | `runner.py wake` (datagram, 60-s backstop) | no action sends it yet | — |

**Held runner branches (3a S2–S6, `runner-s6` contains S2–S5).** `git diff main...runner-s6 -- actions.py` adds **no new registered action**: it adds eight `runner_timeout_*` settings (`at`, `watch`, `run_readings`, `compaction_check`, `last_stretch`, `deferred_start`, `fillup`, `compaction`), raises four defaults and the minimum of `runner_timeout_window_start` (70 → 80), extends the at-job payload (`on_interrupt`, `timeout`) checked in `job_put`, and lets `run_begin` claim a scheduled job; the log component `runner_shadow` and the state component `keepalive.watch` are new. For phase 7 this means: the Settings page must render whatever the registry holds (no fixed key list in code or tests), and every phase-7 edit of `actions.py` waits for the 3a merge like 5a and 6a.

**Not covered by the earlier plans, added here:** `task.instruct` (the instruction box), `setting.reset_many` (section / all reset), `audit.undo` (D-075's undo), `user.invite` (a person user without OIDC), the request kinds `backup_now`, `export_now`, `rollback`, `host_check`, per-actor idempotency replay, the post-commit wake and the high-impact alert entry in `perform()`.

## 2. The writes

Roles: **O** owners, **M** the project's managers, **E** its editors (E+ = editors, managers, owners). Paths are under `/api/v1`. Every write carries an `Idempotency-Key` (a UUID per user action, reused on retry) and, where the target has one, the `version` it saw (§8). "Needs" names the step that must land first besides 6a W5/W9 (sessions, CSRF token, role per endpoint), 5a A3 (the permission matrix, `user:<id>`) and 6c (the read endpoint the view re-renders from), which every row needs.

### 2.1 Step 7a: work writes

| write | endpoint | action | role | step-up | idempotency / conflict | live feedback | needs |
|---|---|---|---|---|---|---|---|
| answer a question or a blocked task | `POST tasks/{id}/answer` | `task.answer` (built) | E+ (D-196); a question with no project: O | no | 409 unless blocked (`current` = the task); same answer again = no-op (built) | returns the new inbox | — |
| decide a decision proposal (a question carrying a decision) | `POST tasks/{id}/proposal` {approve \| edit \| reject, payload?, note?} | `task.answer` as 4b Q3 extends it (applies `decision.add/edit/confirm` in the same transaction) | E+ for a project decision, O install-wide (D-192) | approve / edit yes, reject no | the question's and the decision's version | new inbox; F7 | 4b Q1–Q3, 6b P7 |
| instruction (F1 input a) | `POST projects/{id}/instructions` {text, now} | **new** `task.instruct` (§3.3) | E+; `now` M+ (D-184/D-190) | only with `now` | key; `now` reuses the project's open `work_on_now` | the task; with `now` the request card (§2.2 run-now) | `now`: 3b S5 |
| message to the task-manager (F1 input b, F14) | `POST projects/{id}/messages` {text} | `inbox.post` (4b Q4: project + thread; 7a: wake by role, §3.4) | E+ | no | key; per-user rate limit (`inbox.RATE_MAX`/h) | thread poll: loading → checkmark (noted, D-246) → gone once included (D-248); delivered → chat on answer; "held until <start>" (D-244); error + Retry | 4b Q4, 3a S7 |
| retry a failed message | `POST messages/{id}/retry` | `inbox.retry` (built) | the sender, or M+ of its project | no | 409 unless `error` | as above | 4b Q4 |
| add task | `POST tasks` | `task.add` | E+ | no | key | the project's task list | — |
| edit task (title, description, priority, `estimated_sessions`, project) | `PATCH tasks/{id}` | `task.edit` | E+ (a project change: E+ in both) | no | version | the task | — |
| stage priority / stage position | `POST tasks/{id}/priority`, `POST tasks/{id}/move` | `task.priority`, `task.move` | E+ | no | version | F3 | — |
| cancel / reopen | `POST tasks/{id}/cancel`, `…/reopen` | `task.cancel`, `task.reopen` | E+ | no | version; 409 on state | F3 | — |
| add project | `POST projects` | `project.add` | O | no | key; unique name | F3 (the task-manager session follows at its first start, D-281) | — |
| edit project: name, description | `PATCH projects/{id}` | `project.edit` | E+ | no | version | F3 | — |
| edit project: path, task-manager session | `PATCH projects/{id}/placement` | `project.edit` | O | yes (the path is where sessions run with the guards bypassed) | version | F3 | 6b P7 |
| project rank | `POST projects/{id}/move` | `project.move` | O (cross-project order) | no | version | F3 | — |
| whole project to a priority | `POST projects/{id}/priority` | `project.priority` | E+ (D-064) | no | naturally idempotent | F3 | — |
| add / edit a decision | `POST decisions`, `PATCH decisions/{id}` | `decision.add`, `decision.edit` (4b Q1) | E+ project, O install-wide | yes | key / version | F7 | 4b Q1, 6b P7 |
| expire / delete a decision | `POST decisions/{id}/expire`, `DELETE decisions/{id}` | `decision.expire`, `decision.delete` | as above | yes | version | F7 | 4b Q1 |
| decision links, keywords | `POST decisions/{id}/links`, `PUT decisions/{id}/keywords` | `decision.link`, `decision.keywords` (built) | as above | no | unique link; version | F7 | 4b Q1 |
| review an agent proposal | `POST proposals/{id}/review` {approve \| edit \| reject, note, payload?} | `proposal.review` (5a A4) | E+ (D-190) | when the proposal's action is high-impact (a decision) | 409 unless `proposed`; the payload's version | F9 queue | 5a A4 |

### 2.2 Step 7b: control writes

| write | endpoint | action | role | step-up | idempotency / conflict | live feedback | needs |
|---|---|---|---|---|---|---|---|
| stall decision (continue / ignore) | `PUT stalls/{session}/decision` {decision, stall_ref, note} | `session.decide` (built) | O (D-299) | no | `stall_ref`: 409 if it stalled again; version | F2 | — |
| clear a stall decision | `DELETE stalls/{session}/decision` | `session.clear` | O (D-299) | no | no-op if none | F2 | — |
| standing rule add / remove | `POST rules`, `DELETE rules/{id}` | `rule.add`, `rule.remove` (built) | O (D-299: a rule's cwd is not a project scope) | an "always continue" rule: yes | same scope + match replaces; version | F2 | 6b P7 |
| run now | `POST requests` {kind, target} | `request.add` | `work_on_now`: O + M (D-036/D-153/D-190); `continue_now`: O only (D-299: a stalled session has no project, so no manager can hold this); `review_now`: O | yes | one open per (kind, target): returns it | request card polls `requests/{id}`: open → started (tmux name, RC link) / refused (reason) / failed (reason, pause shown, D-282); the confirm dialog shows session % and week headroom (§5.4) | 3b S5, 6b P7 |
| pause / resume all | `PUT automation` {paused, reason} | `automation.set` | O | resume yes, pause no | idempotent | top bar dot and toggle | — |
| pause / resume a project | `PUT projects/{id}/automation` | `automation.set_project` | M+ (§3.6) | resume yes | idempotent | F3, top bar | — |
| window (F4) | `PUT settings/window` {day, start, n, mode, group?} | `window.set` (built) | O | yes | version of `window_days` | the 7-night preview re-read; runner woken | 6b P7 |
| window preview (no write) | `POST settings/window/preview` | none (`schedule.py` over the candidate value) | O | no | — | the 7 nights incl. DST and the weekly reset | — |
| prompt override set / reset | `PUT prompts/{name}`, `DELETE prompts/{name}` | `prompt.set`, `prompt.reset` (built; 4a P4 test-renders every bundle) | O (D-193) | yes | version; `base_outdated` shown as a three-way view | F8 | 4a P4, 6b P7 |
| prompt validate (no write) | `POST prompts/{name}/validate` | none (`check_prompt` + bundle render) | O | no | — | errors inline | 4a P4 |
| prompt variable set / reset | `PUT projects/{id}/prompt-vars/{name}`, `DELETE …` | `prompt_var.set`, `prompt_var.reset` (built) | E+ (D-197) | no | version | F8 rendered per project | 4a P3 |
| setting set / reset | `PUT settings/{key}`, `DELETE settings/{key}` | `setting.set`, `setting.reset` (built) | O | `auth_*`, host and backup keys (§9.5) | version | F10 | — |
| reset a section / all | `POST settings/reset` {section \| all, versions} | **new** `setting.reset_many` | O | if the set holds a step-up key | `versions` = what the warning showed; any change since → 409 | warning from `POST settings/reset/preview` (no write); then a one-tap undo | — |
| undo | `POST audit/{id}/undo` | **new** `audit.undo` (§3.7) | the original action's role | the original's | the current state must equal the row's `after`, else 409 | F12, the undo toast | — |
| own display settings (time zone, theme, verbose tooltips D-249) | `PUT me/settings/{key}` | the per-user setting action of 6a W7 / 6b P8, extended | self | no | version | the app | 6a W7 |
| grant / revoke access | `POST grants`, `DELETE grants/{id}` | `grant.set`, `grant.remove` (5a A1) | O; M: editor/viewer on their project (D-192) | yes | unique; last-owner guard → 409 (D-183) | F10 | 5a A1, 6b P7 |
| invite a person | `POST users` {label} | **new** `user.invite` (§3.8) | O | yes | key | the one-time enrolment code shown once | 6a W7, 6b P8 |
| rename / revoke a user or machine; reset someone's login | `PATCH users/{id}`, `POST users/{id}/revoke`, `POST users/{id}/auth-reset` | `user.rename`, `user.revoke` (5a A1), `auth.reset_user` (6b P8) | O | rename no; revoke, reset yes | version; last-owner guard | F10 | 5a A1, 6b P8 |
| machine registration code (new or re-issue) | `POST machines/codes` {machine?} | `machine.code` (5a A1) | O | yes | key | the code shown once, held in component memory only | 5a A1 |
| backup now / export now | `POST requests` {kind: backup_now \| export_now} | `request.add`, **new kinds** | O | export yes, backup no | one open per kind | request card; the finished row links its download | 5c B3, 5d export |
| download a backup or export | `POST backups/{id}/download-ticket`, then `GET backups/{id}/download` | **new** `backup.download` (audited, alerted) | O | yes | single-use ticket in the session row, 60 s, never in the URL | the browser's download | 5c B3 |
| rollback to a moment | `POST requests` {kind: rollback, at} | `request.add`, **new kind** | O | yes + the time typed again | one open | request card; the result after the restart from `meta.restored_from` (§3.9) | 5c B5 |
| run host checks | `POST requests` {kind: host_check, name?} | `request.add`, **new kind** | O | no | one open | request card; F13 re-read | 5b K6 |

## 3. Design details

### 3.1 The write plumbing (X0)
- **One generic handler** per write endpoint: principal from the session (`user:<id>`, via `dashboard`), the endpoint's request model → action params, `actions.perform(…, key=<Idempotency-Key>)` in a thread, then the endpoint's read for the response. Action errors map to the one error shape: `Forbidden` → 403 `forbidden`; `store.NotFound` → 404; `Conflict` / `InvalidTransition` → 409 `conflict` with `details.current` (passed through the endpoint's response model for that row type, so a conflict never shows fields the reader's role can't read); `ValueError` → 422 `validation` with the action's message (action messages are user-facing: a test runs every write endpoint's error cases and checks that no path, SQL or traceback appears); `store.DBError` → 503 (built).
- **Idempotency per actor.** `idempotency_keys` is keyed by the key alone; a hit by a different actor is a 409, never a replay of another user's stored response (small change in `_perform`, tested). The client makes the key per user action and reuses it on retry and after step-up (6b P7).
- **Write rate limit** (§9.8: 30 / min per user) on `auth_failures`' pattern (one counter table, additive), 429 `rate_limited`; the message box keeps its own hourly cap.
- **Size limits** from `actions.DASHBOARD_LIMITS` mirrored as `max_length` in the pydantic models, so the schema and the TS types carry them; a test keeps the two equal.
- **Wake after commit:** actions the runner acts on (`request.add`, `inbox.post`, `task.instruct` with `now`, `window.set`, `automation.*`, `session.decide`, `rule.*`, `proposal.review`) declare `wake=True`; `perform()` sends `runner.wake()` after the commit (one place; unless 3b S5 has already put it there). A lost datagram is caught by the runner's 60-s backstop and logged, never an error to the user.
- **409 in the UI** (7a): the card shows the current row next to the user's edit with "apply mine onto it" (resend with the new version) or "drop mine"; never an automatic overwrite.

### 3.2 High-impact writes (Y0)
`Endpoint.step_up` (6b P7) set per §2's column; `Endpoint.confirm` carries the consequence text the client shows (D-230: noun-style title, the explanation in the dialog body, e.g. "starts the task-manager now, outside the window and the budget"). `Spec.high_impact` in `actions.py` makes `_perform` append an alert entry (actor, action, target; never the payload text) to the alerts doc of the target's project, or AFClaude's for install-wide writes, in the same transaction. The route-table test asserts that every endpoint whose action is on the §9.5 list has `step_up=True` and a confirm text.

### 3.3 Instructions (`task.instruct`, X1)
`task.instruct(project, text, now=False)`: one task of the new kind `instruction` (`store.TASK_KINDS`; the column has no CHECK, so no migration), title = the first line cut to 200 characters, description = the full text (≤ 8 KB), `created_by` the user; with `now`, in the same transaction, `work_on_now` for the project exactly as `request.add` (one open per project). A pending instruction is a ready task, so 3b S4's start prefers its project in rank order. The task-manager's continue bundle (4a) gets a line: open instructions first, turned into tasks or goals and finished with a note of what was made. F1 lists a project's open instructions as important (D-230).

### 3.4 Messages (X2)
- `inbox.post(project, text)` after 4b Q4 (thread, project); 7a adds the sender's role: O / M → triage as built (D-241; D-252 straight in while the task-manager is working); E → no triage: `noted`, or straight in while the task-manager is working (§0). Viewers can't post.
- The box (D-246/D-248/D-250, D-230 tooltips): a plain text box; after sending a spinner, then a checkmark (noted) or the thread expanding once delivered and answered; a noted message disappears once `included`; queued (noted) messages sit in a collapsed list that opens upward over the content, scrollable; a held message shows "held until <start>" (D-244); an error shows a red mark with the reason and Retry. After `ui_chat_reset_hours` without a message the box is empty again; closed threads under "earlier messages".
- The message is session input for a task-manager that runs with the guards bypassed (§2): the 8 KB answer limit and the hourly cap apply; the triage prompt is not a security boundary (8c tests injection through it).

### 3.5 Run-now and the request card (Y2)
The confirm dialog shows the target, what is skipped (window, budget) and the current session % and weekly headroom from the status snapshot; above `session_stop_pct` it warns that the session limit is nearly used up (§5.4). The card polls `requests/{id}` (§0); `started` shows the tmux name and the RC link when known (F6), `refused` the reason (running, at its limit until …, the owner holds it), `failed` the reason and the automation pause it caused (D-282) with the owner's resume button. Until the pre-release step (design §13 8d) a failed run-now pauses; after it, the card shows the failure only.

### 3.6 Pause
Install-wide pause: owner-only (global, D-149). A project's pause: its managers and owners (*proposal*: pausing and clearing a project's pause is within the task-manager's own rights since D-283, so D-184 gives it to managers; editors don't get it because a resume lets the project's runs start again). Resuming shows the pause reason and needs step-up (§9.5 "unpause"). Pausing never stops a running session (§5.1).

### 3.7 Settings page, section reset and undo (Y6)
- The page is rendered from `actions.settings()` (section, type, important, doc, bounds/choices, value, default, source); keys with their own control (`window_days` → F4, `automation_paused` / `_reason` → the pause toggle) are marked in the registry (`Setting.own_ui`) and shown read-only with a link. The important ones (pause, tonight's window, `last_mile_hours`, the budget mode, D-075) also sit on the main views with the same endpoints. Section names come from `actions.SECTIONS`; the section list in design §7.4 (auth, host, backup, display) grows as 5b/5c/6a add keys.
- `setting.reset_many(section | all, versions)`: every listed key to its default in one transaction, one audit row with the full before values; refused (409) if any version differs from what the warning listed. The warning lists each key with its current and default value and what it can affect (the doc line).
- `audit.undo(audit_id)`: supported for `setting.set/reset/reset_many`, `window.set`, `prompt.set/reset`, `prompt_var.set/reset`, `rule.add/remove` (their audit rows hold the full before/after); it writes the row's `before` through the same action path (same role check, same step-up, its own audit row naming the undone row), and refuses when the current state differs from the row's `after` (something changed since). Other actions show no undo button (F12). An undo can itself be undone (redo).

### 3.8 Users, grants, machines (Y7)
- `user.invite(label)`: a person user with no grant and a one-time enrolment code (the 6a W7 / `reset-auth` code form: single-use, 24 h, only its hash stored), shown once; the invitee opens `/setup` with it and enrols password + TOTP or a hardware passkey (6b). OIDC users need no invite (6b P5 creates them).
- Grants: managers see only their project's editor/viewer grants; the grant dialog says that editors and managers steer sessions that run with the guards bypassed (§2). Group-derived grants (`oidc:<group>`) are shown read-only.
- Registration codes and the enrolment code are returned once and held in the component's memory until the dialog closes; nothing in web storage (§9.8). The machine token itself never reaches the browser (the machine exchanges the code, §9.7).

### 3.9 Backup, export, rollback, host checks (Y8)
`request.add` gains the kinds `backup_now`, `export_now`, `rollback` (target = an ISO time inside the WAL window) and `host_check` (target = a check name or none); the runner handlers call 5c's `create`, 5d's export, 5c B5's rollback and 5b's `hostcheck.run`. Downloads: `backup.download` (step-up, audited, alerted) stores a single-use ticket in the session row; the plain authenticated `GET` then streams the file once (`Content-Disposition: attachment`, `no-store`). A rollback replaces the DB, so its own request row is gone afterwards: the web process exits on the write fence and restarts (5c B5), and the page shows the result from `meta.restored_from` and the restored install's pause.

## 4. Order and dependencies

- **All of phase 7 waits for 6a W5 (sessions, CSRF token, endpoint roles), W8/W9 and 5a A3 (the permission matrix with `user:<id>`).** Each step that edits `actions.py` also waits for the 3a merge (S2–S6) and runs alone against `actions.py` / `store.py` (5a's rule), built in a worktree, nothing deployed before 8b (D-072/D-157).
- **7a:** X0 first. X1 (answers, instructions) after X0; the `now` toggle stays hidden until 3b S5 and 6b P7. X2 (messages) needs 4b Q4 and 3a S7 (the inbox job in the runner; 5b moves the inbox API into the web service, the temp box keeps working until §10.7). X3 (tasks, projects) after X0. X4 (decisions) after 4b Q1–Q3 and 6b P7. X5 (proposals) after 5a A4. X6 (409 UI, preview) with each view.
- **7b:** Y0 after 6b P7. Y1 (stalls, rules; owner-only, D-299) has no role-matrix dependency beyond Y0. Y2 (run-now) after 3b S5 and 6c's `requests/{id}`. Y3 (pause), Y4 (window, `schedule.py` built), Y6 (settings, reset, undo) after Y0. Y5 (prompts) after 4a P3/P4. Y7 (users) after 5a A1, 6a W7, 6b P8. Y8 after 5c B3/B5, 5d, 5b K6. Y9 last.
- 6c's read views come first for every write (each write returns into a view 6c built). The preview (D-211) is redeployed after X6 and after Y9.

## 5. Steps

**X0: write plumbing.**
- **Build:** §3.1 (generic write handler, error mapping, per-actor idempotency, write rate limit, `max_length` from `DASHBOARD_LIMITS`, `wake=True` + post-commit wake); the TS client's write helper (key per action, reuse on retry, `version` passing, error-code states) and the 409 component.
- **Tests:** each error class → its code and shape; a replayed key returns the stored result once, another actor's key is a 409; 31st write in a minute → 429; the limits equal in the model and `DASHBOARD_LIMITS`; a wake is sent after commit and not on a refused or failed write (fake socket); TS: retry keeps the key, a 409 renders the current row.
- **Done when:** `test_dashboard`, `test_actions`, `test_typing`, `check_public` green; tsc strict and the node tests green.
- **Risk:** a `ValueError` text that carries internals; the error-case test covers every endpoint's action.

**X1: answers and instructions.** `tasks/{id}/answer`, `projects/{id}/instructions`; `task.instruct` and the `instruction` kind; the continue-bundle line (4a); F1's two input boxes. **Tests:** viewer gets no box and a 403; an editor's `now` → 403; `now` writes task + request in one transaction (a failure leaves neither); the inbox response after an answer; component tests per role. **Done when:** as X0 plus `test_mcp_server` (the MCP task tools see the new kind).

**X2: the message box.** §3.4; `projects/{id}/messages`, `messages/{id}/retry`; the box component and the thread poll. **Tests:** owner and manager messages triaged, an editor's noted or straight in while working, never woken; the hourly cap per user; every state of the box (spinner, checkmark, cleared on include, chat on answer, held until, error + retry, reset after `ui_chat_reset_hours`); polling stops when the view is hidden. **Risk:** two inboxes (temp box and dashboard) during the transition write the same table through the same action; the temp box's actor stays `owner`.

**X3: tasks and projects.** The task and project endpoints of §2.1, F3's controls (priority chip cycling, up/down and "move to position…", whole project to medium, add task). **Tests:** the role matrix per endpoint (E+ / O-only placement and rank); version conflicts on each; a project-change edit needs E+ in both projects.

**X4: decisions.** The decision endpoints and `tasks/{id}/proposal`; F7's propose / confirm / reject / edit, the topic shown alone in the confirm dialog (D-176). **Tests:** project vs install-wide roles (D-192); step-up on add, edit, confirm, expire, delete; approve applies the decision and closes the question atomically.

**X5: agent proposals.** `proposals/{id}/review`, F9's queue with run summaries (the task-manager's postpone stays MCP-only). **Tests:** E+ only; a decision payload needs step-up; stale payload version → 409; the task-manager sees the human's decision in its next turn (5a A5's notice).

**X6: preview.** The mock transport answers every 7a write with typed fixtures (state changes held in memory for the session, `connect-src 'none'` stays); preview redeployed (D-211).

**Y0: high-impact mechanism.** §3.2: `step_up` and `confirm` on the endpoints, `Spec.high_impact` and the alert entry. **Tests:** the route-table check against the §9.5 list; one alert entry per high-impact write, none for a refused one; a step-up retry applies once.

**Y1: stalls and rules.** F2's Continue / Ignore / "Always…" (preselecting the most specific cwd) / clear; owner-only throughout (D-299). **Tests:** `stall_ref` 409; an "always continue" rule needs step-up; every stall write is refused for a non-owner, regardless of the session's cwd.

**Y2: run-now.** §3.5; the Run now / Work on now buttons (F2, F3) and the request card. **Tests:** kinds × roles (editors 403, `review_now` owner-only); step-up; the card through every state against a fake runner; works while paused; the warning above `session_stop_pct`.

**Y3: pause.** The top-bar toggle and the project pause (§3.6). **Tests:** resume needs step-up, pause doesn't; the reason shown; a project manager can't touch the install-wide pause.

**Y4: window editor.** F4: seven rows, linked colours, change all linked / only this day / whole week / link to a group, the preview endpoint, the measured ratios (read). **Tests:** each mode against `window.set`; overlap error names the day; DST nights in the preview; the runner woken.

**Y5: prompt editor.** F8: edit, validate, save, reset, diff and the three-way view when `base_outdated`; per-project variables. **Tests:** placeholders enforced; a failing render blocks the save; owner-only overrides vs E+ variables.

**Y6: settings, section reset, undo.** §3.7; F10's sections and F12's undo; per-user display settings. **Tests:** every registry key renders (no fixed list: the runner-s6 keys appear when merged); `reset_many` 409 on a changed key; undo of each supported action and the 409 when changed since; redo.

**Y7: users, grants, machines.** §3.8 in F10. **Tests:** managers grant only editor/viewer on their project; the last-owner guard; the code shown once and absent from any later read; the invite code single-use and expiring.

**Y8: backup, export, rollback, host checks.** §3.9 in F10 and F13. **Tests:** ticket single-use, bound to the session, expiring; a second download without a new ticket 403; the new request kinds and their roles; the rollback's result read after a simulated restart.

**Y9: docs.** Design §8's write table (the endpoints as built, `prompts/{name}` without `?project=`, the new request kinds), §3 F14 (editors and the triage), §9.5 (the list as marked), §13 7a/7b status; README file table; the mypy list for any new module. Preview redeployed (D-211); from 8a on, review happens on the 8b deployment.

## 6. Questions for the owner

None open. D-299 answers the former question 1: stalled sessions belong to no project and are owner-only throughout, not a per-project manager/editor right; see §0.
