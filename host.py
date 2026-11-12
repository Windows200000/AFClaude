#!/usr/bin/env python3
"""Run things on the HOST, wherever AFClaude itself runs.

On the host (AFCLAUDE_IN_CONTAINER unset or empty/0) every function here is the
plain local call it replaces: subprocess.run, os.kill, reading /proc, appending
to a file. Host behaviour is unchanged.

Inside the manager container (docker/, AFCLAUDE_IN_CONTAINER=1) the same calls go
over SSH to the host, where docker/host_exec.py (an SSH forced command installed
by the user, docker/install_host_bridge.sh) checks each one against a strict
whitelist. So every host call is mapped onto one of those shapes here:

  claude ...                              -> claude ...        (whitelisted forms only)
  <repo>/ka_resume.sh ...                 -> ka_resume.sh ...
  tmux has-session -t =NAME               -> tmux-has NAME
  tmux list-panes -s -t =NAME -F #{pane_pid} -> tmux-panes NAME
  tmux kill-session -t =NAME              -> tmux-kill NAME
  tmux list-clients -t =NAME -F #{client_activity} -> tmux-clients NAME
  tmux capture-pane -e -p -t =NAME:       -> tmux-capture NAME   (the compaction's owner-typing check)
  tmux rename-session -t =NAME NEW        -> tmux-rename NAME NEW   (the one-time rename, D-281)
  (NAME, NEW: a tmux session name, afc-<[a-z0-9-], at most 36>, names.TMUX_RE)
  os.kill(pid, SIGTERM)                   -> kill-claude <pid>   (claude pids only)
  /proc/<pid>/environ                     -> claude-env <pid>    (CLAUDE* lines, claude pids only)
  /proc/<pid>/{stat,cmdline} (preflight)  -> proc-snapshot       (ppid + start of every pid, argv of
                                                                  claude / keepalive.py pids only)
  ~/.claude.json cachedUsageUtilization   -> usage-cache
  append to PROGRESS.md / ALERTS.md       -> append-note PROGRESS.md|ALERTS.md (text on stdin)
  rc_send(NAME, text): the Haiku sender     -> rc-send NAME (text on stdin; host_exec.py builds the
                                               same `claude -p` + prompt as rc_send_argv/_prompt here)
  kit_info() / check_kit() / check_host() -> kit-version          (the installed kit's version, whitelist hash,
                                                                  command list and file hashes; check_kit compares
                                                                  the version with HOST_KIT_VERSION, check_host the
                                                                  files and the repo path too: HostKitError)
  host-config get                         (the host kit's host.json, read-only: only the owner's install writes
                                           it, D-338)
  transcripts ID --changed-since TS [--head N] [--tail N] | sessions ID | doc-files read DIR NAME...
                                          (the container's reads of the host's transcripts, the session registry
                                           and the project docs; kit version 2)
  claude auth status | claude doctor | tmux-check | df | clock       (the host checks; kit version 2)

Anything else raises ValueError instead of being sent: it would be denied anyway.
Settings (env, set by docker/compose.yml): AFCLAUDE_BRIDGE_HOST, AFCLAUDE_BRIDGE_USER,
AFCLAUDE_BRIDGE_KEY, AFCLAUDE_BRIDGE_KNOWN_HOSTS.

The code runs from /app in the image, so a path the HOST must understand (the project of "this
checkout", the cwd of a launch, a path in a message to a session) is never the code's own directory:
repo_dir() is the host path of the repo (the container gets it as AFCLAUDE_HOST_REPO from
docker/.env; it never calls the bridge: check_host() compares it with the kit's host.json `repo`, in the
host check, not at import); local_path() / to_local() map it back to the code directory for the places
that read a file or run a stored command inside the container.
"""
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

