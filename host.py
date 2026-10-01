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
  tmux has-session -t =ka-xxxxxxxx        -> tmux-has ka-xxxxxxxx
  tmux list-panes -s -t =ka-xxxxxxxx -F #{pane_pid} -> tmux-panes ka-xxxxxxxx
  tmux kill-session -t =ka-xxxxxxxx       -> tmux-kill ka-xxxxxxxx
  os.kill(pid, SIGTERM)                   -> kill-claude <pid>   (claude pids only)
  /proc/<pid>/environ                     -> claude-env <pid>    (CLAUDE* lines, claude pids only)
  /proc/<pid>/{stat,cmdline} (preflight)  -> proc-snapshot       (ppid + start of every pid, argv of
                                                                  claude / keepalive.py pids only)
  ~/.claude.json cachedUsageUtilization   -> usage-cache
  append to PROGRESS.md / ALERTS.md       -> append-note PROGRESS.md|ALERTS.md (text on stdin)

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

TMUX_NAME = re.compile(r"^=?(ka-[0-9a-f]{8})$")
NOTE_FILES = ("PROGRESS.md", "ALERTS.md")
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
                  "tmux-panes": (["list-panes", "-s", "-t"], ["-F", "#{pane_pid}"])}
        for shape, (pre, post) in shapes.items():
            n = len(pre)
            if len(rest) == n + 1 + len(post) and rest[:n] == pre and rest[n + 1:] == post:
                m = TMUX_NAME.match(rest[n])
                if m:
                    return [shape, m.group(1)]
    if head in ("kill-claude", "claude-env") and len(rest) == 1 and rest[0].isdigit():
        return argv
    if head in ("proc-snapshot", "usage-cache") and not rest:
        return argv
    if head == "append-note" and len(rest) == 1 and rest[0] in NOTE_FILES:
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
