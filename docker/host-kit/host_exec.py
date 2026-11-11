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
  transcripts <config-dir-id> --changed-since <epoch> [--head N] [--tail N]
                           JSON: every transcript (*.jsonl, recursive) under <config dir>/projects changed
                           since then: path, size, mtime, and the projected first/last entries (the fields
                           the stall scan, the keep-alive and the compaction timing read); `truncated` true
                           when a cap cut it (the caller treats that as an error)
  sessions <config-dir-id> JSON of the <config dir>/sessions/*.json files (the running-session registry)
  doc-files read <dir> <name>...
                           JSON {name: text|null} of PROGRESS.md / GOALS.md / EXCEPTIONS.md / DECISIONS.md in
                           a directory at or under an allowed root or the repo (regular files, <= 1 MB)
  claude auth status       the login state (the host check); claude doctor: the settings check
  tmux-check               creates and kills the tmux session afc-check (the host check)
  df                       JSON of the free space under the Claude dir and the repo
  clock                    JSON {epoch_ms} of the host clock (drift check)
  (<config-dir-id>: `default` = ~/.claude, or a key of host.json's optional `config_dirs`)
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
import time
from typing import Any, Dict, List, NamedTuple, NoReturn, Optional, Tuple

# The kit version: bumped with every change of the whitelist (a new command, a new shape) or of the
# host.json format. host.py's HOST_KIT_VERSION is the same string (test_host_kit.py compares them) and
# the container's host check calls `kit-version` against it.
HOST_KIT_VERSION = "2"
CONFIG_FORMAT = 1
# every command head the whitelist knows (the elif chain in main(); test_host_kit.py compares the two)
COMMANDS = ("append-note", "claude", "claude-env", "clock", "df", "doc-files", "host-config", "ka_resume.sh",
            "kill-claude", "kit-version", "proc-snapshot", "rc-send", "sessions", "tmux-capture", "tmux-check",
            "tmux-clients", "tmux-has", "tmux-kill", "tmux-panes", "tmux-rename", "transcripts", "usage-cache")
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
CONFIG_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")      # a Claude config dir's id (`default` = ~/.claude)
DOC_NAMES = ("PROGRESS.md", "GOALS.md", "EXCEPTIONS.md", "DECISIONS.md")   # doc-files read: these only
DOC_MAX = 1024 * 1024                                       # bytes of one doc file
TX_MAX_FILES = 2000                                         # transcripts: files / entries per call
TX_MAX_ENTRIES = 20
TX_TEXT = 300                                               # chars of an entry's text in the projection
TX_TAIL_READ = 16 * 1024 * 1024                             # most bytes read back from a file's end for its tail
SESSIONS_MAX = 500
CHECK_TIMEOUT = 60                                          # seconds the check commands may run (exit 124)
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
    config_dirs: Tuple[Tuple[str, str], ...] = ()   # optional: (id, Claude config dir) beyond `default` (~/.claude)


class ConfigError(Exception):
    pass


def _abs_path(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v.startswith("/") or "\0" in v:
        raise ConfigError(f"{what} must be an absolute path")
    if os.path.normpath(v) != v or v == "/":
        raise ConfigError(f"{what} must be a normalized path below /, no '..' or '//': {v!r}")
    return v


def parse_config(text: str) -> Config:
    """host.json's text -> Config, or ConfigError. These keys: format (CONFIG_FORMAT), roots (1..MAX_ROOTS
    absolute paths), repo, claude (absolute paths), and optionally config_dirs ({id: absolute path})."""
    try:
        d = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"not JSON: {e}")
    base = {"format", "roots", "repo", "claude"}
    if not isinstance(d, dict) or not base <= set(d) <= base | {"config_dirs"}:
        raise ConfigError("must be an object with the keys format, roots, repo, claude (and optionally config_dirs)")
    if d["format"] != CONFIG_FORMAT or isinstance(d["format"], bool):
        raise ConfigError(f"format {d['format']!r}, this kit reads {CONFIG_FORMAT}")
    roots = d["roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ROOTS or len(set(map(str, roots))) != len(roots):
        raise ConfigError(f"roots must be a list of 1..{MAX_ROOTS} different paths")
    dirs = d.get("config_dirs", {})
    if not isinstance(dirs, dict) or len(dirs) > MAX_ROOTS or not all(
            isinstance(k, str) and CONFIG_ID.match(k) and k != "default" for k in dirs):
        raise ConfigError(f"config_dirs must be an object of at most {MAX_ROOTS} ids ({CONFIG_ID.pattern}, not "
                          "'default') to absolute paths")
    return Config([_abs_path(r, "a root") for r in roots], _abs_path(d["repo"], "repo"),
                  _abs_path(d["claude"], "claude"),
                  tuple((k, _abs_path(v, f"config dir {k}")) for k, v in sorted(dirs.items())))


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
    d: Dict[str, Any] = {"format": CONFIG_FORMAT, "roots": cfg.roots, "repo": cfg.repo, "claude": cfg.claude}
    if cfg.config_dirs:
        d["config_dirs"] = dict(cfg.config_dirs)
    return json.dumps(d, indent=2) + "\n"


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


# ------------------------------------------------------------------ reads for the container (§3.4)

def config_dir(ident: str) -> str:
    """The Claude config dir behind an id: `default` is ~/.claude, any other id must be a key of host.json's
    config_dirs. Anything else is a denial (the caller never names a path)."""
    if not CONFIG_ID.match(ident):
        deny("config dir id")
    if ident == "default":
        return os.path.join(HOME, ".claude")
    for k, v in cfg().config_dirs:
        if k == ident:
            return v
    deny(f"config dir id {ident!r} not in host.json")


def project_entry(e: Any) -> Dict[str, Any]:
    """The projection of one transcript entry: the fields the readers use (type, timestamp, uuid, the
    flags, the model, the first TX_TEXT chars of its text), never the raw entry (tool results are large)."""
    if not isinstance(e, dict):
        return {"type": None}
    out: Dict[str, Any] = {k: e[k] for k in ("type", "timestamp", "uuid", "subtype", "operation", "sessionId",
                                              "isSidechain", "isMeta", "isCompactSummary", "isApiErrorMessage")
                           if k in e}
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else e.get("content")
    if isinstance(msg, dict) and "model" in msg:
        out["model"] = msg["model"]
    if isinstance(content, str):
        out["text"] = content[:TX_TEXT]
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        out["text"] = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")[:TX_TEXT]
        out["tool_result"] = any(b.get("type") == "tool_result" for b in blocks)
    return out


def parse_lines(lines: List[bytes]) -> Tuple[List[Dict[str, Any]], int]:
    """(projected entries, number of lines that were not JSON: a half-written last line)."""
    out: List[Dict[str, Any]] = []
    bad = 0
    for ln in lines:
        if not ln.strip():
            continue
        try:
            out.append(project_entry(json.loads(ln)))
        except ValueError:
            bad += 1
    return out, bad


def read_head(path: str, n: int) -> Tuple[List[Dict[str, Any]], int]:
    if n <= 0:
        return [], 0
    lines: List[bytes] = []
    with open(path, "rb") as fh:
        for ln in fh:
            lines.append(ln)
            if len(lines) >= n:
                break
    return parse_lines(lines)


def read_tail(path: str, n: int, size: int) -> Tuple[List[Dict[str, Any]], int, bool]:
    """The last `n` entries, reading back from the end in blocks; (entries, bad lines, complete). Not
    complete when TX_TAIL_READ bytes did not hold n whole lines."""
    if n <= 0 or size == 0:
        return [], 0, True
    block, data, pos = 256 * 1024, b"", size
    with open(path, "rb") as fh:
        while pos > 0 and data.count(b"\n") <= n and size - pos < TX_TAIL_READ:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.split(b"\n")
    if pos > 0:
        lines = lines[1:]               # the first line is cut off
    complete = pos == 0 or len([x for x in lines if x.strip()]) >= n
    entries, bad = parse_lines([x for x in lines if x.strip()][-n:])
    return entries, bad, complete


def transcripts_json(args: List[str]) -> str:
    """transcripts <config-dir-id> --changed-since <epoch> [--head N] [--tail N] -> JSON, or deny."""
    if len(args) < 3 or args[1] != "--changed-since":
        deny("transcripts needs <config-dir-id> --changed-since <epoch>")
    base = os.path.join(config_dir(args[0]), "projects")
    try:
        since = float(args[2])
    except ValueError:
        deny("transcripts: --changed-since is not a number")
    if not 0 <= since < 4e9:
        deny("transcripts: --changed-since out of range")
    opts = {"--head": 1, "--tail": 5}
    rest = args[3:]
    while rest:
        if len(rest) < 2 or rest[0] not in opts or not rest[1].isdigit() or int(rest[1]) > TX_MAX_ENTRIES:
            deny(f"transcripts: bad option {rest[0]!r}")
        opts[rest[0]] = int(rest[1])
        rest = rest[2:]
    files: List[Dict[str, Any]] = []
    truncated = False
    for dirpath, _dirs, names in os.walk(base):         # no symlinked directories are followed
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue                                  # removed while walking
            if not stat.S_ISREG(st.st_mode) or st.st_mtime < since:
                continue
            if len(files) >= TX_MAX_FILES:
                truncated = True
                continue
            try:
                head, bad_h = read_head(path, opts["--head"])
                tail, bad_t, complete = read_tail(path, opts["--tail"], st.st_size)
            except OSError:
                continue
            truncated = truncated or not complete
            files.append({"path": os.path.relpath(path, base), "size": st.st_size, "mtime": st.st_mtime,
                          "head": head, "tail": tail, "bad_lines": bad_h + bad_t, "tail_complete": complete})
    files.sort(key=lambda f: f["path"])
    return json.dumps({"config_dir": args[0], "truncated": truncated, "files": files}) + "\n"


def sessions_json(args: List[str]) -> str:
    """sessions <config-dir-id> -> JSON of <config dir>/sessions/*.json, or deny."""
    if len(args) != 1:
        deny("sessions needs <config-dir-id>")
    base = os.path.join(config_dir(args[0]), "sessions")
    out: List[Dict[str, Any]] = []
    truncated = False
    for path in sorted(glob.glob(os.path.join(base, "*.json"))):
        if len(out) >= SESSIONS_MAX:
            truncated = True
            break
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
                out.append({"file": os.path.basename(path), "error": "not a small regular file"})
                continue
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:      # being rewritten: reported, the caller decides
            out.append({"file": os.path.basename(path), "error": f"unreadable: {e}"})
            continue
        out.append({"file": os.path.basename(path), "mtime": st.st_mtime, "data": data})
    return json.dumps({"config_dir": args[0], "truncated": truncated, "files": out}) + "\n"


def doc_files_json(args: List[str]) -> str:
    """doc-files read <dir> <name>... -> JSON {name: text or null}: `dir` at or under an allowed root or the
    repo, names from DOC_NAMES, regular files only (no symlink), at most DOC_MAX bytes, UTF-8."""
    if len(args) < 3 or args[0] != "read" or len(set(args[2:])) != len(args[2:]) or not set(args[2:]) <= set(DOC_NAMES):
        deny("doc-files read <dir> <name>... (names: " + ", ".join(DOC_NAMES) + ")")
    real = under_roots(args[1])
    if real is None and args[1].startswith("/") and "\0" not in args[1] and \
            os.path.realpath(args[1]) == os.path.realpath(cfg().repo):
        real = os.path.realpath(args[1])
    if real is None:
        deny("doc-files: directory outside the allowed roots")
    if not os.path.isdir(real):
        deny("doc-files: not a directory")
    out: Dict[str, Optional[str]] = {}
    for name in args[2:]:
        try:
            fd = os.open(os.path.join(real, name), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            out[name] = None
            continue
        except OSError as e:
            deny(f"doc-files: {name}: {e.strerror}")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            deny(f"doc-files: {name} is not a regular file")
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(DOC_MAX + 1)
        if len(data) > DOC_MAX:
            deny(f"doc-files: {name} over {DOC_MAX} bytes")
        try:
            out[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            deny(f"doc-files: {name} is not UTF-8")
    return json.dumps(out) + "\n"


def df_json() -> str:
    """Free space under the Claude dir and the repo (host.json's), the two the host check watches."""
    out: Dict[str, Any] = {}
    for key, path in (("claude_dir", os.path.join(HOME, ".claude")), ("repo", cfg().repo)):
        try:
            v = os.statvfs(path)
        except OSError as e:
            deny(f"df: {key}: {e.strerror}")
        out[key] = {"path": path, "total_bytes": v.f_blocks * v.f_frsize, "free_bytes": v.f_bavail * v.f_frsize}
    return json.dumps(out) + "\n"


def tmux_check() -> int:
    """Create the tmux session afc-check, see it, kill it: what the host check needs to know. A leftover
    session of that name (an interrupted earlier check) is killed first. Output: one line, `ok`."""
    name = "afc-check"
    root = cfg().roots[0]

    def tmux(*a: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(["tmux", *a], env=ENV, cwd=root, capture_output=True, text=True, timeout=CHECK_TIMEOUT)

    if tmux("has-session", "-t", "=" + name).returncode == 0:
        log("tmux-check: killing a leftover afc-check")
        tmux("kill-session", "-t", "=" + name)
    made = tmux("new-session", "-d", "-s", name, "sleep 30")
    seen = made.returncode == 0 and tmux("has-session", "-t", "=" + name).returncode == 0
    gone = tmux("kill-session", "-t", "=" + name).returncode == 0 if made.returncode == 0 else False
    if not (seen and gone):
        log(f"tmux-check FAILED: new-session rc={made.returncode} {made.stderr.strip()[:200]!r} seen={seen} killed={gone}")
        print(f"host_exec: tmux-check failed (created {made.returncode == 0}, seen {seen}, killed {gone}): "
              f"{made.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    log("OK tmux-check")
    sys.stdout.write("ok\n")
    return 0


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
    elif head == "claude" and args in (["auth", "status"], ["doctor"]):
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
    elif head == "transcripts":
        emit(transcripts_json(args))
        log(f"OK transcripts {args[0]}")
        return 0
    elif head == "sessions":
        emit(sessions_json(args))
        log(f"OK sessions {args[0]}")
        return 0
    elif head == "doc-files":
        emit(doc_files_json(args))
        log(f"OK doc-files {' '.join(args[2:])}")
        return 0
    elif head == "df" and not args:
        emit(df_json())
        log("OK df")
        return 0
    elif head == "clock" and not args:
        emit(json.dumps({"epoch_ms": int(time.time() * 1000)}) + "\n")
        return 0
    elif head == "tmux-check" and not args:
        return tmux_check()
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
    limit = RC_HOST_TIMEOUT if head == "rc-send" else CHECK_TIMEOUT if args in (["auth", "status"], ["doctor"]) else None
    try:
        return subprocess.run(cmd, input=stdin, env=ENV, cwd=run_cwd, timeout=limit).returncode
    except subprocess.TimeoutExpired:      # run() has killed the child
        log(f"TIMEOUT {head} after {limit} s")
        print(f"host_exec: {head} stopped after {limit} s", file=sys.stderr)
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