# names.TMUX_RE with the target's "=" (exact match) and capture-pane's ":" (the current pane)
TMUX_NAME = re.compile(r"^=?(afc-[a-z0-9-]{1,36})\Z")
TMUX_PANE = re.compile(r"^=(afc-[a-z0-9-]{1,36}):\Z")
TMUX_NEW = re.compile(r"^(afc-[a-z0-9-]{1,36})\Z")   # rename-session's new name
NOTE_FILES = ("PROGRESS.md", "ALERTS.md")
CONFIG_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")   # a Claude config dir's id (host_exec.py CONFIG_ID)
DOC_NAMES = ("PROGRESS.md", "GOALS.md", "EXCEPTIONS.md", "DECISIONS.md")   # doc-files read (host_exec.py DOC_NAMES)
# The bridge of before the host kit (docker/host_exec.py, still the owner's authorized_keys line until the kit
# is installed): these heads are what it knows. Everything else (and `claude auth|doctor`) needs the kit.
LEGACY_HEADS = frozenset(("ka_resume.sh", "claude", "kill-claude", "tmux-has", "claude-env", "tmux-panes",
                          "tmux-kill", "tmux-clients", "tmux-capture", "tmux-rename", "proc-snapshot",
                          "usage-cache", "append-note", "rc-send"))
KIT_FILES = (("host_exec.py", os.path.join("docker", "host-kit", "host_exec.py")), ("ka_resume.sh", "ka_resume.sh"))
                            # the kit files `kit-version` hashes (host_exec.py's KIT_FILES) and where this code has them
# rc_send (docs/rc_server_mode.md §4.1, §8; D-288). Kept identical in docker/host_exec.py (it runs
# alone as the SSH forced command; test_host.py compares them).
RC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")   # a thread's registry `name` (dir + 2 hex)
RC_TEXT_MAX = 16 * 1024                                          # bytes of UTF-8 message text
RC_SEND_FLAGS = ["-p", "--model", "haiku", "--permission-mode", "dontAsk",   # D-047: Haiku in dontAsk
                 "--tools=SendMessage", "--allowedTools=SendMessage", "--strict-mcp-config",   # only that tool exists
                 "--disable-slash-commands", "--output-format", "stream-json", "--verbose"]
RC_HOST_TIMEOUT = 100       # seconds the host side lets the sender run: below rc_continue.SEND_TIMEOUT (the ssh
                            # call's), so it can't deliver after the dispatcher recorded the failure
RC_SEND_PROMPT = ("Call the SendMessage tool exactly once, to the agent named exactly {name!r}, with the "
                  "message between the BEGIN and END lines below as its text, unchanged. If that agent is "
                  "not reachable, do not try any other name or agent and do not retry. Do nothing else. "
                  "Afterwards reply with the tool's result word for word.\nBEGIN\n{text}\nEND")
# The host kit this code was written for (docker/host_exec.py's HOST_KIT_VERSION; test_host_kit.py compares
# them): a kit with another version is the first host check's fail, "re-run the host kit install".
HOST_KIT_VERSION = "3"
HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_TTL = 2.0          # seconds; one preflight reads many pids, a take-over loop polls
_snapshot = {"at": 0.0, "data": None}


def in_container():
    return os.environ.get("AFCLAUDE_IN_CONTAINER", "").strip() not in ("", "0")


class HostRepoError(RuntimeError):
    """In a container, the host path of the repo is missing or not an absolute path (D-235)."""


def repo_dir() -> str:
    """The host path of the AFClaude repo (what projects.path holds for this checkout). On the host it
    is the code's own directory. In the container (code at /app) it is AFCLAUDE_HOST_REPO (docker/.env,
    an absolute path; unset or relative raises HostRepoError: no guess, no fallback to /app, D-235). No
    bridge call here, so a bridge outage never breaks work that only reads the DB: check_host() compares
    this value with the installed kit's host.json `repo` and raises on a mismatch."""
    if not in_container():
        return HERE
    v = os.environ.get("AFCLAUDE_HOST_REPO", "").strip()
    if not v.startswith("/"):
        raise HostRepoError("AFCLAUDE_HOST_REPO (the host path of the repo, docker/.env) is not set to an "
                            f"absolute path: {v!r}")
    return os.path.normpath(v)


