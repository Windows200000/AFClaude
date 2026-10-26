#!/bin/sh
# Entrypoint of the AFClaude manager container (PID 1 is tini).
#   AFCLAUDE_SCHEDULER=on      the resident runner (runner.py serve, phase 3a) for the jobs it owns
#                              (runner.RUNNER_OWNS) beside supercronic with docker/crontab for the
#                              rest, plus the keep-alive watcher and the inbox API
#   AFCLAUDE_SCHEDULER=legacy  the phase 3a rollback (docs/phase3a_plan.md §4): supercronic with
#                              docker/crontab.legacy (the crontab before the runner), the watcher and
#                              the inbox API; no runner
#   AFCLAUDE_SCHEDULER=off     (default) nothing is scheduled; the container only idles, so
#                              `docker exec` works (tests, the MCP server, manual runs)
set -eu
D="${AFCLAUDE_DIR:?}"
CRON_BIN="${AFCLAUDE_SUPERCRONIC:-/usr/local/bin/supercronic}"
# Strict host key checking against the host's own public host key (mounted read-only).
KH="${AFCLAUDE_BRIDGE_KNOWN_HOSTS:-/tmp/afclaude_known_hosts}"
printf 'afclaude-host %s\n' "$(cut -d' ' -f1,2 /run/host_ssh/ssh_host_ed25519_key.pub)" >"$KH"
mkdir -p "${KEEPALIVE_STATE_DIR:?}"   # (the at-shim keeps its jobs in the DB; data/at_spool is legacy)
# D-227: every deploy (`docker compose up -d --build`, i.e. every container start) imports
# prompts/*.md into prompt_defaults, so the DB always has what is currently deployed (prompts
# still ship as files, D-169; prompt_overrides holds the owner's edits). Now that data/ is
# mounted, the DB is reachable if it exists at all; a first install has none yet (store.py init
# is the explicit, separate first-install step), and any other DB problem is store.py's own
# DB error path (logged + alerted via the fallback file). Either way this must never fail the
# container start, so its exit status is never allowed to trip `set -e`.
python3 "$D/store.py" import-prompts >>"$D/data/import_prompts.log" 2>&1 \
  || echo "afclaude: prompt defaults import did not complete this start (see data/import_prompts.log)" >&2
MODE="${AFCLAUDE_SCHEDULER:-off}"
case "$MODE" in
  on|legacy)
    echo "afclaude: scheduler $(echo "$MODE" | tr '[:lower:]' '[:upper:]') ($(date -u +%FT%TZ))"
    # The watcher starts right away (the host crontab's "@reboot ... start_keepalive.sh" line), not
    # only at the next :07/:22/:37/:52 watchdog: retried until it runs (the bridge may need a
    # moment after a rebuild), never while PAUSED exists, never a second one (start_watcher.sh).
    "$D/docker/start_watcher.sh" --boot &
    # The message box's API (D-241): a unix socket in data/api/ for the quickview's nginx; the cron
    # watchdog restarts it (and alerts) if it dies.
    python3 "$D/inbox_api.py" ensure --boot >>"$D/data/inbox_api.log" 2>&1 \
      || echo "afclaude: the inbox API did not start (see data/inbox_api.log)" >&2
    if [ "$MODE" = legacy ]; then
      AFCLAUDE_CRONTAB="$D/docker/crontab.legacy"
      export AFCLAUDE_CRONTAB             # export_quickview.py lists the lines that run
      exec "$CRON_BIN" -passthrough-logs "$AFCLAUDE_CRONTAB"
    fi
    # on: the runner (its stdout is docker logs; it writes data/runner.log itself). The cron
    # watchdog `runner.py ensure` starts it again if it dies (until S8).
    python3 -u "$D/runner.py" serve &
    # supercronic's own lines still go to docker logs; its job starts are also kept in
    # data/cron_runs.log (it survives a container restart), which `runner.py shadow-report`
    # compares with the runner's shadow times (S2). tee -p: a dead grep never takes supercronic down.
    FIFO="$(mktemp -d "${TMPDIR:-/tmp}/afclaude-cron.XXXXXX")/supercronic.fifo"
    mkfifo "$FIFO"
    { tee -p /dev/stderr | grep --line-buffered -F 'msg=starting' >>"$D/data/cron_runs.log"; } <"$FIFO" &
    "$CRON_BIN" -passthrough-logs "${AFCLAUDE_CRONTAB:-$D/docker/crontab}" 2>"$FIFO" &
    CRON=$!
    # This shell stays (no exec): tini passes a `docker stop` only to its child, this shell, which
    # passes it on to supercronic and to the runner, whoever started it (`runner.py stop` waits for
    # its clean exit: up to its 80-s stop grace; compose's stop_grace_period is 90 s).
    STOPPING=0
    term() {
      STOPPING=1
      kill -TERM "$CRON" 2>/dev/null || true
      python3 "$D/runner.py" stop || true
    }
    trap term TERM INT
    RC=0
    while kill -0 "$CRON" 2>/dev/null; do
      if wait "$CRON"; then RC=0; else RC=$?; fi
    done
    if [ "$STOPPING" = 0 ]; then
      echo "afclaude: supercronic ended (exit $RC); stopping the runner, the container exits" >&2
      python3 "$D/runner.py" stop || true
      [ "$RC" != 0 ] || RC=1
    fi
    exit "$RC"
    ;;
  off)
    echo "afclaude: scheduler OFF ($(date -u +%FT%TZ)); idling"
    exec sleep infinity
    ;;
  *) echo "AFCLAUDE_SCHEDULER must be on, legacy or off, not ${MODE}" >&2; exit 2 ;;
esac
