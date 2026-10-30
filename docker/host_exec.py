#!/usr/bin/env python3
"""Whitelisted host executor for the AFClaude manager container (user-chosen design,
2026-09-29: "container = manager, host executes"). The USER installs it as an SSH
forced command (see install_host_bridge.sh). sshd runs it for every call, with the
requested command in $SSH_ORIGINAL_COMMAND. It is split with shlex (never a shell,
never eval), checked against the whitelist below, and run directly. Everything else
is rejected. Every call is logged to data/host_exec.log.

Allowed (exact shapes):
  ka_resume.sh --session <uuid> --tmux <name> --message <text> [--cwd <dir under ROOT>] [--name <t>]
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
  (<name>: a tmux session name, ka-<8 hex> or afc-<[a-z0-9-], at most 36>: TMUX below, names.py)
  proc-snapshot            JSON {pid: [ppid, starttime, argv|null]} of every process; argv only
                           for claude and keepalive.py processes (preflight / watcher lookup)
  usage-cache              JSON of ~/.claude.json "cachedUsageUtilization" (fetchedAtMs, utilization only)
  append-note PROGRESS.md|ALERTS.md   append stdin (<= 16 KB UTF-8 text) to that repo file
  rc-send <name>           a one-shot Haiku `claude -p` (RC_SEND_FLAGS: dontAsk, only SendMessage +
                           ListAgents) asked to SendMessage stdin (<= 16 KB UTF-8 text) to the live
                           session named <name> (RC_NAME): the dispatcher's continue of a `claude rc`
                           thread (docs/rc_server_mode.md §4.1); prompt and flags fixed here
"""
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys

ROOT = "/mnt/BlockVolume/Claude"
REPO = os.path.join(ROOT, "work", "AFClaude")
LOG = os.environ.get("AFCLAUDE_HOST_EXEC_LOG") or os.path.join(REPO, "data", "host_exec.log")  # override: tests only (sshd passes no env)
CLAUDE = os.path.expanduser("~/.local/bin/claude")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX8 = re.compile(r"^[0-9a-f]{8}$")
# the tmux session names (names.TMUX_RE; kept here: this file runs alone as the SSH forced command)
TMUX = re.compile(r"^(ka-[0-9a-f]{8}|afc-[a-z0-9-]{1,36})\Z")
ENV = {"HOME": os.path.expanduser("~"), "USER": os.environ.get("USER", "opc"), "LANG": "C.UTF-8",
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
                 "--allowedTools", "SendMessage", "ListAgents", "--strict-mcp-config",
                 "--disable-slash-commands", "--output-format", "stream-json", "--verbose"]
RC_SEND_PROMPT = ("Call the SendMessage tool exactly once, to the agent named exactly {name!r}, with the "
                  "message between the BEGIN and END lines below as its text, unchanged. If that agent is "
                  "not reachable, do not try any other name or agent and do not retry. Do nothing else. "
                  "Afterwards reply with the tool's result word for word.\nBEGIN\n{text}\nEND")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


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
    cwd = os.path.realpath(seen.get("--cwd", ROOT))
    if cwd != ROOT and not cwd.startswith(ROOT + os.sep):
        deny("ka_resume cwd outside ROOT")
    if seen.get("--effort", "high") not in ("low", "medium", "high", "xhigh", "max"):
        deny("effort")
    if not re.match(r"^[\w.\-]+$", seen.get("--model", "claude-opus-5-5")):
        deny("model")


def rc_send_cmd(args, data):
    """rc-send <name> with the message text `data` (stdin bytes) -> (cmd, stdin bytes), or deny."""
    if len(args) != 1 or not RC_NAME.match(args[0]):
        deny("rc-send needs one thread name")
    if not data or len(data) > RC_TEXT_MAX or b"\0" in data:
        deny("rc-send text empty, too long or binary")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        deny("rc-send text not UTF-8")
    log(f"OK rc-send {args[0]} ({len(data)} bytes)")
    return [CLAUDE] + RC_SEND_FLAGS, RC_SEND_PROMPT.format(name=args[0], text=text).encode()


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
        cmd = [CLAUDE] + args
    elif head == "claude" and args[:3] == ["-p", "--model", "haiku"] and set(args) <= HAIKU_FLAGS:
        cmd, stdin = [CLAUDE] + args, sys.stdin.buffer.read(200_000)
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
    cwd = os.path.join(REPO, "data") if (head == "claude" and args[:1] == ["-p"]) or head == "rc-send" else ROOT
    return subprocess.run(cmd, input=stdin, env=ENV, cwd=cwd).returncode


if __name__ == "__main__":
    sys.exit(main())
