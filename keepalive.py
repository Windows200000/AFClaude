#!/usr/bin/env python3
"""
Keep-alive prototype for the task-manager project (no dashboard).

Watches ONE Claude Code session's transcript. When the session's last message
is the synthetic "You've hit your ... limit" notice, it waits for the limit to
reset and then, only inside the nightly window 23:00-09:00 Europe/Berlin (it
spans midnight) and only
if the weekly budget rule allows it, resumes that session in place with

    ka_resume.sh --session <full-uuid> --message "<continue msg>"   (tmux, no --bg)

(a non-bg resume continues under the same ID and reconnects Remote Control; it
would only fork if another live process held the session, which preflight refuses).

Budget rule (user-specified, see memory task_manager_mcp_plan.md):
  projected end-of-week usage < 90%                          -> continue
  else weekly reset <= 11:00 Berlin after the current window -> continue
  else                                                       -> hold back
Forecast (my choice): linear extrapolation of the weekly % over the elapsed
part of the week, with elapsed floored at 24h so an early-week burst doesn't
explode the projection.

Default is DRY-RUN (decides and logs "WOULD FIRE", never resumes anything).
Pass --arm to actually fire. Create a file named STOP next to this script to
make a running watcher exit.

All log times are Europe/Berlin.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.environ.get("KEEPALIVE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
JOBS_DIR = os.path.expanduser("~/.claude/jobs")
# Per-process registry every live claude writes (<pid>.json: pid, sessionId, procStart,
# kind, entrypoint, ...). Both paths are injectable so tests can use a fake /proc.
SESSIONS_DIR = os.environ.get("KEEPALIVE_SESSIONS_DIR", os.path.expanduser("~/.claude/sessions"))
PROC_DIR = os.environ.get("KEEPALIVE_PROC_DIR", "/proc")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
STATE_DIR = os.environ.get("KEEPALIVE_STATE_DIR", HERE)
STATE_FILE = os.path.join(STATE_DIR, "keepalive_state.json")
LOCK_FILE = os.path.join(STATE_DIR, "keepalive.lock")
STOP_FILE = os.path.join(STATE_DIR, "STOP")
PROGRESS_FILE = os.environ.get("KEEPALIVE_PROGRESS_FILE", os.path.join(HERE, "PROGRESS.md"))
KA_RESUME = os.environ.get("KEEPALIVE_KA_RESUME", os.path.join(HERE, "ka_resume.sh"))  # tests use a stub

WINDOW_START = dtime(23, 0)         # Europe/Berlin, the evening before WINDOW_END
WINDOW_END = dtime(9, 0)            # Europe/Berlin, exclusive
WEEKLY_CUTOFF = dtime(11, 0)        # "reset no later than 11:00 after the window" (on the window-end day)
PROJECTION_THRESHOLD = 90.0         # percent
WEEK = timedelta(days=7)
MIN_ELAPSED = timedelta(hours=24)   # forecast floor

POLL_SECONDS = 30
RESET_GRACE = timedelta(seconds=90)       # fire this long after the reset time
USAGE_MAX_AGE = timedelta(minutes=10)     # refresh /usage if cache older than this
HOLD_RECHECK = timedelta(minutes=15)      # re-evaluate a HOLD decision this often
MAX_FIRES_PER_WINDOW = 4                  # safety cap per night
VERIFY_TIMEOUT = timedelta(minutes=10)

IGNORE_WINDOW = False  # --now: skip the window / window-start-hour gate (budget rule still applies)
BACKLOG_FILE = os.path.join(HERE, "BACKLOG.md")
TAKE_OVER_IDLE = False  # opt-in: SIGTERM an idle interactive holder (e.g. an open terminal)
# Core AFClaude sessions always run on Opus 5.5 with high effort (user rule, 2026-09-26);
# subagents they spawn may use other models.
LAUNCH = {"model": "claude-opus-5-5", "effort": "high", "name": "task-manager keep-alive build"}

SCRUBBED_ENV = {
    "HOME": os.path.expanduser("~"),
    "USER": "opc",
    "LOGNAME": "opc",
    "SHELL": "/bin/bash",
    "PATH": "/home/opc/.local/bin:/home/opc/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin",
    "LANG": "C.UTF-8",
    "TERM": "xterm-256color",
    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
}

def load_prompt(name):
    """prompts/<name>.md, the default prompts (the dashboard will surface/edit these)."""
    with open(os.path.join(HERE, "prompts", name + ".md")) as fh:
        return fh.read()


def session_message(name, afclaude=True, manager=True, **fields):
    """One-line message for a session: <name>.md + guard_respect.md + manager.md
    + manager_afclaude.md (the AFClaude-repo rules). afclaude=False leaves those
    rules out for sessions working elsewhere; manager=False leaves out both
    manager files (the user's own sessions get a neutral message). One line
    because tmux send-keys would submit at the first newline."""
    parts = [load_prompt(name).format(**fields), load_prompt("guard_respect")]
    if manager:
        parts.append(load_prompt("manager"))
        if afclaude:
            parts.append(load_prompt("manager_afclaude"))
    return " ".join(" ".join(parts).split())


LIMIT_TEXT_RE = re.compile(r"hit your (?P<kind>[\w ]*?)\s*limit", re.I)
RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s*)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*\((?P<tz>[^)]+)\)",
    re.I,
)
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def berlin(dt):
    return dt.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg):
    print(f"[{datetime.now(BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


# ---------------------------------------------------------------- transcript

def transcript_path(session_id):
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def iter_entries(path):
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a line still being written


def message_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


def last_message(path):
    """Last user/assistant entry. Metadata lines (system, last-prompt,
    cost-state, atis-latch, ...) routinely FOLLOW a limit notice, so the
    literal last line is not good enough."""
    last = None
    for e in iter_entries(path):
        if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
            last = e
    return last


def stall_info(entry):
    """Return a dict if `entry` is a synthetic usage-limit notice, else None."""
    if not entry or entry.get("type") != "assistant":
        return None
    msg = entry.get("message") or {}
    if msg.get("model") != "<synthetic>":
        return None
    text = message_text(entry)
    m = LIMIT_TEXT_RE.search(text)
    if not (entry.get("error") == "rate_limit" or m):
        return None
    kind_word = (m.group("kind") if m else "").lower()
    kind = "weekly" if "week" in kind_word else "session" if "session" in kind_word else kind_word or "unknown"
    ts = parse_ts(entry.get("timestamp"))
    return {
        "uuid": entry.get("uuid"),
        "timestamp": ts,
        "text": text,
        "kind": kind,
        "reset_from_text": parse_reset_text(text, ts or datetime.now(UTC)),
    }


def parse_reset_text(text, ref):
    """'resets 5pm (UTC)', 'resets 9:30am (UTC)', 'resets Oct 1, 4:59pm (UTC)'.
    Without a date: the first such clock time AFTER `ref` (the notice's own
    timestamp, not "now", so a watcher started late still gets it right)."""
    m = RESET_RE.search(text or "")
    if not m:
        return None
    tzname = m.group("tz").strip()
    try:
        tz = UTC if tzname.upper() == "UTC" else ZoneInfo(tzname)
    except Exception:
        return None
    h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    mi = int(m.group("m") or 0)
    ref_local = ref.astimezone(tz)
    if m.group("mon"):
        mon = MONTHS.get(m.group("mon").lower())
        if not mon:
            return None
        cand = datetime(ref_local.year, mon, int(m.group("day")), h, mi, tzinfo=tz)
        if cand < ref_local - timedelta(days=180):   # year wrap
            cand = cand.replace(year=cand.year + 1)
        return cand.astimezone(UTC)
    cand = ref_local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if cand <= ref_local:
        cand += timedelta(days=1)
    return cand.astimezone(UTC)


# ---------------------------------------------------------------- window

def in_window(now):
    t = now.astimezone(BERLIN).time()
    if WINDOW_START <= WINDOW_END:            # window within one calendar day
        return WINDOW_START <= t < WINDOW_END
    return t >= WINDOW_START or t < WINDOW_END   # window spans midnight


def current_window_end(now):
    """WINDOW_END (Berlin) of the window `now` is in, or of the next window.
    Its date names the window ("night"): fire caps and window-start dedup keys use it."""
    local = now.astimezone(BERLIN)
    d = local.date() if local.time() < WINDOW_END else local.date() + timedelta(days=1)
    return datetime.combine(d, WINDOW_END, tzinfo=BERLIN)


def next_window_start(now):
    """`now` if inside the window, else the next WINDOW_START (Berlin)."""
    if in_window(now):
        return now
    local = now.astimezone(BERLIN)
    start = datetime.combine(local.date(), WINDOW_START, tzinfo=BERLIN)
    return start if start > local else start + timedelta(days=1)


def is_window_start_hour(now):
    """--window-start acts only in WINDOW_START's Berlin hour. Cron fires it at 21:00 and
    22:00 UTC; exactly one of them is 23:xx Berlin (21:00 in CEST, 22:00 in CET)."""
    return now.astimezone(BERLIN).hour == WINDOW_START.hour


def window_start_key(now):
    """Dedup key of the window-start continue: the window's END date (its "night", as the
    fire cap uses), so a 23:00 start on 29.09. is window-start-2026-09-30. On the switch
    night that equals the old 00:00 start's key of 30.09., which is right: one per night."""
    return f"window-start-{current_window_end(now).date()}"


