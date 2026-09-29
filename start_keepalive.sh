#!/usr/bin/env bash
# Start the keep-alive watcher detached, outside any Claude session.
#   ./start_keepalive.sh <FULL-session-uuid>          # dry-run (decides + logs only)
#   ./start_keepalive.sh <FULL-session-uuid> --arm    # really fires `claude --bg --resume`
# Stop a running watcher:  touch STOP   (it exits within 30s; then rm STOP)
# Log: keepalive.log   State: keepalive_state.json
set -euo pipefail
DIR=/mnt/BlockVolume/Claude/work/AFClaude
SID="${1:?usage: $0 <full-session-uuid> [--arm]}"; shift
cd "$DIR"
if pgrep -f "^python3 -u [^ ]*keepalive.py --session" >/dev/null; then
  echo "a watcher is already running:"; pgrep -af "^python3 -u [^ ]*keepalive.py --session"
  echo "stop it first: touch $DIR/STOP; sleep 35; rm $DIR/STOP"; exit 1
fi
rm -f "$DIR/STOP"
# clean env: no CLAUDECODE / CLAUDE_CODE_* / messaging socket from whoever runs this
env -i HOME="$HOME" USER="$USER" LOGNAME="$USER" SHELL=/bin/bash LANG=C.UTF-8 \
  PATH=/home/opc/.local/bin:/usr/local/bin:/usr/bin:/bin \
  setsid nohup python3 -u "$DIR/keepalive.py" --session "$SID" "$@" >>"$DIR/keepalive.log" 2>&1 </dev/null &
sleep 2
tail -3 "$DIR/keepalive.log"
