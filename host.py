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
  (NAME, NEW: a tmux session name, ka-<8 hex> or afc-<[a-z0-9-], at most 36>, names.TMUX_RE)
  os.kill(pid, SIGTERM)                   -> kill-claude <pid>   (claude pids only)
  /proc/<pid>/environ                     -> claude-env <pid>    (CLAUDE* lines, claude pids only)
  /proc/<pid>/{stat,cmdline} (preflight)  -> proc-snapshot       (ppid + start of every pid, argv of
                                                                  claude / keepalive.py pids only)
  ~/.claude.json cachedUsageUtilization   -> usage-cache
  append to PROGRESS.md / ALERTS.md       -> append-note PROGRESS.md|ALERTS.md (text on stdin)
  rc_send(NAME, text): the Haiku sender     -> rc-send NAME (text on stdin; host_exec.py builds the
                                               same `claude -p` + prompt as rc_send_argv/_prompt here)

Anything else raises ValueError instead of being sent: it would be denied anyway.
Settings (env, set by docker/compose.yml): AFCLAUDE_BRIDGE_HOST, AFCLAUDE_BRIDGE_USER,
AFCLAUDE_BRIDGE_KEY, AFCLAUDE_BRIDGE_KNOWN_HOSTS.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import time

# names.TMUX_RE with the target's "=" (exact match) and capture-pane's ":" (the current pane)
TMUX_NAME = re.compile(r"^=?(ka-[0-9a-f]{8}|afc-[a-z0-9-]{1,36})\Z")
TMUX_PANE = re.compile(r"^=(ka-[0-9a-f]{8}|afc-[a-z0-9-]{1,36}):\Z")
TMUX_NEW = re.compile(r"^(ka-[0-9a-f]{8}|afc-[a-z0-9-]{1,36})\Z")   # rename-session's new name
NOTE_FILES = ("PROGRESS.md", "ALERTS.md")
# rc_send (docs/rc_server_mode.md §4.1, §8; D-288). Kept identical in docker/host_exec.py (it runs
# alone as the SSH forced command; test_host.py compares them).
RC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")   # a thread's registry `name` (dir + 2 hex)
RC_TEXT_MAX = 16 * 1024                                          # bytes of UTF-8 message text
RC_SEND_FLAGS = ["-p", "--model", "haiku", "--permission-mode", "dontAsk",   # D-047: Haiku in dontAsk
                 "--allowedTools", "SendMessage", "ListAgents", "--strict-mcp-config",
                 "--disable-slash-commands", "--output-format", "stream-json", "--verbose"]
RC_SEND_PROMPT = ("Call the SendMessage tool exactly once, to the agent named exactly {name!r}, with the "
                  "message between the BEGIN and END lines below as its text, unchanged. If that agent is "
                  "not reachable, do not try any other name or agent and do not retry. Do nothing else. "
                  "Afterwards reply with the tool's result word for word.\nBEGIN\n{text}\nEND")
SNAPSHOT_TTL = 2.0          # seconds; one preflight reads many pids, a take-over loop polls
_snapshot = {"at": 0.0, "data": None}


def in_container():
    return os.environ.get("AFCLAUDE_IN_CONTAINER", "").strip() not in ("", "0")


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


def bridge_argv(argv):
    """Map a host command onto host_exec.py's whitelisted shape, or raise ValueError."""
    argv = [str(a) for a in argv]
    if not argv:
        raise ValueError("empty command")
    head, rest = os.path.basename(argv[0]), argv[1:]
    if head == "claude":
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
    raise ValueError(f"no host_exec.py shape for {shlex.join(argv)[:200]!r}")


def run_on_host(argv, input=None, cwd=None, env=None, timeout=None):
    """subprocess.run(argv, capture_output=True, text=True) on the host.
    Returns a CompletedProcess (stdout/stderr are str). In the container `cwd` and
    `env` are ignored: host_exec.py runs every command with its own fixed env/cwd."""
    if not in_container():
        return subprocess.run(argv, input=input, cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout)
    remote = bridge_argv(argv)
    r = subprocess.run(ssh_argv(remote), input=input, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL if input is None else None)
    return subprocess.CompletedProcess(argv, r.returncode, r.stdout, r.stderr)


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
    data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")   # host_exec.py's cwd for -p
    return subprocess.run(["claude"] + RC_SEND_FLAGS, input=rc_send_prompt(name, text), cwd=data,
                          capture_output=True, text=True, timeout=timeout)