# ---------------------------------------------------------------- usage

def read_usage_cache():
    """Parse ~/.claude.json cachedUsageUtilization -> dict or None."""
    try:
        with open(CLAUDE_JSON) as fh:
            c = json.load(fh).get("cachedUsageUtilization") or {}
    except (OSError, json.JSONDecodeError):
        return None
    if not c.get("fetchedAtMs"):
        return None
    out = {"fetched_at": datetime.fromtimestamp(c["fetchedAtMs"] / 1000, UTC)}
    for lim in (c.get("utilization") or {}).get("limits") or []:
        key = {"session": "session", "weekly_all": "weekly"}.get(lim.get("kind"))
        if key:
            out[key] = {"percent": float(lim.get("percent") or 0),
                        "resets_at": parse_ts(lim.get("resets_at"))}
    util = (c.get("utilization") or {})
    for key, src in (("session", "five_hour"), ("weekly", "seven_day")):
        if key not in out and util.get(src):
            out[key] = {"percent": float(util[src].get("utilization") or 0),
                        "resets_at": parse_ts(util[src].get("resets_at"))}
    return out


def refresh_usage(cwd=HERE):
    """`claude -p /usage` is a local slash command (no model call); it prints
    fresh numbers and rewrites the ~/.claude.json cache. Run it scrubbed, with
    no session persistence so it leaves no transcript behind."""
    try:
        r = subprocess.run(["claude", "-p", "--no-session-persistence", "--permission-mode", "dontAsk", "/usage"],
                           cwd=cwd, env=SCRUBBED_ENV, capture_output=True,
                           text=True, timeout=120)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def fresh_usage(now, force=False):
    u = read_usage_cache()
    if force or not u or now - u["fetched_at"] > USAGE_MAX_AGE:
        rc, out = refresh_usage()
        u = read_usage_cache()
        if not u or now - u["fetched_at"] > USAGE_MAX_AGE:
            log(f"usage refresh failed or cache still stale (rc={rc}): {out.strip()[:200]!r}")
            return None
    return u


