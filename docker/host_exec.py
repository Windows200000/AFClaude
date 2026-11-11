#!/usr/bin/env python3
"""Whitelisted host executor for the AFClaude manager container (user-chosen design,
2026-09-29: "container = manager, host executes"). The USER installs it as an SSH
forced command (see install_host_bridge.sh). sshd runs it for every call, with the
requested command in $SSH_ORIGINAL_COMMAND. It is split with shlex (never a shell,
never eval), checked against the whitelist below, and run directly. Everything else
is rejected. Every call is logged to ~/.local/state/afclaude/host_exec.log (0600, in a 0700 dir
the container cannot reach: never under <repo>/data, which it mounts read-write; opened O_NOFOLLOW,
caller text escaped to one line each). The one-shot `claude -p` calls (usage, Haiku, rc-send) run in
the bridge-owned ~/.local/state/afclaude/cwd with `--setting-sources user`, and ka_resume refuses
a --cwd at or under <repo>/data or not already its own realpath: a container-written .claude/settings.json (hooks) or CLAUDE.md
there would otherwise run on the host (review k2 H2/H3).

Allowed (exact shapes):
  ka_resume.sh --session <uuid> --tmux <name> --message <text> [--cwd <resolved dir under ROOT>] [--name <t>]
               [--model <m>] [--effort low|medium|high|xhigh|max] [--new] [--no-remote-control]
  claude agents --json [--all]
  claude -p --no-session-persistence --permission-mode dontAsk /usage
  claude -p --model haiku <only the flags usage_sampler.py / notify.py use>   (prompt on stdin)
  claude stop <8 hex>
  kill-claude <pid>        SIGTERM, only if /proc/<pid>/cmdline is a claude process
  tmux-has <name>          tmux has-session
  claude-env <pid>         the CLAUDE_* lines of /proc/<pid>/environ, claude pids only
  tmux-panes <name>        tmux list-panes -s -F '#{pane_pid}' (pane pids, read-only)
  tmux-kill <name>         tmux kill-session (the dispatcher's cleanup of its own sessions)
  tmux-clients <name>      tmux list-clients -F '#{client_activity}' (read-only: the owner typing in an
                           attached terminal holds a compaction, D-238)
  tmux-capture <name>      tmux capture-pane -e -p (read-only: a draft in the input box holds a compaction)
  tmux-rename <name> <new>
                           tmux rename-session (the one-time rename to a readable name, D-281)
  (<name>: a tmux session name, afc-<[a-z0-9-], at most 36>: TMUX below, names.py)
  proc-snapshot            JSON {pid: [ppid, starttime, argv|null]} of every process; argv only
                           for claude and keepalive.py processes (preflight / watcher lookup)
  usage-cache              JSON of ~/.claude.json "cachedUsageUtilization" (fetchedAtMs, utilization only)
  append-note PROGRESS.md|ALERTS.md   append stdin (<= 16 KB UTF-8 text) to that repo file
  rc-send <name>           a one-shot Haiku `claude -p` (RC_SEND_FLAGS: dontAsk, the SendMessage tool
                           only) asked to SendMessage stdin (<= 16 KB UTF-8 text) to the session named
                           <name> (RC_NAME): the dispatcher's continue of a `claude rc` thread
                           (docs/rc_server_mode.md §4.1); prompt and flags fixed here. Denied unless
                           <name> is one live ~/.claude/sessions entry, sdk-cli and idle; stopped
                           after RC_HOST_TIMEOUT seconds (exit 124)
"""
import datetime
import glob
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys

ROOT = "/mnt/BlockVolume/Claude"
REPO = os.path.join(ROOT, "work", "AFClaude")
DATA = os.path.join(REPO, "data")        # the container's read-write mount: nothing here is trusted
# the bridge's own state, outside every directory the container can write (override: tests only,
# sshd passes no env)
STATE = os.environ.get("AFCLAUDE_HOST_EXEC_STATE") or os.path.join(os.path.expanduser("~"), ".local", "state", "afclaude")
LOG = os.environ.get("AFCLAUDE_HOST_EXEC_LOG") or os.path.join(STATE, "host_exec.log")
CWD = os.path.join(STATE, "cwd")         # the cwd of the one-shot `claude -p` calls (empty, 0700, bridge-owned)
CLAUDE = os.path.expanduser("~/.local/bin/claude")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX8 = re.compile(r"^[0-9a-f]{8}$")
# the tmux session names (names.TMUX_RE; kept here: this file runs alone as the SSH forced command)
TMUX = re.compile(r"^(afc-[a-z0-9-]{1,36})\Z")
ENV = {"HOME": os.path.expanduser("~"), "USER": os.environ.get("USER", "opc"), "LANG": "C.UTF-8",
       "PATH": os.path.expanduser("~/.local/bin") + ":/usr/local/bin:/usr/bin:/bin",
       "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "TERM": "xterm-256color"}
# the one-shot calls read the user's settings only: a project or local .claude/settings.json (hooks)
# in whatever directory they run in is never loaded (verified: `claude -p` honours project hooks
# in an untrusted dir, even for /usage)
USER_SETTINGS = ["--setting-sources", "user"]
HAIKU_FLAGS = {"-p", "--model", "haiku", "--no-session-persistence", "--output-format", "json",
               "stream-json", "--verbose", "--permission-mode", "dontAsk", "--tools=",
               "--tools=PushNotification", "--allowedTools=PushNotification",
               "--strict-mcp-config", "--disable-slash-commands"}

# rc-send: kept identical to host.py's RC_NAME / RC_TEXT_MAX / RC_SEND_FLAGS / RC_SEND_PROMPT (test_host.py)
RC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
RC_TEXT_MAX = 16 * 1024
RC_SEND_FLAGS = ["-p", "--model", "haiku", "--permission-mode", "dontAsk",
                 "--tools=SendMessage", "--allowedTools=SendMessage", "--strict-mcp-config",
                 "--disable-slash-commands", "--output-format", "stream-json", "--verbose"]
RC_HOST_TIMEOUT = 100       # seconds the sender may run here: below the container's 120 s ssh timeout
SESSIONS = os.environ.get("AFCLAUDE_HOST_EXEC_SESSIONS") or os.path.expanduser("~/.claude/sessions")   # override: tests only
RC_SEND_PROMPT = ("Call the SendMessage tool exactly once, to the agent named exactly {name!r}, with the "
                  "message between the BEGIN and END lines below as its text, unchanged. If that agent is "
                  "not reachable, do not try any other name or agent and do not retry. Do nothing else. "
                  "Afterwards reply with the tool's result word for word.\nBEGIN\n{text}\nEND")


CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_log_failed = False


def one_line(text):
    """`text` with every control character (newline, CR, ESC, NUL, the Unicode line breaks) as a
    visible escape: caller-supplied text never makes a second log line (review k2 H2)."""
    return CTRL.sub(lambda m: m.group().encode("unicode_escape").decode(), text)


def log(msg):
    """Append one line. A log that can't be written (a symlink or a stray file where the log
    belongs, a full disk) is reported once on stderr and the call goes on, as before (the log is
    a record, not a gate)."""
    global _log_failed
    try:
        private_dir(os.path.dirname(LOG))
        fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a", errors="backslashreplace") as fh:   # a lone surrogate is escaped, not a crash
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {one_line(msg)}\n")
    except OSError as e:
        if not _log_failed:
            _log_failed = True
            print(f"host_exec: warning: log {LOG} not written ({e})", file=sys.stderr)


def under_data(path):
    """True when `path` (symlinks resolved) is <repo>/data or below: the container's writable tree."""
    real, data = os.path.realpath(path), os.path.realpath(DATA)
    return real == data or real.startswith(data + os.sep)


def private_dir(path):
    """Create `path` and its missing parents below the home dir as 0700, and make sure the directory is a
    real directory of this user that no group or other can write to: a wider mode of one of ours is
    narrowed to 0700; anything else (a symlink, someone else's, a file) raises OSError."""
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):      # the levels between home and `path` are ours to create private
        todo = []
        p = path
        while p != home and not os.path.lexists(p):
            todo.append(p)
            p = os.path.dirname(p)
        for p in reversed(todo):
            os.mkdir(p, 0o700)
    else:
        os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{path} is not a directory of the bridge user")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def claude_cwd():
    """The bridge-owned, empty, 0700 directory the one-shot `claude -p` calls run in (created on
    first use). Anything that makes it unusable denies the call: never a fallback to another cwd."""
    try:
        private_dir(os.path.dirname(CWD))   # each level private, not only the leaf
        private_dir(CWD)
        st = os.lstat(CWD)
    except OSError as e:
        deny(f"claude cwd unavailable: {e}")
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022 or under_data(CWD):
        deny("claude cwd is not a private directory of the bridge user")
    return CWD


