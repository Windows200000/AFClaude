#!/usr/bin/env python3
"""Whitelisted host executor for the AFClaude manager container (user-chosen design,
2026-09-29: "container = manager, host executes"). The USER installs it as an SSH
forced command (see install_host_bridge.sh). sshd runs it for every call, with the
requested command in $SSH_ORIGINAL_COMMAND. It is split with shlex (never a shell,
never eval), checked against the whitelist below, and run directly. Everything else
is rejected. Every call is logged to <repo>/data/host_exec.log.

It is part of the host kit (docs/phase5b_plan.md §3.2): installed by install_host_bridge.sh into
<share>/afclaude/host-kit/<HOST_KIT_VERSION>/, run through the `current` link. Its machine-specific
values (allowed roots, repo, Claude binary) are in <share>/afclaude/host.json (sshd passes no env):
a missing or malformed file refuses every command. Python 3.9 compatible (the host's python3).

Allowed (exact shapes):
  ka_resume.sh --session <uuid> --tmux <name> --message <text> [--cwd <dir under a root>] [--name <t>]
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
  kit-version              JSON {kit_version, whitelist_hash, commands, config_format}: the caller compares
                           kit_version with its own HOST_KIT_VERSION (a mismatch = re-run the install)
  host-config get          the host.json content (JSON)
  host-config set roots <dir>...|repo <dir>|claude <file>
                           change host.json (the setup's write, 5d): directories must exist, be owned by
                           the host user and not be (or contain) the home or a dot-directory of it
  rc-send <name>           a one-shot Haiku `claude -p` (RC_SEND_FLAGS: dontAsk, the SendMessage tool
                           only) asked to SendMessage stdin (<= 16 KB UTF-8 text) to the session named
                           <name> (RC_NAME): the dispatcher's continue of a `claude rc` thread
                           (docs/rc_server_mode.md §4.1); prompt and flags fixed here. Denied unless
                           <name> is one live ~/.claude/sessions entry, sdk-cli and idle; stopped
                           after RC_HOST_TIMEOUT seconds (exit 124)
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, NamedTuple, NoReturn, Optional, Tuple

# The kit version: bumped with every change of the whitelist (a new command, a new shape) or of the
# host.json format. host.py's HOST_KIT_VERSION is the same string (test_host_kit.py compares them) and
# the container's host check calls `kit-version` against it.
HOST_KIT_VERSION = "1"
CONFIG_FORMAT = 1
# every command head the whitelist knows (the elif chain in main(); test_host_kit.py compares the two)
COMMANDS = ("append-note", "claude", "claude-env", "host-config", "ka_resume.sh", "kill-claude", "kit-version",
            "proc-snapshot", "rc-send", "tmux-capture", "tmux-clients", "tmux-has", "tmux-kill", "tmux-panes",
            "tmux-rename", "usage-cache")
OUT_MAX = 2 * 1024 * 1024    # bytes of output this file produces itself (the commands it only runs write straight through)
KIT_DIR = os.path.dirname(os.path.abspath(__file__))   # <share>/afclaude/host-kit/current when run by sshd
# host.json: next to host-kit/, i.e. <share>/afclaude/host.json (override: tests only, sshd passes no env)
HOST_JSON = os.environ.get("AFCLAUDE_HOST_JSON") or os.path.join(os.path.dirname(os.path.dirname(KIT_DIR)), "host.json")
LOG_OVERRIDE = os.environ.get("AFCLAUDE_HOST_EXEC_LOG")   # override: tests only
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX8 = re.compile(r"^[0-9a-f]{8}$")
# the tmux session names (names.TMUX_RE; kept here: this file runs alone as the SSH forced command)
TMUX = re.compile(r"^(afc-[a-z0-9-]{1,36})\Z")
HOME = os.path.expanduser("~")
ENV = {"HOME": HOME, "USER": os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name, "LANG": "C.UTF-8",
       "PATH": os.path.expanduser("~/.local/bin") + ":/usr/local/bin:/usr/bin:/bin",
       "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "TERM": "xterm-256color"}
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
SET_KEYS = ("roots", "repo", "claude")
MAX_ROOTS = 8


def whitelist_hash() -> str:
    """What `kit-version` reports next to the version: a hash of the command heads, so a command added
    without a version bump shows (test_host_kit.py pins the pair)."""
    return hashlib.sha256("\n".join(sorted(COMMANDS)).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ host.json

class Config(NamedTuple):
    roots: List[str]     # allowed directories (a launch's cwd, project files); symlinks resolved at use
    repo: str            # the checkout the kit's source came from: data/ (log, claude -p cwd), the notes
    claude: str          # the Claude binary


class ConfigError(Exception):
    pass


def _abs_path(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v.startswith("/") or "\0" in v:
        raise ConfigError(f"{what} must be an absolute path")
    if os.path.normpath(v) != v or v == "/":
        raise ConfigError(f"{what} must be a normalized path below /, no '..' or '//': {v!r}")
    return v


def parse_config(text: str) -> Config:
    """host.json's text -> Config, or ConfigError. Exactly these keys: format (CONFIG_FORMAT), roots
    (1..MAX_ROOTS absolute paths), repo, claude (absolute paths)."""
    try:
        d = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"not JSON: {e}")
    if not isinstance(d, dict) or set(d) != {"format", "roots", "repo", "claude"}:
        raise ConfigError("must be an object with exactly the keys format, roots, repo, claude")
    if d["format"] != CONFIG_FORMAT or isinstance(d["format"], bool):
        raise ConfigError(f"format {d['format']!r}, this kit reads {CONFIG_FORMAT}")
    roots = d["roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ROOTS or len(set(map(str, roots))) != len(roots):
        raise ConfigError(f"roots must be a list of 1..{MAX_ROOTS} different paths")
    return Config([_abs_path(r, "a root") for r in roots], _abs_path(d["repo"], "repo"),
                  _abs_path(d["claude"], "claude"))


def load_config(path: str) -> Config:
    """Read and check host.json: a regular file (no symlink), the host user's own, no group/other bits."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise ConfigError(f"{path} unreadable: {e.strerror}")
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ConfigError(f"{path} must be a regular file of this user with mode 0600")
    try:
        with open(path) as fh:
            return parse_config(fh.read())
    except OSError as e:
        raise ConfigError(f"{path} unreadable: {e.strerror}")


