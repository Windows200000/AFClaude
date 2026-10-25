# Phase 6a: dashboard skeleton and local auth (implementation plan)

Plan for design §13 step 6a, with the parts of §3 (the views it starts), §4 (architecture, frontend stack, API types, strict typing), §8 (API surface), §9.3 / §9.5 / §9.8 (login methods, session lifetimes, hardening), §10.1 (the multi-stage image), §10.4 (the first-startup wizard), §10.9 (owner previews) and §12 (tests) that it builds: the Starlette JSON API with its error shape and typed schema, the TypeScript frontend scaffold, sessions, password + TOTP + recovery codes, the first registration and the wizard, CSRF, headers and CSP, the route-table and matrix tests, `health`, the image, and the first owner preview. Each step in §5 is one worker branch; W1 and W2 are built on branch `phase6a`.

Binding inputs: D-070 (built from scratch, not from the quickview), D-071 (option A; generic SSO plus username/password), D-072 / D-157 / D-073 (no internet route and no Claude connection before the gates; reachable behind the login after them), D-074 (purple accent, vibrant status colours, rigid but sleek), D-075 / D-146 (every setting in the DB registry with default, reset, undo and explanation), D-149 / D-172 / D-183 (first browser registration = owner, guarded by the setup code; several owners; the last non-group owner can't be removed), D-150 / D-159 (OIDC with groups and password with mandatory 2FA; hardware passkeys alone, synced ones need a second factor: 6b), D-154 (one write path with audit), D-162 / D-179 (session lifetimes are settings; remember-device 7 days), D-173 (Berlin until the first-login popup; one-time setup code), D-208 / D-209 (Python backend keeps auth, API and writes; TypeScript frontend only; mypy --strict and TS strict, mainly for security), D-211 (non-blocking owner previews behind /private from 6a on, disconnected from the backend), D-230 / D-249 (simple numbers, instant custom tooltips, a "?" toggle for verbose tooltips, no filler text), D-229 (the wizard asks the Claude plan), D-235 (fail fast), D-007 (existing libraries first), D-263 / D-279 (owner questions in plain words, in full). M-010 (dashboard size limits, already in `actions.DASHBOARD_LIMITS`). D-292 (a pinned, checksum-verified Node.js lives in AFClaude's own folder for building and checking the dashboard pages).

## 0. Already decided