# ---------------------------------------------------------------- budget rule

def project_weekly(pct, resets_at, now):
    week_start = resets_at - WEEK
    elapsed = max(now - week_start, MIN_ELAPSED)
    remaining = max(resets_at - now, timedelta(0))
    return pct + pct * (remaining / elapsed)


def budget_decision(usage, now):
    """-> (go: bool, reason: str). Assumes `now` is inside the window."""
    if not usage or "weekly" not in usage or not usage["weekly"]["resets_at"]:
        return False, "HOLD: weekly usage unknown (fail-safe)"
    w = usage["weekly"]
    if w["percent"] >= 100 and w["resets_at"] > now:
        return False, f"HOLD: weekly limit exhausted until {berlin(w['resets_at'])}"
    proj = project_weekly(w["percent"], w["resets_at"], now)
    base = f"week {w['percent']:.0f}% used, projected {proj:.0f}% at reset {berlin(w['resets_at'])}"
    if proj < PROJECTION_THRESHOLD:
        return True, f"CONTINUE: {base} < {PROJECTION_THRESHOLD:.0f}%"
    cutoff = datetime.combine(current_window_end(now).date(), WEEKLY_CUTOFF, tzinfo=BERLIN)
    if w["resets_at"] <= cutoff:
        return True, f"CONTINUE: {base} >= {PROJECTION_THRESHOLD:.0f}%, but weekly reset <= {berlin(cutoff)}"
    return False, f"HOLD: {base} >= {PROJECTION_THRESHOLD:.0f}% and weekly reset after {berlin(cutoff)}"


# ---------------------------------------------------------------- session / firing

def agent_entries(session_id):
    """All `claude agents` rows for this session (a session can show up twice,
    e.g. a dead bg row plus a live interactive one)."""
    try:
        r = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True,
                           text=True, timeout=60, env=SCRUBBED_ENV)
        return [a for a in json.loads(r.stdout or "[]") if a.get("sessionId") == session_id]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def job_state(session_id):
    try:
        with open(os.path.join(JOBS_DIR, session_id[:8], "state.json")) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def tmux_name(session_id):
    return f"ka-{session_id[:8]}"


