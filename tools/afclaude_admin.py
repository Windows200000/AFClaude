#!/usr/bin/env python3
"""Host-access admin commands for the dashboard's local auth (phase 6a W7,
docs/phase6a_plan.md §3.7, §9.3: lockout recovery).

    .venv/bin/python tools/afclaude_admin.py reset-auth <user_id>

Ends the user's browser sessions, clears their login factors and prints a fresh one-time code they
can use at /setup (a 'reset' code, dashboard.setup.reset_auth) -- never stored except as its hash,
never logged anywhere else. A placeholder today (D-235, fails loudly, not quietly): clearing factors
needs W6's credentials/recovery_codes tables and 5a's users table, neither built yet, so this refuses
with a clear error until a real dashboard.setup.OwnerRegistry is wired in (W10's `afclaude` entrypoint
is expected to own that wiring and may fold this command into itself then).
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import store  # noqa: E402
from dashboard import setup  # noqa: E402


def reset_auth(user_id: int, db_path: str | None) -> int:
    conn = store.connect(db_path)
    try:
        code = setup.reset_auth(conn, user_id)
    finally:
        conn.close()
    print(f"user {user_id}: signed out of every session; sign in again at /setup with this one-time "
         f"code (it expires, single use):\n{code}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", dest="db_path", default=None, help="database path (default: store.DB_PATH)")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-auth", help="end a user's sessions, clear their login factors, mint a fresh code")
    reset.add_argument("user_id", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "reset-auth":
            return reset_auth(args.user_id, args.db_path)
        raise AssertionError(f"unknown command {args.command!r}")
    except NotImplementedError as exc:
        print(f"afclaude_admin.py: not available yet: {exc}", file=sys.stderr)
        return 1
    except (ValueError, LookupError) as exc:
        print(f"afclaude_admin.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