def local_path(path: str) -> str:
    """The path inside this process for a host path: in a container a path at or under repo_dir()
    becomes the same path under the code directory (the repo's local-only files are linked there);
    anything else, and everything on the host, is returned unchanged."""
    if not in_container() or not path:
        return path
    root = repo_dir()
    if root == HERE:
        return path
    p = os.path.normpath(path)
    if p == root or p.startswith(root + os.sep):
        return HERE + p[len(root):]
    return path


def headless_cwd() -> str:
    """The cwd of a one-shot `claude -p` run directly on the host (usage, Haiku, rc-send): a
    bridge-owned 0700 directory under ~/.local/state/afclaude, never <repo>/data, which the
    container writes: a .claude/settings.json (hooks) there would run on the host. docker/host_exec.py
    uses the same directory for the calls that come over the bridge (and ignores a cwd sent along);
    in the container the value is only a placeholder. AFCLAUDE_HEADLESS_CWD overrides it (tests)."""
    path = os.environ.get("AFCLAUDE_HEADLESS_CWD") or os.path.join(os.path.expanduser("~"), ".local", "state",
                                                                    "afclaude", "cwd")
    if not in_container():
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def to_local(text: str) -> str:
    """`text` (a stored command line, a cwd) with every occurrence of the host repo path replaced
    by the code directory: at-jobs scheduled before the code moved to /app still name the host
    path. Only whole path prefixes are replaced (AFClaude-wt-x is not AFClaude)."""
    if not text or not in_container() or repo_dir() == HERE:
        return text
    return re.sub(re.escape(repo_dir()) + r"(?![\w.-])", lambda m: HERE, text)


def ssh_argv(remote):
    """The ssh command that asks the host's host_exec.py to run `remote` (an argv list).
    sshd hands the joined string to the forced command as $SSH_ORIGINAL_COMMAND,
    which host_exec.py splits again with shlex (never a shell)."""
    e = os.environ.get
    return ["ssh", "-T", "-i", e("AFCLAUDE_BRIDGE_KEY", "/run/secrets/host_bridge_ed25519"),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + e("AFCLAUDE_BRIDGE_KNOWN_HOSTS", os.path.expanduser("~/.ssh/known_hosts")),
            "-o", "HostKeyAlias=afclaude-host", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
            "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/afclaude-ssh-%C", "-o", "ControlPersist=60",
            f"{e('AFCLAUDE_BRIDGE_USER', 'opc')}@{e('AFCLAUDE_BRIDGE_HOST', 'host.docker.internal')}",
            "--", shlex.join(remote)]


def _is_number(v):
    try:
        float(v)
    except ValueError:
        return False
    return True


def _tx_opts(rest):
    """`--head N` / `--tail N` pairs (host_exec.py's transcripts), each at most once."""
    seen = set()
    while rest:
        if len(rest) < 2 or rest[0] not in ("--head", "--tail") or rest[0] in seen or not rest[1].isdigit():
            return False
        seen.add(rest[0])
        rest = rest[2:]
    return True