def tmux_alive(session_id):
    return subprocess.run(["tmux", "has-session", "-t", "=" + tmux_name(session_id)],
                          capture_output=True).returncode == 0


def tmux_pids(session_id):
    """Pane pids of our own tmux session ka-<id8> (empty if none)."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-s", "-t", "=" + tmux_name(session_id), "-F", "#{pane_pid}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(x) for x in r.stdout.split() if x.isdigit()} if r.returncode == 0 else set()


def pid_alive(pid):
    return bool(pid) and os.path.exists(os.path.join(PROC_DIR, str(pid)))


ARCHIVED_MARK = "this session was ended or archived from another device"
RC_HELD = "held by a `claude rc` server"
OTHER_HELD = "held by another live process"


def proc_argv(pid):
    try:
        with open(os.path.join(PROC_DIR, str(int(pid)), "cmdline"), "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except (OSError, ValueError):
        return []


def _proc_stat(pid):
    """Fields after the `(comm)` of /proc/<pid>/stat: [0]=state, [1]=ppid, [19]=starttime."""
    try:
        with open(os.path.join(PROC_DIR, str(int(pid)), "stat")) as fh:
            return fh.read().rsplit(")", 1)[1].split()
    except (OSError, ValueError, IndexError):
        return []


def proc_ppid(pid):
    try:
        return int(_proc_stat(pid)[1])
    except (ValueError, IndexError):
        return None


def proc_starttime(pid):
    try:
        return _proc_stat(pid)[19]
    except IndexError:
        return None


def session_entry(pid):
    """The ~/.claude/sessions/<pid>.json registry entry of a process, or {}."""
    try:
        with open(os.path.join(SESSIONS_DIR, f"{int(pid)}.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def registry_holders(session_id):
    """Live pids whose ~/.claude/sessions/<pid>.json holds `session_id`. This sees
    holders `claude agents --json` may not list. An entry whose procStart does
    not match the live process's start time is a stale file of a reused pid."""
    out = set()
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path) as fh:
                d = json.load(fh)
            pid = int(d.get("pid") or os.path.basename(path)[:-5])
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if d.get("sessionId") != session_id or not pid_alive(pid):
            continue
        start = d.get("procStart")
        if start is not None and proc_starttime(pid) not in (None, str(start)):
            continue
        out.add(pid)
    return out


def is_ours(pid, ours, depth=32):
    """True if `pid` is one of the pane pids `ours` (our tmux ka-<id8>) or runs under one."""
    while pid and depth > 0:
        if pid in ours:
            return True
        pid, depth = proc_ppid(pid), depth - 1
    return False


def is_rc_server(argv):
    """`claude rc` / `claude remote-control`: the server whose per-thread children
    (`--print --sdk-url ...`) hold the user's Remote Control threads."""
    args = argv[1:]
    return bool(args) and args[0] in ("rc", "remote-control") and "--print" not in args


def is_sdk_child(argv):
    """An rc-server thread child: `.../versions/<v> --print --sdk-url https://.../code/sessions/cse_...`."""
    return any(a == "--sdk-url" or a.startswith("--sdk-url=") for a in argv[1:])


def rc_held(pid):
    """True if `pid` is a `claude rc` server or one of its per-thread children:
    the server itself, anything with `--sdk-url` in its argv, anything whose
    registry entry says entrypoint "sdk-cli", or a child of an rc server.
    Taking such a holder over + resuming does NOT move the thread: killing the
    child does not free it either (the rc server re-serves the thread on the next
    app message and rebuilds the local transcript), so any take-over + resume
    makes a second RC thread with the same uuid (two writers on one transcript;
    live tests 29.09.). So never take it over."""
    argv = proc_argv(pid)
    if is_rc_server(argv) or is_sdk_child(argv) or session_entry(pid).get("entrypoint") == "sdk-cli":
        return True
    ppid = proc_ppid(pid)
    return bool(ppid and is_rc_server(proc_argv(ppid)))


def archived_since_last_message(path):
    """True if an RC 'ended or archived from another device' notice is newer than
    the session's last user/assistant entry."""
    if not path:
        return False
    archived = False
    for e in iter_entries(path):
        t = e.get("type")
        if t in ("user", "assistant") and not e.get("isSidechain"):
            archived = False
        elif t == "system" and ARCHIVED_MARK in str(e.get("content", "")):
            archived = True
    return archived


