#!/usr/bin/env python3
"""The inbox's HTTP endpoint (D-178, D-241; inbox.py has the overview): the only way a message typed
on the temp dashboard reaches the DB. A tiny stdlib HTTP server on a unix socket (no port), run in
the afclaude container (`inbox_api.py serve`); the quickview's nginx proxies /afclaude/api/ to the
socket (quickview/nginx.conf: the socket's directory data/api/ is mounted into that container), so
nothing new is published and the request has passed nginx's X-AFClaude-Key check (D-150) first.

  GET  /afclaude/api/inbox               the box: the chat and the noted messages (inbox.view)
  POST /afclaude/api/inbox               {"text": "..."} -> 201, the new message (pending); the
                                         worker triages it (and delivers a "now" one) right away
  POST /afclaude/api/inbox/<id>/retry    a failed message again -> 200
  POST /afclaude/api/inbox/<id>/dismiss  an answered thread leaves the questions section (D-304) -> 200

Owner-only (the text ends up in the task-manager's session): every request needs the same
X-AFClaude-Key here too (compared with the key file, read on every request, so a rotated key
needs no restart; no key file: every request is refused, 503), a POST must be JSON
(application/json, so a cross-site form can't send one), at most BODY_MAX bytes, and at most
inbox.RATE_MAX messages an hour. Writes go through actions.py (inbox.post / retry / dismiss, actor
owner, via dashboard: audited). The worker (inbox_worker.run_forever) runs in the same process.

  inbox_api.py serve [--socket PATH]    the server (one per data/: a lock file); the entrypoint
                                         starts it with `ensure --boot`
  inbox_api.py ensure [--boot]          start the server detached unless one runs (cron watchdog;
                                         without --boot a server that was not running is an
                                         error: alert + automation paused, D-244)
"""
from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from typing import IO, Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import failstop  # noqa: E402
import inbox  # noqa: E402
import store  # noqa: E402

DATA = os.environ.get("AFCLAUDE_DATA_DIR") or os.path.join(HERE, "data")
SOCKET = os.environ.get("AFCLAUDE_INBOX_SOCKET") or os.path.join(DATA, "api", "inbox.sock")
LOCK = os.path.join(DATA, ".inbox_api.lock")
DOWN = os.path.join(DATA, ".inbox_api.down")     # the watchdog found it down and alerted (once per outage)
LOG = os.path.join(DATA, "inbox_api.log")
KEY_FILE = os.environ.get("AFCLAUDE_INBOX_KEY_FILE", "/run/secrets/afclaude_quickview.key")
KEY_HEADER = "X-AFClaude-Key"
PREFIX = "/afclaude/api/inbox"
MSG_RE = re.compile(r"^/afclaude/api/inbox/(\d{1,9})/(retry|dismiss)$")
BODY_MAX = 16 * 1024
ACTOR, VIA = "owner", "dashboard"


