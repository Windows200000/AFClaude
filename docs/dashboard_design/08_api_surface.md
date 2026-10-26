## 8. API surface

The JSON API is **the** interface of the dashboard (D-209): the TS frontend has no other way to read or change anything, and the API is equally usable by scripts and tests. Rules:
- **Versioned:** everything under `/api/v1`, JSON only (UTF-8, `Content-Type: application/json` required on every write); a breaking change means `/api/v2` next to v1 until the frontend in the same image has moved. Times are ISO 8601 UTC; the client renders them in the user's display time zone.
- **Typed schema:** every endpoint's request and response is declared in the Python schema (§4) and published as `/api/v1/schema.json` (behind login); the TS client types are generated from it. Unknown fields in a request are rejected, not ignored.
- **Errors:** one JSON shape for every error (`{error: {code, message, details?}}`, codes such as `unauthenticated`, `step_up_required`, `forbidden`, `conflict` with the current row, `validation`, `rate_limited`, `maintenance`); never a traceback or internal detail. The client maps codes to UI states (sign in again, re-authenticate, reload and retry on 409).
- **Writes:** POST/PUT/PATCH/DELETE with an `Idempotency-Key` (a UUID per user action, made in the client) and the `version` where one exists; each is one `actions.py` call and returns the new state the view needs (e.g. the new inbox). Every handler checks the role (§2) before anything else.
- **Same-origin only:** the frontend is served from the same origin as the API; no CORS headers are sent, so other origins can't read responses. CSRF rules for the SPA are in §9.8.
- **Auth flows from the TS client:** password + TOTP/recovery code are JSON posts (`auth/login`, `auth/factor`), the session cookie is set by the response and never readable by JS. **WebAuthn ceremonies** are two-step: `auth/passkeys/register/begin` (or `…/login/begin`) returns the options with a server-generated challenge that is stored in the server-side (pre-)session, single-use, valid for a few minutes; the client calls `navigator.credentials.create()` / `.get()` and posts the result to `…/finish`, where python-fido2 verifies challenge, origin, RP ID, flags, attestation (MDS3) and the sign counter (§9.3). The client only converts base64url and never decides anything. **OIDC** is a full-page redirect, not an API call: `/auth/oidc/start` → provider → `/auth/oidc/callback` on the backend, which checks state, nonce and PKCE, sets the session cookie and redirects into the app; provider tokens never reach the browser. **Step-up:** a high-impact write without a fresh authentication gets `step_up_required`; the client shows the re-auth dialog (passkey, password + factor, or an OIDC redirect with `max_age`) and retries the write with the same idempotency key.
- **Live state:** run-now and message request cards (§5.4, F14) poll their request's read endpoint every few seconds while it is open (*proposal*; server-sent events only if polling proves too slow).

Reads: `overview`, `inbox`, `projects`, `queue`, `tasks/{id}`, `stalls`, `rules`, `window` (settings + next 7 windows + ratios), `usage` (the snapshot), `runs`, `sessions/driven`, `handoffs`, `messages` (the open thread, or a closed one by id), `proposals` (per project, with the run summaries), `decisions`, `prompts`, `prompts/{name}` (default, overrides, bundle, diff), `docs`, `reviews`, `audit`, `settings`, `users` (incl. machine users with their token metadata, never the token), `me` (logins, sessions; also the per-session CSRF token, §9.8), `host` (latest checks), `backups`, `requests/{id}` (state of a run-now or message request), `schema.json`, `health` (no secrets).

| write | effect | conflict |
|---|---|---|
| `projects` / `tasks` create, edit, move, priority, cancel, reopen | as built (`project.*`, `task.*`) | version / no-op / 409 on state |
| `tasks/{id}/answer` | blocked → pending; a question → done; a decision proposal → applied or rejected | 409 unless open |
| `stalls/{session}/decision` | continue / ignore / clear with the `stall_ref` shown | 409 if it stalled again |
| `rules` | session / project rule | unique (scope, match) |
| `decisions` create, edit, confirm, delete | §7.6 | version |
| `proposals/{id}` approve / edit / reject (owners, and the project's managers and editors, D-190; task-managers do it through MCP) | applies or drops an agent proposal (§7.9) | 409 unless `proposed`; the payload's version |
| `settings/{key}` set / reset; `settings/reset` (section, all) | registry-validated; undo from audit | version |
| `settings/window` | day, start, n, mode | version |
| `prompts/{name}?project=` PUT / DELETE | override / reset | base_sha256 + version |
| `requests` | `continue_now`, `work_on_now` (owners, the project's managers), `review_now` (owners) (wakes the runner, §5.4) | one open per (kind, target) |
| `messages` | send a message to a project's task-manager (F14): appends to the open thread or opens one, queues `task_manager_message`; wakes the runner when the sender is an owner or a manager of the project, otherwise it waits for the next regular run (D-186); the reply arrives through MCP `afclaude_reply` | thread closed by the reset → a new thread |
| `users`, `grants` | invite, role, project scope, remove (owners; managers grant and revoke editor and viewer on their project, D-192); machine users take grants the same way | version |
| `machines` | create a one-time registration code; rename (label), revoke, re-issue a machine's token (owners, D-195, §9.7) | version |
| `machines/register` | exchanges a one-time registration code for a new machine user's id and token (or a re-issued token); called by `afclaude machine register` on the machine; no session, guarded by the code, rate-limited | code used or expired → 410 |
| `auth/*` | login, factors, passkey ceremonies (begin/finish), OIDC start/callback (redirects), step-up, logout, sessions | §9 |
| `backup`, `export` create / download; restore (setup only); point-in-time rollback to a moment in the WAL window | §11 | one running at a time |
| `host/checks` | run all checks or one (and the end-to-end test) now | one run at a time |

Concurrency: WAL + `BEGIN IMMEDIATE` + 30 s busy timeout serialise writers across containers; dashboard transactions do no I/O. `start_task` claims only pending tasks atomically, so "answer" vs "start" and "cancel" vs "start" have exactly one winner. Shrinking a window or pausing never kills running sessions.