def config_text(cfg: Config) -> str:
    return json.dumps({"format": CONFIG_FORMAT, "roots": cfg.roots, "repo": cfg.repo, "claude": cfg.claude},
                      indent=2) + "\n"


def write_config(path: str, cfg: Config) -> None:
    """Atomic: a temp file in the same directory (0600), then rename."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".host.json.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(config_text(cfg))
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


_cfg: Optional[Config] = None


def cfg() -> Config:
    """host.json, read once per call (sshd starts one process per command); denies when it is missing or
    malformed: nothing is run without it."""
    global _cfg
    if _cfg is None:
        try:
            _cfg = load_config(HOST_JSON)
        except ConfigError as e:
            deny(f"host config: {e}; re-run the host kit install (docker/install_host_bridge.sh)")
    return _cfg


def under_roots(path: str) -> Optional[str]:
    """The resolved path (symlinks and `..` gone) when `path` is absolute and at or below one of the
    allowed roots (resolved too), else None. Part of every command that takes a path from the caller."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        return None
    real = os.path.realpath(path)
    for root in cfg().roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r + os.sep):
            return real
    return None


def log_path() -> str:
    if LOG_OVERRIDE:
        return LOG_OVERRIDE
    if _cfg is not None:
        return os.path.join(_cfg.repo, "data", "host_exec.log")
    return os.path.join(os.path.dirname(HOST_JSON), "host_exec.log")      # no usable config yet


def log(msg: str) -> None:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def deny(why: str) -> NoReturn:
    log(f"DENY {why}: {os.environ.get('SSH_ORIGINAL_COMMAND', '')[:300]!r}")
    print(f"host_exec: denied ({why})", file=sys.stderr)
    sys.exit(126)


def emit(text: str) -> None:
    """Write output this file produced itself; over OUT_MAX it is an error, never a cut-off result."""
    if len(text.encode("utf-8", "replace")) > OUT_MAX:
        deny(f"output over {OUT_MAX} bytes")
    sys.stdout.write(text)


