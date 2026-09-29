#!/bin/sh
# Container counterpart of start_keepalive.sh: start the keep-alive watcher detached,
# unless one is already running (then a silent no-op, so cron can call it as a watchdog).
#   start_watcher.sh [<FULL-session-uuid>]    (default: $AFCLAUDE_MANAGER_SESSION), always --arm
# Stop it: touch data/keepalive/STOP (it exits within 30 s; the next start removes STOP).
# Keep it stopped: touch PAUSED in the host repo (checked by cron and the entrypoint).
set -eu
D="${AFCLAUDE_DIR:?}"; S="${KEEPALIVE_STATE_DIR:?}"
SID="${1:-${AFCLAUDE_MANAGER_SESSION:?}}"
if pgrep -f "^python3 -u [^ ]*keepalive.py --session" >/dev/null; then exit 0; fi
# Never a second watcher next to one still running on the HOST (their locks differ):
# the host's process list comes over the bridge; if it can't be read, don't start either.
cd "$D"
if ! python3 - <<'PY'
import sys
import host
try:
    snap = host.proc_snapshot()
except Exception as e:  # noqa: BLE001
    sys.exit(f"start_watcher: host process list unavailable ({e}); not starting")
if any(p["argv"] and "--session" in p["argv"] and any(a.endswith("keepalive.py") for a in p["argv"][:3])
       for p in snap.values()):
    sys.exit("start_watcher: a keepalive.py watcher runs on the HOST; not starting a second one")
PY
then exit 0; fi
mkdir -p "$S"; rm -f "$S/STOP"
setsid nohup python3 -u "$D/keepalive.py" --session "$SID" --arm >>"$S/keepalive.log" 2>&1 </dev/null &
