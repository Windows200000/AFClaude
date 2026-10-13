#!/bin/sh
# Entrypoint of the AFClaude manager container (PID 1 is tini).
#   AFCLAUDE_SCHEDULER=on   supercronic runs docker/crontab and the keep-alive watcher starts
#   AFCLAUDE_SCHEDULER=off  (default) nothing is scheduled; the container only idles, so
#                           `docker exec` works (tests, the MCP server, manual runs)
set -eu
D="${AFCLAUDE_DIR:?}"
# Strict host key checking against the host's own public host key (mounted read-only).
KH="${AFCLAUDE_BRIDGE_KNOWN_HOSTS:-/tmp/afclaude_known_hosts}"
printf 'afclaude-host %s\n' "$(cut -d' ' -f1,2 /run/host_ssh/ssh_host_ed25519_key.pub)" >"$KH"
mkdir -p "${KEEPALIVE_STATE_DIR:?}" "$D/data/at_spool"
# D-227: every deploy (`docker compose up -d --build`, i.e. every container start) imports
# prompts/*.md into prompt_defaults, so the DB always has what is currently deployed (prompts
# still ship as files, D-169; prompt_overrides holds the owner's edits). Now that data/ is
# mounted, the DB is reachable if it exists at all; a first install has none yet (store.py init
# is the explicit, separate first-install step), and any other DB problem is store.py's own
# DB error path (logged + alerted via the fallback file). Either way this must never fail the
# container start, so its exit status is never allowed to trip `set -e`.
python3 "$D/store.py" import-prompts >>"$D/data/import_prompts.log" 2>&1 \
  || echo "afclaude: prompt defaults import did not complete this start (see data/import_prompts.log)" >&2
case "${AFCLAUDE_SCHEDULER:-off}" in
  on)
    echo "afclaude: scheduler ON ($(date -u +%FT%TZ))"
    # The watcher starts right away (the host crontab's "@reboot ... start_keepalive.sh" line), not
    # only at the next :07/:22/:37/:52 watchdog: retried until it runs (the bridge may need a
    # moment after a rebuild), never while PAUSED exists, never a second one (start_watcher.sh).
    "$D/docker/start_watcher.sh" --boot &
    exec /usr/local/bin/supercronic -passthrough-logs "${AFCLAUDE_CRONTAB:-$D/docker/crontab}"
    ;;
  off)
    echo "afclaude: scheduler OFF ($(date -u +%FT%TZ)); idling"
    exec sleep infinity
    ;;
  *) echo "AFCLAUDE_SCHEDULER must be on or off, not ${AFCLAUDE_SCHEDULER}" >&2; exit 2 ;;
esac
