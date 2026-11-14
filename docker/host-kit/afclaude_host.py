#!/usr/bin/env python3
"""The `afclaude` host wrapper (phase 5b K5, docs/phase5b_plan.md §3.3): every AFClaude command on the host
(the MCP stdio server a Claude session starts, `afclaude tasks ...`, the session hooks) runs inside the
`afclaude-mcp` container through

    docker exec -i [-e NAME ...] afclaude-mcp afclaude <the arguments unchanged>

and the wrapper decides which environment goes in with it. Only the identity inputs of identity.py are passed
(`CLAUDE_CODE_SESSION_ID`, `CLAUDECODE`, `AFCLAUDE_AGENT_ID`, `CLAUDE_GUARD_DISABLE`, the project directory) plus
two the wrapper itself makes: `AFCLAUDE_MACHINE_TOKEN`, read from the machine's token file (mode 0600, owned by
the caller, JSON with a "token" key: `afclaude machine register` writes it; $AFCLAUDE_MACHINE_FILE or
~/.config/afclaude/machine.json), and `AFCLAUDE_MACHINE_HOST`, this host's name (inside the container
socket.gethostname() is the container's, which would trip the two-hostname alert, D-198). Values travel as
`-e NAME` (docker copies them from this process), so no token is in any argv or Claude config.

It refuses (exit 2, a message, nothing run: D-235) rather than passes: any `AFCLAUDE_*` variable in the caller's
environment other than AFCLAUDE_AGENT_ID and AFCLAUDE_MACHINE_FILE (an `AFCLAUDE_DB` or a token in the env would
otherwise be dropped silently while the caller believes it took effect; the token has one source, the file), and
a token file that is not a regular file of this user with mode 0600 or holds no token. No token file is the
host's own session (D-330): nothing is passed for it.

Part of the host kit (§3.2), source file afclaude_host.py (it is installed under the name `afclaude`; the repo has an afclaude.py of its own); stdlib only, Python 3.9 compatible (the host's python3). Not installed by the
kit yet: K5's deploy adds it to the install, the repo's settings and the hook scripts.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
from typing import Dict, List, Mapping, Optional, Tuple

CONTAINER = "afclaude-mcp"
PASS = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "AFCLAUDE_AGENT_ID", "CLAUDE_GUARD_DISABLE", "CLAUDE_PROJECT_DIR")
LOCAL = ("AFCLAUDE_MACHINE_FILE",)                    # read by the wrapper, never passed on
TOKEN_VAR, HOST_VAR, FILE_VAR = "AFCLAUDE_MACHINE_TOKEN", "AFCLAUDE_MACHINE_HOST", "AFCLAUDE_MACHINE_FILE"
DEFAULT_TOKEN_FILE = "~/.config/afclaude/machine.json"
EXIT_REFUSED = 2


class Refused(Exception):
    """The wrapper will not run the command (the message is for the caller; D-235: loud, nothing passed on)."""


def check_env(env: Mapping[str, str]) -> None:
    """Refuse an AFCLAUDE_* variable the wrapper does not pass on."""
    bad = sorted(k for k in env if k.startswith("AFCLAUDE_") and k not in PASS and k not in LOCAL)
    if bad:
        raise Refused("afclaude: refusing to run with %s in the environment: only %s are passed into the container "
                      "(the machine token comes from the token file, never from the environment)"
                      % (", ".join(bad), ", ".join(n for n in PASS if n.startswith("AFCLAUDE_"))))


def read_token(env: Mapping[str, str]) -> Optional[str]:
    """The token of the machine's token file, None without a file; a file that is not 0600 and the caller's, or
    has no token, is refused."""
    path = env.get(FILE_VAR) or os.path.expanduser(DEFAULT_TOKEN_FILE)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise Refused("afclaude: the machine token file %s is not a regular file of this user with mode 0600: fix it "
                      "(chmod 600) or re-register the machine" % path)
    with open(path) as fh:
        try:
            doc = json.load(fh)
        except ValueError as e:
            raise Refused("afclaude: the machine token file %s is not JSON: %s" % (path, e))
    token = doc.get("token") if isinstance(doc, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise Refused("afclaude: the machine token file %s holds no token" % path)
    return token.strip()


def build(argv: List[str], env: Mapping[str, str], cwd: str, hostname: str, docker: str = "docker"
          ) -> Tuple[List[str], Dict[str, str]]:
    """-> (the docker command, the environment of the docker client process). Raises Refused."""
    check_env(env)
    child: Dict[str, str] = dict(env)                        # the docker client needs PATH, HOME, DOCKER_*
    for name in (TOKEN_VAR, HOST_VAR):
        child.pop(name, None)
    passed: List[str] = [n for n in PASS if env.get(n)]
    if "CLAUDE_PROJECT_DIR" not in passed and os.path.isabs(cwd):
        child["CLAUDE_PROJECT_DIR"] = cwd                    # the container's own cwd says nothing about the caller's
        passed.append("CLAUDE_PROJECT_DIR")
    token = read_token(env)
    if token is not None:
        child[TOKEN_VAR] = token
        child[HOST_VAR] = hostname
        passed += [TOKEN_VAR, HOST_VAR]
    cmd = [docker, "exec", "-i"]
    for name in passed:
        cmd += ["-e", name]
    return cmd + [CONTAINER, "afclaude"] + list(argv), child


def main(argv: List[str]) -> int:
    docker = shutil.which("docker")
    try:
        if docker is None:
            raise Refused("afclaude: docker is not on PATH")
        cmd, child = build(argv, os.environ, os.getcwd(), socket.gethostname(), docker)
    except Refused as e:
        print(e, file=sys.stderr)
        return EXIT_REFUSED
    sys.stdout.flush()
    os.execve(cmd[0], cmd, child)                            # the container's process owns stdin/stdout/exit code now
    return 0                                                 # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
