#!/bin/sh
# Entrypoint of the AFClaude manager container (PID 1 is tini).
#   AFCLAUDE_SCHEDULER=runner  the resident runner (runner.py serve, phase 3a S8): this script
#                              `exec`s it, so it is tini's only child and `docker stop`'s SIGTERM
#                              reaches it directly (its clean exit within compose's 90-s stop grace);
#                              no cron. The runner owns every job (runner.JOBS; the inbox API is its
#                              supervised child). Its own loop watchdog exits the process when the
#                              loop is stuck, and `restart: unless-stopped` starts the container again
#   AFCLAUDE_SCHEDULER=off     (default) nothing is scheduled; the container only idles, so
#                              `docker exec` works (tests, the MCP server, manual runs)
# (phase 3a S9 removed the rollback mode `legacy`, supercronic and its crontabs, and the alias `on`)
set -eu
D="${AFCLAUDE_DIR:?}"
# Strict host key checking against the host's own public host key (mounted read-only).
KH="${AFCLAUDE_BRIDGE_KNOWN_HOSTS:-/tmp/afclaude_known_hosts}"
printf 'afclaude-host %s\n' "$(cut -d' ' -f1,2 /run/host_ssh/ssh_host_ed25519_key.pub)" >"$KH"
mkdir -p "${KEEPALIVE_STATE_DIR:?}"
# Phase 4b Q1: the explicit schema step. A non-additive change (store.MIGRATIONS; v5 rebuilds `decisions`)
# never runs on connect(): until this has run, code at the new version can't write the tables the migration
# changes (MigrationNeeded; everything else stays writable, so the host's deploy.py can drain and the old
# container keeps running meanwhile), and after it code at the old version can't write at all (SchemaTooNew),
# so it runs first and alone: the old container is gone by now and the runner has not started. It copies the DB to <db>.v<old>-<utc>.bak before changing anything
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
  runner)
    echo "afclaude: scheduler RUNNER ($(date -u +%FT%TZ))"
    # the runner is the container's main process (its stdout is docker logs; it writes
    # data/runner.log itself). No shell stays behind: a stuck loop is the runner's own watchdog
    # thread (exit 70), a crash its exit code; either ends the container and
    # `restart: unless-stopped` starts it again (the next boot alerts + pauses, D-257).
    exec python3 -u "$D/runner.py" serve
    ;;
  off)
    echo "afclaude: scheduler OFF ($(date -u +%FT%TZ)); idling"
    exec sleep infinity
    ;;
  *) echo "AFCLAUDE_SCHEDULER must be runner or off, not ${MODE}" >&2; exit 2 ;;
esac