def deny(why):
    log(f"DENY {why}: {os.environ.get('SSH_ORIGINAL_COMMAND', '')[:300]!r}")
    print(f"host_exec: denied ({why})", file=sys.stderr)
    sys.exit(126)


def is_claude_cmd(cmd):
    return os.path.basename(cmd) in ("claude", "2.1.283") or "/claude/versions/" in cmd or cmd.endswith("/claude")


def is_claude_pid(pid):
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].decode()
    except (OSError, UnicodeDecodeError):
        return False
    return is_claude_cmd(cmd)


def proc_snapshot():
    """{pid: [ppid, starttime, argv or None]}: what preflight needs from /proc (parent
    chains, start times vs ~/.claude/sessions/*.json, rc-server/--sdk-url argv).
    ppid and start time are shown for every pid (as `ps` does to any user); argv only
    for claude processes and keepalive.py watchers."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
            ppid, start = int(fields[1]), fields[19]
        except (OSError, IndexError, ValueError):
            continue
        show = bool(argv) and (is_claude_cmd(argv[0]) or any(os.path.basename(a) == "keepalive.py" for a in argv[:3]))
        out[d] = [ppid, start, argv if show else None]
    return out


def check_ka_resume(args):
    allowed = {"--session", "--tmux", "--message", "--cwd", "--name", "--model", "--effort"}
    i = 0
    seen = {}
    while i < len(args):
        a = args[i]
        if a in ("--new", "--no-remote-control"):   # flags without a value (--no-remote-control: D-269)
            i += 1
            continue
        if a not in allowed or i + 1 >= len(args):
            deny(f"ka_resume arg {a!r}")
        seen[a] = args[i + 1]
        i += 2
    if not UUID.match(seen.get("--session", "")) or not seen.get("--message"):
        deny("ka_resume needs --session <uuid> --message")
    if not TMUX.match(seen.get("--tmux", "")):
        deny("ka_resume needs --tmux <name>")
    cwd = seen.get("--cwd", ROOT)
    if under_data(cwd):                    # container-writable: its .claude/settings.json would run here (H3)
        deny("ka_resume cwd in the container-writable data dir")
    # the value is passed on unchanged, so it must already be its own resolved path: a symlink or a `..`
    # after one (data/l/.. is read as data by `cd`) or a swap after this check can't change where it lands
    if not os.path.isabs(cwd) or os.path.realpath(cwd) != cwd:
        deny(f"ka_resume --cwd must be a resolved absolute path (no symlink, `..`, `.` or trailing slash): "
             f"use {os.path.realpath(cwd)}")
    if cwd != ROOT and not cwd.startswith(ROOT + os.sep):
        deny("ka_resume cwd outside ROOT")
    if seen.get("--effort", "high") not in ("low", "medium", "high", "xhigh", "max"):
        deny("effort")
    if not re.match(r"^[\w.\-]+$", seen.get("--model", "claude-opus-5-5")):
        deny("model")


def proc_start(pid):
    """/proc/<pid>/stat starttime (what the registry's procStart holds), or None: no such process."""
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def check_rc_thread(name):
    """Deny unless `name` is the registry name of exactly one live ~/.claude/sessions entry (its pid
    alive, procStart matching) that is an sdk-cli thread child and idle. The container picks the
    name, so the host checks it itself: the sender may only ever message such a thread."""
    live = []
    for path in sorted(glob.glob(os.path.join(SESSIONS, "*.json"))):
        try:
            with open(path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue                       # being rewritten: not an entry of ours to judge
        if not isinstance(d, dict) or d.get("name") != name:
            continue
        try:
            pid = int(d.get("pid") or os.path.basename(path)[:-5])
        except ValueError:
            continue
        start = proc_start(pid)
        if start is not None and (d.get("procStart") is None or str(d["procStart"]) == start):
            live.append(d)
    if len(live) != 1:
        deny(f"rc-send: {len(live)} live sessions named {name}")
    if live[0].get("entrypoint") != "sdk-cli" or live[0].get("status") != "idle":
        deny(f"rc-send: {name} is {live[0].get('entrypoint')}/{live[0].get('status')}, not an idle sdk-cli thread")


def rc_send_cmd(args, data):
    """rc-send <name> with the message text `data` (stdin bytes) -> (cmd, stdin bytes), or deny
    (also when <name> is no idle live thread: check_rc_thread)."""
    if len(args) != 1 or not RC_NAME.match(args[0]):
        deny("rc-send needs one thread name")
    if not data or len(data) > RC_TEXT_MAX or b"\0" in data:
        deny("rc-send text empty, too long or binary")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        deny("rc-send text not UTF-8")
    check_rc_thread(args[0])
    log(f"OK rc-send {args[0]} ({len(data)} bytes)")
    return [CLAUDE] + USER_SETTINGS + RC_SEND_FLAGS, RC_SEND_PROMPT.format(name=args[0], text=text).encode()


def main():
    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        argv = shlex.split(raw)
    except ValueError:
        deny("unparsable")
    if not argv:
        deny("empty")
    head, args = argv[0], argv[1:]
    stdin = None
    if head in ("ka_resume.sh", os.path.join(REPO, "ka_resume.sh")):
        check_ka_resume(args)
        cmd = [os.path.join(REPO, "ka_resume.sh")] + args
    elif head == "claude" and args in (["agents", "--json"], ["agents", "--json", "--all"]):
        cmd = [CLAUDE] + args
    elif head == "claude" and args == ["-p", "--no-session-persistence", "--permission-mode", "dontAsk", "/usage"]:
        cmd = [CLAUDE] + USER_SETTINGS + args
    elif head == "claude" and args[:3] == ["-p", "--model", "haiku"] and set(args) <= HAIKU_FLAGS:
        cmd, stdin = [CLAUDE] + USER_SETTINGS + args, sys.stdin.buffer.read(200_000)
    elif head == "rc-send":
        cmd, stdin = rc_send_cmd(args, sys.stdin.buffer.read(RC_TEXT_MAX + 1))
    elif head == "claude" and len(args) == 2 and args[0] == "stop" and HEX8.match(args[1]):
        cmd = [CLAUDE] + args
    elif head == "kill-claude" and len(args) == 1 and args[0].isdigit():
        pid = int(args[0])
        if not is_claude_pid(pid):
            deny("not a claude pid")
        log(f"OK kill-claude {pid}")
        os.kill(pid, signal.SIGTERM)
        return 0
    elif head == "tmux-has" and len(args) == 1 and TMUX.match(args[0]):
        cmd = ["tmux", "has-session", "-t", "=" + args[0]]
    elif head == "tmux-panes" and len(args) == 1 and TMUX.match(args[0]):
        cmd = ["tmux", "list-panes", "-s", "-t", "=" + args[0], "-F", "#{pane_pid}"]
    elif head == "tmux-kill" and len(args) == 1 and TMUX.match(args[0]):
        cmd = ["tmux", "kill-session", "-t", "=" + args[0]]
    elif head == "tmux-clients" and len(args) == 1 and TMUX.match(args[0]):
        cmd = ["tmux", "list-clients", "-t", "=" + args[0], "-F", "#{client_activity}"]
    elif head == "tmux-capture" and len(args) == 1 and TMUX.match(args[0]):
        cmd = ["tmux", "capture-pane", "-e", "-p", "-t", "=" + args[0] + ":"]
    elif head == "tmux-rename" and len(args) == 2 and TMUX.match(args[0]) and TMUX.match(args[1]):
        old, new = args
        cmd = ["tmux", "rename-session", "-t", "=" + old, new]
        log(f"OK {shlex.join(cmd)}")
        r = subprocess.run(cmd, env=ENV, cwd=ROOT, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        # tmux can rename successfully and still exit 1 with "no current client" (no terminal
        # attached to a forced-command/cron run): verify from tmux's own state, not rc (D-281,
        # incident 2026-10-22 05:16).
        renamed = subprocess.run(["tmux", "has-session", "-t", "=" + new], env=ENV, cwd=ROOT,
                                 capture_output=True).returncode == 0 and \
            subprocess.run(["tmux", "has-session", "-t", "=" + old], env=ENV, cwd=ROOT,
                           capture_output=True).returncode != 0
        if renamed:
            if r.returncode != 0:
                log(f"tmux-rename {old} {new}: rc={r.returncode} {r.stderr.strip()[:200]!r} but verified renamed")
            return 0
        log(f"tmux-rename {old} {new} FAILED rc={r.returncode}: {r.stderr.strip()[:200]!r}")
        return r.returncode or 1
    elif head == "proc-snapshot" and not args:
        sys.stdout.write(json.dumps(proc_snapshot()) + "\n")
        log("OK proc-snapshot")
        return 0
    elif head == "usage-cache" and not args:
        try:
            with open(os.path.join(ENV["HOME"], ".claude.json")) as fh:
                raw = json.load(fh).get("cachedUsageUtilization") or {}
            cache = {k: raw[k] for k in ("fetchedAtMs", "utilization") if k in raw}
        except (OSError, ValueError) as e:
            deny(f"usage-cache unreadable: {e}")
        sys.stdout.write(json.dumps(cache) + "\n")
        log("OK usage-cache")
        return 0
    elif head == "append-note" and len(args) == 1 and args[0] in ("PROGRESS.md", "ALERTS.md"):
        data = sys.stdin.buffer.read(16 * 1024 + 1)
        if len(data) > 16 * 1024 or b"\0" in data:
            deny("append-note too long or binary")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            deny("append-note not UTF-8")
        with open(os.path.join(REPO, args[0]), "a") as fh:
            fh.write(text)
        log(f"OK append-note {args[0]} ({len(data)} bytes)")
        return 0
    elif head == "claude-env" and len(args) == 1 and args[0].isdigit():
        if not is_claude_pid(int(args[0])):
            deny("not a claude pid")
        env = open(f"/proc/{args[0]}/environ", "rb").read().split(b"\0")
        sys.stdout.write("\n".join(x.decode(errors="replace") for x in env if x.startswith(b"CLAUDE")) + "\n")
        log(f"OK claude-env {args[0]}")
        return 0
    else:
        deny("not whitelisted")
    log(f"OK {shlex.join(cmd)[:300]}")
    cwd = claude_cwd() if (head == "claude" and args[:1] == ["-p"]) or head == "rc-send" else ROOT
    try:
        return subprocess.run(cmd, input=stdin, env=ENV, cwd=cwd,
                              timeout=RC_HOST_TIMEOUT if head == "rc-send" else None).returncode
    except subprocess.TimeoutExpired:      # run() has killed the sender
        log(f"TIMEOUT rc-send after {RC_HOST_TIMEOUT} s")
        print(f"host_exec: rc-send stopped after {RC_HOST_TIMEOUT} s", file=sys.stderr)
        return 124


if __name__ == "__main__":
    sys.exit(main())
