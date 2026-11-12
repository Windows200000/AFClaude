#!/usr/bin/env python3
"""The web service of the image (phase 5b K4, docs/phase5b_plan.md §3.1): `afclaude web`.

A minimal Starlette app under uvicorn on a unix socket (no port): `GET /health` and the inbox API
(inbox_api.py's routes, moved over from the runner's supervised child; the runner's own `inbox` /
`inbox_verify` jobs stay in the runner and are woken through `data/run/runner.sock`, which the two
containers share through the data directory). The quickview's nginx reaches it through the socket's
directory (data/api/); nothing is published. The dashboard (phase 6) grows from this app.

  GET /health    no key, no secrets: {"status": "ok" | "unhealthy", "problems", "schema",
                 "runner_heartbeat_age_s", "inbox_key"} (service.health_report); 503 when
                 unhealthy, i.e. the database is unreachable or at a schema this code may not write
  /afclaude/api/...   the inbox API (X-AFClaude-Key on every request, see inbox_api.py)

Start: the data directory must be mounted and the master key must load (service.start, D-235: it refuses
with a message, exit 78). Single instance per data directory (a flock); a second one exits 75. Every
way the service does not come up or ends unexpectedly (no inbox key, a second instance, a crash of the
server) is a failstop.fail (alert + pause, component `web`) and exit 1/75, as the runner's supervisor
did for the inbox API's child: docker restarts the container, the next failure is only logged while the
pause holds. A key file that goes missing while it runs shows in /health (503, `inbox_key` false).

`serve --supervised` is the single layout's (the default until the K7 cut-over, D-339) form: the runner
(`SUPERVISED`) starts it as its child in the old container, as it did `inbox_api.py serve`: no mount or
master key check (the runner's start did them), its output goes to data/inbox_api.log, exit 1 without the
key and 0 for a second instance (the runner's supervisor alerts on an exit).

  web.py serve [--socket PATH] [--supervised]   the server (the container's command)
  web.py health [--socket PATH]   GET /health on the socket: exit 0 healthy (the compose health check)
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import http.client
import os
import socket
import sys
import threading
import time
from typing import IO, Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import failstop  # noqa: E402
import inbox_api  # noqa: E402
import service  # noqa: E402
import store  # noqa: E402

DATA = os.environ.get("AFCLAUDE_DATA_DIR") or os.path.join(HERE, "data")
SOCKET = os.environ.get("AFCLAUDE_INBOX_SOCKET") or os.path.join(DATA, "api", "inbox.sock")
LOCK = os.path.join(DATA, ".web.lock")
LOG = os.path.join(DATA, "inbox_api.log")      # --supervised only (the old child's log)
EXIT_LOCKED = 75                      # EX_TEMPFAIL: another web service owns this data directory
HEALTH_TIMEOUT = 10.0
START_TIMEOUT = 20.0


def build_app(key_file: str, wake: inbox_api.Waker) -> Starlette:
    async def health(request: Request) -> Response:
        if request.method != "GET":
            return inbox_api.reply(405, {"error": "method not allowed"})

        def report() -> tuple[int, dict[str, Any]]:
            try:
                conn = store.connect()
            except Exception as e:   # noqa: BLE001 - unreachable is the answer, not a crash
                return 503, {"status": "unhealthy", "problems": [f"database: {type(e).__name__}: {e}"]}
            try:
                body = service.health_report(conn, key_file=key_file)
            finally:
                conn.close()
            return (200 if body["status"] == "ok" else 503), body
        status, body = await run_in_threadpool(report)
        return inbox_api.reply(status, body)

    return Starlette(routes=[Route("/health", health, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]), *inbox_api.routes(key_file, wake)])


def make_server(path: str, application: Starlette, *, log_level: str = "info") -> uvicorn.Server:
    """A uvicorn server on a fresh unix socket at `path` (its directory 0755; uvicorn sets the socket
    0666: nginx's worker in the quickview container runs as another user, the key check guards every
    request). A stale socket of a dead server is removed first (the lock says none runs)."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    if os.path.exists(path):
        os.unlink(path)
    return uvicorn.Server(uvicorn.Config(application, uds=path, log_level=log_level, lifespan="off",
                                         server_header=False))