def preflight(session_id):
    """-> (ok, problems[], plan). No --bg anymore: the session is continued in
    its tmux session (send-keys) or resumed into a new one. A live process
    elsewhere would make a fresh resume FORK a copy, so (rc / registry checks first):
      - live in our tmux            -> plan 'send-keys'
      - live interactive elsewhere  -> refuse (someone's terminal), unless archived
                                       or --take-over-idle: SIGTERM it, then resume
      - held by a `claude rc` server -> always refuse (a take-over would fork the
                                       user's RC thread): the server, a child of it,
                                       argv with --sdk-url, or registry entrypoint sdk-cli
      - any other live pid in ~/.claude/sessions/*.json on this uuid that is not our
        tmux ka-<id8> process and not a `claude agents` row -> refuse (OTHER_HELD)
      - live bg worker              -> plan 'stop-bg-then-resume'
      - nothing alive               -> plan 'resume'"""
    in_tmux = tmux_alive(session_id)
    ours = tmux_pids(session_id) if in_tmux else set()
    # Every live process the session registry says holds this uuid, except our own
    # tmux ka-<id8> process (and its children).
    reg = sorted(p for p in registry_holders(session_id) if not is_ours(p, ours))
    rows = [] if in_tmux else [a for a in agent_entries(session_id)
                               if pid_alive(a.get("pid")) and not is_ours(a.get("pid"), ours)]
    row_pids = [a["pid"] for a in rows]
    held = sorted({p for p in row_pids + reg if rc_held(p)})
    if held:
        return False, [f"{RC_HELD} thread (pid {held}); a take-over would fork the user's Remote Control "
                       "thread, so it is left alone"], None
    extra = [p for p in reg if p not in row_pids]
    if extra:
        return False, [f"{OTHER_HELD} {extra} (~/.claude/sessions" + (", outside our tmux" if in_tmux else
                       ", not listed by `claude agents`") + "); a send or resume would make two writers "
                       "on one transcript, so it is left alone"], None
    if in_tmux:
        return True, [], "send-keys"
    if any(a.get("state") == "working" or a.get("status") == "busy" for a in rows):
        return False, ["session is busy in another process (someone is using it)"], None
    inter = [a for a in rows if a.get("kind") == "interactive"]
    if inter and archived_since_last_message(transcript_path(session_id)):
        # The user archived it in the RC/web UI: that ends Remote Control but leaves the
        # local process idle. Archiving counts as closing (user rule, 29.09.), so take it over.
        return True, [], "take-over:" + ",".join(str(a["pid"]) for a in inter)
    if inter and not TAKE_OVER_IDLE:
        return False, [f"held by an idle interactive process {[a['pid'] for a in inter]} outside tmux; "
                       "a resume would FORK a copy (pass --take-over-idle to SIGTERM it first)"], None
    if inter:
        # An IDLE interactive holder (e.g. a terminal left open) would make a
        # fresh resume fork. Take it over: SIGTERM it, then resume into tmux.
        return True, [], "take-over:" + ",".join(str(a["pid"]) for a in inter)
    if any(a.get("kind") == "background" for a in rows):
        return True, [], "stop-bg-then-resume"
    return True, [], "resume"


def fire(session_id, cwd, message, plan, new=False, name=None, model=None):
    """The one state-changing action: hand over to ka_resume.sh (tmux, no --bg).
    name/model override LAUNCH's for this launch."""
    if plan.startswith("take-over:"):
        import signal
        pids = [int(x) for x in plan.split(":", 1)[1].split(",")]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(30):
            if not any(pid_alive(p) for p in pids):
                break
            time.sleep(1)
    if plan == "stop-bg-then-resume":
        subprocess.run(["claude", "stop", session_id[:8]], env=SCRUBBED_ENV,
                       capture_output=True, timeout=60)
        time.sleep(5)
    cmd = [KA_RESUME, "--session", session_id, "--message", message,
           "--cwd", cwd]
    for k, v in {**LAUNCH, **({"name": name} if name else {}), **({"model": model} if model else {})}.items():
        if v:
            cmd += [f"--{k}", v]
    if new:
        cmd.append("--new")
    r = subprocess.run(cmd, env=SCRUBBED_ENV, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def verify_reply(path, since, timeout=VERIFY_TIMEOUT):
    """Wait for a real (non-synthetic) assistant message newer than `since`."""
    deadline = time.time() + timeout.total_seconds()
    while time.time() < deadline:
        for e in iter_entries(path):
            ts = parse_ts(e.get("timestamp"))
            m = e.get("message") or {}
            if (e.get("type") == "assistant" and ts and ts > since
                    and m.get("model") not in (None, "<synthetic>")):
                return e
        time.sleep(10)
    return None


def process_env_flags(pid):
    try:
        env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    except OSError:
        return None
    wanted = (b"CLAUDE_GUARD_DISABLE=", b"CLAUDE_EFFORT=", b"CLAUDECODE=", b"CLAUDE_CODE_CHILD_SESSION=",
              b"CLAUDE_CODE_MESSAGING_SOCKET=")
    return [x.decode() for x in env if x.startswith(wanted)]


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"handled": {}, "fires": {}}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1, default=str)
    os.replace(tmp, STATE_FILE)


