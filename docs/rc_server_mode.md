# `claude rc` server mode: can AFClaude use it? (goal 11, D-052)

Research only, no code changed. Sources: `claude remote-control --help` and `claude --help` of
Claude Code 2.1.303 (the version installed when this was written), the official docs
(code.claude.com/docs/en/remote-control, /cross-session-messaging, /env-vars, /channels-reference),
the public changelog up to 2.1.307, and a read-only look at the host's running `claude rc` server
and `~/.claude/sessions/`. No server was started, no session was launched or messaged.

Binding inputs: D-042 (every session visible in RC, as if created in RC), D-052 (investigate
hooking into server mode; "sessions don't expire"), D-268 (the final build may need no RC
launches; the manager talks through the dashboard), D-269 (RC is a setting per agent category),
D-045 (no `--bg`), D-047 (permission modes), D-164 (sessions run on the host), D-288 (the owner
approved running the live test of §7: run it, noting the time first; pass → build the hook, fail
→ close goal 11).

## Verdict: partly

- **Launching AFClaude's sessions through a server: not usable.** A server can't be told from the
  host to create a session, and the flags AFClaude relies on don't reach its sessions (§3).
- **Hooking into the owner's server threads: usable for one thing.** AFClaude can't take over or
  resume them (that forks, PROGRESS 29.09.), but it can probably *post a message* into a live one
  over the documented cross-session messaging inbox, which starts a turn when the thread is idle.
  That would turn the dispatcher's "continue it from the app" skip (design §10.8, F2) into a real
  continue. Needs one live test first (§6).
- **Observing server threads: already possible** from `~/.claude/sessions/<pid>.json` (keepalive
  preflight reads it today); a hook could add the claude.ai session link (§4.3).

## 1. What server mode is

`claude remote-control` (alias `claude rc`) is a long-running process that registers the host with
Anthropic's API, polls for work (outbound HTTPS only, no inbound port) and serves up to
`--capacity` (default 32) sessions in the directory it was started in. One session is pre-created
at start (`--[no-]create-session-in-dir`); further sessions are created **from claude.ai/code or
the Claude app** (or a claude.ai project thread that asks to run on this computer). Spawn modes:
`same-dir` (default), `worktree` (one git worktree per on-demand session), `session` (classic
single-session mode, exits when that session ends). Other flags: `--name`,
`--remote-control-session-name-prefix`, `--permission-mode` (for spawned sessions),
`--[no-]chrome`, `-c/--continue`, `--session-id`, `--debug*`, `--verbose`. Global `claude` flags
are not carried to the sessions; any that would change what they can do (e.g. `--settings`) make
the server refuse to start.

What the host shows (owner's server, running since late September): each served thread is a child
process `claude --print --sdk-url https://api.anthropic.com/v1/code/sessions/cse_… --session-id
cse_… --input-format stream-json --output-format stream-json`, driven by the server over pipes,
with no terminal. Idle threads keep their child alive (children several days old were seen). Each
child registers in `~/.claude/sessions/<pid>.json` with `entrypoint: "sdk-cli"`, a
`bridgeSessionId` (`session_…`, the claude.ai URL id), a `messagingSocketPath`, `status`
idle/busy, and a transcript id derived from the `cse_` id (not one AFClaude could choose). The
children run the binary the server was started with, so the server's threads stay on an old
Claude Code until the server is restarted.

## 2. Do server sessions expire?

| | server mode (`claude rc`) | per-session (`claude --remote-control`, AFClaude today) |
|---|---|---|
| idle | thread child stays alive; no idle limit documented | process in tmux stays alive; no idle limit documented (the 60-min retirement was `--bg`, removed by D-045) |
| crash of one session | re-served when the owner sends it a message from a device (≥ 2.1.238) | offline until resumed locally (`ka_resume.sh` reattaches to the same claude.ai session) |
| network outage | server **exits after ~10 min**; `claude rc` again | retries for as long as the outage lasts |
| heartbeat loss ~30 min | (not documented for the server) | disconnects; `/remote-control` to reconnect |
| stop / restart | threads not archived; `claude rc` in the same dir brings them all back within ~4 h | `claude --resume` reattaches |
| new session | owner creates it from the phone/web, nothing local needed | only by a local launch |

So D-052's "sessions don't expire" holds for the convenience part (the owner can start and
re-serve sessions from the phone without touching the host), not as a general lifetime advantage:
on a long network outage the server is the one that dies.

