# Phase 5a: users, roles, the task-manager and agent roles (implementation plan)

Plan for design §2, §7.9, §9.4, §9.6, §9.7 / §13 step 5a: users and grants (owner, manager, editor, viewer), machine users for interactive MCP sessions, one role check in the one write path, task-manager and agent detection, agent proposals with run summaries delivered to the task-manager at once, and the prompts that go with it. Plan only: every step touches `store.py` / `actions.py`, which the held runner branches (3a S2–S6) also change, so nothing is built until those are merged (§4).

Binding inputs: D-149 / D-172 (first registration is owner; several owners; owner grantable through an OIDC group; only owners see global settings), D-183 (the last owner not granted by a group can't be removed), D-184 / D-190 / D-192 (manager = everything the task-manager can do in that project plus forcing an immediate run; editor = the same without forcing; managers grant editor and viewer; editors and managers make project decisions), D-186 (owners' and managers' messages wake the task-manager, editors' wait), D-196 (questions go to owners, managers and editors; viewers see them read-only), D-195 / D-198 (one machine user per machine, no access until granted, any role including owner; a session AFClaude started is always task-manager or agent whatever token it carries), D-160 / D-173 (the agent role, advisory: refused, audited, alerted), D-182 / D-185 (agent writes are proposals to the task-manager, except own hand-offs, own task status and notes, failure escalations, its own branch; the task-manager reviews a branch's changes itself before merging), D-188 / D-194 (proposals reach the task-manager at once, an idle one is woken), D-189 (one task-manager per project; choosing work is algorithmic), D-154 (one write path with audit), D-168 (8a validates roles, 8c tries role escalation), D-243 / D-272 (the task-manager sees what an agent changed, in a compact form), D-283 (the task-manager clears a pause after investigating), D-106 (autonomous deploys), D-209 (mypy --strict for roles and auth), D-235 (fail fast), D-263 / D-279 (owner questions in plain words), D-290 (this server's own Claude sessions get the owner role), D-291 (the usage review writes its report and prediction numbers directly; the task-manager still gets all its results).

## 0. Already decided

- **Enforcement is advisory in the sense of D-160/D-173:** a write outside the role is refused, audited and alerted, but sessions on the same host and UID can still bypass the write path (edit the DB, unset env vars). Closing that needs a separate UID (design §9.6), not part of 5a.
- **Subagents are told apart.** Design §9.6 makes subagent writes agent writes if Claude Code exposes a subagent id. It does: hook inputs carry `agent_id` (and `agent_type`) exactly when the hook fires inside a subagent, `SubagentStart` / `SubagentStop` hooks fire with it, and a `PreToolUse` hook may return `updatedInput`. A0 checks that this carries through to our MCP tools and to `tasks.py` in Bash; if it does not, subagents keep counting as their task-manager, as §9.6 already allows.
- **No person users in 5a.** Logins, `/setup` and the first person owner come with 6a. In 5a `users` holds only machine users; the host's own owner is `cli` (a shell without a Claude session) and `owner` (the temp dashboard behind the owner's proxy login), both owner-level because host access already is (§9.3, §9.7). The 6a setup condition "while `users` is empty" becomes "while no person owner exists" (A8 fixes the text).
- **A machine user granted owner has owner rights through MCP** (D-198). Design §9.7's "never owner-only actions" contradicts D-198 and is corrected in A8.
- **"The last owner" (D-183) means the last person owner whose owner grant is manual.** A machine owner has no dashboard login, so it can't stand in as the install's last owner.
- **Global = owner only (D-149):** settings, windows, prompts (D-193), users and grants beyond D-192, project creation and project rank (the cross-project queue order), `review_now`, install and backup actions. Per project: tasks, stages and their priorities, docs, decisions, questions, stall decisions and rules for the project's sessions, prompt variables (D-197), proposals, messages.
- **D-283 is the task-manager's one owner-only exception:** it may clear a pause after investigating. Its own project's pause for every task-manager; the install-wide pause only for the task-manager of AFClaude's own project (the project whose path is the AFClaude repo), because that pause comes from AFClaude's runner and only that task-manager can fix AFClaude. Every clear by a task-manager raises an alert to the owners.
- **AFClaude's own task-manager keeps its deploy steps (D-106):** the maintenance actions a deploy runs (`install.stamp`, `prompt_var.migrate`, `prompt.defaults_sync`, `setting.import`, the importers, `runner_state.save` from hand-run tools) are allowed for that task-manager, listed in the matrix, audited. Settings, prompts, users and grants stay owner-only.
- **Proposal delivery reuses the built inbox delivery (D-241/D-244) and 3b's run-now path**; no third way to type into a session.
- **This server's own interactive Claude sessions act as owner** (D-290, answers question 1): this host's machine user (§3.6) is granted `owner` at A7, not a lesser role; anyone with access to this server already controls AFClaude directly, so a lower role here would protect nothing.
- **The usage review session writes its own report and prediction numbers directly** (D-291, answers question 2): one more exception alongside its hand-off, task status and alerts (§3.7). It still counts as a run whose summary the task-manager receives in full (§3.8); the task-manager can correct the numbers afterwards but does not have to approve them first.