def kit_version_json() -> str:
    return json.dumps({"kit_version": HOST_KIT_VERSION, "whitelist_hash": whitelist_hash(),
                       "commands": sorted(COMMANDS), "config_format": CONFIG_FORMAT}) + "\n"


def _owned_dir(path: str) -> str:
    """`path` resolved, for host-config set: an existing directory of this user, not the home, an
    ancestor of it or inside a dot-directory of it (~/.ssh, ~/.claude, ...)."""
    real = os.path.realpath(_abs_path(path, "a root"))
    try:
        st = os.stat(real)
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}")
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ConfigError(f"{path} is not a directory of this user")
    home = os.path.realpath(HOME)
    if real == home or home.startswith(real + os.sep):
        raise ConfigError(f"{path} is the home directory or contains it")
    if real.startswith(home + os.sep) and real[len(home) + 1:].startswith("."):
        raise ConfigError(f"{path} is inside a dot-directory of the home")
    return real


def config_set(key: str, values: List[str]) -> None:
    """host-config set KEY VALUE...: the setup's write (5d). roots: 1..MAX_ROOTS directories; repo: a
    directory; claude: an executable file. Every value is checked here, on the host."""
    old = cfg()
    if key == "roots":
        if not 1 <= len(values) <= MAX_ROOTS:
            deny(f"host-config set roots takes 1..{MAX_ROOTS} directories")
        new = old._replace(roots=[_owned_dir(v) for v in values])
    elif key == "repo" and len(values) == 1:
        new = old._replace(repo=_owned_dir(values[0]))
    elif key == "claude" and len(values) == 1:
        real = os.path.realpath(_abs_path(values[0], "claude"))
        if not os.path.isfile(real) or not os.access(real, os.X_OK) or os.stat(real).st_uid not in (0, os.getuid()):
            raise ConfigError(f"{values[0]} is not an executable file of this user or root")
        new = old._replace(claude=_abs_path(values[0], "claude"))
    else:
        deny("host-config set needs roots <dir>..., repo <dir> or claude <file>")
    parse_config(config_text(new))      # the file the next call reads must pass its own check
    write_config(HOST_JSON, new)
    log(f"OK host-config set {key} {' '.join(values)[:200]}")


# ------------------------------------------------------------------ the whitelist

def is_claude_cmd(cmd: str) -> bool:
    return os.path.basename(cmd) in ("claude", "2.1.283") or "/claude/versions/" in cmd or cmd.endswith("/claude")


def is_claude_pid(pid: object) -> bool:
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].decode()
    except (OSError, UnicodeDecodeError):
        return False
    return is_claude_cmd(cmd)


def proc_snapshot() -> Dict[str, List[Any]]:
    """{pid: [ppid, starttime, argv or None]}: what preflight needs from /proc (parent
    chains, start times vs ~/.claude/sessions/*.json, rc-server/--sdk-url argv).
    ppid and start time are shown for every pid (as `ps` does to any user); argv only
    for claude processes and keepalive.py watchers."""
    out: Dict[str, List[Any]] = {}
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


def check_ka_resume(args: List[str]) -> List[str]:
    """Deny a shape that is not ka_resume.sh's; -> the args with --cwd resolved (the path that was checked
    is the one used) or, when the caller gave none, the first root (the script's own default before
    host.json)."""
    allowed = {"--session", "--tmux", "--message", "--cwd", "--name", "--model", "--effort"}
    i = 0
    seen: Dict[str, str] = {}
    out: List[str] = []
    while i < len(args):
        a = args[i]
        if a in ("--new", "--no-remote-control"):   # flags without a value (--no-remote-control: D-269)
            out.append(a)
            i += 1
            continue
        if a not in allowed or i + 1 >= len(args):
            deny(f"ka_resume arg {a!r}")
        seen[a] = args[i + 1]
        if a != "--cwd":
            out += [a, args[i + 1]]
        i += 2
    if not UUID.match(seen.get("--session", "")) or not seen.get("--message"):
        deny("ka_resume needs --session <uuid> --message")
    if not TMUX.match(seen.get("--tmux", "")):
        deny("ka_resume needs --tmux <name>")
    cwd = under_roots(seen["--cwd"]) if "--cwd" in seen else cfg().roots[0]
    if cwd is None:
        deny("ka_resume cwd outside the allowed roots")
    if seen.get("--effort", "high") not in ("low", "medium", "high", "xhigh", "max"):
        deny("effort")
    if not re.match(r"^[\w.\-]+$", seen.get("--model", "claude-opus-5-5")):
        deny("model")
    return out + ["--cwd", cwd]


