#!/usr/bin/env python3
"""
Keep-alive for the AFClaude task-manager session (no dashboard).

Runs begin only at STARTS (D-205): each session-window start of the night window (cron
--window-start every 30 min, acting only when a start is due: 23:00 and 04:00 for the default
23:00 x 2, generally start + k x session_hours per weekday, schedule.py, D-202/D-203; a start postponed for
an active user is re-decided by the watcher before the next start, D-018) and each last-stretch
slot start (D-020). A start that passes the budget rule continues the task-manager session with

    ka_resume.sh --session <full-uuid> --message "<continue msg>"   (tmux, no --bg)

(a non-bg resume continues under the same ID and reconnects Remote Control; it
would only fork if another live process held the session, which preflight refuses).
Today only AFClaude has a task-manager, so a start = this session (picking the project by
rank/priority among several task-managers is design phase 3b).

No continue after a limit (D-204): when the session stops on the synthetic "You've hit your
... limit" notice, the run is done. The watcher only detects and logs it (once per stall: a
PROGRESS note); it never continues the session at the limit reset. The next session-window
start (or the next last-stretch slot start) decides by its own check; that start may well
find the previous run stopped at a limit and continue it (e.g. limit hit before 04:00, next
run at 04:00). Approved stalled sessions of the owner are the dispatcher's job (D-206).

A run ends at the earliest of (D-224, run_active()): the task-manager's clean stop (it runs
`keepalive.py --run-stopped`), a limit hit, RUN_IDLE without a transcript write, and the end of
its session window. The owner's own messages to the session never end a run.

Compaction (D-238): the watcher compacts the task-manager (save a hand-off, /compact, give the
hand-off back; D-233) when a run has ended and after 50 min without a request, while its prompt
cache is still warm, if its context is at or above the break-even from the stored cost model
(compact_timing.py); never at a start any more (compaction_pass).

Budget rule: by default pacing.py (the original linear rule evolved into a night gate: a
night runs a FULL session window only if the week is then predicted to end at or below the
reserve threshold (default: one session window left), w + session cost + the user's forecast
use until the weekly reset; re-checked per session window; the final <= 2 session windows
before the weekly reset fill to 100%; AFClaude yields to an active user by POSTPONING to last
activity + 60 min).
The setting usage_model = "linear" (DB settings, afclaude_config.setting) selects the original
linear rule, which is also the fallback if pacing.py fails:
  projected end-of-week usage < projection_threshold (90%)   -> continue
  else weekly reset <= window end + cutoff_after_window_hours
       (2 h: 11:00 Berlin after a 09:00 end)                  -> continue
  else                                                       -> hold back
Forecast (my choice): linear extrapolation of the weekly % over the elapsed
part of the week, with elapsed floored at 24h so an early-week burst doesn't
explode the projection.

Failures (D-234, D-235): any failure or timeout (a usage reading, the budget model, a host call, the
compaction steps, a continue without a reply, ...) is an error the owner sees: fail_stop() (an
ERROR line, an alert, automation_paused set with the reason; failstop.py), and what was going on is
aborted (a start before its continue went out is recorded as start-failed). No fallback to another
rule or older data, no silent retry on the next tick. While automation_paused is set (by a failure
or by hand) nothing starts or continues; the owner clears it: `python3 tasks.py automation resume`.
The task-manager's checks also hold while its own project is paused (D-244: an inbox error pauses
only the project the message was for; `automation resume --project NAME` clears it): every check
here asks failstop.paused(session=sid), the install-wide pause OR the pause of the project sid is
the task-manager of. --work-on (a backlog project, no session) checks the install-wide one only.

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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import afclaude_config
import runner_state  # phase 2d: the state lives in the DB only (D-161); the log is dual-written
import schedule
import usage_stale
import host  # host calls: local subprocess on the host, the SSH bridge inside the container

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
STATE_FILE = os.path.join(STATE_DIR, "keepalive_state.json")   # names the state; the DB holds it (D-161)
LOCK_FILE = os.path.join(STATE_DIR, "keepalive.lock")
STOP_FILE = os.path.join(STATE_DIR, "STOP")
PROGRESS_FILE = os.environ.get("KEEPALIVE_PROGRESS_FILE", os.path.join(HERE, "PROGRESS.md"))
KA_RESUME = os.environ.get("KEEPALIVE_KA_RESUME", os.path.join(HERE, "ka_resume.sh"))  # tests use a stub

# Automation windows: schedule.py (the window_days / window_tz / session_hours settings, read on
# every call; default every night 23:00 x 2 = 23:00-09:00 Europe/Berlin). The linear rule's
# projection threshold (90%) and reset cutoff (window end + 2 h = 11:00) are settings too, read
# per decision.
WEEK = timedelta(days=7)
MIN_ELAPSED = timedelta(hours=24)   # forecast floor

POLL_SECONDS = 30
RESET_GRACE = timedelta(seconds=90)       # fire this long after the reset time
USAGE_MAX_AGE = timedelta(minutes=10)     # refresh /usage if cache older than this
REFRESH_OK_AGE = timedelta(minutes=2)     # /usage left the cache this young: OK (claude skips the rewrite < 60 s)
POKE_MIN_INTERVAL = timedelta(minutes=30) # at most one token-refresh request per this (sampler + watcher + cron)
USAGE_STATE_FILE = os.path.join(STATE_DIR, "usage_refresh_state.json")
HOLD_RECHECK = timedelta(minutes=15)      # re-evaluate a HOLD decision this often
MAX_FIRES_PER_WINDOW = 4                  # safety cap per night
VERIFY_TIMEOUT = timedelta(minutes=10)
TAKE_OVER_WAIT = 30                       # seconds for a SIGTERMed holder to exit before a take-over resume

IGNORE_WINDOW = False  # --now: skip the window-start-hour gate (budget rule still applies)
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


def session_message(name, afclaude=True, manager=True, insert=None, hooked=False, **fields):
    """One-line message for a session: <name>.md + the standing rules. insert: text that goes
    right after <name>.md (the hand-off after a compaction).
    hooked=True (a session started in this repo, rules_hooked(cwd)): the standing rules already
    come from the repo's SessionStart hook (D-225), so only the one-line pointer
    (rules_pointer.md) follows. Otherwise they are inline: guard_respect.md + manager.md +
    manager_afclaude.md (the AFClaude-repo rules) + afk_rules.md; afclaude=False leaves out
    manager_afclaude.md, manager=False leaves out all but guard_respect.md (the user's own
    sessions get a neutral message). One line because tmux send-keys would submit at the first
    newline."""
    parts = [load_prompt(name).format(**fields)] + ([insert] if insert else [])
    if hooked and manager:
        parts.append(load_prompt("rules_pointer"))
    elif manager:
        parts += [load_prompt("guard_respect"), load_prompt("manager")]
        if afclaude:
            parts.append(load_prompt("manager_afclaude").format(session_stop_pct=session_stop_pct()))
        parts.append(load_prompt("afk_rules"))
    else:
        parts.append(load_prompt("guard_respect"))
    return " ".join(" ".join(parts).split())


# The standing rules the repo's SessionStart hook injects (.claude/settings.json, D-225), in two
# parts, one hook each: Claude Code caps every hook's additionalContext at 10,000 characters
# (anything longer is replaced by a file path and a 2,000-character preview).
RULES_PARTS = {"manager": "the task-manager rules",
               "afclaude": "the AFClaude-repo rules, the guard rule and the AFK rules"}
HOOK_CONTEXT_CAP = 10000


def rules_hooked(cwd):
    """True when a session in cwd gets the standing rules from the SessionStart hook: only a
    session started in this checkout's root (where .claude/settings.json is read; the
    container runs the code under the same path as the host repo)."""
    try:
        return bool(cwd) and os.path.realpath(cwd) == os.path.realpath(HERE)
    except (OSError, ValueError):
        return False


def standing_rules(part):
    """One part of the standing rules (RULES_PARTS), with a header line; variables filled as
    in session_message ({session_stop_pct}). Newlines kept: this is hook context, not tmux."""
    if part == "manager":
        body = [load_prompt("manager")]
    elif part == "afclaude":
        body = [load_prompt("manager_afclaude").format(session_stop_pct=session_stop_pct()),
                load_prompt("guard_respect"), load_prompt("afk_rules")]
    else:
        raise ValueError(f"unknown standing-rules part {part!r} (one of {', '.join(RULES_PARTS)})")
    head = load_prompt("session_rules").format(part=list(RULES_PARTS).index(part) + 1, what=RULES_PARTS[part])
    return "\n\n".join(p.strip() for p in [head] + body)


def session_rules_hook(part, hook_input, project_dir=None):
    """The SessionStart hook's JSON (D-225) for one part of the standing rules, or None when the
    session did not start in this repo's root (nothing is injected then). hook_input: the hook's
    stdin JSON (its cwd). Never raises: if the rules can't be built, the context says where they
    are instead."""
    cwd = (hook_input or {}).get("cwd") or project_dir or os.getcwd()
    if not rules_hooked(cwd):
        return None
    try:
        text = standing_rules(part)
    except Exception as e:   # noqa: BLE001 - a hook must never fail the session start
        text = (f"[AFClaude standing rules, part {part}: could not be built ({type(e).__name__}: {e}). "
                f"Read {os.path.join(HERE, 'prompts')}/manager.md, manager_afclaude.md ({{session_stop_pct}} = "
                f"the session_stop_pct setting, default 95), guard_respect.md and afk_rules.md now and follow them.]")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def print_session_rules(part):
    """--session-rules: the hook command. Prints the hook JSON (nothing outside this repo's
    root) and always exits 0, so a problem never shows up as a hook error at session start."""
    try:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        hook_input = {}
    out = session_rules_hook(part, hook_input if isinstance(hook_input, dict) else {},
                             os.environ.get("CLAUDE_PROJECT_DIR"))
    if out is not None:
        print(json.dumps(out))


def session_stop_pct():
    """The task-manager's clean session stop (the session_stop_pct setting, D-014), e.g. "95"."""
    try:
        return f"{float(afclaude_config.setting('session_stop_pct')):g}"
    except Exception:   # noqa: BLE001 - a message must still go out
        return "95"


LIMIT_TEXT_RE = re.compile(r"hit your (?P<kind>[\w ]*?)\s*limit", re.I)
RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s*)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*\((?P<tz>[^)]+)\)",
    re.I,
)
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def berlin(dt):
    if dt is None:
        return "none (no window on any day)"
    return dt.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg):
    line = f"[{datetime.now(BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    print(line, flush=True)
    runner_state.tee("keepalive", line)    # stdout IS the live keepalive.log: the line into app_log too


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
# All window code is schedule.py (design §6): the per-weekday windows of the window_days setting
# in window_tz, each N whole session windows (also on the DST nights, D-148); the session-window
# starts (D-202/D-203), the start due for a cron run (due_session_start), the postponement deadline
# and the dedup key per start (start_key). It reads the settings on every call.


# ---------------------------------------------------------------- usage

_CACHE_ERROR = None   # why the last read_usage_cache() failed (usage_cache_checked raises it)


def read_usage_cache():
    """Parse ~/.claude.json cachedUsageUtilization -> dict or None (no cache, or the read failed:
    the file, the bridge, a timeout, bad JSON; the read-only views use this)."""
    global _CACHE_ERROR
    _CACHE_ERROR = None
    try:
        if host.in_container():
            c = host.usage_cache()
        else:
            with open(CLAUDE_JSON) as fh:
                c = json.load(fh).get("cachedUsageUtilization") or {}
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        _CACHE_ERROR = e
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


def usage_cache_checked():
    """read_usage_cache() for the runners' decision paths: a read that failed raises (an error
    there, never "no cache", D-234/D-235); no cache at all is still None."""
    u = read_usage_cache()
    if u is None and _CACHE_ERROR is not None:
        raise _CACHE_ERROR
    return u


def refresh_usage(cwd=HERE):
    """`claude -p /usage` is a local slash command (no model call); it prints
    fresh numbers and rewrites the ~/.claude.json cache. Run it scrubbed, with
    no session persistence so it leaves no transcript behind."""
    try:
        r = host.run_on_host(["claude", "-p", "--no-session-persistence", "--permission-mode", "dontAsk", "/usage"],
                             cwd=cwd, env=SCRUBBED_ENV, timeout=120)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


# A minimal real model request (Haiku, no tools, no session file). Its argv must stay one of the
# shapes docker/host_exec.py whitelists for `claude -p --model haiku` (prompt on stdin).
POKE_ARGV = ["claude", "-p", "--model", "haiku", "--no-session-persistence", "--output-format", "json",
             "--tools=", "--strict-mcp-config", "--permission-mode", "dontAsk", "--disable-slash-commands"]
POKE_PROMPT = "Reply with the single word OK."
LAST_REFRESH = {}   # the latest refresh_usage_checked() result (for log lines and alerts)


def _load_usage_state():
    return runner_state.load("keepalive.usage_refresh", USAGE_STATE_FILE)   # the DB (D-161)


def _save_usage_state(d):
    if not runner_state.save("keepalive.usage_refresh", d, USAGE_STATE_FILE):
        log("usage state not saved (recorded in data/ALERTS.fallback.md)")


def poke_auth(cwd=HERE, now=None):
    """One minimal real model request, to renew the OAuth access token. `-p /usage` is a local
    command: on 03./04.10.2026 it printed only the cost summary and refreshed nothing for
    34 h, until the first real request renewed the token (see usage_stale.py). Throttled to
    one per POKE_MIN_INTERVAL across all callers. -> short result text."""
    now = now or datetime.now(UTC)
    st = _load_usage_state()
    last = usage_stale.ts(st.get("poke_at"))
    if last and timedelta(0) <= now - last < POKE_MIN_INTERVAL:
        return f"skipped (last token-refresh request {berlin(last)})"
    st["poke_at"] = now.isoformat()
    _save_usage_state(st)
    try:
        r = host.run_on_host(POKE_ARGV, input=POKE_PROMPT, cwd=cwd, env=SCRUBBED_ENV, timeout=180)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return f"failed: {type(e).__name__}: {e}"[:200]
    try:
        o = json.loads(r.stdout or "{}")
        res = f"is_error={o.get('is_error')} result={str(o.get('result'))[:60]!r}"
    except ValueError:
        res = f"output {(r.stdout or r.stderr or '').strip()[:80]!r}"
    return f"rc={r.returncode} {res}"


def _refreshed(u, before, now):
    """/usage did its job: the cache's fetch time moved on, or it is very young (claude does not
    rewrite a cache younger than 60 s, so 'unchanged' alone is no failure)."""
    if not u:
        return False
    f = u["fetched_at"]
    return (before is not None and f > before) or timedelta(0) <= now - f <= REFRESH_OK_AGE


def refresh_usage_checked(cwd=HERE, now=None):
    """The ONE usage refresh with retry, used by the sampler and the watcher / window-start paths:
    /usage; if the cache did not refresh, one cheap real request (poke_auth: renews an expired
    OAuth token) and /usage again. This one retry is DELIBERATE (D-235 allows deliberate,
    documented retries; task-manager decision 14.10.): `-p /usage` alone does not renew an expired
    token (the 03./04.10. incident, usage_stale.py). If it still did not refresh, the callers treat
    it as an error (fresh_usage: fail_stop), never a second silent retry. -> dict rc, out (last /usage output), usage (cache after,
    or None), ok (refreshed), retried, poke (poke_auth result or None)."""
    now = now or datetime.now(UTC)
    before = (read_usage_cache() or {}).get("fetched_at")
    rc, out = refresh_usage(cwd)
    u = read_usage_cache()
    res = {"rc": rc, "out": out, "usage": u, "ok": _refreshed(u, before, now), "retried": False, "poke": None}
    if not res["ok"]:
        log(f"/usage did not refresh the cache (rc={rc}, cache from "
            f"{berlin(u['fetched_at']) if u else 'n/a'}): {out.strip()[:120]!r}; trying a token-refresh request")
        res["poke"] = poke_auth(cwd, now)
        res["retried"] = True
        log(f"token-refresh request: {res['poke']}")
        rc, out = refresh_usage(cwd)
        u = read_usage_cache()
        res.update(rc=rc, out=out, usage=u, ok=_refreshed(u, before, max(now, datetime.now(UTC))))
    LAST_REFRESH.clear()
    LAST_REFRESH.update(res, at=now)
    return res


