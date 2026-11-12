#!/usr/bin/env python3
"""The inbox's HTTP endpoint (D-178, D-241; inbox.py has the overview): the only way a message typed
on the temp dashboard reaches the DB. The routes of the web service (web.py, K4: `afclaude web`) as a Starlette
app on a unix socket (no port); the quickview's nginx proxies /afclaude/api/ to the socket
(quickview/nginx.conf: the socket's directory data/api/ is mounted into that container), so
nothing new is published and the request has passed nginx's X-AFClaude-Key check (D-150) first.

  GET  /afclaude/api/inbox               the box: the messages on their way, the thread cards (inbox.view)
  POST /afclaude/api/inbox               {"text": "...", "parent"?: id} -> 201, the new message (pending); the
                                         worker triages it (and delivers a "now" one) right away; `parent`: a
                                         message of an answered thread, the text is a follow-up in it (D-324)
  POST /afclaude/api/inbox/<id>/retry    a failed message again -> 200
  POST /afclaude/api/inbox/<id>/dismiss  an answered thread (its first message's id) leaves the dashboard (D-304) -> 200
  POST /afclaude/api/inbox/notice/<id>/dismiss   a notice leaves the questions section (D-306) -> 200
  POST /afclaude/api/questions/<id>/answer   {"answer": "..."} or {"option": i} (the 0-based index of one of
                                         its options; the server stores the option's text) -> 200: a plain
                                         question is answered
                                         (question.answer: stored on it and as an inbox message from the
                                         owner, triaged and delivered like any other; Q3 W2, D-300)
  POST /afclaude/api/questions/<id>/decide   {"choice": "...", "text"?, "note"?, "version": N} -> 200: a
                                         question about a decision entry is decided (question.decide:
                                         approve / edit / reject or confirm / drop); `version` is the entry's
                                         the owner saw, a stale one is a 409 and the page reloads

Owner-only (the text ends up in the task-manager's session): every request needs the same
X-AFClaude-Key here too (compared with the key file, read on every request, so a rotated key
needs no restart; no key file: every request is refused, 503), a POST must be JSON
(application/json, so a cross-site form can't send one), at most BODY_MAX bytes, and at most
inbox.RATE_MAX messages an hour (an answer or a decision's note is a message too: both count). Writes
go through actions.py (inbox.post / retry / dismiss, notice.dismiss, question.answer / decide, actor
owner, via dashboard: audited).

Phase 3a S7 (docs/phase3a_plan.md §5 S7): this process only serves HTTP; a post or a retry wakes
the resident runner (runner.wake: a datagram on data/run/runner.sock, so it reaches the runner's container
through the shared data directory), whose own `inbox` / `inbox_verify` jobs do the triage, delivery
and verification (inbox_worker.py). Phase 5b K4 moved the server out of the runner (it was its supervised
child) into the web service, a container of its own: its lines go to stdout (docker logs) and the old
data/inbox_api.log is no longer written; the single-instance lock and the unix-socket serving are web.py's.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Mapping, Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import inbox  # noqa: E402
import store  # noqa: E402

KEY_FILE = os.environ.get("AFCLAUDE_INBOX_KEY_FILE", "/run/secrets/afclaude_quickview.key")
KEY_HEADER = "X-AFClaude-Key"
PREFIX = "/afclaude/api/inbox"
MSG_RE = re.compile(r"^/afclaude/api/inbox/(\d{1,9})/(retry|dismiss)$")
NOTICE_RE = re.compile(r"^/afclaude/api/inbox/notice/(\d{1,9})/dismiss$")
QUESTION_RE = re.compile(r"^/afclaude/api/questions/(\d{1,9})/(answer|decide)$")
QUESTION_FIELDS = {"answer": {"answer", "option"}, "decide": {"choice", "text", "note", "version"}}
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


class Waker(Protocol):
    """_WakeRunner (the resident runner, S7), or a threading.Event in tests: the handlers only
    ever call `.set()` on it."""
    def set(self) -> None: ...


class WakeRunner:
    """The resident runner's wake (S7): a `.set()` that sends it a datagram, the handlers' one
    `wake.set()` call (route()). Never blocks or raises (runner.wake's own contract)."""
    def set(self) -> None:
        import runner
        runner.wake()


def reply(status: int, body: object) -> Response:
    data = json.dumps(body, ensure_ascii=False, default=str).encode()
    return Response(data, status_code=status, media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


def check_key(headers: Mapping[str, str], key_file: str) -> None:
    want = read_key(key_file)
    if want is None:
        raise HTTPError(503, "the inbox has no key configured; refusing every request")
    got = headers.get(KEY_HEADER.lower()) or ""
    if not hmac.compare_digest(got.encode(), want.encode()):
        raise HTTPError(401, "unauthorized")


def json_body(headers: Mapping[str, str], raw: bytes | None) -> dict[str, Any]:
    """The request's JSON object. `raw` is the body as read before (None: not read, because the
    Content-Length was missing, bad or over BODY_MAX: the matching error is raised here)."""
    ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPError(415, "the body must be application/json")
    try:
        size = int(headers.get("content-length") or "")
    except ValueError:
        raise HTTPError(411, "Content-Length required") from None
    if size < 0 or size > BODY_MAX:
        raise HTTPError(413, f"the body is too large (> {BODY_MAX} bytes)")
    try:
        body = json.loads((raw or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPError(400, "the body is not valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPError(400, "the body must be a JSON object")
    return body


async def read_body(request: Request) -> bytes | None:
    """The body, read only when its declared length is sane (json_body() complains about the rest)."""
    declared = request.headers.get("content-length") or ""
    if not declared.isdigit() or int(declared) > BODY_MAX:
        return None
    return await request.body()


def route(conn: Any, method: str, path: str, headers: Mapping[str, str], raw: bytes | None,
          wake: Waker) -> tuple[int, object]:
    def body_of() -> dict[str, Any]:
        return json_body(headers, raw)
    if path == PREFIX:
        if method == "GET":
            return 200, inbox.view(conn)
        body = body_of()
        if set(body) - {"text", "parent"}:
            raise HTTPError(400, f"unknown field(s) {sorted(set(body) - {'text', 'parent'})}")
        if not inbox.rate_ok(conn):
            raise HTTPError(429, f"more than {inbox.RATE_MAX} messages in the last hour; wait a little")
        params = {"text": body.get("text"), **({"parent": body["parent"]} if "parent" in body else {})}
        m = actions.perform(conn, "inbox.post", params, actor=ACTOR, via=VIA)
        log(f"inbox #{m['id']}: posted ({len(m['text'])} characters)" +
            (f", follow-up in the thread of #{m['parent_id']}" if m["parent_id"] is not None else ""))
        wake.set()
        return 201, inbox.message_view(m)
    question = QUESTION_RE.match(path)
    if question:
        if method != "POST":
            raise HTTPError(405, "method not allowed")
        return answer_question(conn, int(question.group(1)), question.group(2), body_of(), wake)
    notice = NOTICE_RE.match(path)
    if notice:
        if method != "POST":
            raise HTTPError(405, "method not allowed")
        body_of()
        n = actions.perform(conn, "notice.dismiss", {"notice_id": int(notice.group(1))}, actor=ACTOR, via=VIA)
        log(f"notice #{n['id']}: dismissed by the owner")
        return 200, inbox.notice_view(n)
    hit = MSG_RE.match(path)
    if hit and method == "POST":
        body_of()
        verb = hit.group(2)
        m = actions.perform(conn, f"inbox.{verb}", {"message_id": int(hit.group(1))}, actor=ACTOR, via=VIA)
        log(f"inbox #{m['id']}: {'retried' if verb == 'retry' else 'dismissed'} by the owner")
        if verb == "retry":
            wake.set()
        return 200, inbox.message_view(m)
    raise HTTPError(404 if not hit else 405, "not found" if not hit else "method not allowed")


def answer_question(conn: Any, qid: int, verb: str, body: dict[str, Any], wake: Waker) -> tuple[int, object]:
    """The owner answers question `qid` (D-300): "answer" a plain one, "decide" one about a decision
    entry. Same JSON, size and rate rules as the box; a message the answer writes counts as one."""
    extra = set(body) - QUESTION_FIELDS[verb]
    if extra:
        raise HTTPError(400, f"unknown field(s) {sorted(extra)}")
    typed = any(isinstance(body.get(k), str) and body[k].strip() for k in ("text", "note"))
    if (verb == "answer" or typed) and not inbox.rate_ok(conn):
        raise HTTPError(429, f"more than {inbox.RATE_MAX} messages in the last hour; wait a little")
    if verb == "answer":
        r = actions.perform(conn, "question.answer",
                            {"task_id": qid, **{k: body[k] for k in ("answer", "option") if k in body}},
                            actor=ACTOR, via=VIA)
    else:
        r = actions.perform(conn, "question.decide",
                            {"task_id": qid, "choice": body.get("choice"), "decision_version": body.get("version"),
                             **{k: body[k] for k in ("text", "note") if k in body}}, actor=ACTOR, via=VIA)
    mid = r.get("inbox")
    log(f"question #{qid}: {'answered' if verb == 'answer' else 'decided'} by the owner" + (f", inbox #{mid}" if mid else ""))
    if verb == "answer" and mid:
        wake.set()
    return 200, {"question": qid, "status": "done", "inbox": mid}


def handle(method: str, path: str, headers: Mapping[str, str], raw: bytes | None, key_file: str,
           wake: Waker) -> Response:
    """One request after the body was read: the key check, a DB connection, route(), and the error
    mapping (a store error that is the caller's is a 4xx, the rest a 503 with the class and message)."""
    try:
        check_key(headers, key_file)
        conn = store.connect()
        try:
            status, body = route(conn, method, path, headers, raw, wake)
        finally:
            conn.close()
        return reply(status, body)
    except HTTPError as e:
        return reply(e.status, {"error": e.message})
    except actions.Forbidden as e:
        return reply(403, {"error": str(e)})
    except store.NotFound as e:
        return reply(404, {"error": e.args[0] if e.args else "not found"})
    except store.InvalidTransition as e:
        return reply(409, {"error": str(e)})
    except ValueError as e:
        return reply(400, {"error": str(e)})
    except Exception as e:   # noqa: BLE001 - the DB error path (already reported) and anything else
        log(f"ERROR {method} {path}: {type(e).__name__}: {e}")
        return reply(503, {"error": f"{type(e).__name__}: {str(e)[:300]}"})


def routes(key_file: str, wake: Waker) -> list[Route]:
    """The inbox API as Starlette routes: one catch-all, because the old server answered every path
    with the same JSON errors (401 before 404, 405 for any other method, JSON bodies throughout)."""
    async def endpoint(request: Request) -> Response:
        if request.method not in ("GET", "POST"):
            return reply(405, {"error": "method not allowed"})
        headers = {k.lower(): v for k, v in request.headers.items()}
        if request.method == "POST":     # the key first: an unauthenticated caller's body is never waited for (review k4a L1)
            try:
                check_key(headers, key_file)
            except HTTPError as e:
                return reply(e.status, {"error": e.message})
        raw = await read_body(request) if request.method == "POST" else None
        return await run_in_threadpool(handle, request.method, request.url.path, headers, raw, key_file, wake)

    return [Route("/{path:path}", endpoint, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])]


def app(key_file: str, wake: Waker) -> Starlette:
    return Starlette(routes=routes(key_file, wake))
