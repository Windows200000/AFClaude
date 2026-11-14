"""Hold a background worker's report across the task-manager's hand-off + /clear (D-340).

Before the keep-alive's save prompt is answered, the task-manager holds every running background
worker: a report that arrives between the hand-off and the /clear starts a turn the hand-off does
not know, and the /clear fails (D-234). A SendMessage reaches a worker only at its next tool round,
so the hold and the go are flag files a waiting worker polls:

    tasks.py worker hold ID...    the task-manager: hold these workers (then SendMessage each one)
    tasks.py worker go ID...      the task-manager, after the /clear: release them (--all: every hold)
    tasks.py worker wait [ID]     the worker, before its final report: exit 0 = send it now,
                                  exit 3 = still held, run it again (ID default: $AFCLAUDE_AGENT_ID)
    tasks.py worker list          the holds

A hold lasts at most HOLD_MAX: after that `wait` releases the worker itself (it reports and says it
was held that long), so a forgotten go costs half an hour, never a lost report. The flags live in
the directory of the AFClaude database (`worker_holds/<agent id>`), no database row: a waiting
worker holds no connection.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

UTC = timezone.utc
HOLD_MAX = timedelta(minutes=30)   # a hold older than this releases the worker (D-307 #4's exception, D-340)
WAIT_MAX = 540                     # seconds one `wait` call polls (below the Bash tool's 10-min limit)
POLL = 15                          # seconds
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")   # identity_hook.ID_RE
AGENT_VAR = "AFCLAUDE_AGENT_ID"    # identity.AGENT_VAR (exported for a subagent's CLI calls)
EXIT_GO, EXIT_HELD, EXIT_ERROR = 0, 3, 2


def hold_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "worker_holds")


def _agent(agent_id: str) -> str:
    if not ID_RE.match(agent_id or ""):
        raise ValueError(f"not an agent id: {agent_id!r} (letters, digits, . _ -; at most 64)")
    return agent_id


def _path(d: str, agent_id: str) -> str:
    return os.path.join(d, _agent(agent_id))


def hold(d: str, agent_ids: list[str], now: Optional[datetime] = None) -> list[str]:
    """Hold each worker (a new hold restarts its HOLD_MAX). -> the ids."""
    ids = [_agent(a) for a in agent_ids]
    os.makedirs(d, mode=0o700, exist_ok=True)
    at = (now or datetime.now(UTC)).isoformat()
    for a in ids:
        tmp = _path(d, a) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"agent": a, "held_at": at}, fh)
        os.replace(tmp, _path(d, a))
    return ids


def go(d: str, agent_ids: list[str], every: bool = False) -> tuple[list[str], list[str]]:
    """Release the workers (every: all holds). -> (released, not held: no flag for that id)."""
    ids = [h["agent"] for h in holds(d)] if every else [_agent(a) for a in agent_ids]
    done, missing = [], []
    for a in ids:
        try:
            os.remove(_path(d, a))
            done.append(a)
        except FileNotFoundError:
            missing.append(a)
    return done, missing


def held_at(d: str, agent_id: str) -> Optional[datetime]:
    """When the worker was held, or None (not held). An unreadable flag raises (D-235)."""
    try:
        with open(_path(d, agent_id)) as fh:
            return datetime.fromisoformat(json.load(fh)["held_at"])
    except FileNotFoundError:
        return None


def holds(d: str) -> list[dict[str, Any]]:
    """Every hold: {agent, held_at}, oldest first."""
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if ID_RE.match(name):
            at = held_at(d, name)
            if at is not None:
                out.append({"agent": name, "held_at": at})
    return sorted(out, key=lambda h: h["held_at"])


def wait(d: str, agent_id: str, max_seconds: float = WAIT_MAX, poll: float = POLL,
         clock: Callable[[], datetime] = lambda: datetime.now(UTC),
         sleep: Callable[[float], None] = time.sleep) -> tuple[str, Optional[datetime]]:
    """Wait while the worker is held, up to max_seconds. -> (state, held_at): "free" (no hold),
    "go" (released while waiting), "expired" (held HOLD_MAX: the flag is removed, the worker
    reports anyway) or "held" (still held after max_seconds: call again)."""
    _agent(agent_id)
    start = clock()
    first = held_at(d, agent_id)
    if first is None:
        return "free", None
    while True:
        at = held_at(d, agent_id)
        if at is None:
            return "go", first
        if clock() - at >= HOLD_MAX:
            go(d, [agent_id])
            return "expired", at
        if (clock() - start).total_seconds() >= max_seconds:
            return "held", at
        sleep(poll)


def _hm(t: datetime) -> str:
    return t.astimezone().strftime("%H:%M")


def cli(args: Any, db_path: str) -> int:
    """`tasks.py worker hold|go|wait|list` (no database connection: a waiting worker holds none)."""
    d = hold_dir(db_path)
    try:
        if args.worker_cmd == "hold":
            ids = hold(d, args.ids)
            print(f"held {', '.join(ids)}: now SendMessage each one the hold; after the /clear release them with "
                  f"`tasks.py worker go {' '.join(ids)}`")
        elif args.worker_cmd == "go":
            if not args.ids and not args.all:
                raise ValueError("name the agent ids to release, or --all")
            done, missing = go(d, args.ids, args.all)
            print((f"released {', '.join(done)}" if done else "released none")
                  + (f"; not held: {', '.join(missing)}" if missing else ""))
        elif args.worker_cmd == "list":
            rows = holds(d)
            print("\n".join(f"{h['agent']}  held since {_hm(h['held_at'])}" for h in rows) or "no worker is held")
        else:   # wait
            agent = args.id or os.environ.get(AGENT_VAR) or ""
            if not agent:
                raise ValueError(f"no agent id: pass the one the hold message names (${AGENT_VAR} is unset)")
            state, at = wait(d, agent, args.max)
            if state == "held":
                assert at is not None
                left = HOLD_MAX - (datetime.now(UTC) - at)
                print(f"still held (since {_hm(at)}, released by itself in {max(left.total_seconds(), 0) / 60:.0f} "
                      "min): run this command again; do not end your turn")
                return EXIT_HELD
            print({"free": "not held: send your report now",
                   "go": "released: send your report now",
                   "expired": f"the hold ran out after {HOLD_MAX.total_seconds() / 60:g} min with no go: send your "
                              "report now and say in it that it was held that long"}[state])
    except (ValueError, OSError, KeyError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_GO