def progress_note(line):
    with open(PROGRESS_FILE, "a") as fh:
        fh.write(f"- {datetime.now(BERLIN).strftime('%H:%M')} [keepalive.py] {line}\n")


# ---------------------------------------------------------------- main loop

def evaluate(session_id, now, usage_getter=fresh_usage):
    """One decision pass. Returns (action, detail, stall) where action is one
    of: NO_STALL, WAIT_RESET, WAIT_WINDOW, HOLD, FIRE."""
    path = transcript_path(session_id)
    if not path:
        return "NO_TRANSCRIPT", "transcript not found", None
    last = last_message(path)
    stall = stall_info(last)
    if not stall:
        if last and (last.get("message") or {}).get("model") == "<synthetic>" and last.get("isApiErrorMessage"):
            return "OTHER_ERROR", f"stopped on a non-limit API error (not handled): {message_text(last)[:120]!r}", None
        return "NO_STALL", "last message is not a limit notice", None
    # The notice text comes from the server and is authoritative. The usage
    # cache is only a fallback: it was a day stale when the rc_poc watcher
    # trusted it and fired 80 min early.
    reset = stall["reset_from_text"]
    if reset is None:
        u = (read_usage_cache() or {}).get(stall["kind"]) or {}
        reset = u.get("resets_at") if u.get("resets_at") and u["resets_at"] > stall["timestamp"] \
            else stall["timestamp"] + timedelta(hours=5)
    not_before = reset + RESET_GRACE
    if now < not_before:
        return "WAIT_RESET", f"{stall['kind']} limit resets {berlin(reset)}; fire not before {berlin(not_before)}", stall
    if not IGNORE_WINDOW and not in_window(now):
        return "WAIT_WINDOW", f"reset passed; outside window, next window {berlin(next_window_start(now))}", stall
    usage = usage_getter(now)   # only fetched when a fire is actually on the table
    s = (usage or {}).get("session") or {}
    if s.get("percent", 0) >= 100 and s.get("resets_at") and s["resets_at"] > now:
        return "WAIT_RESET", f"live usage still shows session limit 100% until {berlin(s['resets_at'])}", stall
    go, reason = budget_decision(usage, now)
    return ("FIRE" if go else "HOLD"), reason, stall


def run(args):
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another keepalive.py instance holds the lock; exiting")
    sid = args.session
    mode = "ARMED" if args.arm else "DRY-RUN"
    log(f"keepalive start ({mode}) target={sid} window {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M} Europe/Berlin pid={os.getpid()}")
    st = load_state()
    last_line = None
    next_eval = datetime.min.replace(tzinfo=UTC)
    while True:
        if os.path.exists(STOP_FILE):
            log("STOP file present, exiting")
            return
        now = datetime.now(UTC)
        if now >= next_eval:
            action, detail, stall = evaluate(sid, now)
            line = f"{action}: {detail}"
            if line != last_line:
                log(line)
                last_line = line
            next_eval = now + (HOLD_RECHECK if action == "HOLD" else timedelta(seconds=POLL_SECONDS))
            if action == "FIRE":
                handle_fire(sid, stall, detail, st, args)
                last_line = None
        if args.once:
            return
        time.sleep(POLL_SECONDS)