class Handle:
    """A server running in a thread (the tests': bind()); serve_forever() blocks until shutdown()."""

    def __init__(self, server: uvicorn.Server) -> None:
        self.server = server
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error: BaseException | None = None

    def _run(self) -> None:
        try:
            asyncio.run(self.server.serve())      # no signal handlers: not the main thread
        except BaseException as e:   # noqa: BLE001 - handed to bind(), which raises it in the caller
            self.error = e

    def serve_forever(self) -> None:
        self.thread.join()

    def shutdown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def server_close(self) -> None:
        pass


def bind(path: str, key_file: str, wake: inbox_api.Waker) -> Handle:
    """The app serving on `path` in a thread; returns once the socket answers (or raises)."""
    srv = make_server(path, build_app(key_file, wake), log_level="warning")
    h = Handle(srv)
    h.thread.start()
    deadline = time.monotonic() + START_TIMEOUT
    while not srv.started:
        if h.error is not None or not h.thread.is_alive():
            raise RuntimeError(f"the web server did not start: {h.error!r}")
        if time.monotonic() > deadline:
            srv.should_exit = True
            raise TimeoutError(f"the web server did not start within {START_TIMEOUT:.0f} s")
        time.sleep(0.02)
    return h


def take_lock() -> IO[str] | None:
    os.makedirs(DATA, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def log_to_file() -> None:
    """--supervised: point stdout and stderr at data/inbox_api.log (append). The runner reads its child's
    pipes only when the child exits (an alert's tail), so the running server's own lines would otherwise
    never reach the log. Opened only once the server really starts: an early exit keeps its line on the
    pipe, which is what the runner's alert quotes."""
    fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)


def fail(subject: str, detail: str) -> None:
    """The web service stops: alert + pause (failstop, D-235), as the runner did for its inbox API child."""
    failstop.fail("web", subject, detail, log=inbox_api.log)


def serve(path: str, key_file: str = inbox_api.KEY_FILE, *, supervised: bool = False) -> int:
    if not supervised:
        try:
            service.start("web", mounted=service.in_container())
        except service.ServiceError as e:
            return service.refuse("web", e)
    lock = take_lock()
    if lock is None:
        inbox_api.log(f"another web service holds {LOCK}; exiting")
        if supervised:
            return 0
        fail("the web service was not running", f"a second web service holds {LOCK}: two containers on one data directory?")
        return EXIT_LOCKED
    if inbox_api.read_key(key_file) is None:      # fail fast: without the key every inbox request would be refused
        msg = f"no inbox key in {key_file}: not serving (the inbox API would answer every request 503)"
        inbox_api.log(f"ERROR: {msg}")
        lock.close()
        if not supervised:
            fail("the inbox API was not running", msg)
        return 1
    if supervised:
        log_to_file()
    try:
        make_server(path, build_app(key_file, inbox_api.WakeRunner())).run()
    except Exception as e:   # noqa: BLE001 - alerted, then the container restarts
        if supervised:
            raise                # the traceback is on the pipe the runner's alert quotes
        fail("the web service crashed", f"{type(e).__name__}: {e}")
        return 1
    except SystemExit as e:  # uvicorn ends a failed start (socket bind) with sys.exit(1)
        if e.code and not supervised:
            fail("the web service crashed", f"the server exited with {e.code} (see docker logs)")
        raise
    finally:
        lock.close()
    return 0


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path_ = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path_)


def health_cli(path: str) -> int:
    """0 = /health answered 200 on the socket; anything else is unhealthy and says why (stdout)."""
    try:
        c = UnixHTTP(path, HEALTH_TIMEOUT)
        c.request("GET", "/health")
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        c.close()
    except (OSError, http.client.HTTPException) as e:
        print(f"unhealthy: no answer on {path}: {type(e).__name__}: {e}")
        return 1
    print(text if r.status == 200 else f"unhealthy: HTTP {r.status}: {text}")
    return 0 if r.status == 200 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The AFClaude web service (phase 5b K4).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("serve", "run the server on the unix socket"), ("health", "GET /health on the socket")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--socket", default=SOCKET)
        if name == "serve":
            p.add_argument("--supervised", action="store_true",
                           help="the single layout's child of the runner (own log file, no master key check)")
    args = ap.parse_args(argv)
    return serve(args.socket, supervised=args.supervised) if args.cmd == "serve" else health_cli(args.socket)


if __name__ == "__main__":
    sys.exit(main())