USAGE_FAILED = "usage reading failed: /usage did not refresh the usage cache"


def usage_failure_detail(r):
    """The alert text of a failed usage refresh (refresh_usage_checked's result `r`)."""
    cache = (r.get("usage") or read_usage_cache() or {}).get("fetched_at")
    return (f"`claude -p /usage` did not refresh ~/.claude.json (cache from {berlin(cache) if cache else 'n/a'}), "
            f"also not after a token-refresh request ({r.get('poke') or 'not tried'}); rc={r.get('rc')}. Last "
            f"/usage output: {(r.get('out') or '').strip()[:300]!r}. A real Claude request (or `claude /login`) "
            "on the host usually fixes it.")


def fresh_usage(now, force=False, display=False):
    """The usage cache, refreshed first if forced or older than USAGE_MAX_AGE. A refresh that
    fails (also after refresh_usage_checked's one token-refresh retry) is an error (D-234/D-235):
    fail_stop (alert, automation paused) and None; the caller starts nothing. display=True (a
    read-only view: --decide, usage_report.py) never pauses: it only logs the error and
    returns None."""
    u = read_usage_cache()
    if force or not u or now - u["fetched_at"] > USAGE_MAX_AGE:
        r = refresh_usage_checked(now=now)
        u = r["usage"]
        if not u or now - u["fetched_at"] > USAGE_MAX_AGE:
            if display:
                log(f"ERROR (display only, not paused): {USAGE_FAILED}: {usage_failure_detail(r)}")
            else:
                fail_stop(USAGE_FAILED, usage_failure_detail(r))
            return None
    return u


# ---------------------------------------------------------------- budget rule

def project_weekly(pct, resets_at, now):
    week_start = resets_at - WEEK
    elapsed = max(now - week_start, MIN_ELAPSED)
    remaining = max(resets_at - now, timedelta(0))
    return pct + pct * (remaining / elapsed)


def _budget_model():
    """pacing.py (forecast-driven pacing) unless the usage_model setting is "linear"."""
    if afclaude_config.usage_model() == "linear":
        return None
    import pacing
    return pacing


def _ratio_text(extra, ratio):
    return f" ≈ {extra / ratio:.0f}% of a session window" if ratio and extra is not None else ""


BUDGET_ADVICE = "; check usage_report.py / keepalive.py --decide and stop before exceeding it"


def budget_eval(usage, now, display=False):
    """THE budget evaluation every caller uses (watcher, window-start, last mile, deferred
    window-start, --decide, --work-on, the dispatcher gate, the quickview), so they all show
    the same number. -> dict(go, reason, headroom, text, postpone, recheck_at, last_mile,
    session_cap). Model: afclaude_config.usage_model(). A failure of pacing.py is an error
    (D-234/D-235): fail_stop (alert, automation paused) and a HOLD (failed_eval, mode "failed"),
    no fallback to another rule. display=True (the quickview, --decide): the same HOLD with the
    error in it, but never a pause (a display path never pauses; the runners that hit the same
    failure do)."""
    try:
        m = _budget_model()
        if m:
            d = m.decide(usage, now)
            pct = ((usage or {}).get("weekly") or {}).get("percent")
            text = m.budget_text(d, pct)
            if d["headroom"] is not None:
                text += BUDGET_ADVICE
            return dict(d, text=text, last_mile=d.get("mode") == "last_mile")
        return linear_eval(usage, now)
    except Exception as e:   # noqa: BLE001 - any model failure: an error, nothing starts
        if not display:
            fail_stop("budget model failed", f"{type(e).__name__}: {e}"[:500])
        return failed_eval(f"{type(e).__name__}: {e}")


def failed_eval(why):
    """The decision after a budget-model failure: HOLD, nothing runs (D-234/D-235)."""
    return {"go": False, "reason": f"HOLD: budget model failed ({why})"[:300], "headroom": None,
            "text": "budget for this run: none (budget model failed)", "postpone": False, "recheck_at": None,
            "last_mile": False, "session_cap": 85.0, "mode": "failed"}


def budget_decision(usage, now):
    """-> (go: bool, reason: str), from budget_eval()."""
    d = budget_eval(usage, now)
    return d["go"], d["reason"]


def budget_headroom(usage, now):
    """How much more weekly % this run may use -> (extra_weekly_pct | None, text), from
    budget_eval(): the same number as in the decision reason."""
    d = budget_eval(usage, now)
    return d["headroom"], d["text"]


def _limit_ratio():
    try:
        import limit_ratio
        r = limit_ratio.compute(limit_ratio.load_samples(limit_ratio.SAMPLES), now=datetime.now(UTC))
        return limit_ratio.preferred_ratio(r).get("value")   # per-window estimate when available
    except Exception:   # noqa: BLE001 - the ratio text is advisory
        return None


def linear_eval(usage, now, reason_suffix=""):
    go, reason = linear_budget_decision(usage, now)
    extra, text = linear_budget_headroom(usage, now)
    if extra is not None:
        reason += f"; budget for this run +{extra:.1f}%"
        text += _ratio_text(extra, _limit_ratio()) + BUDGET_ADVICE
    lm = in_last_mile(now, usage or {"weekly": {}})
    return {"go": go, "reason": reason + reason_suffix, "headroom": extra, "text": text, "postpone": False,
            "recheck_at": None, "last_mile": lm, "session_cap": 100.0 if lm else 85.0, "mode": "linear"}


def last_mile_hours(weekly_pct, resets_at=None, now=None):
    """Last-stretch length in hours: the last_mile_hours setting, by default "auto" =
    min(ceil(session windows of quota left), 2) x session length (pacing.py)."""
    setting = afclaude_config.last_mile_setting()
    if setting != "auto":
        return setting
    import pacing
    if weekly_pct is None:
        return pacing.SESSION_H
    ratio, _ = pacing.ratio_info()
    return pacing.last_mile_hours(weekly_pct, ratio, "auto")


def last_mile_left(resets_at, now, weekly_pct=None):
    """Time left until the weekly reset if `now` is inside the last-mile period, else None."""
    if not resets_at:
        return None
    lm = timedelta(hours=last_mile_hours(weekly_pct, resets_at, now))
    if lm <= timedelta(0):
        return None
    left = resets_at.astimezone(UTC) - now.astimezone(UTC)   # UTC: DST-safe
    return left if timedelta(0) < left <= lm else None


def in_last_mile(now, usage=None):
    """Last-mile check from given usage or the cached usage (no refresh)."""
    w = (usage or read_usage_cache() or {}).get("weekly") or {}
    return last_mile_left(w.get("resets_at"), now, w.get("percent")) is not None


def linear_settings():
    """The linear rule's settings: (projection threshold %, reset cutoff after the window end)."""
    s = afclaude_config.settings("projection_threshold", "cutoff_after_window_hours")
    return s["projection_threshold"], timedelta(hours=s["cutoff_after_window_hours"])


def linear_budget_headroom(usage, now):
    """Linear rule: how much more weekly % can be used before the projection reaches the
    projection_threshold setting (in the last mile: up to 100%). -> (extra | None, text)."""
    w = (usage or {}).get("weekly") or {}
    if w.get("percent") is None or not w.get("resets_at"):
        return None, "budget unknown"
    pct, reset = float(w["percent"]), w["resets_at"]
    if last_mile_left(reset, now, pct) is not None:
        extra, target = max(100.0 - pct, 0.0), "100% (last mile)"
    else:
        elapsed = max(now - (reset - WEEK), MIN_ELAPSED)
        remaining = max(reset - now, timedelta(0))
        thr, _ = linear_settings()
        extra = max(thr * elapsed / (elapsed + remaining) - pct, 0.0)
        target = f"a projected {thr:.0f}%"
    return extra, f"budget for this run: about +{extra:.1f} weekly % (now {pct:.0f}%) before reaching {target}"


def linear_budget_decision(usage, now):
    """The linear rule (projection < projection_threshold, the reset cutoff window end +
    cutoff_after_window_hours, last mile) -> (go, reason)."""
    if not usage or "weekly" not in usage or not usage["weekly"]["resets_at"]:
        return False, "HOLD: weekly usage unknown (fail-safe)"
    w = usage["weekly"]
    if w["percent"] >= 100 and w["resets_at"] > now:
        return False, f"HOLD: weekly limit exhausted until {berlin(w['resets_at'])}"
    left = last_mile_left(w["resets_at"], now, w["percent"])
    if left is not None:
        # last-mile rule (owner, 30.09.): the remaining quota would expire unused
        return True, (f"CONTINUE: last mile, weekly reset in {int(left.total_seconds() // 3600)}h"
                      f"{int(left.total_seconds() % 3600 // 60):02d}m ({w['percent']:.0f}% used): using the remaining quota")
    proj = project_weekly(w["percent"], w["resets_at"], now)
    base = f"week {w['percent']:.0f}% used, projected {proj:.0f}% at reset {berlin(w['resets_at'])}"
    thr, after = linear_settings()
    if proj < thr:
        return True, f"CONTINUE: {base} < {thr:.0f}%"
    win = schedule.current_or_next(now)
    cutoff = (win.end if win else now) + after
    if w["resets_at"] <= cutoff:
        return True, f"CONTINUE: {base} >= {thr:.0f}%, but weekly reset <= {berlin(cutoff)}"
    return False, f"HOLD: {base} >= {thr:.0f}% and weekly reset after {berlin(cutoff)}"


# ---------------------------------------------------------------- session / firing

def agent_entries(session_id):
    """All `claude agents` rows for this session (a session can show up twice,
    e.g. a dead bg row plus a live interactive one). A failure (timeout, bridge, exit code,
    bad JSON) raises (OSError / TimeoutExpired / ValueError): an empty list would read as
    "nothing holds the session" and a resume could fork it (D-234/D-235)."""
    r = host.run_on_host(["claude", "agents", "--json", "--all"], timeout=60, env=SCRUBBED_ENV)
    if r.returncode != 0:
        raise OSError(f"`claude agents` failed rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
    return [a for a in json.loads(r.stdout or "[]") if a.get("sessionId") == session_id]


def job_state(session_id):
    try:
        with open(os.path.join(JOBS_DIR, session_id[:8], "state.json")) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def tmux_name(session_id):
    return f"ka-{session_id[:8]}"


def tmux_alive(session_id):
    return host.run_on_host(["tmux", "has-session", "-t", "=" + tmux_name(session_id)],
                            timeout=30).returncode == 0


def tmux_pids(session_id):
    """Pane pids of our own tmux session ka-<id8> (empty if none). A failed host call raises
    (preflight() refuses then)."""
    r = host.run_on_host(["tmux", "list-panes", "-s", "-t", "=" + tmux_name(session_id), "-F", "#{pane_pid}"],
                         timeout=30)
    return {int(x) for x in r.stdout.split() if x.isdigit()} if r.returncode == 0 else set()


def pid_alive(pid):
    if host.in_container():   # the container's /proc is not the host's
        return bool(pid) and host.proc_info(pid) is not None
    return bool(pid) and os.path.exists(os.path.join(PROC_DIR, str(pid)))


ARCHIVED_MARK = "this session was ended or archived from another device"
RC_HELD = "held by a `claude rc` server"
OTHER_HELD = "held by another live process"


def proc_argv(pid):
    if host.in_container():   # argv of claude / keepalive.py processes only
        return list((host.proc_info(pid) or {}).get("argv") or [])
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
    if host.in_container():
        return (host.proc_info(pid) or {}).get("ppid")
    try:
        return int(_proc_stat(pid)[1])
    except (ValueError, IndexError):
        return None


def proc_starttime(pid):
    if host.in_container():
        return (host.proc_info(pid) or {}).get("start")
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


def latest_entrypoint(path):
    """`entrypoint` of the session's latest user/assistant entry: "sdk-cli" for
    turns written by an rc-server thread child (or another SDK host), "cli" for
    terminal / tmux turns. Only the LATEST counts: a session can carry old
    "sdk-cli" entries (e.g. from a former --bg start) and be a terminal one now."""
    if not path:
        return None
    return (last_message(path) or {}).get("entrypoint")


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
    """_preflight(), failing safe (refuse) if the host's process info is unavailable
    (inside the container it comes over the SSH bridge)."""
    try:
        return _preflight(session_id)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, [f"host process info unavailable ({type(e).__name__}: {str(e)[:200]}); not touching the session"], None


def _preflight(session_id):
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
      - nothing alive, latest user/assistant entry has entrypoint "sdk-cli"
                                    -> refuse (RC_HELD: an idle rc-server/SDK thread)
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
    if latest_entrypoint(transcript_path(session_id)) == "sdk-cli":
        # Nothing alive, but the last turn came from an rc-server/SDK host: the rc
        # server re-serves the thread on the next app message, so a resume now would
        # FORK the user's Remote Control thread once it's used again.
        return False, [f"{RC_HELD}: rc-server/SDK-hosted thread (idle; latest transcript entry has "
                       "entrypoint sdk-cli); a resume would fork the user's Remote Control thread, "
                       "so it is left alone"], None
    return True, [], "resume"


def fire(session_id, cwd, message, plan, new=False, name=None, model=None):
    """The one state-changing action: hand over to ka_resume.sh (tmux, no --bg).
    name/model override LAUNCH's for this launch."""
    if plan.startswith("take-over:"):
        pids = [int(x) for x in plan.split(":", 1)[1].split(",")]
        for pid in pids:
            try:
                host.kill_claude(pid)
            except ProcessLookupError:
                pass
        for _ in range(TAKE_OVER_WAIT):
            if not any(pid_alive(p) for p in pids):
                break
            time.sleep(1)
        else:   # still alive: a resume now would make a second writer on the transcript (a fork)
            raise RuntimeError(f"take-over: pid(s) {[p for p in pids if pid_alive(p)]} still alive "
                               f"{TAKE_OVER_WAIT} s after SIGTERM; not resumed")
    if plan == "stop-bg-then-resume":
        r = host.run_on_host(["claude", "stop", session_id[:8]], env=SCRUBBED_ENV, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"`claude stop {session_id[:8]}` failed rc={r.returncode}: "
                               f"{(r.stderr or r.stdout or '').strip()[:200]}; not resumed")
        time.sleep(5)
    cmd = [KA_RESUME, "--session", session_id, "--message", message,
           "--cwd", cwd]
    for k, v in {**LAUNCH, **({"name": name} if name else {}), **({"model": model} if model else {})}.items():
        if v:
            cmd += [f"--{k}", v]
    if new:
        cmd.append("--new")
    r = host.run_on_host(cmd, env=SCRUBBED_ENV, timeout=120)
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
    env = host.claude_env(pid)
    if env is None:
        return None
    wanted = ("CLAUDE_GUARD_DISABLE=", "CLAUDE_EFFORT=", "CLAUDECODE=", "CLAUDE_CODE_CHILD_SESSION=",
              "CLAUDE_CODE_MESSAGING_SOCKET=")
    return [x for x in env if x.startswith(wanted)]


# ---------------------------------------------------------------- state

def load_state(path=None):
    """The keepalive state: the DB (D-161) for the live path (None = STATE_FILE), a side file
    (tests) otherwise."""
    return runner_state.load("keepalive", path or STATE_FILE, {"handled": {}, "fires": {}})


def save_state(st, path=None):
    """-> stored (False: a DB failure, the save went to the fallback file, §7.8)."""
    return runner_state.save("keepalive", st, path or STATE_FILE)   # one transaction, merged per entry (phase 2d)


RUN_IDLE = timedelta(minutes=15)      # run_active(): the run's transcripts were written this recently
RUN_RESULTS = ("continued-in-place", "no-reply-within-timeout")   # fires that started a run
RUN_READING_EVERY = timedelta(minutes=5)  # = run_metrics.RUN_SAMPLE_EVERY: extra usage readings during a run
RUN_STOPS_KEPT = 20                   # st["run_stops"]: the task-manager's last clean stops (--run-stopped)


def run_active(session_id, now, st=None):
    """A keep-alive run is going right now (read-only; the quickview's next run, D-202): a real
    fire for this session (a window-start, postponed start, last-stretch slot or fill-up; not a
    dry-run or a refused preflight) less than one session length ago (the session window's end),
    and since the latest such fire none of the run's other ends (D-224, the earliest wins): the
    task-manager's clean stop (--run-stopped), a limit hit (D-204: the transcript's last entry is
    a limit notice, or the watcher noted one), or RUN_IDLE without a write to the session's
    transcript or one of its subagents'. After a stop the owner chatting with the session no
    longer keeps the run going; a later fire starts a new run. A compaction started after the fire
    (D-238: only ever when the run was over) counts as an end too, so its own transcript writes
    never revive the run. -> the fire time or None."""
    st = st if st is not None else load_state()
    fired = [parse_ts(v.get("at")) for v in (st.get("handled") or {}).values()
             if isinstance(v, dict) and v.get("result") in RUN_RESULTS]
    fired = [t for t in fired if t and now - afclaude_config.SESSION_LENGTH <= t <= now]
    if not fired:
        return None
    fire = max(fired)
    stops = [usage_stale.ts(t) for t, sid in (st.get("run_stops") or {}).items() if sid in (None, session_id)]
    hits = [usage_stale.ts(t) for t in (st.get("limit_hits") or {}).values()]
    comps = [usage_stale.ts(v.get("at")) for v in (st.get("compactions") or {}).values()
             if isinstance(v, dict) and v.get("session") in (None, session_id) and v.get("status") in COMPACT_STARTED]
    if any(t and fire <= t <= now for t in stops + hits + comps):
        return None
    path = transcript_path(session_id)
    if not path or stall_info(last_message(path)):
        return None
    files = [path] + glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "*.jsonl"))
    try:
        last = max(os.path.getmtime(f) for f in files if os.path.exists(f))
    except ValueError:
        return None
    return fire if now - datetime.fromtimestamp(last, UTC) <= RUN_IDLE else None