def handle_fire(sid, stall, reason, st, args):
    key = stall["uuid"] or str(stall["timestamp"])
    if key in st["handled"]:
        return
    night = current_window_end(datetime.now(UTC)).date().isoformat()
    if st["fires"].get(night, 0) >= MAX_FIRES_PER_WINDOW:
        log(f"fire cap reached for window ending {night}; not firing")
        return
    ok, problems, plan = preflight(sid)
    log(f"preflight: {'ok, plan=' + plan if ok else 'PROBLEMS: ' + '; '.join(problems)}")
    if not ok:
        st["handled"][key] = {"at": datetime.now(UTC).isoformat(), "result": "preflight-failed", "problems": problems}
        save_state(st)
        progress_note(f"did NOT fire for stall {key}: preflight failed: {problems}")
        alert(f"keep-alive did NOT continue {sid[:8]}: preflight failed", "; ".join(problems))
        return
    msg = session_message("continue", reason=reason, progress=PROGRESS_FILE)
    path = transcript_path(sid)
    # the last recorded cwd can be a directory that has since been renamed/deleted
    cwd = next((c for c in ((last_message(path) or {}).get("cwd"), HERE) if c and os.path.isdir(c)), HERE)
    if not args.arm:
        log(f"WOULD FIRE (dry-run): plan={plan} cwd={cwd} ka_resume.sh --session {sid} {LAUNCH} {msg!r}")
        st["handled"][key] = {"at": datetime.now(UTC).isoformat(), "result": "dry-run", "plan": plan, "reason": reason}
        save_state(st)
        progress_note(f"DRY-RUN would have continued {sid[:8]} via {plan} ({reason})")
        return
    sent_at = datetime.now(UTC)
    rc, out, err = fire(sid, cwd, msg, plan)
    st["fires"][night] = st["fires"].get(night, 0) + 1
    log(f"FIRED plan={plan} rc={rc} stdout={out.strip()!r} stderr={err.strip()!r}")
    result = {"at": sent_at.isoformat(), "plan": plan, "rc": rc, "stdout": out, "stderr": err, "reason": reason}
    reply = verify_reply(path, sent_at) if rc == 0 else None
    live = [a for a in agent_entries(sid) if pid_alive(a.get("pid"))]
    result["env"] = [process_env_flags(a["pid"]) for a in live]
    if reply:
        result["result"] = "continued-in-place"
        result["reply_model"] = (reply.get("message") or {}).get("model")
        log(f"VERIFIED: new assistant message in {sid[:8]} at {reply.get('timestamp')} "
            f"model={result['reply_model']} env={result['env']}")
    else:
        result["result"] = "no-reply-within-timeout" if rc == 0 else "launcher-failed"
        log(f"VERIFY FAILED: {result['result']}")
        alert(f"keep-alive continue of {sid[:8]} failed: {result['result']}",
              f"plan={plan} rc={rc} stdout={out.strip()[:300]} stderr={err.strip()[:500]}")
    st["handled"][key] = result
    save_state(st)
    progress_note(f"fired continue for {sid[:8]} via {plan}: {result.get('result')} ({reason})")


def load_backlog():
    """BACKLOG.md numbered items: '1. **Title** - description' (+ indented continuation lines)."""
    items, cur = [], None
    with open(BACKLOG_FILE) as fh:
        for line in fh:
            m = re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*\s*(?:[—-]+\s*)?(.*)$", line.rstrip("\n"))
            if m:
                cur = {"n": int(m.group(1)), "title": m.group(2), "desc": m.group(3).strip()}
                items.append(cur)
            elif cur and line.startswith("   ") and line.strip():
                cur["desc"] += " " + line.strip()
            else:
                cur = None
    return items