def bridge_argv(argv):
    """Map a host command onto host_exec.py's whitelisted shape, or raise ValueError."""
    argv = [str(a) for a in argv]
    if not argv:
        raise ValueError("empty command")
    head, rest = os.path.basename(argv[0]), argv[1:]
    if head == "claude":      # claude auth status / claude doctor are whitelisted by the kit, not here
        return ["claude"] + rest
    if head == "ka_resume.sh":
        return ["ka_resume.sh"] + rest
    if head == "tmux":
        shapes = {"tmux-has": (["has-session", "-t"], []), "tmux-kill": (["kill-session", "-t"], []),
                  "tmux-panes": (["list-panes", "-s", "-t"], ["-F", "#{pane_pid}"]),
                  "tmux-clients": (["list-clients", "-t"], ["-F", "#{client_activity}"]),
                  "tmux-capture": (["capture-pane", "-e", "-p", "-t"], [])}
        for shape, (pre, post) in shapes.items():
            n = len(pre)
            if len(rest) == n + 1 + len(post) and rest[:n] == pre and rest[n + 1:] == post:
                m = (TMUX_PANE if shape == "tmux-capture" else TMUX_NAME).match(rest[n])
                if m:
                    return [shape, m.group(1)]
        if len(rest) == 4 and rest[:2] == ["rename-session", "-t"]:
            m, new_name = TMUX_NAME.match(rest[2]), TMUX_NEW.match(rest[3])
            if m and new_name and rest[2].startswith("="):
                return ["tmux-rename", m.group(1), rest[3]]
    if head in ("kill-claude", "claude-env") and len(rest) == 1 and rest[0].isdigit():
        return argv
    if head in ("proc-snapshot", "usage-cache") and not rest:
        return argv
    if head == "append-note" and len(rest) == 1 and rest[0] in NOTE_FILES:
        return argv
    if head == "rc-send" and len(rest) == 1 and RC_NAME.match(rest[0]):
        return argv
    if head == "kit-version" and not rest:
        return argv
    if head == "host-config" and rest == ["get"]:      # there is no `set`: host.json is the owner's (D-338)
        return argv
    if head in ("tmux-check", "df", "clock") and not rest:
        return argv
    if head == "sessions" and len(rest) == 1 and CONFIG_ID.match(rest[0]):
        return argv
    if head == "transcripts" and len(rest) >= 3 and CONFIG_ID.match(rest[0]) and rest[1] == "--changed-since" \
            and _is_number(rest[2]) and _tx_opts(rest[3:]):
        return argv
    if head == "doc-files" and len(rest) >= 3 and rest[0] == "read" and rest[1].startswith("/") \
            and set(rest[2:]) <= set(DOC_NAMES) and len(set(rest[2:])) == len(rest[2:]):
        return argv
    raise ValueError(f"no host_exec.py shape for {shlex.join(argv)[:200]!r}")


def run_on_host(argv, input=None, cwd=None, env=None, timeout=None):
    """subprocess.run(argv, capture_output=True, text=True) on the host.
    Returns a CompletedProcess (stdout/stderr are str). In the container `cwd` and
    `env` are ignored: host_exec.py runs every command with its own fixed env/cwd. On the host a
    one-shot `claude -p` gets `--setting-sources user` like the bridge's (docker/host_exec.py): no
    project or local .claude/settings.json (hooks) of its cwd is ever loaded."""
    if not in_container():
        if os.path.basename(argv[0]) == "claude" and "-p" in argv and "--setting-sources" not in argv:
            argv = [argv[0], "--setting-sources", "user", *argv[1:]]
        return subprocess.run(argv, input=input, cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout)
    remote = bridge_argv(argv)
    r = subprocess.run(ssh_argv(remote), input=input, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL if input is None else None)
    if r.returncode == 126 and "not whitelisted" in r.stderr:
        # the bridge does not know this shape: the pre-kit bridge for a kit-only command, or a kit older than
        # this code. One answer, no extra round trip before the call (D-235)
        raise HostKitError(f"the host bridge does not know {shlex.join(remote)[:80]!r}: it needs the host kit "
                           f"{HOST_KIT_VERSION!r} (run docker/install_host_bridge.sh on the host)")
    return subprocess.CompletedProcess(argv, r.returncode, r.stdout, r.stderr)


class HostKitError(RuntimeError):
    """The installed host kit is not the one this code needs, or does not answer (D-235)."""