## 1. Today (as-is inventory)

| what | where | state |
|---|---|---|
| actors | `actions.ACTOR_RE`: `owner`, `cli`, `dispatcher`, `keepalive`, `runner[:<job>]`, `mcp[:<session>]` | no user, machine, task-manager or agent actors |
| audit rows (live DB, 04.–22.10., 2,272 rows) | `runner:*` ~1,950; `cli` 243 (doc.append 93, task.edit/add/move 62, inbox.reply 17, decision.import 15, handoff.write 9, automation.set 5, …); `owner` 30 (29 temp-dashboard inbox posts); bare `keepalive` 7 | **no `mcp:` row ever**: the task-manager and its subagents write only through `tasks.py`, so their writes are recorded as `cli`, the same actor as the owner's shell |
| write permission | `Spec.owner_only` on 21 of 58 actions; `is_autonomous()`: only `via="mcp"` is checked, and only for `owner_only` actions; autonomous = `CLAUDE_GUARD_DISABLE=1` or the session id in `driven_sessions` (kind ≠ stall), a project's `manager_session`, the own-session list or the `manager_session` setting | the CLI is never checked; an autonomous session may add/edit tasks, answer questions, write docs and decisions' keywords |
| ad-hoc checks | `handoff.write` (own session only through MCP), `handoff.consume` and `_runner_only` (not via MCP), comments "roles come with 5a" on `prompt_var.*` and `doc.*` | per action |
| session identity | MCP: `CLAUDE_CODE_SESSION_ID` from the server's env (set by Claude Code when it starts the stdio server: checked on the live processes); CLI: the same variable is in every Bash tool call's env, but `tasks.py` ignores it | a subagent's Bash and MCP calls carry its parent's session id; the MCP env is fixed at server start (a `/clear` gives the session a new id the server never sees) |
| task-manager registry | `projects.manager_session` (AFClaude's set), `driven_sessions` (kinds `task-manager`, `review`, `own`, `stall`; `project_id` since 3b S1) | built (3b S1/S2) |
| task assignment | `tasks.assigned_session` (set by `task.start`) | built |
| people and machines | nothing: no `users`, `grants`, tokens; the MCP server is registered per user in the Claude config with no credential | all interactive sessions are owner-level |
| hooks | repo `.claude/settings.json`: SessionStart only (standing rules, decision TLDR) | no PreToolUse / Subagent hooks |

## 2. What changes

- One **principal** per write (person, machine, task-manager, agent, runner, host owner) resolved once at the entry point (CLI, MCP, runner, temp dashboard) and passed to `actions.perform`, which checks it against the action's **permission entry** before the action runs (D-154: the same place that audits). Deny by default: an action without an entry fails a test.
- **Agents' writes** outside their direct writes become `agent_proposals` rows instead of changes; the task-manager reviews them through MCP/CLI, owners, managers and editors from the dashboard later (7a).
- **Reads** through MCP and the CLI are filtered by grants too (a machine user sees only its projects; viewers see questions without being able to answer).
- **Not in 5a:** person logins, sessions, `/setup`, step-up (6a/6b); the OIDC login itself (6b: 5a builds the group-grant sync it calls); the remote MCP transport (5b: 5a builds the token check it uses); the users/grants and proposal UI (7a/7b).

## 3. Design details

### 3.1 Principals and actor strings
| principal | actor | resolved when |
|---|---|---|
| host owner | `cli` / `owner` (kept) | a CLI run with no Claude session in its env; the temp dashboard |
| person | `user:<id>` | 6a (dashboard session) |
| machine user | `machine:<id>` | a valid machine token and no AFClaude-driven session |
| task-manager | `task-manager:<session>` | the session id is a project's `manager_session` and no subagent id |
| agent | `agent:<session>` or `agent:<session>:<agent_id>` | the session is in `driven_sessions`, or is a task-manager's session with a subagent id, or `CLAUDE_GUARD_DISABLE=1`, or nothing identifies it |
| runner | `runner:<component>` | a runner process (no Claude session in its env) |

- `ACTOR_RE` gains the four new forms; old rows stay as they are (append-only). A runner actor claimed from inside a Claude session (e.g. the task-manager running `keepalive.py --decide` by hand) is resolved to that session's principal, never accepted as a runner.
- `audit_log` gains `session` (the Claude session id behind a machine, task-manager or agent write, D-195's "MCP session id when known") and `origin` (JSON: proposal id and agent actor for a write applied from a proposal). Additive columns.
- `VIAS` gains `proposal` (a write applied from an approved proposal).

### 3.2 Schema (additive, no schema-version bump)
- `users(id, kind person|machine, label, created_at, created_by, revoked_at, version)`.
- `grants(id, user_id, scope 'owner'|'project', project_id, level viewer|editor|manager, source 'manual'|'oidc:<group>', created_at, created_by)`, unique (user, scope, project, source). `scope='owner'` rows have no level.
- `machine_tokens(id, user_id, token_sha256, label, hostname_at_issue, created_at, revoked_at, last_seen_at, last_hostname, last_transport)`.
- `machine_registrations(id, code_sha256, user_id NULL|re-issue target, created_by, created_at, expires_at, used_at)`.
- `agent_proposals(id, project_id, run_id, actor, action, payload JSON, version_seen, summary, status proposed|approved|edited|rejected|postponed, reviewer, note, created_at, decided_at)`.
- `agent_runs(id, project_id, session_id, agent_id, task_id, kind subagent|session, started_at, ended_at, end_reason, summary JSON, reviewed_by, reviewed_at)`.
- `tasks.blocked_by` (actor) and `tasks.forwarded_at`: a task an agent blocked reaches the human inbox only once its task-manager forwards it (§7.9).

### 3.3 Identity at the entry points (`identity.py`, mypy --strict)
- **CLI** (`tasks.py`, `telemetry.py`, `project_docs.py`, `decisions.py`, `prompts.py`, `backup.py`, the hand-run paths of `keepalive.py`, `limit_ratio.py`, `runner.py`, 17 literal `actor="cli"` in 9 modules): `identity.resolve(env)` replaces the literal. No `CLAUDECODE` in the env → `cli`. A Claude session → by its `CLAUDE_CODE_SESSION_ID` (+ `AFCLAUDE_AGENT_ID` when A0's hook stamps it) per §3.1; an interactive session on the host → the host's machine user, read from its token file.
- **MCP** (`mcp_server.act` and the read tools): the same function with the server's env and the hook's stamped arguments; a hook-stamped session id wins over the env's (fresh after `/clear`). The token comes from the env (`AFCLAUDE_MACHINE_TOKEN`, 5b's container form) or the token file (`~/.config/afclaude/machine.json`, mode 0600, checked) for the host-side stdio server, so 5a needs no edit of the Claude config. Unknown or revoked token → no machine identity, an audited refusal on the first write.
- **Hooks (A0 decides the exact form):** a `PreToolUse` hook on `mcp__afclaude__.*` stamps `session_id` and `agent_id` into hidden tool arguments; on Bash it prefixes `AFCLAUDE_AGENT_ID=<id>` when the command runs one of our CLIs; `SubagentStart`/`SubagentStop` open and close `agent_runs`. They live in the repo's `.claude/settings.json` now and in AFClaude's own config dir later (5d); in a project without them, subagents count as their task-manager (§9.6's fallback, logged once per session so it is visible, not silent).
- **Runner:** components keep `runner:<component>`; the bare `dispatcher` / `keepalive` literals become `runner:dispatcher` / `runner:keepalive`.

### 3.4 The permission check in the write path
- Each action declares `perm=Perm(scope, human, task_manager, agent, runner)`:
  - `scope`: `global`, or a function that finds the target's project (task → `project_id`, doc → project, request → target project or the stalled session's project by cwd, decision → `project_id`, proposal → project, …).
  - `human`: the least role needed (`viewer` never writes; `editor`, `manager`, `owner`); `force=True` marks a forcing write (run-now, a waking message) that editors may not do (D-184/D-190).
  - `task_manager`: `no` | `own_project` | `self_project` (AFClaude's own task-manager only: deploy steps, the install-wide resume).
  - `agent`: `refuse` | `propose` | `direct_if(<predicate>)` (§3.7).
  - `runner`: whether a runner component may call it (the `_runner_only` steps become `human=None, runner=True`).
- `perform()` resolves the principal's effective role for the scope (owners: everything; else the highest grant on that project; machine users likewise) **inside the transaction, before the action runs**, and either runs it, stores a proposal (agent + `propose`) or raises `Forbidden`. A refusal writes an audit row (`action=<name>`, `after={"refused": reason}`) in its own transaction and an alert (once per actor and action per hour, so a looping session can't flood the alerts doc).
- `owner_only` and `is_autonomous()` are replaced by the matrix; `owner_only=True` maps to `human=owner, task_manager=no, agent=refuse` unless listed otherwise in §0.
- **Reads:** `roles.visible_projects(principal)` filters every MCP/CLI listing; `task.answer` needs editor; the inbox listing shows viewers the same questions without the answer action (D-196).

### 3.5 Grants, OIDC groups, the last owner
- Actions `grant.set` / `grant.remove`: owners grant anything (making an owner is a high-impact write: an alert now, step-up from 6b); a project's managers grant and revoke `editor` and `viewer` on that project only (D-192). `user.rename` / `user.revoke` for machine users.
- `grant.sync_groups(user, groups, provider)`: computes the `oidc:<group>` grants from the `auth_oidc_group_map` setting (§9.4), adds and removes only rows with an `oidc:` source, never touches manual ones. 6b calls it at every OIDC login; 5a tests it with fixed claims, incl. a group mapped to `owner`.
- **Last-owner guard (D-183):** `grant.remove`, a demotion and `user.revoke` refuse when they would leave no unrevoked person user with a manual owner grant. Before 6a there is none; the guard then protects nothing and the host owner (`cli`) is the owner of record.

### 3.6 Machine users (D-195, D-198)
- `machine.code` (owner): a one-time registration code (single-use, 15 min, only its hash stored); for a re-issue, bound to the existing machine user. `machine.register` (no principal, guarded by the code; rate-limited): creates the machine user (label = hostname) and a token `afcm_<id>_<random 32 bytes>` (only its sha256 stored, compared with `hmac.compare_digest`), shown once; a re-issue revokes the old token in the same transaction.
- `tasks.py machine code|register|list|rename|revoke`; `register` writes the token file. The remote form (`afclaude machine register <URL> <code>`, `machines/register`) comes with 5b/6a and calls the same action.
- Every authenticated use updates `last_seen_*` (at most once a minute); a token seen from two hostnames within 24 h raises an alert (D-198).
- A new machine user has no grants (D-195); the setup wizard (6a) offers one; for this install, A7 grants this host's the owner role (D-290).

### 3.7 Agent direct writes and proposals (D-182, D-185)
| agent write | rule |
|---|---|
| `handoff.write` | direct: its own tmux session only (built check) |
| `task.start` | direct: a pending task of its project; sets `assigned_session` to the agent's actor |
| `task.update` / `task.block` / `task.finish` / notes | direct when `assigned_session` is the agent; `blocked` sets `blocked_by`, no inbox entry until forwarded |
| `doc.append` to the project's `alerts` doc; the DB error fallback file | direct (failure escalations, §7.8) |
| the usage review's own report and updated prediction numbers | direct (D-291); the run's summary still carries them to the task-manager in full (§3.8) |
| its own branch / worktree | git, not the DB; merging is the task-manager's after it read the change manifest (D-185, D-243/D-272); prompt rule, not enforceable in 5a |
| everything else in its project (docs, goals, progress, new tasks, edits of other tasks, questions, decision proposals, prompt variables) | proposal |
| global or owner-only actions; anything with no project to propose to | refused, audited, alerted |
- A proposal stores the action, its params (incl. the `version` the agent saw) and a one-line summary built from the action and its target; the tool returns "proposed as P-n, waiting for the task-manager's review".
- `proposal.review(id, approve|edit|reject|postpone, note, payload?)`: task-manager of the project (MCP `afclaude_review`, `tasks.py review`), and from 7a owners, managers and editors (D-190). Approve or edit runs the payload through `perform()` as the reviewer with `via=proposal` and `origin`; a version mismatch is the usual 409 (edit onto the current state or reject). `postpone` keeps it for the next continue prompt (D-188). Nothing is applied by timeout; `proposal_alert_hours` (default 24) alerts on the oldest pending one.
- Questions from agents: the task-manager answers them itself (into the agent's task notes) or approves them, which creates the human question with the agent named as origin.

### 3.8 Run summaries and delivery (D-188, D-194, D-243)
- An `agent_runs` row opens at `SubagentStart` (subagents) or at the driven session's start (separate sessions) and closes at `SubagentStop` / the session's end, kill or stall. At close, `proposals.summary(run)` lists every write of the run from the audit log (actor = the agent) and its proposals: pending proposals with payloads, direct task changes, hand-offs, alerts. Code changes are not repeated: the summary names the run's branch and the task-manager reads it with `change_manifest.py` (D-243 as refined by D-272).
- **Delivery:** a subagent's summary goes into the task-manager's context when the subagent returns (A0: a `PostToolUse` hook on the Agent tool adding `additionalContext`, or the next MCP/CLI result of the task-manager carrying "N proposals pending"). A proposal from a separate session goes through the inbox delivery path (a short notice at the task-manager's next idle point); a task-manager that isn't running is woken through 3b's run-now path with a `proposal_review` request (D-194), with the same rules as a waking owner message (D-186: no window or budget gate, the preflight applies).

## 4. Order and dependencies

- **Before A1:** 3a S2–S6 merged (they change `actions.py` and `store.py`); no other branch editing `store.py`, `actions.py`, `mcp_server.py` or `tasks.py` in parallel with an A-step. `mcp_server.py` and `tasks.py` are used by every live session: A2 onwards are built in worktrees and deployed between runs.
- A0 has no dependency (a throwaway session, no live change). A1 → A2 → A3 → A4 → A5 → A6 → A7 → A8 in order; A1 can start before A0 ends.
- **3b:** A2 relies on S1 (`driven_sessions.project_id`) and S3 (the task-manager registry); A5's wake needs S5 (run-now). **4a:** A6 edits the task-manager template after P3. **4b:** Q1's decision actions and Q4's message threads get their permission entries in A3 if merged before, otherwise when they land (the matrix test makes that mandatory); phase 4's open question 2 (does the task-manager record the owner's chat statements directly?) decides whether the task-manager gets a "record an owner statement" write.
- **Later phases build on 5a:** 5b (the remote MCP transport checks tokens with A1/A2), 6a (person users, `/setup`, the API's route and matrix tests on A3's matrix), 6b (OIDC logins call `grant.sync_groups`), 7a/7b (proposal and grant UIs), 8a/8c (D-168: the matrix test is 8a's starting point; 8c's per-role test users).

## 5. Steps

**A0: identity spike (no live change).**
- **Build:** in a throwaway session with a test MCP server and a stub CLI: check that `agent_id` reaches `PreToolUse` for MCP and Bash calls inside a subagent and is absent on the main thread; that `updatedInput` changes an MCP tool's arguments and a Bash command under the permission modes we run (auto, bypass); `SubagentStart`/`SubagentStop` payloads; whether a `PostToolUse` hook on the Agent tool can add context to the parent; the session id after `/clear`, `--resume` and a compaction in the MCP env vs the hook input; background subagents.
- **Done when:** results written into §3.3/§3.8 as built, with the chosen hook forms. **Risk:** a hook on every MCP and Bash call adds latency; measured here (target < 50 ms).

**A1: schema, roles core, machine users.**
- **Build:** §3.2 tables and columns (store functions, triggers: `grants` and `users` versioned, `machine_tokens` never deleted); `roles.py` (pure: principals, effective role, the last-owner rule, group sync); actions `grant.set/remove`, `grant.sync_groups`, `machine.code/register/rename/revoke`, `user.rename/revoke`; `tasks.py machine …`, `tasks.py grant …`; no caller passes a principal yet, nothing is refused.
- **Tests:** grants by scope and source; managers grant only editor/viewer on their project; group sync adds/removes only `oidc:` rows; last-owner guard (manual person owner vs group-derived vs machine owner); codes single-use and expiring; only hashes stored; re-issue revokes the old token; the two-hostname alert.
- **Done when:** suites, `test_typing` (`roles.py` on the list), `test_mcp_server`, `check_public` green. Deploy: imports only.
- **Risk:** additive tables on the live DB; none expected.

**A2: identity at every entry point.**
- **Build:** `identity.py` (§3.3); the CLI literals, `mcp_server.act` and the runner actors switched to it; A0's hooks in `.claude/settings.json`; `ACTOR_RE`, `audit_log.session`; `tasks.py whoami` (prints the resolved principal). Still no refusals: the old `owner_only` rule stays in force.
- **Tests:** each row of §3.1 from fixture envs (shell, interactive session with and without a token, task-manager, subagent with a stamped id, driven agent, guard bypass with an unknown id, runner actor claimed inside a session, revoked token); a driven session with a machine token stays task-manager/agent (D-198).
- **Done when:** one night's audit rows show `task-manager:` and `agent:` actors where `cli` was before, and no write failed.
- **Risk:** a CLI path missed keeps writing as `cli` (owner): a test greps for literal `actor="cli"` outside `identity.py`.

**A3: the permission matrix, in shadow mode.**
- **Build:** `Perm` on all actions (§3.4), project resolvers, read filters for MCP and CLI listings; setting `roles_enforce` (`shadow` | `on`, owner-only, removed in A8): in shadow every would-be refusal or proposal is logged (`app_log` warning + a daily list for the task-manager) and the write goes on under the old rule.
- **Tests:** the action-matrix test (every action × owner, manager, editor, viewer, none, machine with and without a grant, task-manager own/other project, AFClaude's task-manager, agent, runner, host owner: allowed / proposal / refused), a new action without a `Perm` fails; viewers see questions but `task.answer` is refused; only managers and owners force; managers can't touch global settings or project rank.
- **Done when:** a week of shadow logs reviewed by the task-manager; every unexpected would-be refusal fixed in the matrix or the prompts.
- **Risk:** the matrix is long; it is one table in one place, and A8/8a read it against §2.

**A4: proposals, runs, review.**
- **Build:** §3.7/§3.8's tables, the proposal capture in `perform()`, `proposal.review`, `agent_runs` open/close (hook CLI and the runner's driven-session start/end), `proposals.summary`, MCP `afclaude_proposals` / `afclaude_review`, `tasks.py proposals|review`, the `proposal_alert_hours` check (a runner tick), `blocked_by` / forwarding.
- **Tests:** each direct write applies; every other agent write stores a proposal and changes nothing; approve/edit/reject/postpone; a stale version gives a 409; nothing applies while the task-manager is down; the summary lists every write of the run incl. direct ones; an agent-blocked task isn't in the inbox until forwarded.
- **Done when:** in shadow, the summaries of one night's subagent runs match what the agents did (checked by the task-manager against their reports).

**A5: delivery.**
- **Build:** the subagent summary into the task-manager's context (A0's form); separate-session proposals through the inbox notice; the `proposal_review` wake through 3b S5; postponed proposals in the continue prompt (a 4a bundle insert).
- **Tests:** a proposal reaches a running task-manager at its next idle point and an idle one is woken, with no window or budget gate; postponed ones appear in the next continue.
- **Risk:** a wake loop (an agent proposing while its task-manager is down); one open `proposal_review` request per project.

**A6: prompts.**
- **Build:** the task-manager template (review proposals and run summaries, read the change manifest before merging D-185/D-272, clear a pause only after investigating D-283, answer or forward agents' questions); the worker brief and `.claude/agents/afclaude-worker.md` (what is direct, what becomes a proposal, start your task to own it); MCP tool descriptions (proposal results, machine identity).
- **Done when:** the 4a render test passes and one night runs with them in shadow.

**A7: cut-over to enforcement.**
- **Build:** register this host's machine user and grant it the owner role (D-290); then `roles_enforce=on`: refusals refuse, agent writes become proposals; the old `owner_only` / `is_autonomous` code removed.
- **Done when:** three nights with no unexplained refusal, every proposal reviewed, the alerts doc free of role violations except deliberate tests.
- **Risk:** a night run blocked by a missed permission: the refusal alerts and the fail-stop pauses (D-235); the fix is a matrix entry, deployed between runs.

**A8: docs and cleanup.** Design §2 (owner-question answers), §9.3 ("no person owner" instead of "users is empty"), §9.6 (subagent identification as built, the D-283 and deploy exceptions), §9.7 (machine owners per D-198), §7.2 (tables as built), §13 5a status; README file table (`roles.py`, `identity.py`); mypy list; the `roles_enforce` setting removed.

## 6. Questions for the owner

None open. D-290 answers the former question 1 (this host's own interactive sessions get the owner role) and D-291 answers the former question 2 (the usage review writes its report and numbers directly, with the task-manager still getting all results); see §0, §3.6 and §3.7.