def work_on_project(sel, args):
    """Start a backlog project immediately in a NEW tmux session, ignoring the nightly
    window. The budget rule still applies. `sel` = list number or title substring."""
    items = load_backlog()
    hits = [i for i in items if (sel.isdigit() and i["n"] == int(sel)) or (not sel.isdigit() and sel.lower() in i["title"].lower())]
    if len(hits) != 1:
        sys.exit(f"--work-on {sel!r}: {'no match' if not hits else 'ambiguous'}; backlog: "
                 + "; ".join(f"{i['n']}={i['title']}" for i in items))
    p = hits[0]
    now = datetime.now(UTC)
    go, reason = budget_decision(fresh_usage(now, force=True), now)
    log(f"work-on {p['title']!r} (window ignored): {reason}")
    if not go:
        progress_note(f"work-on {p['title']!r} not started: {reason}")
        return
    sid = str(uuid.uuid4())
    slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-")
    msg = session_message("project_start", title=p["title"], desc=p["desc"], slug=slug)
    if not args.arm:
        log(f"WOULD START (dry-run): new session {sid} for {p['title']!r} cwd={HERE}")
        return
    sent_at = datetime.now(UTC)
    rc, out, err = fire(sid, HERE, msg, "resume", new=True, name=p["title"])
    log(f"STARTED project {p['title']!r} session={sid} rc={rc} stdout={out.strip()!r} stderr={err.strip()!r}")
    st = load_state()
    st.setdefault("projects", {})[sid] = {"title": p["title"], "at": sent_at.isoformat(), "rc": rc, "reason": reason}
    save_state(st)
    reply = None
    if rc == 0:
        deadline = time.time() + 120
        while not transcript_path(sid) and time.time() < deadline:
            time.sleep(5)
        path = transcript_path(sid)
        reply = verify_reply(path, sent_at) if path else None
    log(f"work-on {p['title']!r}: {'VERIFIED new assistant message' if reply else 'VERIFY FAILED'}")
    progress_note(f"started backlog project {p['title']!r} as session {sid[:8]} "
                  f"({'verified' if reply else 'NOT verified'}; {reason})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=os.environ.get("KEEPALIVE_SESSION"),
                    help="FULL session UUID to keep alive")
    ap.add_argument("--arm", action="store_true", help="actually fire resumes (default: dry-run)")
    ap.add_argument("--once", action="store_true", help="single evaluation pass, then exit")
    ap.add_argument("--decide", action="store_true",
                    help="print the window + budget decision for right now and exit")
    ap.add_argument("--window-start", action="store_true",
                    help="one-shot: at window start, continue the session if the budget rule allows")
    ap.add_argument("--now", action="store_true",
                    help="ignore the 23:00-09:00 window (and, with --window-start, the start-hour gate); budget rule still applies")
    ap.add_argument("--work-on", metavar="PROJECT",
                    help="start a BACKLOG.md project right now in a new tmux session (list number or title substring); "
                         "needs --arm to actually start")
    ap.add_argument("--take-over-idle", action="store_true",
                    help="if an idle interactive process (e.g. an open terminal) holds the session, SIGTERM it and resume in tmux")
    ap.add_argument("--model", default=LAUNCH["model"], help="model to relaunch with (tmux resume)")
    ap.add_argument("--effort", default=LAUNCH["effort"], help="effort to relaunch with")
    ap.add_argument("--name", default=LAUNCH["name"], help="Remote Control display name")
    args = ap.parse_args()
    LAUNCH.update(model=args.model, effort=args.effort, name=args.name)
    global TAKE_OVER_IDLE, IGNORE_WINDOW
    TAKE_OVER_IDLE = args.take_over_idle
    IGNORE_WINDOW = args.now
    if args.work_on:
        return work_on_project(args.work_on, args)
    if args.window_start and not args.session:
        sys.exit("--window-start needs --session")
    if args.window_start:
        # Start-of-window continue (no stall needed): budget rule + window, then fire.
        now = datetime.now(UTC)
        if not args.now and not is_window_start_hour(now):
            return
        go, reason = budget_decision(fresh_usage(now, force=True), now)
        log(f"window-start: {reason}")
        if go:
            key = f"manual-now-{now.isoformat()}" if args.now else window_start_key(now)
            stall = {"uuid": key, "timestamp": now}
            handle_fire(args.session, stall, "window start, " + reason, load_state(), args)
        else:
            progress_note(f"window-start {berlin(now)}: not resumed, {reason}")
        return
    if args.decide:
        now = datetime.now(UTC)
        u = fresh_usage(now, force=True)
        print(f"now {berlin(now)} in_window={in_window(now)} next_window={berlin(next_window_start(now))}")
        print(f"usage: {json.dumps(u, default=str)}")
        print(budget_decision(u, now))
        if args.session:
            print(evaluate(args.session, now, lambda n: u)[:2])
        return
    if not args.session or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", args.session):
        sys.exit("--session must be the FULL session UUID (a short id or a prefix makes resume fork)")
    run(args)


def alert(subject, body=""):
    """Failure alert (ALERTS.md + PROGRESS.md + best-effort push). Never raises."""
    log(f"ALERT: {subject}")
    try:
        from notify import notify
        log(f"alert push: {notify(subject, body)}")
    except Exception as e:  # noqa: BLE001 - alerts must not take the watcher down
        log(f"alert itself failed: {e!r}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # e.g. the 2026-09-29 00:00 fire died on "Permission denied: ka_resume.sh" and nobody noticed
        import traceback
        traceback.print_exc()
        alert(f"keepalive.py crashed: {type(e).__name__}: {e}"[:200], traceback.format_exc()[-1500:])
        sys.exit(1)
