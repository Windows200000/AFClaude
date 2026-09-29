#!/usr/bin/env bash
# Launches the AFK keep-alive build session (scheduled via `at` for 00:00 Europe/Berlin).
# Scrubs the scheduling session's Claude env vars (CLAUDECODE, CLAUDE_CODE_*, sockets)
# with env -i so the new session starts as a clean top-level session.
set -uo pipefail
DIR=/mnt/BlockVolume/Claude/work/AFClaude
LOG="$DIR/launch.log"
PROMPT="$(cat "$DIR/nightly_prompt.md")"

{
  echo "[$(TZ=Europe/Berlin date '+%F %T %Z')] launching"
  cd /mnt/BlockVolume/Claude || exit 1
  env -i \
    HOME=/home/opc USER=opc LOGNAME=opc SHELL=/bin/bash \
    PATH=/home/opc/.local/bin:/home/opc/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin \
    LANG="${LANG:-C.UTF-8}" TERM=xterm-256color \
    XDG_RUNTIME_DIR=/run/user/1000 \
    CLAUDE_GUARD_DISABLE=1 \
    claude --bg --remote-control --name "task-manager keep-alive build" \
      --model claude-opus-5-5 --effort high --permission-mode auto \
      "$PROMPT"
  echo "[$(TZ=Europe/Berlin date '+%F %T %Z')] exit=$?"
} >>"$LOG" 2>&1
