#!/bin/sh
# Entrypoint of the AFClaude manager container (PID 1 is tini).
#   AFCLAUDE_SCHEDULER=runner  the resident runner only (runner.py serve, phase 3a S8): this script
#                              `exec`s it, so it is tini's only child and `docker stop`'s SIGTERM
#                              reaches it directly (its clean exit within compose's 90-s stop grace);
#                              no supercronic, no cron. The runner owns every job (runner.RUNNER_OWNS,
#                              the keep-alive watcher's passes and the inbox API's supervision
#                              included). Its own loop watchdog exits the process when the loop is
#                              stuck, and `restart: unless-stopped` starts the container again
#   AFCLAUDE_SCHEDULER=on      the same as `runner` (S8: the name the S2-S7 deploys set; supercronic
#                              beside the runner is gone, docker/crontab has no job line left)
#   AFCLAUDE_SCHEDULER=legacy  the phase 3a rollback (docs/phase3a_plan.md §4): supercronic with
#                              docker/crontab.legacy (the crontab before the runner), the watcher and
#                              the inbox API (with its worker thread); no runner
#   AFCLAUDE_SCHEDULER=off     (default) nothing is scheduled; the container only idles, so
#                              `docker exec` works (tests, the MCP server, manual runs)
set -eu
D="${AFCLAUDE_DIR:?}"
CRON_BIN="${AFCLAUDE_SUPERCRONIC:-/usr/local/bin/supercronic}"
# Strict host key checking against the host's own public host key (mounted read-only).
KH="${AFCLAUDE_BRIDGE_KNOWN_HOSTS:-/tmp/afclaude_known_hosts}"
printf 'afclaude-host %s\n' "$(cut -d' ' -f1,2 /run/host_ssh/ssh_host_ed25519_key.pub)" >"$KH"
mkdir -p "${KEEPALIVE_STATE_DIR:?}"   # (the at-shim keeps its jobs in the DB; data/at_spool is legacy)
# Phase 4b Q1: the explicit schema step. A non-additive change (store.MIGRATIONS; v5 rebuilds `decisions`)
# never runs on connect(): until this has run, code at the new version can't write to the DB (MigrationNeeded),
# and after it code at the old version can't (SchemaTooNew), so it runs first and alone: the old container is
# gone by now and the runner has not started. It copies the DB to <db>.v<old>-<utc>.bak before changing anything
# (an unchanged DB that already has such a copy gets no second one) and is a no-op when the DB is current.
# Exit 3 = no database yet (a first install): the start goes on. Any other failure alerts the owner itself
# (store.py: the fallback alert file + a push, the DB being unwritable) and the container then IDLES instead of
# starting the runner: a runner on a read-only DB crashes at boot, the container exits, `restart: unless-stopped`
# loops, and every loop would run migrate again. Idle, it stays up and unhealthy: `runner.py health` reports the
# schema state, `docker exec` works for the fix, and the next container start retries the migration (D-235).
rc=0
python3 "$D/store.py" migrate >>"$D/data/migrate.log" 2>&1 || rc=$?
if [ "$rc" -eq 3 ]; then
  echo "afclaude: store migrate: no database yet (a first install), nothing to migrate" >&2
elif [ "$rc" -ne 0 ]; then
  echo "afclaude: store migrate FAILED (exit $rc, see data/migrate.log; the owner was alerted): not starting the scheduler; idling, unhealthy, until the next container start" >&2
  exec sleep infinity
fi
# D-227: every deploy (`docker compose up -d --build`, i.e. every container start) imports
# prompts/*.md into prompt_defaults, so the DB always has what is currently deployed (prompts
# still ship as files, D-169; prompt_overrides holds the owner's edits). Now that data/ is
# mounted, the DB is reachable if it exists at all; a first install has none yet (store.py init
# is the explicit, separate first-install step), and any other DB problem is store.py's own
# DB error path (logged + alerted via the fallback file). Either way this must never fail the
# container start, so its exit status is never allowed to trip `set -e`.
python3 "$D/store.py" import-prompts >>"$D/data/import_prompts.log" 2>&1 \
  || echo "afclaude: prompt defaults import did not complete this start (see data/import_prompts.log)" >&2
# Phase 4a P3: the prompt variables of the project at this checkout (AFClaude itself) are seeded
# after the defaults import, only where a project never had a value (idempotent, audited as
# runner:deploy). Same rule as above: never fails the container start; until it has run, a
# session message or the SessionStart hook that needs those values errors visibly instead of
# going out without them (prompts.NotMigrated).
python3 "$D/prompts.py" migrate --deploy >>"$D/data/import_prompts.log" 2>&1 \
  || echo "afclaude: prompt variables migrate did not complete this start (see data/import_prompts.log)" >&2
MODE="${AFCLAUDE_SCHEDULER:-off}"
case "$MODE" in
  on|runner|legacy)
    echo "afclaude: scheduler $(echo "$MODE" | tr '[:lower:]' '[:upper:]') ($(date -u +%FT%TZ))"
    # legacy: the watcher starts right away (the host crontab's "@reboot ... start_keepalive.sh"
    # line), not only at the next :07/:22/:37/:52 watchdog: retried until it runs (the bridge may
    # need a moment after a rebuild), never while PAUSED exists, never a second one
    # (start_watcher.sh). on: the runner runs the watcher's passes (phase 3a S6), no watcher.
    if [ "$MODE" = legacy ]; then
      "$D/docker/start_watcher.sh" --boot &
      # The message box's API (D-241): a unix socket in data/api/ for the quickview's nginx; the
      # cron watchdog restarts it (and alerts) if it dies. S7: `on` mode no longer starts it here;
      # the runner keeps it running itself (runner.SUPERVISED).
      python3 "$D/inbox_api.py" ensure --boot >>"$D/data/inbox_api.log" 2>&1 \
        || echo "afclaude: the inbox API did not start (see data/inbox_api.log)" >&2
      AFCLAUDE_CRONTAB="$D/docker/crontab.legacy"
      export AFCLAUDE_CRONTAB             # export_quickview.py lists the lines that run
      exec "$CRON_BIN" -passthrough-logs "$AFCLAUDE_CRONTAB"
    fi
    # on / runner: the runner is the container's main process (its stdout is docker logs; it writes
    # data/runner.log itself). No shell stays behind, no watchdog line: a stuck loop is the runner's
    # own watchdog thread (exit 70), a crash its exit code; either ends the container and
    # `restart: unless-stopped` starts it again (the next boot alerts + pauses, D-257).
    exec python3 -u "$D/runner.py" serve
    ;;
  off)
    echo "afclaude: scheduler OFF ($(date -u +%FT%TZ)); idling"
    exec sleep infinity
    ;;
  *) echo "AFCLAUDE_SCHEDULER must be runner, on, legacy or off, not ${MODE}" >&2; exit 2 ;;
esac
