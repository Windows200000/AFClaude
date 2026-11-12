#!/usr/bin/env python3
"""What the three services of the image share at start (phase 5b K4, docs/phase5b_plan.md §3.1, §3.5):
the runner (`afclaude runner`), the MCP server (`afclaude mcp serve`) and the web service (`afclaude web`).

Every service refuses to start, with a message that names the fix, when

  * the data directory is not a mounted volume (compose's `${AFCLAUDE_DATA:?...}` mounts it; the image's
    own directory is read-only, so an unmounted one could only come back empty: D-163/D-180, "nothing
    creates it implicitly"), or
  * the master key can't be loaded or doesn't match the database (`masterkey.ensure`, K3).

`docker/entrypoint.sh` runs `service.py check-mount <name>` first of all (before any migration), and the
long-running processes call `start(name)` themselves, because the key lives in the process that
`ensure()` ran in. `service.py ensure-key <name>` is the same check for a service that idles (the
runner's `off` mode). A refusal exits 78 (EX_CONFIG): `restart: unless-stopped` retries it with
docker's back-off, the message is in `docker logs`, and where the data directory is writable the
owner is alerted once (the fallback alert file + a push, as the entrypoint's other refusals do).

`health_report` is what the web and MCP services answer on `GET /health`: no secrets, only whether the
database is reachable and at a schema this code may write, how old the runner's heartbeat is and whether
the inbox key is there.

Two layouts (D-339): `single` (the default until the K7 cut-over: one container, the runner supervising
the inbox API, docker/compose.yml; no master key yet) and `services` (three containers,
docker/compose.services.yml sets AFCLAUDE_LAYOUT=services; the master key is checked at every start).

  service.py check-mount <name>     exit 78 unless the data directory is a mounted volume
  service.py ensure-key <name>      load/create/check the master key (exit 78 on a problem)
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import masterkey  # noqa: E402
import store  # noqa: E402

SERVICES = ("runner", "mcp", "web")
LAYOUTS = ("single", "services")
DEFAULT_LAYOUT = "single"             # until K7 flips it (D-339)
MOUNTINFO = "/proc/self/mountinfo"
EXIT_REFUSED = 78                      # EX_CONFIG
NOT_MOUNTED_FIX = ("AFCLAUDE_DATA is the host directory compose mounts as the data directory: set it "
                   "(the setup chooses it, there is no default) and start the services with compose")


class ServiceError(Exception):
    """A service must not start: the message says why and what to do."""


def layout(env: Mapping[str, str] | None = None) -> str:
    """AFCLAUDE_LAYOUT: `single` (default: the old one-container layout) or `services`. Anything else is an
    error, not a guess (D-235)."""
    v = ((os.environ if env is None else env).get("AFCLAUDE_LAYOUT") or DEFAULT_LAYOUT).strip()
    if v not in LAYOUTS:
        raise ServiceError(f"AFCLAUDE_LAYOUT must be one of {', '.join(LAYOUTS)}, not {v!r}")
    return v


def data_dir() -> str:
    """The data directory as the code knows it: where the database lives (store.DB_PATH)."""
    return os.path.dirname(os.path.abspath(store.DB_PATH))


def _unescape(field: str) -> str:
    """mountinfo writes space, tab, newline and backslash in a path as octal escapes (\\040 ...)."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mount_points(mountinfo: str = MOUNTINFO) -> set[str]:
    """Every mount point of this process's mount namespace (the 5th field of a mountinfo line)."""
    with open(mountinfo, encoding="utf-8") as fh:
        return {_unescape(parts[4]) for parts in (line.split() for line in fh) if len(parts) > 4}


def check_mounted(path: str, mountinfo: str = MOUNTINFO) -> None:
    """Raise ServiceError unless `path` is itself a mount point (a bind mount from the host)."""
    real = os.path.realpath(path)
    if real not in mount_points(mountinfo):
        raise ServiceError(
            f"the data directory {path} is not a mounted volume: nothing is mounted there, and the image's own "
            f"directory is read-only and empty, so this service would run on no data. {NOT_MOUNTED_FIX}.")