## 3. Can a server spawn or adopt AFClaude's sessions?

No, on every path checked:

- **Spawn on request:** there is no local CLI, socket or file to ask a running server for a new
  session; only claude.ai/the app (Anthropic's internal API, which design §1 rules out driving)
  or the one session pre-created at server start.
- **One server per AFClaude session** (`claude rc --spawn session --name …` in tmux): this would
  work mechanically but gains nothing over `claude --remote-control` and loses what AFClaude needs:
  no `--model`/`--effort`/`--allowedTools`/initial prompt per launch (not carried; `--settings`
  refused), no chosen session id, no `--resume <uuid>` of an existing transcript, and no terminal
  UI, so `tmux send-keys`, the runner's `/compact` (D-238), owner-typing detection and the
  compaction marker flow all break. Commands sent as messages arrive as plain text and never run.
  Environment (e.g. the guard bypass) would be the server's, for every thread at once.
- **Adopt a running tmux session:** no such feature. `claude rc --session-id <id>` / `--continue`
  only bring back sessions *a server* was serving (within ~4 h of its stop); for sessions started
  with `--remote-control` the docs say to use `claude --resume`. Taking a server thread the other
  way (stop its child, resume in tmux) forks it (tested 29.09.; preflight refuses since, `RC_HELD`).
- **Auto-connect** (`remoteControlAtStartup: true` in user settings) is the official "every
  session in RC" switch, but it is per interactive process like `--remote-control` and would also
  hit every session the owner starts by hand; D-269's per-category flag is the finer tool.
- **Channels** (MCP push into a session) need `--channels` / the development flag on an
  interactive session; the flag is ignored in `-p`/SDK mode, which is what server threads are.

## 4. What AFClaude could hook into

### 4.1 The inbox socket of a live server thread (documented, the useful one)

Cross-session messaging (≥ 2.1.224, on by default) gives every session, `-p` and server threads
included, a per-user Unix socket (`messagingSocketPath`; `CLAUDE_CODE_MESSAGING_SOCKET` inside the
session). A message delivered to an **idle** session starts a new turn; a busy one reads it
between tool calls. Inbound rules: with no `crossSessionInbound` setting, a session that prompts
for permissions (default/auto/acceptEdits/dontAsk) delivers messages unless the sender claims to
bypass permissions; a bypassing session holds them (and a `-p` session drops a held message after
`dialogExpiry`, 5 min). The receiver is told the text came from another session, not the owner:
it can't approve prompts, change configuration or run slash commands, which is fine for a
neutral "continue" after a limit reset.

Hook-in shape: the dispatcher's approved continue of an RC_HELD stall (category `continue`) would,
instead of skipping, deliver the neutral continue text into that thread's socket, through a new
whitelisted host-bridge shape (D-164: the container can't reach host sockets; the docs say
container and host sessions can't message each other). Fail-fast (D-235): no live child / no
socket, a refused or held message, or no new transcript entry within the verify window is an
error + alert, never a silent skip.

Open: the documented part of the socket protocol is only the optional auth line
`{"type":"auth","token":…}`; the message line itself is not documented. Writing it by hand would
be an internal protocol (excluded by design §1). The documented sender is Claude's own
`SendMessage` tool, so the safe sender is a one-shot Haiku `claude -p` asked to send the text to
the thread by its session name / id (one small turn per continue), or a documented format if
Anthropic publishes one. A thread whose child has exited has no socket; only a message from a
device re-serves it, so those stay "continue it from the app".

### 4.2 Sending to AFClaude's own tmux sessions the same way (side finding)

The same inbox exists in every AFClaude-launched session. It would avoid `send-keys` racing the
owner's typing, but messages are not owner prompts (they carry the "from another session" frame,
and `/compact` can't be sent), so it is not a drop-in replacement for `ka_resume.sh`'s send-keys.
Not proposed now.

### 4.3 Observation

`~/.claude/sessions/<pid>.json` already gives, per server thread: pid, transcript id,
`bridgeSessionId` (link `claude.ai/code/<id>`), cwd, idle/busy, name, socket path. It is an
undocumented file format (AFClaude's preflight already depends on it). The documented alternative
is a hook: server threads load user/project settings, so a `SessionStart`/`Stop` hook sees
`CLAUDE_CODE_BRIDGE_SESSION_ID` and could report thread + link to AFClaude (F6 "RC link when
known", design §10.8). `claude agents --json` does not list server threads.

## 5. Auth, account, settings

- Same claude.ai login and subscription as everything else on the host (API keys unsupported; on
  a Team plan an Owner must have enabled the Remote Control toggle; usage counts against the same
  plan). The server's threads appear in the owner's account only.
- First start needs a terminal for the one-time "Enable Remote Control?" confirmation and for
  folder trust; a server can't be started headless in an untrusted dir.
- Transcripts of RC-connected sessions are stored on Anthropic's servers (true for AFClaude's
  `--remote-control` sessions today as well).
- `crossSessionInbound` (user/project settings) and permission deny rules on
  `SendMessage`/`ListAgents` can turn messaging off; if the owner ever sets `refuse`, §4.1 stops
  working and must fail loudly.

## 6. D-269 and the current launch path

D-269 is **implemented and deployed** (merged/deployed 21.10.): `remote_control.py`, settings
`remote_control_task_manager` / `_review` / `_continue` / `_test` in `actions.SETTINGS` (owner-only,
default true), read strictly by `keepalive.fire(category=…)` → `ka_resume.sh --no-remote-control`;
also `launch_nightly.sh` and `test_e2e_tmux.sh`; shown in the quickview export.

Server mode doesn't fit as a third value of these settings (§3). If §4.1 is built, it belongs to
the dispatcher's handling of RC_HELD stalls, independent of `remote_control_continue` (that setting
decides how AFClaude *launches* a continue; an injected message launches nothing). If D-268's
"no RC launches" is adopted, §4.1 still matters, since the owner's own server threads stall too.

## 7. Proposed live test (owner approved, D-288: run it, note the time first; pass → build §4.1, fail → close goal 11)

A throwaway thread briefly appears in the RC list during the test; stopped and archived at the end (step 4).

1. In a scratch git dir (trusted once by hand), start `claude rc --spawn session --name
   afc-rc-test --permission-mode auto` in a tmux window; note the child's `~/.claude/sessions`
   entry (`entrypoint`, `messagingSocketPath`, `bridgeSessionId`).
2. Leave it idle; from a one-shot `claude -p --model <haiku>` run in the same user, ask it to
   `SendMessage` "reply PONG" to that thread. Check: the thread starts a turn, the reply lands in
   its transcript, the owner's app shows it; how the sender addressed it (name vs id).
3. Repeat while the thread sits on a usage-limit notice (or a synthetic stand-in) to confirm a
   message resumes work after the reset.
4. Stop the server, run `claude rc --session-id <id>` in the same dir, confirm it comes back; then
   archive the thread from the app and stop everything.

Pass → build §4.1 as one worker branch (host-bridge shape, dispatcher path, tests, alert on every
failure). Fail → keep "continue it from the app" and close goal 11.

## 8. Live test 30.10. (D-288)

Claude Code 2.1.314, 03:07-03:12 UTC (04:07-04:12 Berlin). Server in tmux `afc-rc-test`, scratch dir
`data/rc-test` (no `.git` of its own: with one, `claude rc` asked for folder trust, which would have
meant editing `~/.claude.json`; without it the repo's existing trust applied and no prompt came). Three
Haiku `claude -p --allowedTools SendMessage ListAgents` senders plus one more for the resume check.

**Verdict: partial (step 3 skipped). The core of §4.1 works; build it with the constraints below.**

- **Step 1:** `claude rc --spawn session --name afc-rc-test --permission-mode auto` started with no
  prompt and printed the claude.ai session link. Its child (`--print --sdk-url …`) registered in
  `~/.claude/sessions/<pid>.json` with `entrypoint: "sdk-cli"`, `messagingSocketPath`,
  `bridgeSessionId`, `status: idle`. The thread's name was **derived** (`rc-test-f1`: directory
  name + 2 hex characters), not the `--name` value, and changed on every start (`rc-test-4f` after the resume).
- **Step 2:** the Haiku sender's `SendMessage` to `rc-test-f1` returned success ("in that session's
  inbox"); about 1 s later the idle thread started a turn: the transcript got a user entry
  `<cross-session-message from=… from-mode="prompting">reply PONG</cross-session-message>` plus the
  "not typed by your user" frame, then an assistant entry `PONG` (the thread ran on the default model,
  not on the sender's). `status` went busy and back to idle. Addressing: **by name only**.
  The session id (`e5409af6-…`) and the short ref `ListAgents` prints (`3de30d`) both failed with "No agent
  named … is reachable". `ListAgents` shows no socket path; the name has to be read from the
  session registry (`name` field) when sending. Not checked: that the owner's app shows the message
  (no way from the host).
- **Step 3: skipped.** No cheap synthetic usage-limit state exists (it would need a real limit
  notice in the thread); a busy-thread and a held-message case were not tried either.
- **Step 4:** `Ctrl-C` in the tmux window ended the server and its child (nothing left, registry
  entry gone). `claude rc --session-id session_015KMK…` (the `bridgeSessionId`) in the same dir
  brought the thread back with the same claude.ai link, same transcript (`e5409af6-…`) and a new pid;
  a message to it ("what were you asked to reply with earlier?") was answered `PONG`, so context
  survived. Everything was stopped; only the `afc-rc-test` tmux session was created and killed.
  The thread is not archived: the owner archives it in the app.
- **Stall scan:** `stalled.py scan` showed no hit for the test thread (it ended on a normal
  end_turn), and `keepalive` preflight already refuses sdk-cli threads (`RC_HELD`).
- **Side effect:** the thread ran in a directory under the repo, so it loaded the repo's CLAUDE.md
  and `UserPromptSubmit` hook (the "you are the manager" reminder). Owner threads will behave the same way.

What §4.1 needs if built: read the thread's current `name` and `status` from the registry at send
time (names are not stable across server restarts); send through a one-shot Haiku `claude -p`
(one sender transcript of about 0.006 USD, roughly 3 s) running on the host, via a whitelisted
host-bridge shape; verify success by a new `<cross-session-message>` user entry followed by an
assistant entry in the thread's transcript within the window, else error + alert (D-235); treat
"No agent named … reachable" (child gone, name changed) as that error. Cost of the turn itself is the
thread's model, not Haiku. Untested and to be covered by the build's own tests: a thread that is
busy, a bypass-permissions thread holding the message, and a real usage-limit stall.

## 9. Built (branch rc-continue, D-288)

§4.1 as specified, with the constraints of §8. `dispatcher.py` `Pass.continue_stalled`: when the preflight
refuses an approved stall as `keepalive.RC_LIVE` (a live rc-server thread child holds it), `continue_rc`
sends the neutral `continue_foreign` text into the thread instead of skipping; `keepalive.RC_EXITED` (the
child has exited) stays a quiet "continue it from the app" skip, retried every pass, now recorded in the
dispatcher state as `rc_held[<stall>] = {"at", "state": "app-only"}` (a busy thread: `"busy"`).

`rc_continue.send` (new module): reads the thread's registry entry at send time (one live `sdk-cli`
process with `--sdk-url` or an rc-server parent; its current `name` and `status`), refuses a busy thread
(skip, next pass) and, as errors, an unknown status, an unusable name or a child started with
`bypassPermissions` (it would hold the message); runs the Haiku sender `host.rc_send(name, text)`:
`claude -p --model haiku --permission-mode dontAsk --allowedTools SendMessage ListAgents
--strict-mcp-config --disable-slash-commands --output-format stream-json --verbose`, a fixed prompt, the
text between BEGIN/END; checks its stream-json (exactly one SendMessage whose input names that thread,
a result that is not "No agent named … is reachable", not an error, not held/refused); then polls the
thread's transcript for a new `<cross-session-message>` user entry followed by an assistant entry
within 180 s. Every failure (no registry entry, no live child, not reachable, held/refused, sender
timeout of 120 s or exit code, no verified entry) is recorded as `handled[<stall>]` result
`rc-send-error`, alerted and pauses automation (D-234/D-235); one try per stall (D-206). A success is
`rc-delivered` with the thread's name. Dry-run logs "WOULD MESSAGE" and sends nothing.

Host bridge: new whitelisted shape `rc-send <name>` (text on stdin, at most 16 KB UTF-8) in
`docker/host_exec.py`, which builds the same flags and prompt itself (the container chooses only the
name and the text). The forced command points at the live checkout's `docker/host_exec.py`, so the
merge activates it; `install_host_bridge.sh` does not need to run again.

Switched on by deploying (merge + container rebuild with `tools/deploy.py`); there is no separate
setting: it replaces the RC_HELD skip for live threads. Not live-tested here (no real server or message):
the sender's flags differ from §8 only by `--permission-mode dontAsk` (D-047), `--strict-mcp-config`,
`--disable-slash-commands` and stream-json output; the success text of a delivered SendMessage, a held
message's wording and the busy / bypass / usage-limit cases are covered by unit tests with a mocked
sender and registry only.