def kit_info(timeout=30):
    """The installed kit's `kit-version` answer ({kit_version, whitelist_hash, commands, config_format}) over
    the bridge, or None when the bridge is the one from before the host kit (it denies the command as
    "not whitelisted", rc 126: no kit installed yet, a state the callers report). Raises HostKitError when
    the call fails otherwise (bridge down; a kit whose host.json is missing or malformed refuses it too)
    or the answer is not that JSON."""
    r = subprocess.run(ssh_argv(bridge_argv(["kit-version"])), capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if r.returncode == 126 and "not whitelisted" in r.stderr:
        return None
    if r.returncode != 0:
        raise HostKitError(f"kit-version failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[:300]!r}; "
                           "the bridge is down or host.json is missing: "
                           "re-run the host kit install (docker/install_host_bridge.sh)")
    try:
        info = json.loads(r.stdout)
        if not isinstance(info, dict) or not isinstance(info.get("kit_version"), str) \
                or not isinstance(info.get("commands"), list):
            raise ValueError("no kit_version or commands")
    except ValueError as e:
        raise HostKitError(f"kit-version answered something else: {e}: {r.stdout[:200]!r}")
    return info


def check_kit(timeout=30):
    """kit_info(), or HostKitError when its version is not HOST_KIT_VERSION (naming both). None: no kit
    installed yet (the legacy bridge answers): the first host check reports that, it is not an error here."""
    info = kit_info(timeout)
    if info is not None and info["kit_version"] != HOST_KIT_VERSION:
        raise HostKitError(f"host kit version {info['kit_version']!r}, this code needs {HOST_KIT_VERSION!r}: "
                           "re-run the host kit install (docker/install_host_bridge.sh)")
    return info


def kit_source_hashes():
    """{file name: sha256 hex} of this code's own copies of the kit files (what the install copies)."""
    out = {}
    for name, rel in KIT_FILES:
        with open(os.path.join(HERE, rel), "rb") as fh:
            out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


def host_config(timeout=30):
    """The installed kit's host.json (`host-config get`) as a dict; HostKitError when the call fails or
    does not answer JSON."""
    r = run_on_host(["host-config", "get"], timeout=timeout)
    try:
        if r.returncode != 0:
            raise ValueError(f"rc {r.returncode}: {(r.stderr or r.stdout).strip()[:200]!r}")
        conf = json.loads(r.stdout)
        if not isinstance(conf, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise HostKitError(f"host-config get did not give host.json: {e}")
    return conf


def check_host(timeout=30):
    """The bridge half of the host check (D-164), run by a runner job or the dashboard, never at import or
    per request: kit_info(), and for an installed kit its version (HOST_KIT_VERSION), the sha256 of each kit
    file against this code's own copies (a ka_resume.sh or argument-shape change without a version bump
    shows here) and host.json's `repo` against repo_dir() (AFCLAUDE_HOST_REPO). Any difference raises
    HostKitError: the caller alerts and pauses (D-235, D-257). -> the kit info, or None while no kit is
    installed yet (the pre-kit bridge)."""
    info = check_kit(timeout)
    if info is None:
        return None
    mine = kit_source_hashes()
    theirs = info.get("files")
    if theirs != mine:
        raise HostKitError(f"the installed host kit {info['kit_version']!r} has other files than this code "
                           f"(kit {theirs!r}, here {mine!r}): re-run the host kit install "
                           "(docker/install_host_bridge.sh), and bump HOST_KIT_VERSION if the files changed")
    repo = host_config(timeout).get("repo")
    if not isinstance(repo, str) or os.path.normpath(repo) != repo_dir():
        raise HostKitError(f"host.json's repo is {repo!r} but AFCLAUDE_HOST_REPO is {repo_dir()!r}: fix docker/.env "
                           "or re-run the host kit install with the right checkout")
    return info


# ------------------------------------------------------------------ helpers

def kill_claude(pid):
    """SIGTERM a claude process. On the host a vanished pid raises ProcessLookupError
    (as os.kill does); via the bridge a refused/vanished pid just returns False."""
    if not in_container():
        os.kill(int(pid), signal.SIGTERM)
        return True
    return run_on_host(["kill-claude", str(int(pid))], timeout=30).returncode == 0


def claude_env(pid):
    """The CLAUDE* entries of a claude process's environment (list of 'K=V'), or None."""
    if not in_container():
        try:
            env = open(f"/proc/{int(pid)}/environ", "rb").read().split(b"\0")
        except (OSError, ValueError):
            return None
        return [x.decode(errors="replace") for x in env if x.startswith(b"CLAUDE")]
    try:
        r = run_on_host(["claude-env", str(int(pid))], timeout=30)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else None


def usage_cache():
    """Bridge only: the host's ~/.claude.json cachedUsageUtilization (dict, {} if none).
    A bind mount of ~/.claude.json would go stale: claude replaces that file."""
    r = run_on_host(["usage-cache"], timeout=30)
    if r.returncode != 0:
        raise OSError(f"usage-cache via host bridge failed rc={r.returncode}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout or "{}") or {}


def proc_snapshot(max_age=SNAPSHOT_TTL):
    """Bridge only: {pid: {"ppid": int, "start": str, "argv": [..] or None}} of the host."""
    now = time.monotonic()
    if _snapshot["data"] is None or now - _snapshot["at"] > max_age:
        r = run_on_host(["proc-snapshot"], timeout=30)
        if r.returncode != 0:
            raise OSError(f"proc-snapshot via host bridge failed rc={r.returncode}: {r.stderr.strip()[:200]}")
        raw = json.loads(r.stdout or "{}")
        _snapshot["data"] = {int(k): {"ppid": v[0], "start": str(v[1]), "argv": v[2]} for k, v in raw.items()}
        _snapshot["at"] = now
    return _snapshot["data"]


def proc_info(pid):
    """Bridge only: the proc_snapshot() entry of `pid`, or None if it is not alive."""
    try:
        return proc_snapshot().get(int(pid))
    except (TypeError, ValueError):
        return None


def append_note(path, text):
    """Append `text` to PROGRESS.md / ALERTS.md. In the container the host repo is
    read-only, so the append goes through the bridge (by file name)."""
    if not in_container():
        with open(path, "a") as fh:
            fh.write(text)
        return
    name = os.path.basename(path)
    r = run_on_host(["append-note", name], input=text, timeout=30)
    if r.returncode != 0:
        raise OSError(f"append-note {name} via host bridge failed rc={r.returncode}: {r.stderr.strip()[:200]}")


def rc_send_prompt(name, text):
    """The Haiku sender's prompt: deliver `text` to the rc thread named `name` (rc_send)."""
    return RC_SEND_PROMPT.format(name=name, text=text)


def rc_send(name, text, timeout):
    """One-shot Haiku `claude -p` on the host whose SendMessage delivers `text` into the
    cross-session inbox of the live session named `name` (docs/rc_server_mode.md §4.1: the
    documented sender; the socket protocol is never written by hand). -> CompletedProcess with
    the sender's stream-json on stdout; the caller checks it. In the container this is the
    whitelisted `rc-send NAME` shape (D-164: the container can't reach host sockets). A bad name
    or text raises ValueError; a timeout raises subprocess.TimeoutExpired (D-234)."""
    if not RC_NAME.match(name or ""):
        raise ValueError(f"not an rc thread name: {name!r}")
    if not text or "\0" in text or len(text.encode()) > RC_TEXT_MAX:
        raise ValueError("rc_send text empty, binary or too long")
    if in_container():
        return run_on_host(["rc-send", name], input=text, timeout=timeout)
    return subprocess.run(["claude"] + RC_SEND_FLAGS, input=rc_send_prompt(name, text), cwd=headless_cwd(),
                          capture_output=True, text=True, timeout=timeout)