def ensure_key() -> str:
    """masterkey.ensure on this install's database. -> the key id. ServiceError for any problem
    (no database, a key that is missing, torn or does not match)."""
    try:
        conn = store.connect()
    except store.DBUnavailable as e:
        raise ServiceError(f"the database is not usable, so the master key can't be checked: {e}") from e
    try:
        return masterkey.ensure(data_dir(), conn)
    except masterkey.MasterKeyError as e:
        raise ServiceError(f"master key: {e}") from e
    finally:
        conn.close()


def start(name: str, *, mounted: bool = True, key: bool = True) -> str | None:
    """The start check of a long-running service: the mount (unless `mounted` is False, for a process
    that is not in a container) and the master key, which stays loaded in this process (unless `key` is
    False: the single layout has no key yet). -> key id, or None without the key check."""
    if name not in SERVICES:
        raise ServiceError(f"unknown service {name!r} (one of {', '.join(SERVICES)})")
    if mounted:
        check_mounted(data_dir())
    return ensure_key() if key else None


def in_container(env: Mapping[str, str] | None = None) -> bool:
    return bool((os.environ if env is None else env).get("AFCLAUDE_IN_CONTAINER"))


def refuse(name: str, err: Exception, *, alert: bool = True) -> int:
    """Print why the service does not start (docker logs), alert the owner once where the data
    directory can take the alert file, and return the exit code. An alert that can't be written
    is printed too (D-235: nothing swallowed)."""
    print(f"afclaude {name}: refusing to start: {err}", file=sys.stderr, flush=True)
    if alert:
        try:
            store.alert_once(store.fallback_path(), f"AFClaude {name} does not start",
                             f"{err}\nThe {name} service exits and docker restarts it; it starts once this is fixed.",
                             f"service:{name}")
        except Exception as e:   # noqa: BLE001 - reported on stderr, the refusal itself is the main message
            print(f"afclaude {name}: the owner could not be alerted either: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
    return EXIT_REFUSED


def health_report(conn: Any, *, key_file: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The body of `GET /health`: {"status": "ok" | "unhealthy", "problems": [...], "schema": int,
    "runner_heartbeat_age_s": int | None, "inbox_key": bool | None}. Unhealthy only for what makes this
    service useless: a database this code can't write (schema_problem) or, for the web service (a key_file
    given), no readable inbox key. The runner's heartbeat is
    reported, not judged: the runner has its own health check and may be switched off on purpose."""
    problems: list[str] = []
    schema = store.get_meta(conn, "schema_version")
    mismatch = store.schema_problem(conn)
    if mismatch is not None:
        problems.append(f"schema: {mismatch}")
    beat_age: int | None = None
    row = store.get_runner_job(conn, "runner")
    beat = store.parse_iso(row["heartbeat_at"]) if row and row.get("heartbeat_at") else None
    if beat is not None:
        beat_age = max(0, int(((now or datetime.now(timezone.utc)) - beat).total_seconds()))
    key_ok: bool | None = None          # None: this service has no inbox key to report (the MCP service)
    if key_file is not None:
        try:
            with open(key_file, encoding="utf-8") as fh:
                key_ok = bool(fh.read().strip())
        except OSError:
            key_ok = False
        if not key_ok:       # the inbox API can't serve without it: that is an outage, not a detail (D-235, review k4a M1)
            problems.append("inbox key: no readable key file (every inbox request is refused)")
    return {"status": "unhealthy" if problems else "ok", "problems": problems,
            "schema": int(schema) if schema and schema.isdigit() else None,
            "runner_heartbeat_age_s": beat_age, "inbox_key": key_ok}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("check-mount", "ensure-key") or args[1] not in SERVICES:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, name = args
    try:
        if cmd == "check-mount":
            check_mounted(data_dir())
            return 0
        print(f"afclaude {name}: master key {ensure_key()} loaded", flush=True)
        return 0
    except ServiceError as e:
        # an unmounted data directory has nowhere to put an alert file (read-only image)
        return refuse(name, e, alert=cmd != "check-mount")


if __name__ == "__main__":
    sys.exit(main())
