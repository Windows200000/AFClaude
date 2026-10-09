#!/bin/sh
# Container counterpart of start_keepalive.sh: start the keep-alive watcher detached,
# unless one is already running (then a silent no-op, so cron can call it as a watchdog).
#   start_watcher.sh [<FULL-session-uuid>]    (default: $AFCLAUDE_MANAGER_SESSION), always --arm
#   start_watcher.sh --boot [<uuid>]          the entrypoint's start at container start: tries now
#       and again every AFCLAUDE_BOOT_RETRY s (15) until a watcher runs, PAUSED appears or
#       AFCLAUDE_BOOT_TRIES (60, ~15 min; by then the :07/:22/:37/:52 watchdog has taken over)
#       ran out (the host bridge may need a moment after a rebuild)
# Stop it: touch data/keepalive/STOP (it exits within 30 s; the next start removes STOP).
# Keep it stopped: touch PAUSED in the host repo (checked here, by cron and by --boot).
# Never two: a running watcher (pgrep) or one on the HOST makes this a no-op, concurrent starts
# (the entrypoint's and the watchdog's) are serialized by a lock file, and keepalive.py's own
# lock makes any second instance exit.
set -eu
D="${AFCLAUDE_DIR:?}"; S="${KEEPALIVE_STATE_DIR:?}"
WATCHER='^python3 -u [^ ]*keepalive.py --session'
if [ "${1:-}" = "--boot" ]; then
  shift
  i=0
  while [ "$i" -lt "${AFCLAUDE_BOOT_TRIES:-60}" ]; do
    [ -e "$D/PAUSED" ] && exit 0
    "$0" "$@" || true
    sleep "${AFCLAUDE_BOOT_SETTLE:-2}"
    if pgrep -f "$WATCHER" >/dev/null; then exit 0; fi
    i=$((i + 1))
    sleep "${AFCLAUDE_BOOT_RETRY:-15}"
  done
  echo "start_watcher --boot: no watcher after ${AFCLAUDE_BOOT_TRIES:-60} tries; the watchdog cron keeps trying" >&2
  exit 0
fi
SID="${1:-${AFCLAUDE_MANAGER_SESSION:?}}"
[ -e "$D/PAUSED" ] && exit 0
mkdir -p "$S"
exec 9>"$S/.start_watcher.lock"
flock -n 9 || exit 0                 # another start is under way
if pgrep -f "$WATCHER" >/dev/null; then exit 0; fi
# Never a second watcher next to one still running on the HOST (their locks differ):
# the host's process list comes over the bridge; if it can't be read, don't start either.
# A watcher is `keepalive.py --session <uuid>` without a one-shot flag: the */30 --window-start
# run, --plan-fillup-test or --decide (also this container's own, visible on the host) are none.
cd "$D"
if ! python3 - <<'PY'
import sys
import host
ONE_SHOT = {"--window-start", "--plan-fillup-test", "--decide", "--last-mile", "--once", "--work-on"}
try:
    snap = host.proc_snapshot()
except Exception as e:  # noqa: BLE001
    sys.exit(f"start_watcher: host process list unavailable ({e}); not starting")
if any(p["argv"] and "--session" in p["argv"] and any(a.endswith("keepalive.py") for a in p["argv"][:3])
       and not ONE_SHOT & set(p["argv"]) for p in snap.values()):
    sys.exit("start_watcher: a keepalive.py watcher runs on the HOST; not starting a second one")
PY
then exit 0; fi
rm -f "$S/STOP"
setsid nohup python3 -u "$D/keepalive.py" --session "$SID" --arm >>"$S/keepalive.log" 2>&1 </dev/null 9>&- &