- **No new Python dependency.** Everything 6a needs is already in the `.venv` through `mcp==2.2.0`: Starlette 1.7.0 (app, routing, `StaticFiles`, `TestClient`), uvicorn 0.54.0, pydantic 2.13.5 (request/response models, `extra="forbid"`, JSON Schema), jsonschema 4.26 (contract validation in tests), cryptography 50.0.1 (Argon2id with PHC strings, RFC 6238 TOTP, AES-GCM). They are only transitive today; W10 pins them directly with hashes (§9.8).
- **The schema is pydantic models, published as OpenAPI 3.1.** The design asks for "request and response shapes declared once, in Python (typed, checked by mypy --strict)" and a generated JSON Schema. Pydantic models are that declaration (mypy checks them without a plugin; `extra="forbid"` rejects unknown request fields, §8), and `models_json_schema` emits JSON Schema 2020-12. The document is OpenAPI 3.1 (a standard wrapper whose schemas are plain JSON Schema 2020-12), committed as `dashboard/schema.json` and served as `/api/v1/schema.json` behind login.
- **TS types from our own small generator** over the JSON Schema subset pydantic emits (objects, required, enums/consts, arrays, unions, nullables, refs, maps); any other construct is a generation error, never a guessed type. Checked first: `openapi-typescript` and `json-schema-to-typescript` (npm packages with their own dependency trees; the design keeps the npm tree to five packages and says "no codegen package"), `pydantic-to-typescript` (a Python wrapper that shells out to `json-schema-to-typescript`), `datamodel-code-generator` (the other direction). About 150 lines of our own code are smaller than any of them.
- **Sessions are our own server-side rows**, not Starlette's `SessionMiddleware` (a signed client-side cookie: not revocable, not listable in F11, no idle timeout without trusting the client). `starsessions` / `starlette-session` were checked: extra dependencies for the ~150 lines that a table plus actions.py need anyway.
- **CSRF is our own** (§9.8: JSON content type, same `Origin`, `Sec-Fetch-Site`, per-session token in `X-CSRF-Token`, held in memory by the client). `starlette-csrf` is a double-submit cookie that JS must read, which §9.8 rules out.
- **Password hashing and TOTP from `cryptography`**: `Argon2id.derive_phc_encoded` / `verify_phc_encoded` (RFC 9106 parameters: 64 MiB, t=3, p=4; the PHC string stores them, so they can be raised later) and `twofactor.totp.TOTP` (SHA-1, 6 digits, 30 s, ±1 step, the last used step stored against replay). `argon2-cffi` and `pyotp` are not needed.
- **Login name is a username** (D-071 "simple username/password"); e-mail is display only and never used to link identities (§9.3). Password: 12–128 characters, no composition rules (NIST SP 800-63B).
- **Origin is required on every write** (stricter than §9.8's "where the browser sends it": every current browser sends `Origin` on a `fetch` POST/PUT/PATCH/DELETE; scripts and tests set it). `Sec-Fetch-Site`, when present, must be `same-origin`. *Interpretation, 8a checks it.*
- **Every CSRF failure is a 403 `forbidden`**, including a non-JSON content type (8b's curl check expects 403 for a form post).
- **Error codes** are §8's list plus `not_found`, `method_not_allowed`, `payload_too_large`, `unavailable` (the DB is down, §7.8) and `internal` (§8 says "codes such as"). An unexpected exception is a 500 `internal` with a fixed message; the traceback goes to the server log only.
- **`health` is public and minimal:** `{status: ok | unavailable, db: ok | <the DBError kind>}` from `store.health()`; 503 when the DB is not healthy (the red banner of §7.8 reads it). No path, no version, no host detail.
- **`Host` is checked** against the host of `AFCLAUDE_PUBLIC_URL` (a localhost-bound app reached through a tunnel is otherwise open to DNS rebinding), in our own outermost middleware rather than Starlette's `TrustedHostMiddleware`, which answers in plain text, outside the error shape and the security headers.
- **Unknown `/api/…` paths are a JSON 404**; every other GET path is the app shell (the router decides), so a typo in an API call never returns HTML.
- **Runs on Python 3.12 only** (the `.venv` and the image; the host's system Python is 3.9): its tests run with `.venv/bin/python -m unittest test_dashboard`, like `test_mcp_server`.
- **Nothing listens before W10/8b.** W1–W9 provide `dashboard.app.create_app()` and tests (Starlette's `TestClient`, in-process, no port). No serve command is run on the host; the `afclaude web` service comes with the image (W10) and is deployed only at 8b (D-072/D-157).
- **The quickview is not reused** (D-070), except its look (D-074): the colour tokens (purple `#7e22ce`/`#a855f7`, green, amber, red, pink for the preview marker) move into the frontend's one stylesheet in W3.
- **A pinned, checksum-verified Node.js in AFClaude's own folder** (D-292, answers question 1): downloaded once into AFClaude's folder, checked against its published checksum, not installed system-wide and no service; every test run and commit can then check the TypeScript pages in seconds, not only inside a docker build. §3.10.

## 1. Today (as-is inventory)

| what | where | state |
|---|---|---|
| temp dashboard | `export_quickview.py` → `quickview/AFClaude.html` behind `/private` (nginx key header); the inbox box over `inbox_api.py` (stdlib HTTP on a unix socket: key header, JSON-only POST, body cap, rate limit) | in use until §10.7; not part of 6a (D-070) |
| phase-1 backend | schema v4, `actions.py` (validate → transaction → audit → idempotency → version, `via="dashboard"` with `DASHBOARD_LIMITS`, M-010), `store.py` (§7.8 error path, `store.health()`) | built; actors are `owner`/`cli`/`mcp:*`/`runner:*` (no `user:<id>` before 5a) |
| users, grants, roles | design §2, §9.4; 5a plan in progress (worktree `phase5a-plan`, nothing written yet) | not built: no `users`/`grants` tables, no role matrix |
| auth tables and settings | §7.2 auth row, §9.5 settings | not built; no `auth_*` key in `actions.SETTINGS` |
| master key | §10.2 (`AFCLAUDE_MASTER_KEY` or a generated `/data/master.key`) | not built (5b) |
| web stack in the `.venv` | Starlette, uvicorn, pydantic, jsonschema, cryptography (all through `mcp==2.2.0`) | present, unpinned as direct dependencies |
| image | `docker/Dockerfile`: one stage, `python:3.12-slim-bookworm`, `pip install mcp==2.2.0` | no Node stage, no web service; `docker/*` is also touched by the held 3a branches |
| Node / npm | — | not installed on the host, not in any image |
| strict typing | `mypy.ini` (24 modules), `test_typing.py`, pre-commit | no dashboard module yet |
| held branches | 3a runner (S2–S6): `actions.py`, `runner*.py`, `docker/*`, `export_quickview.py`, `keepalive.py`, `schedule.py`, `usage_review.py`, `usage_sampler.py`, `failstop.py` | unmerged; any 6a step that edits these waits for the merge |

## 2. Library research (D-007)

| need | chosen | also checked |
|---|---|---|
| web framework, static files, test client | Starlette (in the `.venv`) | FastAPI (pulls its own dependency tree; its OpenAPI generator is what we write in W2 anyway, with less control over the error shape) |
| request/response models, JSON Schema | pydantic v2 (in the `.venv`) | `TypedDict` + hand validation (no schema generation, every check by hand), msgspec / attrs + cattrs (new dependencies) |
| JSON Schema → TS | own generator (§0) | openapi-typescript, json-schema-to-typescript, pydantic-to-typescript, datamodel-code-generator |
| contract validation in tests | jsonschema (in the `.venv`) | — |
| password hash, TOTP, secret encryption | cryptography (in the `.venv`) | argon2-cffi, pyotp, PyNaCl (each a new dependency for what cryptography already has) |
| sessions, CSRF, rate limits | own on the DB (§0) | Starlette `SessionMiddleware`, starsessions, starlette-csrf, slowapi (in-memory, per process: wrong for three containers on one DB) |
| frontend runtime, build, tests | design §4: Preact, esbuild, TypeScript, `node:test`, preact-render-to-string, uPlot (6c) | chosen in the design (D-208/D-209); W3 pins exact versions |
| QR code for TOTP enrolment | open: the `otpauth://` URI shown as text plus a QR rendered server-side as SVG | a JS QR package would be a sixth npm package (a design change, §4); a small Python QR encoder is the likelier choice, decided in W6 after a check of what is maintained |

## 3. Design details

### 3.1 Package layout
`dashboard/` (Python, mypy --strict): `app.py` (`create_app(WebConfig, authenticate, endpoints=ENDPOINTS)`), `errors.py` (codes, `ApiError`, the one error body), `security.py` (headers and CSP middleware, the write guard), `routes.py` (the endpoint table and its dispatcher), `api_types.py` (every request/response model), `codegen.py` (`python -m dashboard.codegen [--check]`: `dashboard/schema.json` and `dashboard/web/src/api/types.gen.ts`). Later: `sessions.py`, `passwords.py`, `totp.py`, `setup.py` (W5–W7). `dashboard/web/` is the TS frontend (W3).

### 3.2 The endpoint table
One frozen `Endpoint(name, method, path, access, request, response, handler, idempotent)` per route; the Starlette routes, the schema's `paths`, the route-table test and (W5) the role matrix are all derived from it, so a route can't exist outside it. `access` is `public` or `session` (W5 adds the minimum role); `public` is allowed only for `health`, the app shell and its assets, the login, setup and (6b) OIDC endpoints and `machines/register` (§9.4). A `session` route without a session is a 401 `unauthenticated`; until W5 no request has a session (the authenticator `create_app` is given says "none"), so every such route is closed.

### 3.3 The write path in the API
For POST/PUT/PATCH/DELETE, in this order: the write guard (JSON content type, `Origin` equal to the public origin, `Sec-Fetch-Site` same-origin when sent; W5 adds `X-CSRF-Token`), the session and role check (W5), the body (at most 64 KiB, counted while reading; then JSON; then the endpoint's request model with `extra="forbid"`; a validation error lists field paths and messages, never the input), the `Idempotency-Key` (a UUID, required on idempotent endpoints, passed to `actions.perform`), then the handler. The handler returns the endpoint's response model (anything else is a 500: the schema can't be bypassed).

### 3.4 Headers
On every response: the CSP of §9.8 (`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; require-trusted-types-for 'script'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, HSTS (`max-age=63072000; includeSubDomains`), `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`; `Cache-Control: no-store` except on hashed assets under `/assets/` (`public, max-age=31536000, immutable`). No CORS header is ever sent. Trusted Types with Preact: Preact writes the DOM through properties, and the one raw-HTML sink (`SanitizedHtml`, 6c) gets a Trusted Types policy; W3 confirms in a headless browser that the shell runs with zero CSP violations.

### 3.5 Sessions (W5)
- Tables (additive, through `store.py`'s migrations; `users` and `grants` come from 5a): `auth_sessions` (sha256 of the id, user, kind `pre`/`full`, created, last seen, authenticated at, factors used, remembered-device id, CSRF token hash, user agent summary, ended at/why), `credentials` (user, kind `password`/`totp`, the PHC string or the encrypted seed, last TOTP step, created, version), `recovery_codes` (user, hash, used at), `remembered_devices` (user, token hash, created, expires, revoked), `auth_failures` (account or IP key, ts) for the rate limits.
- Cookie `__Host-afc_session` (Secure, HttpOnly, SameSite=Lax, Path=/), 256-bit random id; only its hash is stored. The id rotates at login, at step-up and at every factor change. A pre-session (no user) carries the CSRF token for the login and setup posts.
- Lifetimes from the §9.5 settings (`auth_idle_timeout_min` 30, `auth_absolute_lifetime_h` 12, `auth_remember_device_days` 7 (D-179), `auth_reauth_window_min` 10, `auth_max_sessions_per_user` 10, `auth_login_rate`), new `auth` section in `actions.SETTINGS` (owner-only, high-impact).
- `last_seen` is written at most once a minute per session, through a registered `actions.py` appender-style action without an `audit_log` row (the session row is its own record, like telemetry, §7.3); login, logout, factor changes and revocations are audited. *Interpretation, 8a checks it.*
- `me` returns the user (or none), the CSRF token, the display time zone and (5a) the grants; the client keeps the token in memory only.

### 3.6 Password, TOTP, recovery codes (W6)
- Factor rule (§9.3) in one pure function over the factors of the session: complete with (a) OIDC, (b) a hardware passkey with UV (6b), or (c) two different factors from {password, TOTP, synced passkey, recovery code}. In 6a only (c) with password + TOTP or password + recovery code exists.
- `auth/login` (username, password) → a pre-session with `password` done, or a full session when the device is remembered (§9.5; never for step-up); `auth/factor` (TOTP code or recovery code) → full session, rotated id. Wrong username and wrong password take the same time and give the same error.
- TOTP seeds (160 bit) and the recovery-code pepper are encrypted with the master key (AES-256-GCM, §7.1). The master key is 5b's (§10.2); if 5b has not landed, W6 builds its loader exactly as §10.2 says and 5b reuses it.
- Rate limits per §9.5 (5 failures / 15 min per account, 20 per IP, then exponential backoff), from `auth_failures`; repeated failures raise an alert.

### 3.7 Setup and the first-startup wizard (W7)
- While `users` has no person, the web process creates a one-time setup code at start (8 groups of 4 base32 characters), prints it to its log (stdout) and stores only its hash with an expiry; `/setup` needs it. Registering the first owner is one transaction (`BEGIN IMMEDIATE`, re-checks that there is no person yet), so two racing tabs can't both win; the code is spent in the same transaction.
- First owner: username, password, TOTP enrolment confirmed with a code, then the 10 recovery codes shown once. A hardware passkey as the first owner's login comes with 6b.
- The wizard (§10.4) then runs as views over the APIs that exist: time zone (the browser's, sets `window_tz` and the owner's display zone, D-148/D-173) and the Claude plan (D-229, `claude_plan`). The host check (5b), the working root (5d) and this host's machine registration (5a) join the wizard in the step that builds their API; until then the wizard shows them as "not available yet", never as done.
- `afclaude admin reset-auth` (lockout recovery, §9.3): host access removes a user's factors and sessions and prints a fresh setup-style code for that user.

### 3.8 Frontend scaffold (W3)
- `dashboard/web/`: `package.json` with exact versions of `preact`, `typescript`, `esbuild`, `preact-render-to-string` (uPlot joins in 6c), the committed `package-lock.json`, `tsconfig.json` (`strict: true`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`), `build.mjs` (esbuild: hashed `assets/app-<hash>.js` and `.css`, `index.html` written from a template without inline script or style), `src/`.
- `src/api/client.ts`: the typed client over `types.gen.ts` (one function per endpoint name), the CSRF header on every write, an idempotency key per user action reused on retry, error codes mapped to UI states (`unauthenticated` → login, `step_up_required` → re-auth dialog (6b), `conflict` → reload-and-retry), no `any`.
- `src/router.ts`: the typed route table (the F-views as a fixed list) over `history`, no package.
- App shell, layout and top bar (§3: automation state, runner dot, tonight's window, inbox badge; placeholders until 6c have data), the status colour tokens (D-074), the tooltip component (D-230: instant, ~120 ms fade/scale, `prefers-reduced-motion`, tap on touch) and the "?" toggle for verbose tooltips (D-249; held in memory until 7b makes it a per-user setting, no web storage, §9.8).
- Source checks (tests): no explicit `any` in `src/api/`, no `innerHTML`/`dangerouslySetInnerHTML`/`eval`/`new Function` anywhere in `src/`.
- `test_dashboard_web.py` runs `npm ci --ignore-scripts`-installed `tsc --noEmit`, the `node:test` suite and the esbuild build when the pinned Node (§3.10) is present; it fails (not skips) when `AFCLAUDE_REQUIRE_NODE=1` (set in the image's build stage and, once §3.10's copy exists, in the pre-commit hook too).

### 3.9 Owner preview (W4, D-211)
`npm run build:preview`: an esbuild `define` swaps the API client's transport for a mock that serves typed fixtures (`src/preview/fixtures.ts`, typed by `types.gen.ts`, so a schema change breaks the build, not the preview); `index.html` of the preview build carries `connect-src 'none'`. Dummy data only. Deploying the output to its own `/private` path, the "needs your input" item and the pink outline (already `preview_from` in the phase strip) are the task-manager's.

### 3.10 Pinned development Node.js (D-292)
- `tools/node_pin.py fetch|verify|which`: downloads the pinned version's tarball for the host's platform from nodejs.org into `.node-afclaude/<version>/` (repo-relative, gitignored), checks it against the matching line of nodejs.org's published `SHASUMS256.txt` (itself pinned by hash in the tool, not fetched trustingly at run time), and extracts it; `which` prints the `node`/`npm` paths for scripts to use. No system install, no PATH change, no service; deleting the folder removes it entirely.
- The version pinned here is the same `NODE_VERSION` W10's multi-stage Dockerfile pins (one source of truth in `tools/node_pin.py`, read by both the dev tool and the Dockerfile's `ARG`), so "what builds the image" and "what checks a commit" never drift apart.
- `test_dashboard_web.py` and the pre-commit hook call `tools/node_pin.py which` first; missing or a checksum mismatch is a clear failure (D-235), never a silent skip once `AFCLAUDE_REQUIRE_NODE=1`. The README gets a one-line "fetch once with `tools/node_pin.py fetch`" setup note.

## 4. Order

- **W0 (pinned Node, §3.10):** no dependency; built first since W3 needs it.
- **W1, W2:** no dependency; built on `phase6a`.
- **W3:** needs W0's pinned Node for development (D-292); otherwise independent. **W4** after W3.
- **W5:** needs 5a's `users`/`grants` and the `user:<id>` actor (5a), and edits `actions.py` (settings, actions), so it waits for the 3a merge too. **W6** after W5. **W7** after W6. **W8** after W3 and W7.
- **W9 (route × role matrix)** with W5 (it needs 5a's roles).
- **W10 (image + `afclaude web`)** edits `docker/*`: after the 3a merge, together with 5b's three-service compose; no published port, no route (D-073/D-157).
- **W11 (docs)** last.
- `actions.py` and `store.py` are imported by every live process: W5–W7 are built in worktrees and merged one at a time; nothing of 6a is deployed before 8b.

## 5. Steps

**W0: pinned development Node.js.** §3.10; `tools/node_pin.py fetch|verify|which`; `.gitignore` entry for `.node-afclaude/`; the pinned `SHASUMS256.txt` line and `NODE_VERSION` shared with W10's Dockerfile `ARG`. **Tests:** a wrong checksum refused before extraction; `which` fails clearly when nothing was fetched; the version constant used by both this tool and (once W10 exists) the Dockerfile are the same string, checked by a test that reads both. **Done when:** `tools/node_pin.py fetch && tools/node_pin.py which` works on this host and `test_dashboard_web.py` picks it up without `AFCLAUDE_REQUIRE_NODE`.

**W1: API skeleton (built on `phase6a`: 36 tests in `test_dashboard.py` with W2).**
- **Build:** `dashboard/` (§3.1) with `app.py`, `errors.py`, `security.py`, `routes.py`; the endpoint table (§3.2), the write path up to the handler (§3.3, without the CSRF token and roles, which need W5), the headers (§3.4), the Host check, the app shell and `/assets/` from a built `dist/` (missing `index.html` = `create_app` refuses to start), JSON 404/405/500 in the one error shape, `GET /api/v1/health`. The modules join the mypy list.
- **Tests:** `test_dashboard.py`: deny by default (every non-public endpoint 401s without a session; the public list is exactly the allowed one); the error shape on 401/403/404/405/413/422/500/503 with no traceback or input echo; the write guard (form post, `text/plain`, missing or foreign `Origin`, `Sec-Fetch-Site: cross-site` → 403; JSON same-origin passes); body cap counted on a body without `Content-Length`; unknown fields → 422; idempotency key required and validated; the headers and CSP on the shell, the API and an error; asset caching vs `no-store`; wrong `Host` refused; `health` 200/503 with only the kind.
- **Done when:** `test_dashboard`, `test_typing`, `check_public` green. Deploy: none (nothing imports it).
- **Risk:** Starlette's own 404/405 for unmatched routes bypass the shape unless handled; the tests hit them.

**W2: typed schema, generators, contract tests (built on `phase6a`).**
- **Build:** `api_types.py` (the error body and `health`; each later step adds its models), `codegen.py` (§0: OpenAPI 3.1 from the endpoint table; TS interfaces, the error-code union and an `Endpoints` map from it), committed `dashboard/schema.json` and `dashboard/web/src/api/types.gen.ts`; `GET /api/v1/schema.json` (session).
- **Tests:** the committed files equal a fresh generation (stale = fail, with the command to run); every response in `test_dashboard` is validated against the committed schema by the test client wrapper; the generator rejects unsupported schema constructs; every endpoint has a schema entry; TS output for a fixture model covers objects, optional vs nullable, enums, arrays, unions, maps, refs.
- **Done when:** as W1. **Risk:** pydantic's schema output changes between versions (W10 pins it; the staleness test catches a drift).

**W3: TS frontend scaffold.** §3.8. **Tests:** tsc strict, `node:test` for the client (error mapping, idempotency-key reuse, CSRF header on every write) and the router, preact-render-to-string for the shell and the tooltip, the source checks, `npm audit` clean (high/critical). **Done when:** the Python wrapper test green with Node present; the shell renders at 390 px in a headless browser with zero CSP violations and console errors (screenshot in the report). **Risk:** Trusted Types and Preact (§3.4): if the shell can't run under it, drop `require-trusted-types-for` and say so (the design marks it as a proposal).

**W4: preview build (D-211).** §3.9. **Tests:** the preview bundle contains no `fetch` to anything but the mock (source check on the built file), its `index.html` has `connect-src 'none'`, fixtures type-check. **Done when:** the task-manager has the static output to deploy; it adds the review item.

**W5: sessions, CSRF token, `me`, role declarations.** §3.5; the CSRF token in the write guard; each endpoint's minimum role; `actions.py` actions `auth.session_*`, settings `auth_*`. **Tests:** lifetimes with a fake clock (idle, absolute, max sessions, rotation), the cookie flags, CSRF token missing/wrong/from another session, a revoked session refused at once, the route × role matrix (W9) against 5a's roles. **Done when:** as W1 plus the full suite (`actions.py` changed). **Risk:** a session touch per request becoming write load: at most one write per session per minute.

**W6: password, TOTP, recovery codes, login.** §3.6; `auth/login`, `auth/factor`, `auth/logout`; the master-key loader if 5b hasn't built it. **Tests:** §12's auth list for these factors: Argon2id round trip and parameter floor, TOTP window ±1 and replay of a used step refused, recovery codes single-use, the factor rule table, remembered device skips only the second factor and never step-up, rate limits and backoff, equal errors for unknown user and wrong password, seeds encrypted at rest (no plaintext seed in the DB file). **Risk:** a lost master key makes every TOTP seed unreadable: the master key is in every backup (§10.2, 5c B2).

**W7: setup, first registration, wizard, reset-auth.** §3.7. **Tests:** the setup code single-use, expiring, only its hash stored, constant-time compare; two racing registrations, one winner; setup closed once a person exists; recovery codes shown once; `window_tz` and `claude_plan` set through `actions.py`; `reset-auth` ends the user's sessions and factors.

**W8: auth views (TS).** Login, second factor, setup and wizard, recovery codes (shown once, with a "saved" confirmation), "no access yet", sign out, and the first-login time-zone popup (§3). **Tests:** component tests per view and role, the client's flows against the mock transport. Preview redeployed (D-211).

**W9: route × role matrix.** Every endpoint × owner / manager / editor / viewer / no grant / machine user (refused: no dashboard login) against its declared minimum role. Built with W5.

**W10: image and the web service.** The multi-stage Dockerfile of §10.1 (pinned Node by digest, `npm ci --ignore-scripts`, `npm audit`, `tsc`, tests, esbuild; runtime stage with `dist/` only), Python dependencies pinned with hashes (`requirements.txt`: starlette, uvicorn, pydantic, mcp and their trees; `pip-audit`), `afclaude web` (uvicorn on the internal network only, no published port). With 5b's compose. **Done when:** the image builds with a failing typecheck or test making the build fail (checked once on purpose); nothing deployed (8b).

**W11: docs.** Design §8 / §9.8 (the interpretations above), §13 6a status, README file table, the mypy list.

## 6. Questions for the owner

None open. D-292 answers the former question 1: a pinned, checksum-verified Node.js in AFClaude's own folder; see §0, §3.10, W0.