def proc_start(pid: object) -> Optional[str]:
    """/proc/<pid>/stat starttime (what the registry's procStart holds), or None: no such process."""
    try:
        with open(f"/proc/{int(str(pid))}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def check_rc_thread(name: str) -> None:
    """Deny unless `name` is the registry name of exactly one live ~/.claude/sessions entry (its pid
    alive, procStart matching) that is an sdk-cli thread child and idle. The container picks the
    name, so the host checks it itself: the sender may only ever message such a thread."""
    live: List[Dict[str, Any]] = []
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


def rc_send_cmd(args: List[str], data: bytes) -> Tuple[List[str], bytes]:
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
    return [cfg().claude] + RC_SEND_FLAGS, RC_SEND_PROMPT.format(name=args[0], text=text).encode()


def main() -> int:
    config = cfg()          # first: no host.json (or a malformed one) refuses every command
    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        argv = shlex.split(raw)
    except ValueError:
        deny("unparsable")
    if not argv:
        deny("empty")
    head, args = argv[0], argv[1:]
    if head not in COMMANDS:
        deny("not whitelisted")
    stdin: Optional[bytes] = None
    cmd: List[str]
    root = config.roots[0]
    if head == "ka_resume.sh":
        cmd = [os.path.join(KIT_DIR, "ka_resume.sh")] + check_ka_resume(args)
        # the script writes its launch files outside the versioned kit directory
        ENV["AFCLAUDE_KIT_RUN"] = os.path.join(os.path.dirname(os.path.dirname(KIT_DIR)), "run")
    elif head == "claude" and args in (["agents", "--json"], ["agents", "--json", "--all"]):
        cmd = [config.claude] + args
    elif head == "claude" and args == ["-p", "--no-session-persistence", "--permission-mode", "dontAsk", "/usage"]:
        cmd = [config.claude] + args
    elif head == "claude" and args[:3] == ["-p", "--model", "haiku"] and set(args) <= HAIKU_FLAGS:
        cmd, stdin = [config.claude] + args, sys.stdin.buffer.read(200_000)
    elif head == "rc-send":
        cmd, stdin = rc_send_cmd(args, sys.stdin.buffer.read(RC_TEXT_MAX + 1))
    elif head == "claude" and len(args) == 2 and args[0] == "stop" and HEX8.match(args[1]):
        cmd = [config.claude] + args
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
        r = subprocess.run(cmd, env=ENV, cwd=root, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        # tmux can rename successfully and still exit 1 with "no current client" (no terminal
        # attached to a forced-command/cron run): verify from tmux's own state, not rc (D-281,
        # incident 2026-10-22 05:16).
        renamed = subprocess.run(["tmux", "has-session", "-t", "=" + new], env=ENV, cwd=root,
                                 capture_output=True).returncode == 0 and \
            subprocess.run(["tmux", "has-session", "-t", "=" + old], env=ENV, cwd=root,
                           capture_output=True).returncode != 0
        if renamed:
            if r.returncode != 0:
                log(f"tmux-rename {old} {new}: rc={r.returncode} {r.stderr.strip()[:200]!r} but verified renamed")
            return 0
        log(f"tmux-rename {old} {new} FAILED rc={r.returncode}: {r.stderr.strip()[:200]!r}")
        return r.returncode or 1
    elif head == "proc-snapshot" and not args:
        emit(json.dumps(proc_snapshot()) + "\n")
        log("OK proc-snapshot")
        return 0
    elif head == "usage-cache" and not args:
        try:
            with open(os.path.join(ENV["HOME"], ".claude.json")) as fh:
                raw_cache = json.load(fh).get("cachedUsageUtilization") or {}
            cache = {k: raw_cache[k] for k in ("fetchedAtMs", "utilization") if k in raw_cache}
        except (OSError, ValueError) as e:
            deny(f"usage-cache unreadable: {e}")
        emit(json.dumps(cache) + "\n")
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
        with open(os.path.join(config.repo, args[0]), "a") as fh:
            fh.write(text)
        log(f"OK append-note {args[0]} ({len(data)} bytes)")
        return 0
    elif head == "claude-env" and len(args) == 1 and args[0].isdigit():
        if not is_claude_pid(int(args[0])):
            deny("not a claude pid")
        env = open(f"/proc/{args[0]}/environ", "rb").read().split(b"\0")
        emit("\n".join(x.decode(errors="replace") for x in env if x.startswith(b"CLAUDE")) + "\n")
        log(f"OK claude-env {args[0]}")
        return 0
    elif head == "kit-version" and not args:
        emit(kit_version_json())
        log("OK kit-version")
        return 0
    elif head == "host-config" and args == ["get"]:
        emit(config_text(config))
        log("OK host-config get")
        return 0
    elif head == "host-config" and len(args) >= 3 and args[0] == "set" and args[1] in SET_KEYS:
        try:
            config_set(args[1], args[2:])
        except ConfigError as e:
            deny(f"host-config set: {e}")
        return 0
    else:
        deny("not whitelisted")
    log(f"OK {shlex.join(cmd)[:300]}")
    run_cwd = os.path.join(config.repo, "data") if (head == "claude" and args[:1] == ["-p"]) or head == "rc-send" else root
    try:
        return subprocess.run(cmd, input=stdin, env=ENV, cwd=run_cwd,
                              timeout=RC_HOST_TIMEOUT if head == "rc-send" else None).returncode
    except subprocess.TimeoutExpired:      # run() has killed the sender
        log(f"TIMEOUT rc-send after {RC_HOST_TIMEOUT} s")
        print(f"host_exec: rc-send stopped after {RC_HOST_TIMEOUT} s", file=sys.stderr)
        return 124


def install_args(argv: List[str]) -> int:
    """The two things the install script asks of this file from a shell, never over ssh (sshd hands the
    forced command no arguments, and it refuses to run with any when SSH_CONNECTION is set):
      --print-version
      --check-config <host.json>     (exit 0 when it passes load_config, else 1 with the reason)
      --write-config <host.json> --root DIR... --repo DIR --claude FILE   (a new file; never overwrites)"""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_ORIGINAL_COMMAND"):
        deny("install mode over ssh")
    if argv == ["--print-version"]:
        print(HOST_KIT_VERSION)
        return 0
    if len(argv) == 2 and argv[0] == "--check-config":
        try:
            load_config(argv[1])
        except ConfigError as e:
            print(f"host_exec: {e}", file=sys.stderr)
            return 1
        return 0
    if len(argv) >= 2 and argv[0] == "--write-config":
        roots: List[str] = []
        opts: Dict[str, str] = {}
        rest = argv[2:]
        while rest:
            if len(rest) < 2 or rest[0] not in ("--root", "--repo", "--claude"):
                print("usage: --write-config FILE --root DIR [--root DIR...] --repo DIR --claude FILE", file=sys.stderr)
                return 2
            if rest[0] == "--root":
                roots.append(rest[1])
            else:
                opts[rest[0]] = rest[1]
            rest = rest[2:]
        try:
            if os.path.lexists(argv[1]):
                raise ConfigError(f"{argv[1]} exists")
            if "--repo" not in opts or "--claude" not in opts:
                raise ConfigError("--repo and --claude are required")
            new = Config([_owned_dir(r) for r in roots], _owned_dir(opts["--repo"]), _abs_path(opts["--claude"], "claude"))
            if not os.access(new.claude, os.X_OK):
                raise ConfigError(f"{new.claude} is not executable")
            parse_config(config_text(new))
            write_config(argv[1], new)
        except ConfigError as e:
            print(f"host_exec: {e}", file=sys.stderr)
            return 1
        return 0
    print("usage: host_exec.py --print-version | --check-config FILE | --write-config ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(install_args(sys.argv[1:]) if len(sys.argv) > 1 else main())