def mark_run_stopped(session_id, now, path=None):
    """--run-stopped (D-224): the task-manager stopped cleanly, so its run is over at `now`, even
    while the owner keeps chatting with the session (an owner prompt never ends a run). Merged
    into the state as it is in the DB (one row per stop; the watcher's in-memory state may be
    days old, as in note_limit_hit, and its saves never drop the stop); run_active() and
    run_metrics ignore a stop older than the run's fire. `path`: a side file (tests). -> `now`,
    None if it couldn't be stored (a DB failure: it is in data/ALERTS.fallback.md, §7.8)."""
    disk = load_state(path)
    stops = disk.setdefault("run_stops", {})
    stops[now.isoformat()] = session_id
    for k in sorted(stops)[:-RUN_STOPS_KEPT]:     # keep the last RUN_STOPS_KEPT
        stops.pop(k, None)
    return now if save_state(disk, path) else None


def progress_note(line):
    """A note in the progress doc of the project whose directory holds PROGRESS_FILE, in the DB
    (doc.append, actor runner:keepalive; design §7.7): the file gets it at the next export. Never
    raises (a DB failure: data/ALERTS.fallback.md, §7.8)."""
    import project_docs
    project_docs.append_note("progress", f"- {datetime.now(BERLIN).strftime('%H:%M')} [keepalive.py] {line}\n",
                             "runner:keepalive", repo=os.path.dirname(os.path.abspath(PROGRESS_FILE)))


# ---------------------------------------------------------------- main loop

def stall_status(session_id, now):
    """Limit detection only (no window, no budget, no usage fetch). -> (action, detail, stall),
    action one of NO_TRANSCRIPT, OTHER_ERROR, NO_STALL, WAIT_RESET (the limit has not reset +
    RESET_GRACE yet), RESET_PASSED; stall["reset"] = the reset time."""
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
        u = (usage_cache_checked() or {}).get(stall["kind"]) or {}
        reset = u.get("resets_at") if u.get("resets_at") and u["resets_at"] > stall["timestamp"] \
            else stall["timestamp"] + timedelta(hours=5)
    stall["reset"] = reset
    not_before = reset + RESET_GRACE
    if now < not_before:
        return "WAIT_RESET", f"{stall['kind']} limit resets {berlin(reset)}; not before {berlin(not_before)}", stall
    return "RESET_PASSED", f"{stall['kind']} limit reset {berlin(reset)} has passed", stall


def evaluate(session_id, now, usage_getter=fresh_usage):
    """Continue decision for an APPROVED stalled session (the owner's own, D-206; the
    dispatcher's 10-min duty): right at its limit reset + RESET_GRACE, any time of day, with no
    window and no AFClaude budget/pacing gate; only a live session limit still at 100% waits.
    The AFClaude task-manager and every other AFClaude run never get this (D-204: a limit hit
    ends the run; the watcher only logs it). -> (action, detail, stall), action one of
    NO_TRANSCRIPT, OTHER_ERROR, NO_STALL, WAIT_RESET, USAGE_FAILED, FIRE."""
    action, detail, stall = stall_status(session_id, now)
    if action != "RESET_PASSED":
        return action, detail, stall
    usage = usage_getter(now)   # only fetched when a fire is actually on the table
    if usage is None:           # the reading failed (fresh_usage: alert + pause): never continue blind
        return "USAGE_FAILED", f"{detail}; the usage reading failed, not continued (D-234/D-235)", stall
    s = (usage or {}).get("session") or {}
    if s.get("percent", 0) >= 100 and s.get("resets_at") and s["resets_at"] > now:
        return "WAIT_RESET", f"live usage still shows session limit 100% until {berlin(s['resets_at'])}", stall
    w = (usage or {}).get("weekly") or {}
    if w.get("percent", 0) >= 100 and w.get("resets_at") and w["resets_at"] > now:
        return "WAIT_RESET", f"live usage still shows weekly limit 100% until {berlin(w['resets_at'])}", stall
    return "FIRE", f"{detail}; approved stall, continued at its reset (D-206)", stall


LIMIT_HIT_NOTE = ("the run is over: no continue at the limit reset (D-204); the next session-window start or "
                  "last-stretch slot start decides by its own check")


def note_limit_hit(sid, stall, st, now):
    """Once per stall of the watched (task-manager) session: log + PROGRESS note that the run
    ended at the limit (D-204: if the session is full, that is the job done for the run)."""
    key = stall.get("uuid") or str(stall.get("timestamp"))
    if key in st.setdefault("limit_hits", {}):
        return False
    # merge into the file as it is now: the cron --window-start process writes it too, and the
    # watcher's in-memory state may be days old (never overwrite its handled keys)
    disk = load_state()
    hits = disk.setdefault("limit_hits", {})
    hits.update(st["limit_hits"])
    hits[key] = now.isoformat()
    for k in sorted(hits, key=lambda k: hits[k])[:-20]:     # keep the last 20
        hits.pop(k, None)
    save_state(disk)
    st["limit_hits"] = dict(hits)
    nxt = schedule.next_session_start(now)
    progress_note(f"{sid[:8]} hit its {stall.get('kind')} limit at {berlin(stall['timestamp']) if stall.get('timestamp') else '?'}"
                  f" (resets {berlin(stall['reset']) if stall.get('reset') else '?'}): {LIMIT_HIT_NOTE}; "
                  f"next session-window start {berlin(nxt)}")
    return True


WAKE_SLACK = timedelta(seconds=1)     # wake this long after a due time (resets_at jitters by ms)
MIN_SLEEP = 0.5                       # seconds; never a busy loop
LM_BOUNDARY_EVERY = timedelta(minutes=5)   # re-read the next last-stretch slot start this often


def _now():
    return datetime.now(UTC)


_sleep = time.sleep   # run()'s clock and sleep: tests replace both


def sleep_seconds(now, *due):
    """The watcher's sleep: POLL_SECONDS, or less to wake at the earliest due time after `now`
    (a planned fill-up start, a last-stretch slot start, a postponed window start's recheck),
    so a start fires at its time, not up to a poll / fill-up interval late."""
    s = float(POLL_SECONDS)
    for d in due:
        if d is not None and d > now:
            s = min(s, (d - now).total_seconds())
    return max(s, MIN_SLEEP)


def last_mile_next_start(now, usage=None):
    """The next time after `now` at which the last stretch opens or a new last-stretch slot
    starts (weekly reset - k x session length, last_mile_slot), or None. From the usage cache."""
    w = ((usage if usage is not None else usage_cache_checked()) or {}).get("weekly") or {}
    reset = w.get("resets_at")
    if not reset:
        return None
    lm = timedelta(hours=last_mile_hours(w.get("percent"), reset, now))
    if lm <= timedelta(0):
        return None
    sl = afclaude_config.SESSION_LENGTH
    cands = [reset - lm] + [reset - k * sl for k in range(1, int(lm / sl) + 1) if k * sl < lm]
    future = [c for c in cands if c > now]
    return min(future) if future else None


def run(args):
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another keepalive.py instance holds the lock; exiting")
    sid = args.session
    mode = "ARMED" if args.arm else "DRY-RUN"
    log(f"keepalive start ({mode}) target={sid} windows {schedule.describe()} pid={os.getpid()}")
    st = load_state()
    last_line = None
    next_eval = next_reading = next_fillup = next_compact = lm_checked = datetime.min.replace(tzinfo=UTC)
    lm_start = None
    while True:
        if os.path.exists(STOP_FILE):
            log("STOP file present, exiting")
            return
        now = _now()
        if now >= lm_checked + LM_BOUNDARY_EVERY or (lm_start is not None and now >= lm_start):
            lm_checked = now
            try:     # wake at the next last-stretch slot start, not up to a poll (or a HOLD recheck) late
                lm_start = last_mile_next_start(now)
            except Exception as e:   # noqa: BLE001 - an error (D-234/D-235); the watcher goes on
                lm_start = None
                fail_stop("last-stretch slot time failed", f"{type(e).__name__}: {e}")
        if now >= next_eval:
            # Detection + logging only: a limit hit ends the run, the watcher never continues the
            # session at the reset (D-204). Runs begin only at starts: the session-window starts
            # (cron --window-start, a postponed one below) and the last-stretch slot starts.
            next_eval = now + timedelta(seconds=POLL_SECONDS)
            try:
                action, detail, stall = stall_status(sid, now)
                if stall:
                    action, detail = "LIMIT_HIT", f"{detail}; {LIMIT_HIT_NOTE}"
                    note_limit_hit(sid, stall, st, now)
                line = f"{action}: {detail}"
                if line != last_line:
                    log(line)
                    last_line = line
                # a last-stretch slot start decides by its own check, stalled or not (D-204)
                next_lm = last_mile_pass(sid, now, st, args)
                if next_lm:
                    next_eval = max(next_eval, next_lm)
            except Exception as e:   # noqa: BLE001 - an error (D-234/D-235); the watcher goes on
                fail_stop(f"watcher pass failed: {type(e).__name__}", str(e)[:500])
        if lm_start is not None and lm_start + WAKE_SLACK < next_eval:
            next_eval = lm_start + WAKE_SLACK      # a new slot's start is not held by the last one's HOLD
        # a postponed session-window start (D-018/D-202), also when the previous run ended at a
        # limit (D-204: "limit hit before 4am and the next run starts at 4am")
        try:
            deferred_due = deferred_window_start_pass(sid, now, st, args)
        except Exception as e:   # noqa: BLE001 - an error (D-234/D-235); the watcher goes on
            deferred_due = None
            fail_stop(f"postponed window-start pass failed: {type(e).__name__}", str(e)[:500])
        if now >= next_fillup:    # the fill-up run of an AFClaude session window (D-212)
            next_fillup = now + FILLUP_EVERY
            try:
                due = fillup_pass(sid, now, args)
                if isinstance(due, datetime):     # its planned start (or recheck): wake right then
                    next_fillup = max(min(next_fillup, due + WAKE_SLACK), now + timedelta(seconds=MIN_SLEEP))
            except Exception as e:   # noqa: BLE001 - an error (D-234/D-235); the watcher goes on
                fail_stop("fill-up pass failed", f"{type(e).__name__}: {e}")
        if now >= next_reading:   # the fill-time measurement's extra readings (D-207)
            next_reading = now + RUN_READING_EVERY
            watch_run_usage(sid, now)
        if now >= next_compact:   # compact at a run end or after 50 min idle (D-238)
            next_compact = now + COMPACT_CHECK_EVERY
            try:
                compaction_pass(sid, _now(), args)
            except Exception as e:   # noqa: BLE001 - an error (D-234/D-235); the watcher goes on
                fail_stop(f"compaction pass failed: {type(e).__name__}", str(e)[:500])
        if args.once:
            return
        _sleep(sleep_seconds(_now(), next_eval, next_fillup, next_reading, next_compact,
                             deferred_due + WAKE_SLACK if isinstance(deferred_due, datetime) else None))


LAST_MILE_RECHECK = timedelta(minutes=15)


def _round_reset(resets_at):
    """resets_at jitters by milliseconds between usage fetches (16:59:59.557 vs
    17:00:00.320): round to the nearest minute, like usage_sampler.track_cycle."""
    r = resets_at.astimezone(UTC) + timedelta(seconds=30)
    return r.replace(second=0, microsecond=0)