def log(msg: str) -> None:
    print(f"[{datetime.now(inbox.BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def read_key(path: str) -> str | None:
    """The quickview key (the file holds it alone, a trailing newline is ignored); None if unreadable."""
    try:
        with open(path) as fh:
            key = fh.read().strip()
    except OSError:
        return None
    return key or None


class HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, key_file: str, wake: threading.Event) -> None:
        self.key_file, self.wake = key_file, wake
        super().__init__(path, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "afclaude-inbox"
    sys_version = ""

    def address_string(self) -> str:      # a unix socket has no client address
        return "unix"

    def log_message(self, format: str, *args: Any) -> None:
        log("http " + (format % args))

    # ---- plumbing
    def reply(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def check_key(self) -> None:
        want = read_key(self.server.key_file)
        if want is None:
            raise HTTPError(503, "the inbox has no key configured; refusing every request")
        got = self.headers.get(KEY_HEADER) or ""
        if not hmac.compare_digest(got.encode(), want.encode()):
            raise HTTPError(401, "unauthorized")

    def json_body(self) -> dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPError(415, "the body must be application/json")
        try:
            size = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise HTTPError(411, "Content-Length required") from None
        if size < 0 or size > BODY_MAX:
            raise HTTPError(413, f"the body is too large (> {BODY_MAX} bytes)")
        try:
            body = json.loads(self.rfile.read(size).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "the body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HTTPError(400, "the body must be a JSON object")
        return body

    def handle_api(self, method: str) -> None:
        try:
            self.check_key()
            path = self.path.split("?", 1)[0]
            conn = store.connect()
            try:
                status, body = self.route(conn, method, path)
            finally:
                conn.close()
            self.reply(status, body)
        except HTTPError as e:
            self.reply(e.status, {"error": e.message})
        except actions.Forbidden as e:
            self.reply(403, {"error": str(e)})
        except store.NotFound as e:
            self.reply(404, {"error": e.args[0] if e.args else "not found"})
        except store.InvalidTransition as e:
            self.reply(409, {"error": str(e)})
        except ValueError as e:
            self.reply(400, {"error": str(e)})
        except Exception as e:   # noqa: BLE001 - the DB error path (already reported) and anything else
            log(f"ERROR {method} {self.path}: {type(e).__name__}: {e}")
            self.reply(503, {"error": f"{type(e).__name__}: {str(e)[:300]}"})

    def route(self, conn: Any, method: str, path: str) -> tuple[int, object]:
        if path == PREFIX:
            if method == "GET":
                return 200, inbox.view(conn)
            body = self.json_body()
            if set(body) - {"text"}:
                raise HTTPError(400, f"unknown field(s) {sorted(set(body) - {'text'})}")
            if not inbox.rate_ok(conn):
                raise HTTPError(429, f"more than {inbox.RATE_MAX} messages in the last hour; wait a little")
            m = actions.perform(conn, "inbox.post", {"text": body.get("text")}, actor=ACTOR, via=VIA)
            log(f"inbox #{m['id']}: posted ({len(m['text'])} characters)")
            self.server.wake.set()
            return 201, inbox.message_view(m)
        hit = MSG_RE.match(path)
        if hit and method == "POST":
            self.json_body()
            verb = hit.group(2)
            m = actions.perform(conn, f"inbox.{verb}", {"message_id": int(hit.group(1))}, actor=ACTOR, via=VIA)
            log(f"inbox #{m['id']}: {'retried' if verb == 'retry' else 'dismissed'} by the owner")
            if verb == "retry":
                self.server.wake.set()
            return 200, inbox.message_view(m)
        raise HTTPError(404 if not hit else 405, "not found" if not hit else "method not allowed")

    def do_GET(self) -> None:
        self.handle_api("GET")

    def do_POST(self) -> None:
        self.handle_api("POST")

    def _not_allowed(self) -> None:
        self.reply(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = _not_allowed


def bind(path: str, key_file: str, wake: threading.Event) -> Server:
    """The server on a fresh socket at `path` (its directory 0755, the socket 0666: nginx's worker
    in the quickview container runs as another user; the key check guards every request)."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    if os.path.exists(path):
        os.unlink(path)        # a stale socket of a dead server (the lock says none runs)
    srv = Server(path, key_file, wake)
    os.chmod(path, 0o666)
    return srv


def take_lock() -> IO[str] | None:
    os.makedirs(DATA, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def serve(path: str) -> int:
    lock = take_lock()
    if lock is None:
        log("another inbox_api.py serve holds the lock; exiting")
        return 0
    if read_key(KEY_FILE) is None:      # fail fast: without the key every request would be refused
        log(f"ERROR: no key in {KEY_FILE}; not serving (the watchdog alerts it)")
        lock.close()
        return 1
    import inbox_worker
    wake = threading.Event()
    threading.Thread(target=inbox_worker.run_forever, args=(wake,), name="inbox-worker", daemon=True).start()
    srv = bind(path, KEY_FILE, wake)
    log(f"inbox API listening on {path} (pid {os.getpid()})")
    if os.path.exists(DOWN):
        os.unlink(DOWN)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        lock.close()
    return 0


def ensure(boot: bool) -> int:
    """Start `serve` detached unless one holds the lock (the cron watchdog; the entrypoint's start)."""
    lock = take_lock()
    if lock is None:
        return 0
    lock.close()
    with open(LOG, "a") as out:
        subprocess.Popen([sys.executable, "-u", os.path.abspath(__file__), "serve"], stdout=out,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, cwd=HERE)
    if boot:
        log("inbox API started")
    elif not os.path.exists(DOWN):      # one error per outage: serve() removes the mark once it listens
        with open(DOWN, "w") as fh:
            fh.write(datetime.now(inbox.BERLIN).isoformat() + "\n")
        # an inbox error: alert + pause (D-244); install-wide: the API serves every project's inbox
        failstop.fail("inbox", "inbox API was not running",
                      "the watchdog started it again; if it keeps failing, see data/inbox_api.log", log=log)
    else:
        log("inbox API still not running; started again (already alerted)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The inbox's HTTP endpoint (D-178/D-241).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the server on the unix socket")
    p.add_argument("--socket", default=SOCKET)
    p = sub.add_parser("ensure", help="start the server detached unless one runs")
    p.add_argument("--boot", action="store_true", help="the container start: no alert")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return serve(args.socket)
    return ensure(args.boot)


if __name__ == "__main__":
    sys.exit(main())