def last_mile_slot(left):
    """Session slot of the last mile, counted back from the weekly reset: 1 = the final
    session length before it, 2 = the one before, ... (the "auto" last mile is a whole
    number of slots, so each slot is one session window that ends on a slot boundary)."""
    return max(1, -int(-left.total_seconds() // afclaude_config.SESSION_LENGTH.total_seconds()))


def last_mile_key(resets_at, slot=None):
    k = f"last-mile-{_round_reset(resets_at).isoformat()}"
    return k if slot is None else f"{k}-s{slot}"


def last_mile_handled(key, handled):
    """True if this slot of this weekly cycle's last mile was already handled, also under
    an older raw (un-rounded) reset time such as last-mile-2026-10-01T16:59:59.557562+00:00-s1."""
    if key in handled:
        return True
    want = re.fullmatch(r"last-mile-(.+?)(-s\d+)?", key)
    for k in handled:
        m = re.fullmatch(r"last-mile-(.+?)(-s\d+)?", k)
        if not (m and want) or m.group(2) != want.group(2):
            continue
        try:
            t = parse_ts(m.group(1))
        except ValueError:
            continue
        if t and t.tzinfo and last_mile_key(t) + (m.group(2) or "") == key:
            return True
    return False


def last_mile_next_slot(now, st=None):
    """Read-only (the quickview's next run, D-204): inside the last stretch, None while the
    current slot's start is still due (not handled yet), else the next slot start, or False if
    the final slot was handled (no more run before the weekly reset: nothing continues at a
    limit reset in between). Outside the last stretch: None."""
    w = (read_usage_cache() or {}).get("weekly") or {}
    left = last_mile_left(w.get("resets_at"), now, w.get("percent"))
    if left is None:
        return None
    slot = last_mile_slot(left)
    st = st if st is not None else load_state()
    if not last_mile_handled(last_mile_key(w["resets_at"], slot), st.get("handled") or {}):
        return None
    return _round_reset(w["resets_at"]) - (slot - 1) * afclaude_config.SESSION_LENGTH if slot > 1 else False


def last_mile_pass(sid, now, st, args):
    """Once per last-mile slot (one session length, counted back from the weekly reset):
    continue the session when the last mile opens and again at each later slot start, even
    outside the night window, so the remaining quota gets used (D-020). A slot start is a
    start like a session-window start (D-204): it decides by its own check whether or not the
    previous slot's run ended at a limit, and nothing continues at a limit reset in between
    (a slot already handled waits for the next slot). A limit that has not reset yet makes the
    slot's start wait for that reset. Paused automation (D-234/D-235) holds it like a HOLD.
    Returns the next re-check time after a HOLD, else None."""
    w = (usage_cache_checked() or {}).get("weekly") or {}
    left = last_mile_left(w.get("resets_at"), now, w.get("percent"))
    if left is None:
        return None
    slot = last_mile_slot(left)
    key = last_mile_key(w["resets_at"], slot)
    if last_mile_handled(key, st["handled"]):
        return None
    if automation_paused(f"the last-stretch slot {key}", sid):
        return now + LAST_MILE_RECHECK
    action, detail, stall = stall_status(sid, now)
    if action == "WAIT_RESET":
        log(f"last-mile: slot start waits for the limit reset, {detail}")
        return stall["reset"] + RESET_GRACE
    d = budget_eval(fresh_usage(now, force=True), now)
    log(f"last-mile: {d['reason']}")
    if not d["go"]:
        return max(d.get("recheck_at") or now + LAST_MILE_RECHECK, now + timedelta(seconds=POLL_SECONDS)) \
            if d.get("postpone") else now + LAST_MILE_RECHECK
    handle_fire(sid, {"uuid": key, "timestamp": now, "prompt": "last_mile", "budget": d["text"],
                      "headroom": d.get("headroom"),
                      "start_at": _round_reset(w["resets_at"]) - slot * afclaude_config.SESSION_LENGTH},
                d["reason"], st, args)
    return None


# ---------------------------------------------------------------- window start

DB_PAUSE_RECHECK = timedelta(minutes=5)   # a DB pause at a window start: recheck this soon,
                                           # but never past schedule.postpone_deadline (D-202)


def window_start_pass(sid, now, args):
    """The session-window-start continue (no stall needed), one decision per session-window start
    of the night (D-202: 23:00 and 04:00): FIRE, a final HOLD, or a POSTPONE (e.g. the user was
    active) that the watcher re-decides at its recheck time (deferred_window_start_pass), but only
    while that is before the night's next session-window start: a later recheck (also any at the
    last start, the run would end after the window end) is skipped; the next session-window start
    decides again. A FIRE dropped for a DB pause (handle_fire -> db_paused) is postponed the same
    way (db_paused's docstring): nothing else makes this start due again before then. Paused
    automation (D-234/D-235) skips the start (a progress note says so); a failed usage reading
    is an error (fresh_usage: alert + pause). -> the decision dict."""
    if automation_paused(f"the session-window start {berlin(now)}", sid):
        d = {"go": False, "reason": f"PAUSED: automation is paused ({paused_reason(sid)})", "postpone": False}
        progress_note(f"window-start {berlin(now)}: not resumed, {d['reason']}; clear it with "
                      f"`{resume_hint(sid)}` once the error is fixed (D-234/D-235)")
        return d
    u = fresh_usage(now, force=True)
    if u is None:      # fresh_usage raised the error (alert + pause): no decision on unknown numbers
        d = {"go": False, "reason": "HOLD: usage reading failed (automation paused, D-234/D-235)",
             "postpone": False}
        progress_note(f"window-start {berlin(now)}: not resumed, {d['reason']}")
        return d
    d = budget_eval(u, now)
    log(f"window-start: {d['reason']}")
    key = f"manual-now-{now.isoformat()}" if args.now else schedule.start_key(now)
    deadline = schedule.postpone_deadline(now)
    defer = bool(d.get("postpone") and d.get("recheck_at") and not args.now and d["recheck_at"] < deadline)
    if not args.now and getattr(args, "arm", False):
        try:                                  # score the forecast later (pacing.forecast_errors)
            import pacing
            pacing.record_forecast(d, ((u or {}).get("weekly") or {}).get("resets_at"))
        except Exception as e:   # noqa: BLE001 - an error (D-234/D-235): the start is aborted
            fail_stop("forecast log failed at a session-window start", f"{type(e).__name__}: {e}")
            progress_note(f"window-start {berlin(now)}: not resumed, the forecast log failed (automation paused)")
            return dict(d, go=False, reason=f"HOLD: forecast log failed ({type(e).__name__})")
    if d["go"]:
        stall = {"uuid": key, "timestamp": now, "budget": d["text"], "headroom": d.get("headroom"),
                 "start_at": schedule.latest_session_start(now) or now}
        fired = handle_fire(sid, stall, "window start, " + d["reason"], load_state(), args)
        if fired == "db_paused" and not args.now:
            recheck_at = now + DB_PAUSE_RECHECK
            if recheck_at < deadline:
                defer_window_start(key, sid, recheck_at, d["reason"], deadline)
                progress_note(f"window-start {berlin(now)}: database problem, postponed to "
                              f"{berlin(recheck_at)} to retry once it's healthy again (D-202)")
            else:
                progress_note(f"window-start {berlin(now)}: database problem at the night's last "
                              "session-window start, not retried before the window ends (D-202)")
    elif defer:
        defer_window_start(key, sid, d["recheck_at"], d["reason"], deadline)
        progress_note(f"window-start {berlin(now)}: postponed to {berlin(d['recheck_at'])}, {d['reason']}")
    elif d.get("postpone") and d.get("recheck_at") and not args.now:
        progress_note(f"window-start {berlin(now)}: not resumed, postponed past the night's next session-window "
                      f"start ({berlin(d['recheck_at'])} >= {berlin(deadline)}), skipped to the next "
                      f"session-window start {berlin(schedule.next_session_start(now))} (D-202); {d['reason']}")
    else:
        progress_note(f"window-start {berlin(now)}: not resumed, {d['reason']}")
    return d


# ---------------------------------------------------------------- postponed window start

DEFER_FILE = os.path.join(STATE_DIR, "keepalive_deferred.json")


def load_deferred():
    return runner_state.load("keepalive.deferred", DEFER_FILE)   # the DB (D-161)


def save_deferred(d):
    runner_state.save("keepalive.deferred", d, DEFER_FILE)


def defer_window_start(key, sid, recheck_at, reason, deadline=None):
    """The session-window-start decision was a POSTPONE (the user was active, ...): the watcher
    re-decides at recheck_at, still once per session-window start (same dedup key), and only
    before `deadline` (the night's next session-window start, D-202)."""
    d = {k: v for k, v in load_deferred().items() if k == key}   # older starts are over
    d[key] = {"session": sid, "recheck_at": recheck_at.astimezone(UTC).isoformat(), "reason": reason,
              "deferred_at": datetime.now(UTC).isoformat()}
    if deadline is not None:
        d[key]["deadline"] = deadline.astimezone(UTC).isoformat()
    save_deferred(d)


def pending_deferral(sid, now):
    """The recheck time of the postponed session-window start the watcher will still re-decide
    for this session (deferred_window_start_pass), or None. Read-only (the quickview's next run)."""
    if not schedule.in_window(now):
        return None
    key = schedule.start_key(now)
    ent = load_deferred().get(key)
    if not ent or ent.get("session") != sid or key in load_state().get("handled", {}):
        return None
    return parse_ts(ent.get("recheck_at"))


def deferred_window_start_pass(sid, now, st, args):
    """Watcher side of a postponed session-window start: at its recheck time, decide again and
    fire under the same window-start key, postpone again, or drop it (a final HOLD, or the
    recheck would reach the night's next session-window start or the window end: that start
    decides itself, D-202). A FIRE dropped for a DB pause (handle_fire -> db_paused) is
    postponed again the same way, instead of being dropped with the entry already removed.
    -> the next recheck time or None."""
    defs = load_deferred()
    if not defs:
        return None
    # the key changes at each session-window start, so an entry is stale from the next start on
    key = schedule.start_key(now) if schedule.in_window(now) else None
    ent = defs.get(key) if key else None
    stale = [k for k in defs if k != key or (ent and ent.get("session") != sid)]
    if ent and ent.get("session") != sid:
        ent = None
    if ent and (key in st["handled"] or key in load_state().get("handled", {})):
        stale.append(key)
        ent = None
    if stale:
        for k in stale:
            defs.pop(k, None)
        save_deferred(defs)
    if not ent:
        return None
    due = parse_ts(ent.get("recheck_at")) or now
    if now < due:
        return due
    paused = automation_paused(f"the postponed session-window start {key}", sid)
    u = None if paused else fresh_usage(now, force=True)
    if u is None:   # paused, or the usage reading failed (fresh_usage: alert + pause): dropped
        defs.pop(key, None)
        save_deferred(defs)
        progress_note(f"window-start (postponed) {berlin(now)}: not resumed, "
                      + (f"automation is paused ({paused_reason(sid)})" if paused else
                         "the usage reading failed (automation paused)") + " (D-234/D-235)")
        return None
    d = budget_eval(u, now)
    log(f"window-start (postponed): {d['reason']}")
    deadline = schedule.postpone_deadline(now)
    again = bool(d.get("postpone") and d.get("recheck_at") and d["recheck_at"] < deadline)
    if d["go"]:
        fired = handle_fire(sid, {"uuid": key, "timestamp": now, "budget": d["text"], "headroom": d.get("headroom"),
                                  "start_at": schedule.latest_session_start(now) or now},
                            "window start (postponed), " + d["reason"], st, args)
        if fired == "db_paused":
            recheck_at = now + DB_PAUSE_RECHECK
            if recheck_at < deadline:
                ent["recheck_at"] = recheck_at.astimezone(UTC).isoformat()
                ent["reason"] = d["reason"]
                save_deferred(defs)
                return recheck_at
            defs.pop(key, None)
            save_deferred(defs)
            progress_note(f"window-start (postponed) {berlin(now)}: database problem at the night's last "
                          "session-window start, not retried before the window ends (D-202)")
            return None
        defs.pop(key, None)
        save_deferred(defs)
        return None
    if again:
        ent["recheck_at"] = d["recheck_at"].astimezone(UTC).isoformat()
        ent["reason"] = d["reason"]
        save_deferred(defs)
        return d["recheck_at"]
    defs.pop(key, None)
    save_deferred(defs)
    if d.get("postpone") and d.get("recheck_at"):
        progress_note(f"window-start (postponed) {berlin(now)}: not resumed, postponed past the night's next "
                      f"session-window start ({berlin(d['recheck_at'])} >= {berlin(deadline)}), skipped to the "
                      f"next session-window start {berlin(schedule.next_session_start(now))} (D-202); {d['reason']}")
    else:
        progress_note(f"window-start (postponed) {berlin(now)}: not resumed, {d['reason']}")
    return None


def _fire_usage():
    """The usage cache at a fire (session/weekly %, reset times, fetch time; the start decision
    just refreshed it), JSON-safe, or None. A failure raises (handle_fire aborts the start)."""
    import run_metrics
    return run_metrics.fire_usage(usage_cache_checked())


def watch_run_usage(sid, now):
    """While a run is going: one extra usage reading per run_metrics.RUN_SAMPLE_EVERY into
    the run readings in the DB (D-207, the fill-time measurement). A failure is an error
    (D-234/D-235: alert + automation paused); the watcher itself goes on."""
    try:
        import run_metrics
        run_metrics.watch_sample(sid, now)
    except Exception as e:   # noqa: BLE001 - reported, never takes the watcher down
        import failstop
        if isinstance(e, failstop.Failed):
            fail_stop(e.why, e.detail)
        else:
            fail_stop("run usage reading failed", f"{type(e).__name__}: {e}")


_PAUSE_LOGGED = {}     # {session or None: the pause reason last logged for its checks} (once per reason)


def automation_paused(what, sid=None):
    """The pause switch (automation_paused, D-234/D-235; failstop.paused(): a DB that can't be
    read counts as paused): the install-wide pause, or (D-244) the pause of the project `sid` is
    the task-manager of. True = don't start `what`; logged once per pause reason. Set by any
    runner's failure (fail_stop) or by hand; the owner clears it (`tasks.py automation resume`,
    a project's with `--project NAME`)."""
    import failstop
    p = failstop.paused(session=sid)
    if not p["paused"]:
        if _PAUSE_LOGGED.pop(sid, None) is not None:
            log("automation_paused cleared" + (f" for {sid[:8]}" if sid else "") + ": autonomous starts resume")
        return False
    reason = p.get("reason") or "no reason given"
    if reason != _PAUSE_LOGGED.get(sid):
        scope = f"the project {p['project']}" if p.get("scope") == "project" else "automation_paused"
        log(f"PAUSED ({scope}), not starting {what}: {reason}; the owner clears it with "
            f"`{_resume_of(p)}`")
        _PAUSE_LOGGED[sid] = reason
    return True


def _resume_of(p):
    import failstop
    return failstop.resume_cmd(p.get("project") if p.get("scope") == "project" else None)


def paused_reason(sid=None):
    """The pause reason for a log line or a note (the strict read; with `sid`, its project's too)."""
    import failstop
    p = failstop.paused(session=sid)
    reason = p.get("reason") or "no reason given"
    return f"project {p['project']}: {reason}" if p.get("scope") == "project" else reason


def resume_hint(sid=None):
    """The command that clears what pauses `sid` now (the install-wide one, else its project's)."""
    import failstop
    return _resume_of(failstop.paused(session=sid))


_DB_PAUSE = None   # the database problem the starts are paused for (logged once per problem)


def db_paused(what):
    """A broken AFClaude database pauses every autonomous start (docs/dashboard_design.md §7.8,
    D-171): store.db_gate checks it (alerting the owner once per episode) and handle_fire does
    not mark the start handled. A stall or last-mile-slot continue is decided again every
    watcher tick, so that alone is enough for it to retry once the database is healthy again.
    A session-window start is different: window_start_pass only runs once, when the cron's
    --window-start call finds it due (schedule.due_session_start, within CRON_STEP of the
    start) -- nothing else makes it due again, so handle_fire reports "db_paused" and its
    caller (window_start_pass, deferred_window_start_pass) must postpone it explicitly
    (defer_window_start) for the watcher to retry it. -> True = don't start."""
    global _DB_PAUSE
    import store
    problem = store.db_gate("keepalive")
    why = runner_state.load_failed()
    if problem is None and why:   # its state couldn't be read (health passed): never start on a default
        problem = store.DBUnavailable(f"the runner state could not be read ({why})", "error")
        problem.escalation = "retried on the next check"
    if problem is None:
        if _DB_PAUSE is not None:
            log("database healthy again: autonomous starts resume")
            _DB_PAUSE = None
        return False
    msg = f"{problem.kind}: {problem}"
    if msg != _DB_PAUSE:
        log(f"PAUSED, not starting {what}: database problem ({msg}); {problem.escalation}")
        progress_note(f"keep-alive paused, not starting {what}: AFClaude database problem ({problem.kind})")
        _DB_PAUSE = msg
    return True


def handle_fire(sid, stall, reason, st, args):
    """One start (a session-window start, a postponed one, a last-stretch slot, a fill-up).
    -> None (already handled, fire cap reached, or it fired / was handled below), "db_paused"
    (dropped without marking handled; a window-start caller must postpone it itself, see
    db_paused's docstring), "paused" (automation_paused: dropped, not handled) or "failed" (the
    start was aborted on an error, D-234/D-235: recorded as start-failed, alerted, automation
    paused; abort_start)."""
    import failstop
    key = stall["uuid"] or str(stall["timestamp"])
    if key in st["handled"]:
        return None
    if getattr(st, "unavailable", False):   # the state is only a default (DB unreadable): decide again later
        log(f"PAUSED, not starting {sid[:8]} ({reason}): the keepalive state could not be read from the database")
        return "db_paused"
    if automation_paused(f"{sid[:8]} ({reason})", sid):
        return "paused"
    night = schedule.night(datetime.now(UTC))
    if st["fires"].get(night, 0) >= MAX_FIRES_PER_WINDOW:
        log(f"fire cap reached for window ending {night}; not firing")
        return None
    if db_paused(f"{sid[:8]} ({reason})"):
        return "db_paused"
    ok, problems, plan = preflight(sid)
    log(f"preflight: {'ok, plan=' + plan if ok else 'PROBLEMS: ' + '; '.join(problems)}")
    if not ok:
        held = owner_holds(problems)
        st["handled"][key] = {"at": datetime.now(UTC).isoformat(), "result": "preflight-failed", "problems": problems,
                              "held": held}
        save_state(st)
        if held:   # the owner has the session (Remote Control, a terminal, mid-turn): a skip, not a failure
            progress_note(f"start {key} of {sid[:8]} skipped: the owner holds the session ({problems[0]}); "
                          "no pause, the next start decides again")
            return None
        progress_note(f"did NOT fire for stall {key}: preflight failed: {problems}")
        fail_stop(f"keep-alive did NOT continue {sid[:8]}: preflight failed", "; ".join(problems))
        return None
    lock = None
    try:
        # a compaction (the watcher's, D-238) may be typing into the session: wait for it (the cron's
        # window-start runs in its own process); the watcher's own starts never overlap one
        lock = session_lock(sid, COMPACT_LOCK_WAIT)
        if lock is None:
            raise failstop.Failed("the session is still busy with a compaction",
                                  f"its lock was held for {COMPACT_LOCK_WAIT} (keepalive_session_{sid[:8]}.lock)")
        return _fire_start(sid, stall, reason, st, args, key, night, plan)
    except failstop.Failed as e:
        abort_start(sid, key, st, reason, plan, e.why, e.detail, e.record)
    except Exception as e:   # noqa: BLE001 - any other failure before the continue went out
        abort_start(sid, key, st, reason, plan, "start failed", f"{type(e).__name__}: {e}")
    finally:
        release_lock(lock)
    return "failed"


OWNER_HOLDS = (RC_HELD, OTHER_HELD, "session is busy in another process", "held by an idle interactive process")


def owner_holds(problems):
    """A preflight refusal because the session is in use (a Remote Control thread, an attached
    terminal or another live process holding it, a busy turn): a normal skip like the
    dispatcher's (task-manager decision 14.10. on D-234/D-235), not a failure. Anything else (the
    host process info unavailable, ...) is a failure."""
    return bool(problems) and all(any(m in p for m in OWNER_HOLDS) for p in problems)


def abort_start(sid, key, st, reason, plan, why, detail="", record=None):
    """A start that failed before its continue went out (D-234/D-235): recorded as start-failed
    (handled: not retried), a progress note, and fail_stop (alert, automation paused: no run until
    the owner has looked at it). Nothing more is sent to the session."""
    rec = {"at": datetime.now(UTC).isoformat(), "result": "start-failed", "plan": plan, "reason": reason,
           "session": sid, "error": f"{why}: {detail}"[:500] if detail else why}
    if record:
        rec["compaction"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in record.items()}
    st["handled"][key] = rec
    save_state(st)
    progress_note(f"start {key} of {sid[:8]} ABORTED ({reason}): {why}; automation paused until the owner "
                  "clears it (D-234/D-235)")
    fail_stop(f"start of {sid[:8]} aborted: {why}", detail)


def _fire_start(sid, stall, reason, st, args, key, night, plan):
    """handle_fire after a passed preflight. A failure before the continue goes out raises (the
    caller aborts the start); a failure after it is recorded with the run and raised as an error
    (fail_stop) here."""
    budget = stall.get("budget") or budget_headroom(usage_cache_checked(), datetime.now(UTC))[1]

    path = transcript_path(sid)
    cwd = session_cwd(path)

    def build(insert=None):
        # a session in this repo has its standing rules from the SessionStart hook: only a pointer (D-225)
        return session_message(stall.get("prompt", "continue"), reason=f"{reason}; {budget}", progress=PROGRESS_FILE,
                               insert=insert, hooked=rules_hooked(cwd), **(stall.get("fields") or {}))
    held, held_ids = inbox_held(sid)     # D-244: held "now" inbox messages go along, to be answered
    notes, note_ids = inbox_notes(sid)   # D-241: the owner's noted inbox messages go along
    # D-254/D-261: a night start (not a last-stretch slot or a fill-up) names the due nightly tasks
    tasks, task_names = nightly_tasks_due(cwd) if stall.get("prompt", "continue") == "continue" else (None, [])
    msg = build(with_notes(with_notes(with_notes(stall.get("insert"), held), notes), tasks))
    # no compaction at a start any more (D-238): the watcher compacts when a run ends or the session
    # goes idle, while the cache is warm (compaction_pass); a start only waits for a running one
    if not args.arm:
        log(f"WOULD FIRE (dry-run): plan={plan} cwd={cwd} ka_resume.sh --session {sid} {LAUNCH} {msg!r}")
        st["handled"][key] = {"at": datetime.now(UTC).isoformat(), "result": "dry-run", "plan": plan, "reason": reason}
        save_state(st)
        progress_note(f"DRY-RUN would have continued {sid[:8]} via {plan} ({reason})")
        return None
    at_fire = _fire_usage()          # the run's start values (run_metrics.py, D-207)
    offset = os.path.getsize(path) if held_ids and path else 0   # the held messages' markers come after
    sent_at = datetime.now(UTC)
    rc, out, err = fire(sid, cwd, msg, plan)
    errors = []                      # failures after the continue went out: recorded, then raised;
                                     # (subject, detail[, session]): an inbox one pauses only sid's project (D-244)
    st["fires"][night] = st["fires"].get(night, 0) + 1
    log(f"FIRED plan={plan} rc={rc} stdout={out.strip()!r} stderr={err.strip()!r}")
    result = {"at": sent_at.isoformat(), "plan": plan, "rc": rc, "stdout": out, "stderr": err, "reason": reason,
              "session": sid, "usage": at_fire, "budget_pct": stall.get("headroom")}
    if task_names:
        result["nightly_tasks"] = task_names   # nightly_tasks.track counts the runs that took them along
    if rc == 0 and held_ids:
        result["inbox_now"] = held_ids
        if not inbox_delivered(held_ids, f"{plan}, with the start {key}"):
            errors.append((f"held inbox messages sent with the continue of {sid[:8]} not marked delivered",
                           f"messages {held_ids} went out with {key}; they would go out once more", sid))
    if rc == 0 and note_ids:
        result["inbox_notes"] = note_ids
        if not inbox_included(note_ids, key):
            errors.append((f"inbox notes sent with the continue of {sid[:8]} not marked included",
                           f"messages {note_ids} went out with {key}; they would go out once more", sid))
    reply = verify_reply(path, sent_at) if rc == 0 else None
    if rc == 0 and held_ids:             # D-244: each held message's marker must be in the transcript
        import inbox
        unseen = [i for i in held_ids if not (path and inbox.seen(path, offset, inbox.marker(i)))]
        if unseen:
            result["inbox_unseen"] = unseen
            stored = inbox_failed(unseen, f"sent with the start {key}, but not in the session's transcript")
            errors.append((f"held inbox messages sent with the continue of {sid[:8]} not in its transcript",
                           f"messages {unseen} (start {key})" + ("" if stored else "; the error could not be "
                                                                 "stored on them either"), sid))
    try:
        live = [a for a in agent_entries(sid) if pid_alive(a.get("pid"))]
        result["env"] = [process_env_flags(a["pid"]) for a in live]
    except Exception as e:   # noqa: BLE001 - recorded, then raised as an error below
        result["env_error"] = f"{type(e).__name__}: {e}"[:300]
        errors.append((f"process check after the continue of {sid[:8]} failed", result["env_error"]))
    if reply:
        result["result"] = "continued-in-place"
        result["reply_model"] = (reply.get("message") or {}).get("model")
        log(f"VERIFIED: new assistant message in {sid[:8]} at {reply.get('timestamp')} "
            f"model={result['reply_model']} env={result.get('env')}")
    else:
        result["result"] = "no-reply-within-timeout" if rc == 0 else "launcher-failed"
        log(f"VERIFY FAILED: {result['result']}")
        errors.insert(0, (f"keep-alive continue of {sid[:8]} failed: {result['result']}",
                          f"plan={plan} rc={rc} (no reply within {VERIFY_TIMEOUT} after the continue if rc=0) "
                          f"stdout={out.strip()[:300]} stderr={err.strip()[:500]}"))
    st["handled"][key] = result
    save_state(st)
    progress_note(f"fired continue for {sid[:8]} via {plan}: {result.get('result')} ({reason})")
    for subject, detail, *inbox_sid in errors:
        fail_stop(subject, detail, session=inbox_sid[0] if inbox_sid else None)
    return None


# ---------------------------------------------------------------- fill-up run (D-212)
# After a run ended early (the task-manager stops cleanly at session_stop_pct, D-014), continue it
# once more shortly before the session reset: start = reset - (100 - session %) / rate x
# fillup_factor (fillup.py). D-202 lets runs begin only at session-window starts; the fill-up is the
# owner's explicit exception (D-212): it continues a run's own session window, once per window.

FILLUP_FILE = os.path.join(STATE_DIR, "keepalive_fillup.json")   # {"plan": the current plan (quickview),
                                                                  #  "test": a --plan-fillup-test entry}
FILLUP_EVERY = timedelta(seconds=60)
FILLUP_PREFETCH = timedelta(minutes=2)   # refresh the usage this long before a planned start ...
FILLUP_FRESH = timedelta(minutes=3)      # ... so the start fires at once on numbers at most this old
_FILLUP_LINE = None


def load_fillup():
    return runner_state.load("keepalive.fillup", FILLUP_FILE)   # the DB (D-161)


def save_fillup(d):
    runner_state.save("keepalive.fillup", d, FILLUP_FILE)


def _fillup_log(line):
    global _FILLUP_LINE
    if line != _FILLUP_LINE:
        log(line)
        _FILLUP_LINE = line


def _fillup_rows():
    """The run rows (run_metrics.py) for the fill rate; a failure raises (fillup_pass: an error)."""
    import run_metrics
    return run_metrics.read_rows()


def _fillup_ratio():
    """The session->weekly ratio (pacing.py); a failure raises (no default ratio, D-234/D-235)."""
    import pacing
    return float(pacing.ratio_info()[0])


def _limit_hit_in(sid, reset):
    """The session stopped on a limit notice inside the session window ending at `reset`."""
    path = transcript_path(sid)
    stall = stall_info(last_message(path)) if path else None
    t = (stall or {}).get("timestamp")
    return bool(stall and t and reset - afclaude_config.SESSION_LENGTH <= t < reset)


def _fillup_user(now, last_stretch, s):
    """D-018 yield -> (active, recheck_at): unknown sampler data counts as active."""
    if last_stretch and not s["pacing_last_mile_yield"]:
        return False, None
    import pacing
    msu = pacing.minutes_since_user(now)
    if msu is None:
        return True, now + timedelta(minutes=15)
    idle = float(s["pacing_idle_min"])
    return (msu < idle), now + timedelta(minutes=max(idle - msu, 1.0))


def fillup_decision(sid, now, usage=None):
    """The fill-up decision for the current session window (fillup.decide with the live inputs)."""
    import fillup
    import failstop
    s = afclaude_config.settings("fillup_enabled", "fillup_factor", "pacing_idle_min", "pacing_last_mile_yield")
    s["automation_paused"] = failstop.paused(session=sid)["paused"]   # strict (unreadable = paused); D-244: its project too
    u = usage if usage is not None else (usage_cache_checked() or {})
    sess, w = u.get("session") or {}, u.get("weekly") or {}
    reset = sess.get("resets_at")
    st = load_state()
    probe = (reset - timedelta(minutes=1)) if reset else now
    last_stretch = bool(reset) and last_mile_left(w.get("resets_at"), probe, w.get("percent")) is not None
    active, recheck = (False, None)
    if s["fillup_enabled"] and reset and reset > now:
        active, recheck = _fillup_user(now, last_stretch, s)
    return fillup.decide(
        now=now, session_pct=sess.get("percent"), session_reset=reset, weekly_pct=w.get("percent"),
        handled=st.get("handled") or {}, rows=_fillup_rows(), ratio=_fillup_ratio(),
        enabled=bool(s["fillup_enabled"]), factor=float(s["fillup_factor"]), paused=bool(s["automation_paused"]),
        run_active=run_active(sid, now, st) is not None, limit_hit=bool(reset) and _limit_hit_in(sid, reset),
        in_night=bool(reset) and schedule.in_window(probe), in_last_stretch=last_stretch,
        next_start=schedule.next_session_start(now), user_active=active, user_recheck=recheck,
        budget_fallback=lambda: budget_eval(u, now).get("headroom"))


def _fillup_text(d):
    return (f"fill-up of the session window resetting {berlin(d['reset'])}: ~{d['remaining']:.0f}% left, "
            f"rate {d['rate']:.1f} %/h ({d['rate_src']}), {d['time_min']:.1f} min x {d['factor']:g} -> start "
            f"{berlin(d['start'])}, weekly cost ~{d['cost']:.1f}% (ratio {d['ratio']:.3g})")


def _fire_fillup(sid, now, d, args, test=False):
    what = "TEST fill-up (--plan-fillup-test)" if test else "fill-up run (D-212)"
    stall = {"uuid": d["key"], "timestamp": now, "prompt": "fillup", "headroom": d.get("cost"),
             "budget": (f"budget for this run: the rest of this session window (~{float(d['remaining']):.0f}% "
                        f"session" + (f", ~{d['cost']:.1f}% weekly)" if d.get("cost") is not None else ")")),
             "fields": {"remaining": f"{float(d['remaining']):.0f}", "reset": d["reset"].astimezone(BERLIN).strftime("%H:%M %Z")}}
    log(f"{what}: FIRE {d['key']}")
    return handle_fire(sid, stall, f"{what}, session window resets {berlin(d['reset'])}", load_state(), args)


def fillup_test_pass(sid, now, args, test):
    """-> the time to look again (its start while planned, a re-check while held), or None."""
    import failstop
    import fillup
    st = load_state()
    d = fillup.decide_test(now=now, test=test, handled=st.get("handled") or {},
                           paused=failstop.paused(session=sid)["paused"])
    _fillup_log(f"fill-up TEST: {d['status']}: {d['reason']} (start {berlin(d['start'])}, key {d['key']})")
    if d["status"] == "fire":
        _fire_fillup(sid, now, d, args, test=True)
        d = fillup.decide_test(now=now, test=test, handled=load_state().get("handled") or {}, paused=False)
    if d["status"] in ("done", "expired"):
        f = load_fillup()
        f.pop("test", None)
        save_fillup(f)
        progress_note(f"fill-up TEST {d['key']}: {d['reason']}")
    return d.get("recheck_at")


def _usage_young(u, now):
    f = (u or {}).get("fetched_at")
    return isinstance(f, datetime) and timedelta(0) <= now - f <= FILLUP_FRESH


def fillup_pass(sid, now, args):
    """Once per watcher minute: plan / fire the fill-up of the current session window (D-212).
    A --plan-fillup-test entry for this session takes precedence. A failure raises (the watcher
    reports it: fail_stop).
    The fire decides on usage at most FILLUP_FRESH old: within FILLUP_PREFETCH of the planned start
    the usage is refreshed ahead, so the run fires AT its start (the watcher wakes then, run()),
    not after a /usage call. -> the time to look again (the planned start, a re-check), or None."""
    test = load_fillup().get("test")
    if isinstance(test, dict) and test.get("session") == sid:
        return fillup_test_pass(sid, now, args, test)
    u = usage_cache_checked() or {}
    d = fillup_decision(sid, now, u)
    planned = d["status"] == "wait" and d.get("recheck_at") is not None and d.get("recheck_at") == d.get("start")
    if (d["status"] == "fire" or (planned and d["start"] - now <= FILLUP_PREFETCH)) and not _usage_young(u, now):
        fu = fresh_usage(now, force=True)   # the plan used the cache: decide on fresh numbers
        if fu is None:                       # fresh_usage raised the error (alert + automation paused)
            if d["status"] == "fire":
                return None                  # not fired; the pause holds the next passes
        else:
            u = fu
            d = fillup_decision(sid, now, u)
    f = load_fillup()
    plan = {k: (v.astimezone(UTC).isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}
    if f.get("plan") != plan:
        f["plan"] = plan
        save_fillup(f)
    _fillup_log(f"fill-up: {d['status']}: {d['reason']}" + (f"; {_fillup_text(d)}" if "start" in d else ""))
    if d["status"] == "fire":
        _fire_fillup(sid, now, d, args)
        return None
    return d.get("recheck_at")


def plan_fillup_test(sid, remaining, args):
    """--plan-fillup-test: plan ONE fill-up for this session at session reset - remaining / rate x
    fillup_factor, bypassing the run / window / budget conditions (still preflight, dedup,
    automation_paused, db_paused). The running watcher picks it up from the DB (keepalive.fillup)."""
    import fillup
    now = datetime.now(UTC)
    u = fresh_usage(now, force=True) or read_usage_cache() or {}
    reset = (u.get("session") or {}).get("resets_at")
    if not reset or reset <= now:
        sys.exit("no live session window (session resets_at unknown or passed)")
    factor = float(afclaude_config.setting("fillup_factor"))
    p = fillup.plan(reset, 100.0 - float(remaining), _fillup_rows(), factor, _fillup_ratio())
    entry = {"session": sid, "key": fillup.test_key(reset), "reset": reset.astimezone(UTC).isoformat(),
             "start": p["start"].astimezone(UTC).isoformat(), "remaining": float(remaining),
             "rate": p["rate"], "rate_src": p["rate_src"], "factor": factor, "cost": p["cost"],
             "created_at": now.isoformat(), "test": True}
    print(f"TEST fill-up for {sid[:8]}: session reset {berlin(reset)}, remaining {float(remaining):g}%, rate "
          f"{p['rate']:.1f} %/h ({p['rate_src']}), {p['time_min']:.2f} min x {factor:g} -> fire at {berlin(p['start'])}")
    if not args.arm:
        print("dry-run: not scheduled (pass --arm)")
        return
    f = load_fillup()
    f["test"] = entry
    save_fillup(f)
    log(f"fill-up TEST scheduled: {entry}")
    progress_note(f"fill-up TEST scheduled for {sid[:8]} at {berlin(p['start'])} (reset {berlin(reset)}, "
                  f"~{float(remaining):g}% left); the running watcher fires it")
    print(f"scheduled (DB: keepalive.fillup); the running watcher fires it at {berlin(p['start'])}")


# ---------------------------------------------------------------- compaction (D-144, D-233, D-234, D-238)
# When (D-238, replacing D-191/D-218's "at a start, by the conversation's age"): the watcher compacts
# the task-manager while its prompt cache is still warm, (a) right when a keep-alive run has ended
# (D-224) and (b) after compact_timing.IDLE_AFTER (50 min) without a request, shortly before the
# 1-hour cache expires (this also covers the owner's daytime chats), each only if its context is at
# or above the break-even computed from the stored cost model (compact_timing.py: below it a
# compaction costs more than the later re-reads it saves). compaction_pass() looks once a minute
# (run()) and holds while a run uses the session (run_active), a turn or a subagent is running, the
# owner is typing (an attached terminal with recent keystrokes, a draft in the input box, a dialog
# open), the owner holds the session elsewhere (preflight: Remote Control, another process), the
# session isn't live in our tmux session, it stopped at a limit or its session limit is nearly used
# up, or a start that types into it is due within COMPACT_GUARD (the start uses the warm cache; the
# run's end compacts). How: compact_with_handoff(), then the hand-off goes back:
#   1. once the session's turn has ended (up to COMPACT_IDLE_WAIT), types the save prompt
#      (prompts/session_end_save.md) and waits for the session's NEW hand-off in the DB
#      (afclaude_handoff / tasks.py handoff write; up to SAVE_TIMEOUT). The hand-off is written
#      directly before the compaction (D-233), so it also holds the owner's interactive chats;
#   2. sends `/compact <prompts/compact.md>` and waits for the compaction in the transcript (a
#      compact_boundary / isCompactSummary entry; up to COMPACT_TIMEOUT);
#   3. sends prompts/compact_after.md with that hand-off (prompts/compact_resume.md): the session
#      answers "READY" without starting work; the hand-off is consumed.
# Any failure or timeout on the way (a busy session, no new hand-off, a send failure, no compaction
# marker, the saved hand-off not found, no reply, a DB error) is an error (D-234/D-235): the
# compaction stops right there (no /compact after a failed save), the attempt is recorded (the
# keepalive state's "compactions", one per activity: never retried), alerted, and automation is
# paused until the owner has looked at it. An older hand-off is never injected in place of a fresh
# one. The session lock (session_lock) keeps a compaction and a start from typing into the session
# at the same time: a start (the cron's window-start) waits for a running compaction up to
# COMPACT_LOCK_WAIT, then is aborted.

COMPACT_TIMEOUT = timedelta(minutes=10)     # wait this long for the compaction marker, then: error
COMPACT_IDLE_WAIT = timedelta(minutes=5)    # wait this long for a busy session to finish its turn, then: error
COMPACT_QUIET = timedelta(seconds=30)       # idle = transcript unchanged this long + the turn ended
COMPACT_POLL = 10                           # seconds
SAVE_TIMEOUT = timedelta(minutes=10)        # wait this long for the hand-off after the save prompt, then: error
TASKS_PY = os.path.join(HERE, "tasks.py")
COMPACT_CHECK_EVERY = timedelta(seconds=60)   # compaction_pass in the watcher
COMPACT_GUARD = timedelta(minutes=40)       # no compaction this close before a start that types into the session
COMPACT_LOCK_WAIT = timedelta(minutes=40)   # a start waits this long for a running compaction, then: error
COMPACT_SESSION_RESERVE = 3.0               # session % a compaction needs (~2 %, D-238)
OWNER_QUIET = timedelta(minutes=10)         # keystrokes in an attached terminal this recent: the owner is typing
SUBAGENT_QUIET = timedelta(minutes=5)       # a subagent transcript written this recently: still working
COMPACTIONS_KEPT = 30                       # st["compactions"]: the last attempts (and missed ones)
COMPACT_STARTED = ("running", "compacted", "failed")   # an attempt that typed into the session
SESSION_LOCK_DIR = os.environ.get("KEEPALIVE_LOCK_DIR", STATE_DIR)
TRIGGER_TEXT = {   # trigger -> (the save prompt's why, the resume text's when)
    "run-end": ("the run is over", "right after the run ended"),
    "idle": ("you have been idle for 50 min and your prompt cache expires soon", "while it was idle"),
}


def _compaction_settings():
    return afclaude_config.settings("compaction_enabled")


def session_lock(sid, wait=timedelta(0)):
    """The session's send lock (a file lock in SESSION_LOCK_DIR): a compaction holds it from the
    save prompt to the hand-off's reply, a start while it sends. -> the open lock (release it with
    release_lock), or None if another process still held it after `wait`."""
    os.makedirs(SESSION_LOCK_DIR, exist_ok=True)
    fh = open(os.path.join(SESSION_LOCK_DIR, f"keepalive_session_{sid[:8]}.lock"), "w")
    deadline = time.time() + wait.total_seconds()
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            left = deadline - time.time()
            if left <= 0:
                fh.close()
                return None
            time.sleep(min(5.0, left))


def release_lock(fh):
    if fh is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def session_cwd(path):
    """The session's last recorded cwd if it still exists (it can have been renamed/deleted), else HERE."""
    return next((c for c in ((last_message(path) or {}).get("cwd") if path else None, HERE)
                 if c and os.path.isdir(c)), HERE)


def session_idle(path, now=None):
    """The session finished its turn: the transcript has not changed for COMPACT_QUIET and its last
    user/assistant entry ends a turn (assistant end_turn, a limit notice, a compaction summary or a
    local slash command's output). No transcript: nothing to wait for."""
    if not path or not os.path.exists(path):
        return True
    now = now or datetime.now(UTC)
    if now - datetime.fromtimestamp(os.path.getmtime(path), UTC) < COMPACT_QUIET:
        return False
    last = last_message(path)
    if last is None or last.get("isCompactSummary"):
        return True
    m = last.get("message") or {}
    if last.get("type") == "assistant":
        return m.get("model") == "<synthetic>" or m.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens")
    return message_text(last).lstrip().startswith(("<local-command", "<command-name>"))


def compacted_since(path, since):
    """The compaction marker newer than `since` (5 s of clock slack): the compact_boundary system
    entry or the isCompactSummary user entry Claude Code writes into the same transcript."""
    if not path or not os.path.exists(path):
        return None
    floor = since - timedelta(seconds=5)
    for e in iter_entries(path):
        if (e.get("type") == "system" and e.get("subtype") == "compact_boundary") or e.get("isCompactSummary"):
            ts = parse_ts(e.get("timestamp"))
            if ts and ts >= floor:
                return e
    return None


def compact_command():
    return "/compact " + " ".join(load_prompt("compact").split())


def wait_idle(path):
    """Wait up to COMPACT_IDLE_WAIT for the session to finish its turn. -> True (idle) / False."""
    deadline = time.time() + COMPACT_IDLE_WAIT.total_seconds()
    while not session_idle(path):
        if time.time() >= deadline:
            return False
        time.sleep(COMPACT_POLL)
    return True


def compaction_busy(sid, what):
    """The session was still mid-turn after COMPACT_IDLE_WAIT (`what`: before which step)."""
    log(f"compaction: {sid[:8]} still busy after {COMPACT_IDLE_WAIT} ({what})")
    return {"status": "busy", "busy_before": what}


def compact_session(sid, cwd, path):
    """Send /compact into our live tmux session and wait until the transcript shows it finished.
    -> dict status: compacted | timeout (no marker within COMPACT_TIMEOUT) | busy (still mid-turn
    after COMPACT_IDLE_WAIT) | send-failed. Only "compacted" lets the start go on
    (compact_with_handoff)."""
    if not wait_idle(path):
        return compaction_busy(sid, "before /compact")
    since = datetime.now(UTC)
    rc, out, err = fire(sid, cwd, compact_command(), "send-keys")
    log(f"compaction: sent /compact to {sid[:8]} rc={rc} stdout={out.strip()!r} stderr={err.strip()!r}")
    if rc != 0:
        return {"status": "send-failed", "sent_at": since, "rc": rc,
                "error": f"rc={rc} {out.strip()[:200]} {err.strip()[:300]}".strip()}
    deadline = time.time() + COMPACT_TIMEOUT.total_seconds()
    while True:
        hit = compacted_since(path, since)
        if hit:
            log(f"compaction: {sid[:8]} compacted ({hit.get('subtype') or 'summary'} at {hit.get('timestamp')})")
            return {"status": "compacted", "sent_at": since, "done_at": hit.get("timestamp")}
        if time.time() >= deadline:
            log(f"compaction: no compaction marker in {sid[:8]}'s transcript after {COMPACT_TIMEOUT}")
            return {"status": "timeout", "sent_at": since}
        time.sleep(COMPACT_POLL)


def _entry_ts(e, key="timestamp"):
    try:
        ts = parse_ts((e or {}).get(key))
    except (AttributeError, ValueError):
        return None
    return ts.replace(tzinfo=UTC) if ts and not ts.tzinfo else ts


def last_activity(path):
    """When the session was last active: the timestamp of its transcript's last user/assistant
    entry (interactive chats included). Read before the save prompt, so the save turn itself never
    counts (kept in the start's record). None if unknown."""
    if not path or not os.path.exists(path):
        return None
    try:
        return _entry_ts(last_message(path))
    except OSError:
        return None


def replied_since(path, since):
    """The session answered a message typed at `since` and finished that turn (idle again)."""
    last = last_message(path) if path and os.path.exists(path) else None
    ts = _entry_ts(last)
    return bool(last and last.get("type") == "assistant" and ts and ts >= since - timedelta(seconds=5)
                and session_idle(path))


def newest_handoff_id(sid):
    """The id of the session's newest hand-off, consumed or not (0: none). A DB problem raises."""
    import store
    conn = store.connect()
    try:
        h = store.latest_handoff(conn, sid, unconsumed=False)
    finally:
        conn.close()
    return h["id"] if h else 0


def save_text(sid, trigger="run-end"):
    return " ".join(load_prompt("session_end_save").format(session=sid, tasks=TASKS_PY,
                                                           why=TRIGGER_TEXT[trigger][0]).split())


SAVE_WHY = {   # why there is no new hand-off: the error says it (D-234)
    "busy": f"the session was still mid-turn {COMPACT_IDLE_WAIT.total_seconds() / 60:g} min after the compaction "
            "was due, so no save prompt was sent",
    "timeout": f"no new hand-off within {SAVE_TIMEOUT.total_seconds() / 60:g} min of the save prompt",
    "no-handoff": "the session answered the save prompt without writing a hand-off",
    "send-failed": "the save prompt could not be sent",
    "db-error": "the hand-offs could not be read",
}
COMPACT_WHY = {
    "busy": f"the session was still mid-turn {COMPACT_IDLE_WAIT.total_seconds() / 60:g} min after the save",
    "timeout": f"no compaction marker within {COMPACT_TIMEOUT.total_seconds() / 60:g} min of /compact",
    "send-failed": "/compact could not be sent",
}


def save_before_compaction(sid, cwd, path, trigger="run-end"):
    """D-233: the hand-off is written directly before the compaction. Wait until the session's turn
    ended (else busy), type the save prompt, then wait for a new hand-off row of the session (up to
    SAVE_TIMEOUT; given up early once the session answered and went idle without one). -> dict
    status: saved (+ handoff_id) | busy | db-error | send-failed | no-handoff | timeout (the session
    still working on it or silent: no new row in time), active (the session's last activity before
    the save prompt), sent_at, error. Only "saved" lets the compaction go on (D-234)."""
    out = {"active": last_activity(path)}
    if not wait_idle(path):
        return dict(out, status="busy")
    try:
        before = newest_handoff_id(sid)
    except Exception as e:   # noqa: BLE001 - an error (D-234): no save prompt, the start is aborted
        log(f"compaction: {sid[:8]}'s hand-offs unreadable ({type(e).__name__}: {e}); no save prompt")
        return dict(out, status="db-error", error=f"{type(e).__name__}: {e}"[:200])
    since = datetime.now(UTC)
    rc, o, err = fire(sid, cwd, save_text(sid, trigger), "send-keys")
    log(f"compaction: sent the save prompt to {sid[:8]} rc={rc} stdout={o.strip()!r} stderr={err.strip()!r}")
    out["sent_at"] = since
    if rc != 0:
        return dict(out, status="send-failed", rc=rc, error=f"rc={rc} {err.strip()[:300]}".strip())
    deadline = time.time() + SAVE_TIMEOUT.total_seconds()
    while True:
        done = replied_since(path, since)   # before the DB read: the row is written before the reply
        try:
            newest = newest_handoff_id(sid)
        except Exception as e:   # noqa: BLE001 - an error (D-234): store.retrying already retried (D-171)
            log(f"compaction: {sid[:8]}'s hand-offs unreadable while waiting for the save ({type(e).__name__}: {e})")
            return dict(out, status="db-error", error=f"{type(e).__name__}: {e}"[:200])
        if newest > before:
            log(f"compaction: {sid[:8]} saved hand-off #{newest} right before the compaction (D-233)")
            return dict(out, status="saved", handoff_id=newest)
        if done:
            return dict(out, status="no-handoff")
        if time.time() >= deadline:
            return dict(out, status="timeout", still_busy=not session_idle(path))
        time.sleep(COMPACT_POLL)


def compact_resume_text(sid, save, trigger="run-end"):
    """The text that gives the session its hand-off back after a compaction -> (text, hand-off row):
    the session's latest unconsumed hand-off, which must be the one the save right before this
    compaction wrote (`save`: save_before_compaction's result, status "saved"; compact_resume.md).
    Anything else raises failstop.Failed (D-234): no save, the saved hand-off not found (e.g.
    consumed or gone meanwhile), or the DB could not be read."""
    import failstop
    import store
    want = int((save or {}).get("handoff_id") or 0)
    if (save or {}).get("status") != "saved" or not want:
        raise failstop.Failed("no fresh hand-off to inject", f"save status {(save or {}).get('status')!r}")
    try:
        conn = store.connect()
        try:
            h = store.latest_handoff(conn, sid)
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - an error (D-234): no continue without the hand-off
        raise failstop.Failed("the saved hand-off could not be read", f"{type(e).__name__}: {e}"[:300]) from None
    if h is None or h["id"] < want:
        raise failstop.Failed("the saved hand-off was not found",
                              f"hand-off #{want} was saved, but the latest unconsumed one of {sid[:8]} is "
                              + (f"#{h['id']}" if h else "none"))
    return load_prompt("compact_resume").format(handoff=store.render_handoff(h), progress=PROGRESS_FILE,
                                                when=TRIGGER_TEXT[trigger][1]), h


def _jsonable(d):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def compact_with_handoff(sid, cwd, path, trigger="run-end"):
    """The compaction (D-233/D-234/D-238): save_before_compaction -> compact_session ->
    compact_resume_text. trigger: run-end | idle (TRIGGER_TEXT: the prompts' wording). -> (the
    hand-off text to send after the compaction, the hand-off row to consume once it went out, the
    record: save (its result), status "compacted", sent_at, done_at, handoff_id). Every failure or
    timeout raises failstop.Failed(why, detail, record) and stops right there: no /compact after a
    failed save, no hand-off after a failed compaction (the caller records and reports it,
    compaction_pass). Also the live test (tools/test_compact_live.sh) runs exactly this."""
    import failstop
    rec = {}
    try:
        save = save_before_compaction(sid, cwd, path, trigger)
    except Exception as e:   # noqa: BLE001
        raise failstop.Failed("the save step failed", f"{type(e).__name__}: {e}"[:300], rec) from None
    rec["save"] = _jsonable(save)
    if save["status"] != "saved":
        st = save["status"]
        why = SAVE_WHY.get(st, f"save status {st}")
        if st == "timeout" and save.get("still_busy"):
            why += " (the session was still working on it)"
        raise failstop.Failed(f"no fresh hand-off before the compaction: {why}", save.get("error") or "", rec)
    try:
        comp = compact_session(sid, cwd, path)
    except Exception as e:   # noqa: BLE001
        raise failstop.Failed("the /compact step failed", f"{type(e).__name__}: {e}"[:300], rec) from None
    rec.update(_jsonable(comp))
    if comp["status"] != "compacted":
        raise failstop.Failed(f"compaction not done: {COMPACT_WHY.get(comp['status'], comp['status'])}",
                              comp.get("error") or "", rec)
    try:
        insert, h = compact_resume_text(sid, save, trigger)
    except failstop.Failed as e:
        raise failstop.Failed(e.why, e.detail, rec) from None
    rec["handoff_id"] = h["id"]
    return insert, h, rec


def consume_handoff(h, start_key):
    """Mark the injected hand-off consumed by this start (actions.py, audited). -> True / False
    (logged; handle_fire raises it as an error)."""
    import actions
    import store
    try:
        conn = store.connect()
        try:
            actions.perform(conn, "handoff.consume", {"handoff_id": h["id"], "start_key": start_key},
                            actor="keepalive", via="runner")
        finally:
            conn.close()
        return True
    except Exception as e:   # noqa: BLE001 - it was sent; the caller reports it (fail_stop)
        log(f"hand-off #{h.get('id')} not marked consumed: {type(e).__name__}: {e}")
        return False


def inbox_notes(sid):
    """D-241: the owner's noted inbox messages for this session, as one line for the next message
    into it (inbox.notes_insert) -> (text or None, ids). A DB failure raises: the caller aborts
    what it was about to send (D-235)."""
    import inbox
    import store
    conn = store.connect()
    try:
        return inbox.notes_insert(conn, sid)
    finally:
        conn.close()


def nightly_tasks_due(cwd):
    """D-254/D-261: the nightly tasks due now, as one line for a night start's message
    (nightly_tasks.start_insert) -> (text or None, their names). Only the AFClaude task-manager
    (a session in this checkout, rules_hooked) does them. An unreadable state raises
    failstop.Failed: the caller aborts the start (D-235)."""
    if not rules_hooked(cwd):
        return None, []
    import nightly_tasks
    return nightly_tasks.start_insert(datetime.now(UTC))


def inbox_held(sid):
    """D-244: the "now" inbox messages still waiting for this session (held), as one line for a
    start's message (inbox.held_insert) -> (text or None, ids). A DB failure raises: the caller
    aborts the start (D-235)."""
    import inbox
    import store
    conn = store.connect()
    try:
        return inbox.held_insert(conn, sid)
    finally:
        conn.close()


def _inbox_act(name, ids, **params):
    """One inbox.<name> action per message id (actions.py, audited, actor keepalive). -> True /
    False (logged; the caller reports it, fail_stop)."""
    import actions
    import store
    try:
        conn = store.connect()
        try:
            for i in ids:
                actions.perform(conn, name, dict(params, message_id=i), actor="keepalive", via="runner")
        finally:
            conn.close()
        return True
    except Exception as e:   # noqa: BLE001 - the caller reports it (fail_stop)
        log(f"{name} of inbox messages {list(ids)} failed: {type(e).__name__}: {e}")
        return False


def inbox_delivered(ids, plan):
    """The held "now" messages `ids` went out with a start (`plan` names it): pending -> delivered."""
    return _inbox_act("inbox.deliver", ids, plan=plan)


def inbox_failed(ids, error):
    """The held "now" messages `ids` failed after they went out (no marker in the transcript): an
    error on each, shown on the dashboard (the caller raises the error itself, fail_stop)."""
    return _inbox_act("inbox.fail", ids, error=error)


def inbox_included(ids, by):
    """The noted messages `ids` went out with the message `by` (a start's key, a compaction):
    noted -> included (actions.py, audited). -> True / False (logged; the caller reports it: they
    were sent, and would go out once more with the next message)."""
    if not ids:
        return True
    import actions
    import store
    try:
        conn = store.connect()
        try:
            actions.perform(conn, "inbox.include", {"message_ids": list(ids), "by": by}, actor="keepalive",
                            via="runner")
        finally:
            conn.close()
        return True
    except Exception as e:   # noqa: BLE001 - it was sent; the caller reports it (fail_stop)
        log(f"inbox notes {list(ids)} not marked included: {type(e).__name__}: {e}")
        return False


def with_notes(text, notes):
    """`text` (or nothing) followed by the inbox notes (or nothing)."""
    return f"{text} {notes}" if text and notes else (text or notes)


# ---- the triggers (D-238): compaction_pass, once a minute in the watcher

_ANSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b[()][A-Za-z0-9]|\x1b[=>]")
PROMPT_GLYPHS = ("❯", ">")
# A horizontal rule or a box border, optionally with a title in it (the input box's top border
# carries the session's name: "──── my session ─"): starts and ends with rule characters.
_RULE = re.compile(r"[─╭╰]+(?: .*? )?[─╮╯]+")
# A dialog or a menu in place of the input box: its key hints ("Enter to confirm · Esc to cancel",
# "↑/↓ to navigate") or a numbered option under the cursor ("❯ 1. Yes"). Not the footer's own hints
# ("esc to interrupt", "esc to clear input").
_DIALOG = re.compile(r"\b(?:esc|enter|return|space|tab) to (?:cancel|confirm|select|continue|go back|close|exit"
                     r"|quit|dismiss|skip|deny|toggle|navigate)\b|↑\s*/?\s*↓ to |^\s*[❯>›]\s*\d+\.", re.I)


def _plain(line):
    return _ANSI.sub("", line)


def _sgr(params, dim, inverse):
    """Apply one SGR sequence's parameters to the (dim, inverse) state."""
    ps = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else [0]
    i = 0
    while i < len(ps):
        p = ps[i]
        if p == 0:
            dim = inverse = False
        elif p == 2:
            dim = True
        elif p == 22:
            dim = False
        elif p == 7:
            inverse = True
        elif p == 27:
            inverse = False
        elif p in (38, 48, 58):          # extended colour: skip its arguments
            i += 2 if i + 1 < len(ps) and ps[i + 1] == 5 else 4
        i += 1
    return dim, inverse


def _is_rule(line):
    p = _plain(line).strip()
    return len(p) >= 10 and sum(ch in "─╭╮╰╯" for ch in p) >= 10 and _RULE.fullmatch(p) is not None


def pane_input(text):
    """The input box of a Claude Code pane capture (tmux capture-pane -e -p): the lines between the
    last two horizontal rules (the top one may carry a title), the first one starting with the
    prompt glyph. -> (kind, typed): kind "box" with typed = the characters shown in normal video
    after the glyph (a dimmed suggestion or placeholder, the inverse-video cursor, spaces and the
    box's borders don't count); "dialog" when a dialog or a menu is open in its place; None when
    the pane shows neither (not a Claude Code screen this parser knows: owner_typing raises)."""
    lines = text.split("\n")
    rules = [i for i, ln in enumerate(lines) if _is_rule(ln)]
    region = lines[rules[-2] + 1:rules[-1]] if len(rules) >= 2 else []
    if not region or not _plain(region[0]).strip().lstrip("│").lstrip().startswith(PROMPT_GLYPHS):
        tail = lines[rules[-min(2, len(rules))] + 1:] if rules else lines[-15:]   # the dialog, not the chat
        return ("dialog" if any(_DIALOG.search(_plain(ln)) for ln in tail) else None), ""
    typed, dim, inverse = [], False, False
    for n, ln in enumerate(region):
        glyph = n == 0
        pos = 0
        for m in list(_ANSI.finditer(ln)) + [None]:
            chunk = ln[pos:m.start()] if m else ln[pos:]
            for ch in chunk:
                if glyph and ch in PROMPT_GLYPHS:
                    glyph = False
                    continue
                if not dim and not inverse and not ch.isspace() and ch != "│":
                    typed.append(ch)
            if m is None:
                break
            if m.group(2) == "m":
                dim, inverse = _sgr(m.group(1), dim, inverse)
            pos = m.end()
    return "box", "".join(typed)


def owner_typing(sid, now):
    """Why the owner may be typing into the session's tmux pane right now, or None: an attached
    terminal with keystrokes in the last OWNER_QUIET, a draft in the input box, or a dialog or menu
    open in its place. A failed tmux call or a pane that shows neither the input box nor a dialog
    (pane_input can't read it) raises (compaction_pass: an error, D-235)."""
    t = tmux_name(sid)
    r = host.run_on_host(["tmux", "list-clients", "-t", "=" + t, "-F", "#{client_activity}"], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"tmux list-clients {t} failed rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
    acts = [int(x) for x in r.stdout.split() if x.isdigit()]
    if acts and now.timestamp() - max(acts) < OWNER_QUIET.total_seconds():
        return f"the owner typed in a terminal attached to {t} {(now.timestamp() - max(acts)) / 60:.0f} min ago"
    r = host.run_on_host(["tmux", "capture-pane", "-e", "-p", "-t", f"={t}:"], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"tmux capture-pane {t} failed rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
    kind, typed = pane_input(r.stdout)
    if kind is None:
        tail = [_plain(ln).strip() for ln in r.stdout.split("\n") if _plain(ln).strip()][-3:]
        raise RuntimeError(f"can't read {t}'s pane: neither Claude Code's input box nor a dialog or menu "
                           f"(last lines: {' | '.join(x[:80] for x in tail) or 'none'})")
    if kind == "dialog":
        return f"a dialog or menu is open on {t}'s pane"
    if typed:
        return f"a draft in {t}'s input box ({len(typed)} characters)"
    return None


def compaction_busy_reason(path, now):
    """Why the session can't take the save prompt now (a turn running, a subagent working), or None."""
    if not session_idle(path, now):
        return "a turn is running"
    subs = glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "*.jsonl"))
    newest = max((os.path.getmtime(f) for f in subs if os.path.exists(f)), default=None)
    if newest is not None and now - datetime.fromtimestamp(newest, UTC) < SUBAGENT_QUIET:
        return "a subagent is working"
    return None


def latest_run_fire(sid, st, now):
    """The latest fire that started a run of this session (as run_active counts them), or None."""
    fired = [parse_ts(v.get("at")) for v in (st.get("handled") or {}).values()
             if isinstance(v, dict) and v.get("result") in RUN_RESULTS and v.get("session") in (None, sid)]
    fired = [t for t in fired if t and t <= now]
    return max(fired) if fired else None


def run_ended(sid, sc, st, now):
    """D-238 (a): the session's latest run (run_active None: the caller checks) did something and
    the session's last prompt since its fire was the run's (a keep-alive message, a task
    notification, ...), not the owner's: the owner chatting after the run gets the idle trigger
    (they may go on). A run that ended at a limit is held separately (_compaction_hold)."""
    import compact_timing
    fire = latest_run_fire(sid, st, now)
    if fire is None or not sc.requests or sc.requests[-1].ts < fire:
        return False
    kind = compact_timing.last_prompt_kind(sc, since=fire)
    return kind is not None and kind != "owner"


def start_soon(sid, now):
    """A start that types into the session within COMPACT_GUARD (the cron's next session-window
    start, a postponed start's recheck, a planned fill-up, a last-stretch slot): -> why, else None.
    That start uses the warm cache (and the watcher's own starts must not wait behind a ~25-min
    compaction); its run's end compacts. A failed read raises (compaction_pass: an error)."""
    def soon(t):
        return t is not None and now <= t < now + COMPACT_GUARD
    nxt = schedule.next_session_start(now)
    if soon(nxt):
        return f"the session-window start {berlin(nxt)} is due within {COMPACT_GUARD.total_seconds() / 60:g} min"
    for v in load_deferred().values():
        r = parse_ts(v.get("recheck_at")) if isinstance(v, dict) and v.get("session") in (None, sid) else None
        if soon(r):
            return f"a postponed session-window start rechecks at {berlin(r)}"
    plan = load_fillup().get("plan") or {}
    start = parse_ts(plan.get("start")) if plan.get("status") in ("wait", "fire") and plan.get("start") else None
    if soon(start) or plan.get("status") == "fire":
        return f"the fill-up run is planned for {berlin(start) if start else 'now'}"
    lm = last_mile_next_start(now)
    if soon(lm):
        return f"the last-stretch slot starts {berlin(lm)}"
    return None


def _compaction_hold(sid, now, path):
    """The holds that need more than the transcript (a start soon, the limit, the host's process
    info, the tmux pane), checked only when a compaction is due. -> why, else None. A failure
    raises."""
    why = start_soon(sid, now)
    if why:
        return why
    if stall_info(last_message(path)):
        return "the session stopped at a usage limit"
    s = (usage_cache_checked() or {}).get("session") or {}
    pct, reset = s.get("percent"), s.get("resets_at")
    if pct is not None and pct > 100 - COMPACT_SESSION_RESERVE and (reset is None or reset > now):
        return f"the session limit is at {pct:g}% (a compaction needs ~{COMPACT_SESSION_RESERVE:g}%)"
    ok, problems, plan = preflight(sid)
    if not ok:
        if owner_holds(problems):
            return f"the owner holds the session ({problems[0]})"
        raise RuntimeError("preflight failed: " + "; ".join(problems))
    if plan != "send-keys":
        return f"the session is not live in its tmux session (plan {plan}): nothing to type /compact into"
    return owner_typing(sid, now)


def record_compaction(sid, key, rec):
    """The attempt `key` (one per activity: the last request's time) in the keepalive state's
    "compactions" (merged into the DB's state; the last COMPACTIONS_KEPT). -> stored."""
    disk = load_state()
    comps = disk.setdefault("compactions", {})
    comps[key] = dict(_jsonable(rec), session=sid)
    for k in sorted(comps, key=lambda k: str((comps[k] or {}).get("at")))[:-COMPACTIONS_KEPT]:
        comps.pop(k, None)
    ok = save_state(disk)
    if not ok:
        log(f"compaction record {key} not stored in the DB (kept in data/ALERTS.fallback.md)")
    return ok


_COMPACT_LINE = None
_COMPACT_TRIED = set()   # keys attempted by this process (a second guard next to the state)


def _compact_log(line):
    global _COMPACT_LINE
    if line != _COMPACT_LINE:
        log(line)
        _COMPACT_LINE = line


def compaction_pass(sid, now, args):
    """D-238: compact the task-manager when its run ended or after 50 min idle, while the cache is
    warm, if its context is at or above the break-even (see the section comment). -> the
    compact_timing.Decision, or None (compaction_enabled off, automation paused, no transcript).
    A hold is logged once per reason; an attempt is recorded (record_compaction) and never retried
    for the same activity; a context above the break-even whose cache expired without a compaction
    is recorded "missed" (the quickview shows both). A failure of the compaction itself is recorded
    and reported here (fail_stop); a failure before it (the transcript, the cost model, a host
    call) raises (run() reports it: fail_stop)."""
    import compact_timing as ct
    if not _compaction_settings()["compaction_enabled"]:
        _compact_log("compaction: compaction_enabled is off")
        return None
    if automation_paused(f"the compaction of {sid[:8]}", sid):
        return None
    path = transcript_path(sid)
    if not path:
        _compact_log(f"compaction of {sid[:8]}: no transcript")
        return None
    model = ct.load()
    sc = ct.scan_cached(path)
    state = ct.context_state(sc, model, now)
    st = load_state()
    using = run_active(sid, now, st) is not None
    d = ct.decide(now, state, run_using=using, run_ended=not using and run_ended(sid, sc, st, now),
                  busy=compaction_busy_reason(path, now))
    if d.key and (d.key in (st.get("compactions") or {}) or d.key in _COMPACT_TRIED):
        return d
    if d.due:
        why = _compaction_hold(sid, now, path)
        if why:
            d = ct.Decision(False, None, why, d.key)
    ctx = state.get("context")
    head = (f"compaction of {sid[:8]}: context {ctx / 1000:.0f}k, break-even {state['break_even'] / 1000:.0f}k: "
            if ctx is not None else f"compaction of {sid[:8]}: ")
    rec = {"at": now, "trigger": d.trigger, "reason": d.reason, "context": ctx, "break_even": state["break_even"],
           "est_cost_pct": round(state["compaction_cost"], 2) if ctx is not None else None,
           "saves_per_reread_pct": (round(ct.reread_cost(model, ctx - model["compaction"]["base_tokens"]), 2)
                                    if ctx is not None else None)}
    if not d.due:
        expires = state.get("cache_expires_at")
        if d.missed and d.key and expires and now - expires < timedelta(hours=1):
            _COMPACT_TRIED.add(d.key)
            record_compaction(sid, d.key, dict(rec, status="missed", last_hold=_COMPACT_LINE))
        _compact_log(head + d.reason)
        return d
    _COMPACT_TRIED.add(d.key)
    if not getattr(args, "arm", False):
        _compact_log(head + f"WOULD COMPACT (dry-run, {d.trigger}: {d.reason})")
        record_compaction(sid, d.key, dict(rec, status="dry-run"))
        return d
    lock = session_lock(sid)
    if lock is None:
        _COMPACT_TRIED.discard(d.key)
        _compact_log(head + "a start is typing into the session (its lock is held): later")
        return d
    try:
        _compact_now(sid, path, d, rec, head)
    finally:
        release_lock(lock)
    return d


def _compaction_failed(sid, key, rec, why, detail="", steps=None):
    rec.update(status="failed", error=(f"{why}: {detail}" if detail else why)[:500], done_at=datetime.now(UTC))
    if steps:
        rec["steps"] = _jsonable(steps)
    record_compaction(sid, key, rec)
    progress_note(f"compaction of {sid[:8]} FAILED ({rec.get('trigger')}): {why}; automation paused until the "
                  "owner clears it (D-234/D-235)")
    fail_stop(f"compaction of {sid[:8]} failed: {why}", detail)


def _compact_now(sid, path, d, rec, head):
    """The compaction (compact_with_handoff), then the hand-off back to the session
    (compact_after.md) and its reply; recorded either way. A failure: _compaction_failed."""
    import failstop
    cwd = session_cwd(path)
    log(head + f"COMPACTING ({d.trigger}: {d.reason})")
    record_compaction(sid, d.key, dict(rec, status="running"))
    try:
        try:
            insert, h, steps = compact_with_handoff(sid, cwd, path, d.trigger)
        except failstop.Failed as e:
            return _compaction_failed(sid, d.key, rec, e.why, e.detail, e.record)
        rec["steps"] = _jsonable(steps)
        notes, note_ids = inbox_notes(sid)   # D-241: the owner's noted inbox messages, read after the hand-off
        msg = session_message("compact_after", hooked=rules_hooked(cwd), resume=with_notes(insert, notes))
        sent_at = datetime.now(UTC)
        rc, out, err = fire(sid, cwd, msg, "send-keys")
        if rc != 0:
            return _compaction_failed(sid, d.key, rec, "the hand-off could not be sent after the compaction",
                                      f"rc={rc} {out.strip()[:200]} {err.strip()[:300]}".strip())
        if note_ids:
            rec["inbox_notes"] = note_ids
            if not inbox_included(note_ids, f"compaction {d.key}"):
                fail_stop(f"inbox notes sent after the compaction of {sid[:8]} not marked included",
                          f"messages {note_ids}; they would go out once more", session=sid)   # D-244
        consumed = consume_handoff(h, d.key)
        reply = verify_reply(path, sent_at)
    except Exception as e:   # noqa: BLE001 - an error (D-234/D-235): recorded, reported
        return _compaction_failed(sid, d.key, rec, f"compaction step failed: {type(e).__name__}", str(e)[:500])
    rec.update(status="compacted", handoff_id=h["id"], handoff_consumed=consumed, replied=bool(reply),
               done_at=datetime.now(UTC))
    record_compaction(sid, d.key, rec)
    progress_note(f"compacted {sid[:8]} ({d.trigger}, D-238): context {rec['context'] / 1000:.0f}k >= break-even "
                  f"{rec['break_even'] / 1000:.0f}k, est. cost ~{rec['est_cost_pct']}% session, saves "
                  f"~{rec['saves_per_reread_pct']}% per later re-read; hand-off #{h['id']} given back")
    if not consumed:
        fail_stop(f"hand-off #{h['id']} of {sid[:8]} not marked consumed after its compaction",
                  "it was sent; a later compaction would not find a fresh one")
    if not reply:
        fail_stop(f"no reply from {sid[:8]} to its hand-off after the compaction",
                  f"none within {VERIFY_TIMEOUT} of the message")
    return None


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
    if automation_paused(f"the backlog project {p['title']!r}"):
        sys.exit(f"automation is paused ({paused_reason()}); clear it first: {resume_hint()}")
    go, reason = budget_decision(fresh_usage(now, force=True), now)
    log(f"work-on {p['title']!r} (window ignored): {reason}")
    if not go:
        progress_note(f"work-on {p['title']!r} not started: {reason}")
        return
    sid = str(uuid.uuid4())
    slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-")
    msg = session_message("project_start", title=p["title"], desc=p["desc"], slug=slug, hooked=rules_hooked(HERE))
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
    if not reply:
        fail_stop(f"backlog project start failed: {'no reply' if rc == 0 else 'launcher failed'}",
                  f"{p['title']!r} session {sid} rc={rc} stdout={out.strip()[:300]} stderr={err.strip()[:300]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=os.environ.get("KEEPALIVE_SESSION"),
                    help="FULL session UUID to keep alive")
    ap.add_argument("--arm", action="store_true", help="actually fire resumes (default: dry-run)")
    ap.add_argument("--once", action="store_true", help="single evaluation pass, then exit")
    ap.add_argument("--decide", action="store_true",
                    help="print the window + budget decision for right now and exit")
    ap.add_argument("--last-mile", action="store_true",
                    help="one-shot: continue the session if the last-mile period before the weekly reset is open")
    ap.add_argument("--window-start", action="store_true",
                    help="one-shot (cron, every 30 min): at a due session-window start (23:00, 04:00 by default; D-202), "
                         "continue the session if the budget rule allows")
    ap.add_argument("--now", action="store_true",
                    help="with --window-start: ignore the start-hour gate (a manual start now); budget rule still applies")
    ap.add_argument("--work-on", metavar="PROJECT",
                    help="start a BACKLOG.md project right now in a new tmux session (list number or title substring); "
                         "needs --arm to actually start")
    ap.add_argument("--plan-fillup-test", action="store_true",
                    help="schedule ONE test fill-up run (D-212) for --session at session reset - remaining / rate "
                         "x fillup_factor; the running watcher fires it (needs --arm to schedule)")
    ap.add_argument("--remaining", type=float, default=3.0, help="with --plan-fillup-test: session %% left to fill")
    ap.add_argument("--run-stopped", action="store_true",
                    help="the task-manager stopped cleanly: its run is over now (D-224; the session: --session, "
                         "else this Claude Code session, else the manager session)")
    ap.add_argument("--session-rules", choices=list(RULES_PARTS), metavar="PART",
                    help="the repo's SessionStart hook (D-225): print one part of the standing rules "
                         f"({' / '.join(RULES_PARTS)}) as hook JSON for a session started in this repo; "
                         "reads the hook input from stdin")
    ap.add_argument("--take-over-idle", action="store_true",
                    help="if an idle interactive process (e.g. an open terminal) holds the session, SIGTERM it and resume in tmux")
    ap.add_argument("--model", default=LAUNCH["model"], help="model to relaunch with (tmux resume)")
    ap.add_argument("--effort", default=LAUNCH["effort"], help="effort to relaunch with")
    ap.add_argument("--name", default=LAUNCH["name"], help="Remote Control display name")
    args = ap.parse_args()
    if args.session_rules:
        return print_session_rules(args.session_rules)
    LAUNCH.update(model=args.model, effort=args.effort, name=args.name)
    global TAKE_OVER_IDLE, IGNORE_WINDOW
    afclaude_config.LOG = log            # a settings problem (DB unreadable) lands in this log
    TAKE_OVER_IDLE = args.take_over_idle
    IGNORE_WINDOW = args.now
    if args.run_stopped:
        # the task-manager runs this on the host, where STATE_DIR is the pre-container repo dir:
        # the stop always goes into the live state, i.e. the DB (D-161), never a file there
        sid = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or afclaude_config.manager_session()
        t = mark_run_stopped(sid, datetime.now(UTC), runner_state.live_path("keepalive"))
        if t is None:
            sys.exit(f"run of {sid[:8]}: the stop could not be stored in the DB (kept in data/ALERTS.fallback.md)")
        print(f"run of {sid[:8]} marked stopped at {berlin(t)}")
        return
    if args.work_on:
        return work_on_project(args.work_on, args)
    if args.plan_fillup_test:
        if not args.session:
            sys.exit("--plan-fillup-test needs --session")
        return plan_fillup_test(args.session, args.remaining, args)
    if args.last_mile:
        if not args.session:
            sys.exit("--last-mile needs --session")
        st = load_state()
        nxt = last_mile_pass(args.session, datetime.now(UTC), st, args)
        print("not in the last mile" if not in_last_mile(datetime.now(UTC)) else
              (f"held, re-check after {berlin(nxt)}" if nxt else "done (fired or already handled)"))
        return
    if args.window_start and not args.session:
        sys.exit("--window-start needs --session")
    if args.window_start:
        # Session-window-start continue (no stall needed, D-202): budget rule + window, then fire.
        now = datetime.now(UTC)
        if not args.now and schedule.due_session_start(now) is None:
            return                       # no session-window start due in this cron slot
        window_start_pass(args.session, now, args)
        return
    if args.decide:
        now = datetime.now(UTC)
        u = fresh_usage(now, force=True, display=True)
        nws = schedule.next_window_start(now)
        print(f"now {berlin(now)} in_window={schedule.in_window(now)} "
              f"next_window={berlin(nws) if nws else 'none'} ({schedule.describe()})")
        print(f"usage: {json.dumps(u, default=str)}")
        d = budget_eval(u, now, display=True)
        print((d["go"], d["reason"]))
        print(d["text"])
        if d.get("postpone") and d.get("recheck_at"):
            print(f"postponed: recheck {berlin(d['recheck_at'])}")
        w = (u or {}).get("weekly") or {}
        if w.get("resets_at"):
            setting = afclaude_config.last_mile_setting()
            lm = timedelta(hours=last_mile_hours(w.get("percent"), w["resets_at"], now))
            how = ("auto: min(ceil(session windows of quota left), 2) x session length"
                   if setting == "auto" else "last_mile_hours")
            print(f"last mile ({how}): {lm} before the weekly reset → opens {berlin(w['resets_at'] - lm)}"
                  if lm > timedelta(0) else "last mile: off")
        for k, v in load_deferred().items():
            print(f"postponed window start {k}: recheck {v.get('recheck_at')}")
        if d.get("forecast_source"):
            print(f"forecast: {d['forecast_source']}")
        try:
            import pacing
            for line in pacing.threshold_lines(pacing.threshold_info(now=now, decision=d)):
                print(line)
        except Exception:   # noqa: BLE001 - advisory
            pass
        if args.session:
            print(stall_status(args.session, now)[:2])
        return
    if not args.session or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", args.session):
        sys.exit("--session must be the FULL session UUID (a short id or a prefix makes resume fork)")
    run(args)


def alert(subject, body=""):
    """Failure alert (notify.py: the alerts + progress docs in the DB, a best-effort push). Never raises."""
    log(f"ALERT: {subject}")
    try:
        from notify import notify
        log(f"alert push: {notify(subject, body)}")
    except Exception as e:  # noqa: BLE001 - alerts must not take the watcher down
        log(f"alert itself failed: {e!r}")


def fail_stop(subject, detail="", session=None):
    """A failure or timeout (D-234/D-235): an ERROR line, automation_paused with the reason, an
    alert (failstop.fail with this module's log and alert). The caller aborts what it was doing.
    session: an inbox error (D-244) pauses only the project this session is the task-manager of.
    Never raises."""
    import failstop
    return failstop.fail("keepalive", subject, detail, log=log, alert=alert, session=session)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # e.g. the 2026-09-29 00:00 fire died on "Permission denied: ka_resume.sh" and nobody noticed
        import traceback
        traceback.print_exc()
        fail_stop(f"keepalive.py crashed: {type(e).__name__}", f"{e}\n{traceback.format_exc()[-1500:]}")
        sys.exit(1)
