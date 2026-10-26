#!/usr/bin/env python3
"""compact_timing.py: when to compact the task-manager session, and what its context costs
(D-237, D-238; docs/dashboard_design/05_runners.md §5.5).

Every request of a Claude Code session re-sends the whole conversation. Within the prompt cache's
lifetime (1 hour for the main conversation on a subscription) that is a cheap cache read; the
first request after a longer break re-writes the whole context into the cache (a "re-read"),
which costs ~25x as much per token. So a big context is expensive mainly through re-reads, and
the time to compact it is while the cache is still warm (D-238):
  (a) when a keep-alive run ends (D-224), and
  (b) after IDLE_AFTER (50 min) without a request, shortly before the cache expires (this also
      covers the owner's daytime chats),
each only if the context is at or above the break-even below (below it, no compaction: "maybe
compacting isn't necessary at all after just questions"). keepalive.compaction_pass() runs the
triggers in the watcher; this module holds the pure parts: the cost model, the transcript
reading, the break-even and the decision, and the quickview's context block (D-237).

The cost model (a telemetry record, kind usage.cost_model, table cost_model; written only by the
audited action cost_model.set): session-% per 1M tokens of the reference model (Opus 5.5) per
token kind (cache read, cache write, uncached input, output), weekly-% per session-%, the cache
lifetime and the compaction's token sizes. Without a stored model the built-in one is used: the
calibration of the compaction evaluation (14.10.2026: a regression of the 15-minute /usage
samples on the transcript tokens, R² 0.92; three single re-reads checked against the measured
jump). `python3 compact_timing.py fit [--store]` re-fits it from the samples and the transcripts
(the monthly usage review runs it); `model` prints the one in use.

Break-even (tokens). Compacting a context of C tokens now costs (all while the cache is warm):
    cost(C) = C·r                (the summarization request reads the context from the cache)
            + (S + H)·o          (the summary and the hand-off the session writes, output)
            + B·w                (the compacted context, B tokens, written into the cache)
and saves, at the next re-read after a break, (C − B)·w (re-reading B instead of C). It pays off
from the first later re-read on when (C − B)·w ≥ cost(C), i.e. from
    C* = ((S + H)·o + 2·B·w) / (w − r)
r, w, o: the model's cache-read / cache-write / output weights; B: the context right after a
compaction (system prompt, tools, summary, hand-off and rules; ~57k measured), S: the summary
(~12k output tokens), H: the hand-off turn's output (~2k). With the built-in model C* ≈ 177k
(D-238: "today ≈180k"), and cost(C*) ≈ 1.8 session-%. The save turn's own tool calls (commit,
PROGRESS) are part of H; the decision ignores them.

Transcript conventions (keepalive.iter_entries reads the file): a request is an assistant entry
with `message.usage` (deduplicated by requestId: a request's content blocks are separate entries);
its context = input + cache_creation + cache_read tokens; a re-read is a request with a context
of at least REREAD_MIN_TOKENS whose cache_creation is at least REREAD_FRACTION of its context
(D-237). A prompt is a non-sidechain user entry with text (not a tool result); an owner prompt is
one with origin kind "human" that is not a keep-alive message (KEEPALIVE_PREFIXES: those are
typed into tmux too).
"""
from __future__ import annotations

import glob
import json
import math
import os
import sqlite3
import statistics
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
KIND = "usage.cost_model"
SCHEMA = "afclaude.cost_model/1"
REFERENCE_MODEL = "claude-opus-5-5"
REREAD_MIN_TOKENS = 40_000          # a smaller context is never counted as a re-read
REREAD_FRACTION = 0.8               # cache_creation >= this x the context = a re-read (D-237)
IDLE_AFTER = timedelta(minutes=50)  # D-238 (b): compact after this long without a request
SEND_MARGIN = timedelta(minutes=3)  # the save prompt must go out at least this long before the cache expires
RUN_END_SLACK = timedelta(seconds=5)
KEEPALIVE_PREFIXES = ("[keep-alive]", "This session was compacted", "You are the long-running", "[AFClaude",
                      "/compact", "<command-name>/compact")

# USD per 1M tokens: (input, 5-min cache write, 1-hour cache write, cache read, output). The
# prices the evaluation's calibration used (14.10.2026); a fit converts every model's tokens to
# API dollars with them, and the weights of a non-reference model are the reference weights
# scaled by these ratios. A model missing here fails the fit (add it; no guessed price).
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.2, 20.0),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.5, 25.0),
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.1, 10.0),
    "claude-sonnet-5": (2.0, 2.5, 4.0, 0.2, 10.0),
    "claude-haiku-5-5": (0.1, 0.125, 0.2, 0.01, 0.5),
    "claude-haiku-4-5-20251001": (1.0, 1.25, 2.0, 0.1, 5.0),
}
P_IN, P_W5, P_W1H, P_READ, P_OUT = range(5)

BUILTIN: dict[str, Any] = {
    "schema": SCHEMA,
    "source": "built-in: the compaction evaluation of 2026-10-14 (15-min /usage samples vs transcript tokens)",
    "fitted_at": "2026-10-14T12:00:00+00:00",
    "reference_model": REFERENCE_MODEL,
    # session-% per 1M tokens of the reference model
    "weights": {"cache_read": 0.61, "cache_write": 15.3, "input": 12.2, "output": 61.0},
    "weekly_per_session": 0.157,
    "cache_ttl_min": 60,
    "compaction": {"base_tokens": 57_000, "summary_tokens": 12_000, "handoff_tokens": 2_000},
    "fit": {"n": 206, "r2": 0.92, "const_pct": 1.3},
}
WEIGHT_KEYS = ("cache_read", "cache_write", "input", "output")
COMPACTION_KEYS = ("base_tokens", "summary_tokens", "handoff_tokens")


class FitError(ValueError):
    """The samples or transcripts don't support a fit (too few intervals, a bad fit, an unpriced model)."""


# ---------------------------------------------------------------- the cost model

def _positive(x: object, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)) or float(x) <= 0:
        raise ValueError(f"the cost model's {what} must be a positive number, got {x!r}")
    return float(x)


def validate(doc: object) -> dict[str, Any]:
    """The checked model document (a copy). ValueError if a field is missing or not usable."""
    if not isinstance(doc, Mapping):
        raise ValueError("the cost model is a JSON object")
    d: dict[str, Any] = json.loads(json.dumps(dict(doc)))
    if d.get("schema") != SCHEMA:
        raise ValueError(f"the cost model's schema must be {SCHEMA!r}, got {d.get('schema')!r}")
    if d.get("reference_model") not in PRICES:
        raise ValueError(f"the cost model's reference_model must be a priced model, got {d.get('reference_model')!r}")
    w = d.get("weights")
    c = d.get("compaction")
    if not isinstance(w, dict) or not isinstance(c, dict):
        raise ValueError("the cost model needs 'weights' and 'compaction' objects")
    for k in WEIGHT_KEYS:
        _positive(w.get(k), f"weights.{k}")
    for k in COMPACTION_KEYS:
        _positive(c.get(k), f"compaction.{k}")
    _positive(d.get("weekly_per_session"), "weekly_per_session")
    _positive(d.get("cache_ttl_min"), "cache_ttl_min")
    if float(w["cache_write"]) <= float(w["cache_read"]):
        raise ValueError("the cost model's cache write weight must be above its cache read weight")
    return d


def builtin() -> dict[str, Any]:
    return validate(BUILTIN)


def load(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """The model in use: the stored one (the DB's cost_model record), else the built-in one
    (marked by its `source`). A DB that can't be read, or a stored model that isn't valid,
    raises (store.DBError / ValueError): never a silent fall back to the built-in one."""
    own = conn is None
    c = store.connect() if conn is None else conn
    try:
        row = store.retrying(lambda: c.execute("SELECT payload FROM cost_model WHERE account_id=? AND doc_key='model'",
                                               (store.DEFAULT_ACCOUNT,)).fetchone(), c)
    finally:
        if own:
            c.close()
    if row is None:
        return builtin()
    return validate(json.loads(row[0]))


def store_model(doc: Mapping[str, Any], actor: str = "cli", via: str = "cli") -> dict[str, Any]:
    """Replace the stored model (the audited action cost_model.set). Raises store.DBError / ValueError."""
    import actions
    conn = store.connect()
    try:
        out: dict[str, Any] = actions.perform(conn, "cost_model.set", {"doc": dict(doc)}, actor=actor, via=via)
        return out
    finally:
        conn.close()


def _price(model: str) -> tuple[float, float, float, float, float]:
    p = PRICES.get(model)
    if p is None:
        raise FitError(f"no price for model {model!r} in compact_timing.PRICES (add it)")
    return p


def per_token(m: Mapping[str, Any], model: Optional[str] = None) -> dict[str, float]:
    """session-% per token by kind for `model` (default: the reference model): the reference
    weights scaled by the API price ratios (a 5-minute cache write by its own price)."""
    ref = _price(str(m["reference_model"]))
    p = _price(model) if model and model != m["reference_model"] else ref
    w = m["weights"]
    return {"read": float(w["cache_read"]) * p[P_READ] / ref[P_READ] / 1e6,
            "write_1h": float(w["cache_write"]) * p[P_W1H] / ref[P_W1H] / 1e6,
            "write_5m": float(w["cache_write"]) * p[P_W5] / ref[P_W1H] / 1e6,
            "input": float(w["input"]) * p[P_IN] / ref[P_IN] / 1e6,
            "output": float(w["output"]) * p[P_OUT] / ref[P_OUT] / 1e6}


def break_even(m: Mapping[str, Any]) -> int:
    """C* in tokens (see the module doc): ((S + H)·o + 2·B·w) / (w − r)."""
    k = per_token(m)
    c = m["compaction"]
    out_tokens = float(c["summary_tokens"]) + float(c["handoff_tokens"])
    return int(round((out_tokens * k["output"] + 2 * float(c["base_tokens"]) * k["write_1h"])
                     / (k["write_1h"] - k["read"])))


def compaction_cost(m: Mapping[str, Any], context: float) -> float:
    """Session-% a warm compaction of `context` tokens costs (the module doc's cost(C))."""
    k = per_token(m)
    c = m["compaction"]
    return (context * k["read"] + (float(c["summary_tokens"]) + float(c["handoff_tokens"])) * k["output"]
            + float(c["base_tokens"]) * k["write_1h"])


def reread_cost(m: Mapping[str, Any], tokens: float) -> float:
    """Session-% a full re-read (cache write) of `tokens` costs."""
    return tokens * per_token(m)["write_1h"]


def weekly(m: Mapping[str, Any], session_pct: float) -> float:
    return session_pct * float(m["weekly_per_session"])


# ---------------------------------------------------------------- the transcript

def _iter_entries(path: str) -> Iterator[dict[str, Any]]:
    """keepalive.iter_entries (the keep-alive's transcript reader; keepalive.py is untyped)."""
    import keepalive
    fn: Callable[[str], Iterable[dict[str, Any]]] = getattr(keepalive, "iter_entries")
    for e in fn(path):
        if isinstance(e, dict):
            yield e


def _ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _int(x: object) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


@dataclass
class Request:
    ts: datetime
    model: str
    input: int = 0
    write: int = 0          # cache_creation_input_tokens
    write_5m: int = 0       # ... of it with the 5-minute lifetime
    read: int = 0           # cache_read_input_tokens
    output: int = 0

    @property
    def context(self) -> int:
        return self.input + self.write + self.read

    @property
    def reread(self) -> bool:
        return self.context >= REREAD_MIN_TOKENS and self.write >= REREAD_FRACTION * self.context

    def cost(self, m: Mapping[str, Any]) -> dict[str, float]:
        """Session-% by part: write (all cache writes), read, input, output."""
        k = per_token(m, self.model)
        return {"write": (self.write - self.write_5m) * k["write_1h"] + self.write_5m * k["write_5m"],
                "read": self.read * k["read"], "input": self.input * k["input"], "output": self.output * k["output"]}

    def dollars(self) -> dict[str, float]:
        """API dollars by part (the fit's features): read, write, io (input + output)."""
        p = _price(self.model)
        return {"read": self.read * p[P_READ] / 1e6,
                "write": ((self.write - self.write_5m) * p[P_W1H] + self.write_5m * p[P_W5]) / 1e6,
                "io": (self.input * p[P_IN] + self.output * p[P_OUT]) / 1e6}


@dataclass
class Prompt:
    ts: datetime
    kind: str               # owner | keepalive | summary (a compaction summary) | other


@dataclass
class Scan:
    requests: list[Request] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)
    compactions: list[datetime] = field(default_factory=list)   # compact_boundary times


def request_of(e: Mapping[str, Any]) -> Optional[tuple[str, Request]]:
    """(dedup key, Request) for an assistant entry carrying usage, else None."""
    if e.get("type") != "assistant":
        return None
    msg = e.get("message")
    if not isinstance(msg, dict):
        return None
    u = msg.get("usage")
    model = msg.get("model")
    ts = _ts(e.get("timestamp"))
    if not isinstance(u, dict) or not isinstance(model, str) or model == "<synthetic>" or ts is None:
        return None
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    assert isinstance(cc, dict)
    write = _int(u.get("cache_creation_input_tokens"))
    r = Request(ts=ts, model=model, input=_int(u.get("input_tokens")), write=write,
                write_5m=min(write, _int(cc.get("ephemeral_5m_input_tokens"))),
                read=_int(u.get("cache_read_input_tokens")), output=_int(u.get("output_tokens")))
    key = e.get("requestId") or msg.get("id") or e.get("uuid") or ts.isoformat()
    return str(key), r


def _text(e: Mapping[str, Any]) -> Optional[str]:
    """A user entry's prompt text, None for a tool result or no text."""
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        parts = [str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts) if parts else None
    return None


def prompt_kind(e: Mapping[str, Any], text: str) -> str:
    if e.get("isCompactSummary"):
        return "summary"
    if text.lstrip().startswith(KEEPALIVE_PREFIXES):
        return "keepalive"
    origin = e.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind == "human" and not e.get("isMeta"):
        return "owner"
    return "other"


def scan(path: str) -> Scan:
    """The main session's requests, prompts and compactions (sidechain entries are skipped; a
    subagent writes its own transcript). OSError if the file can't be read."""
    reqs: dict[str, Request] = {}
    out = Scan()
    for e in _iter_entries(path):
        if e.get("isSidechain"):
            continue
        t = e.get("type")
        if t == "assistant":
            got = request_of(e)
            if got:
                key, r = got
                old = reqs.get(key)
                if old is None or r.output >= old.output:
                    reqs[key] = r
        elif t == "user":
            text = _text(e)
            ts = _ts(e.get("timestamp"))
            if text is not None and ts is not None and not text.startswith("[Request interrupted"):
                out.prompts.append(Prompt(ts, prompt_kind(e, text)))
        elif t == "system" and e.get("subtype") == "compact_boundary":
            ts = _ts(e.get("timestamp"))
            if ts:
                out.compactions.append(ts)
    out.requests = sorted(reqs.values(), key=lambda r: r.ts)
    out.prompts.sort(key=lambda p: p.ts)
    out.compactions.sort()
    return out


_CACHE: dict[str, tuple[tuple[float, int], Scan]] = {}


def scan_cached(path: str) -> Scan:
    """scan(), re-read only when the file changed (the watcher looks every minute)."""
    st = os.stat(path)
    sig = (st.st_mtime, st.st_size)
    hit = _CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    s = scan(path)
    _CACHE[path] = (sig, s)
    return s


# ---------------------------------------------------------------- the context now

def context_state(s: Scan, m: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The context the next message re-sends and how long the cache holds it:
    context (tokens: the last request's context + its reply; after a compaction with no request
    since, the model's base_tokens, `estimated`), last_request_at, cache_expires_at, minutes_left,
    warm, reread_cost / weekly (session-% / weekly-% a re-read of it costs), break_even, above."""
    be = break_even(m)
    if not s.requests:
        return {"context": None, "last_request_at": None, "break_even": be, "above": False}
    last = s.requests[-1]
    ctx, estimated = last.context + last.output, False
    if s.compactions and s.compactions[-1] > last.ts:
        ctx, estimated = int(m["compaction"]["base_tokens"]), True
    expires = last.ts + timedelta(minutes=float(m["cache_ttl_min"]))
    cost = reread_cost(m, ctx)
    return {"context": ctx, "estimated": estimated, "last_request_at": last.ts, "cache_expires_at": expires,
            "minutes_left": max(0.0, (expires - now).total_seconds() / 60), "warm": now < expires,
            "reread_cost": cost, "reread_weekly": weekly(m, cost), "break_even": be, "above": ctx >= be,
            "compaction_cost": compaction_cost(m, ctx)}


@dataclass
class Decision:
    due: bool
    trigger: Optional[str]      # run-end | idle (when due)
    reason: str
    key: Optional[str] = None   # the attempt's dedup key: the last request's time
    missed: bool = False        # above the break-even, but the cache expired before a compaction


def decide(now: datetime, state: Mapping[str, Any], *, run_using: bool, run_ended: bool,
           busy: Optional[str]) -> Decision:
    """D-238's triggers on the context state (context_state). run_using: a keep-alive run still
    uses the session (keepalive.run_active); run_ended: the latest run is over and the session's
    last prompt was the run's, not the owner's (keepalive.run_ended); busy: why the session can't
    take a message now (a turn running, a subagent working), else None. Order: no requests,
    below the break-even, a run using it, busy, the cache (about to be) expired, then run-end
    (at once) or idle (IDLE_AFTER since the last request)."""
    last = state.get("last_request_at")
    if state.get("context") is None or not isinstance(last, datetime):
        return Decision(False, None, "no request in the transcript yet")
    key = f"compact-{last.astimezone(UTC).isoformat()}"
    ctx, be = int(state["context"]), int(state["break_even"])
    if ctx < be:
        return Decision(False, None, f"context {ctx / 1000:.0f}k below the break-even {be / 1000:.0f}k: "
                                     "no compaction (D-238)", key)
    if run_using:
        return Decision(False, None, "a keep-alive run is using the session", key)
    if busy:
        return Decision(False, None, busy, key)
    expires = state["cache_expires_at"]
    if now >= expires - SEND_MARGIN:
        return Decision(False, None, f"the prompt cache expired{'' if now >= expires else ' too soon'} "
                                     f"({expires.astimezone(BERLIN):%H:%M}): a compaction now would re-read the "
                                     "context first, so none", key, missed=True)
    if run_ended:
        return Decision(True, "run-end", "the run ended (D-224)", key)
    idle = now - last
    if idle >= IDLE_AFTER:
        return Decision(True, "idle", f"idle {idle.total_seconds() / 60:.0f} min, the cache expires "
                                      f"{expires.astimezone(BERLIN):%H:%M}", key)
    return Decision(False, None, f"idle {idle.total_seconds() / 60:.0f} min: compacts at "
                                 f"{(last + IDLE_AFTER).astimezone(BERLIN):%H:%M} if still idle", key)


def last_prompt_kind(s: Scan, since: Optional[datetime] = None) -> Optional[str]:
    ps = [p for p in s.prompts if since is None or p.ts >= since]
    return ps[-1].kind if ps else None


# ---------------------------------------------------------------- the quickview block (D-237)

def turns(s: Scan) -> list[tuple[Prompt, list[Request]]]:
    """Each prompt with the requests it caused (up to the next prompt)."""
    out: list[tuple[Prompt, list[Request]]] = []
    reqs = s.requests
    i = 0
    for n, p in enumerate(s.prompts):
        end = s.prompts[n + 1].ts if n + 1 < len(s.prompts) else None
        while i < len(reqs) and reqs[i].ts < p.ts:
            i += 1
        j = i
        while j < len(reqs) and (end is None or reqs[j].ts < end):
            j += 1
        out.append((p, reqs[i:j]))
        i = j
    return out


def _r(x: float, nd: int = 2) -> float:
    return round(x, nd)


def owner_messages(s: Scan, m: Mapping[str, Any], since: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """Per owner message (newest first, at most `limit`, no text): its time, the gap since the
    session's previous request, whether its first request re-read the context and how many
    tokens that wrote, and the whole turn's est. session-% / weekly-%."""
    out = []
    prev: Optional[datetime] = None
    req_times = [r.ts for r in s.requests]
    for p, reqs in turns(s):
        if p.kind != "owner" or p.ts < since:
            continue
        before = [t for t in req_times if t < p.ts]
        prev = before[-1] if before else None
        first = reqs[0] if reqs else None
        cost = sum(sum(r.cost(m).values()) for r in reqs)
        out.append({"at": p.ts.isoformat(), "at_berlin": p.ts.astimezone(BERLIN).strftime("%a %d.%m. %H:%M"),
                    "gap_min": round((p.ts - prev).total_seconds() / 60) if prev else None,
                    "reread": bool(first and first.reread), "reread_tokens": first.write if first and first.reread else 0,
                    "context": first.context if first else None, "requests": len(reqs),
                    "session_pct": _r(cost), "weekly_pct": _r(weekly(m, cost), 3)})
    return list(reversed(out))[:limit]


def daily_split(s: Scan, m: Mapping[str, Any], since: datetime) -> list[dict[str, Any]]:
    """Per Berlin day: the main session's est. session-% split into re-reads (the cache writes
    of re-read requests), warm reads (cache reads), other writes (+ uncached input) and output."""
    days: dict[str, dict[str, float]] = {}
    for r in s.requests:
        if r.ts < since:
            continue
        d = days.setdefault(r.ts.astimezone(BERLIN).date().isoformat(),
                            {"rereads": 0.0, "warm_reads": 0.0, "writes": 0.0, "output": 0.0, "requests": 0, "n_rereads": 0})
        c = r.cost(m)
        if r.reread:
            d["rereads"] += c["write"]
            d["n_rereads"] += 1
        else:
            d["writes"] += c["write"]
        d["writes"] += c["input"]
        d["warm_reads"] += c["read"]
        d["output"] += c["output"]
        d["requests"] += 1
    out = []
    for day in sorted(days):
        d = days[day]
        total = d["rereads"] + d["warm_reads"] + d["writes"] + d["output"]
        out.append({"day": day, **{k: (_r(v, 1) if k not in ("requests", "n_rereads") else int(v)) for k, v in d.items()},
                    "total": _r(total, 1), "weekly": _r(weekly(m, total), 2)})
    return out


def context_block(path: Optional[str], now: datetime, m: Optional[Mapping[str, Any]] = None, days: int = 7,
                  messages: int = 20) -> dict[str, Any]:
    """The quickview's task-manager context block (D-237). Raises on a read failure (the caller
    shows the error; a display path never pauses, D-239)."""
    m = m if m is not None else load()
    if not path:
        return {"error": "no transcript of the task-manager session"}
    s = scan_cached(path)
    st = context_state(s, m, now)
    since = now - timedelta(days=days)

    def iso(x: object) -> object:
        return x.isoformat() if isinstance(x, datetime) else x
    live = {k: (iso(v) if not isinstance(v, float) else _r(v)) for k, v in st.items()}
    for k in ("last_request_at", "cache_expires_at"):
        v = st.get(k)
        if isinstance(v, datetime):
            live[k + "_berlin"] = v.astimezone(BERLIN).strftime("%a %d.%m. %H:%M")
    return {"live": live, "messages": owner_messages(s, m, since, messages), "days": daily_split(s, m, since),
            "model": {"source": m.get("source"), "fitted_at": m.get("fitted_at"), "weights": m["weights"],
                      "weekly_per_session": m["weekly_per_session"], "break_even": break_even(m),
                      "fit": m.get("fit")}}


# ---------------------------------------------------------------- the fit (the monthly usage review)

def all_requests(projects_dir: str, since: datetime) -> list[Request]:
    """Every request in every transcript under projects_dir (main sessions and subagents) at or
    after `since` (files not modified since then are skipped)."""
    out: list[Request] = []
    cutoff = since.timestamp()
    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        reqs: dict[str, Request] = {}
        for e in _iter_entries(path):
            got = request_of(e)
            if got and got[1].ts >= since:
                key, r = got
                old = reqs.get(key)
                if old is None or r.output >= old.output:
                    reqs[key] = r
        out.extend(reqs.values())
    out.sort(key=lambda r: r.ts)
    return out


def read_samples(since: datetime, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
    """The non-stale /usage samples (usage_samples) at or after `since`, oldest first. A DB
    error raises."""
    own = conn is None
    c = store.connect() if conn is None else conn
    try:
        rows = store.retrying(lambda: c.execute(
            "SELECT payload FROM usage_samples WHERE account_id=? AND stale=0 AND ts>=? ORDER BY ts, id",
            (store.DEFAULT_ACCOUNT, store.telemetry_ts(since.isoformat()))).fetchall(), c)
    finally:
        if own:
            c.close()
    out = []
    for (p,) in rows:
        try:
            d = json.loads(p)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _meter(row: Mapping[str, Any], kind: str) -> tuple[Optional[float], Optional[datetime]]:
    u = row.get("usage")
    k = u.get(kind) if isinstance(u, dict) else None
    if not isinstance(k, dict) or not isinstance(k.get("percent"), (int, float)):
        return None, None
    return float(k["percent"]), _ts(str(k.get("resets_at") or "").replace(" ", "T"))


def _row_time(row: Mapping[str, Any]) -> Optional[datetime]:
    u = row.get("usage")
    f = u.get("fetched_at") if isinstance(u, dict) else None
    return _ts(str(f).replace(" ", "T")) if f else _ts(row.get("at"))


def intervals(samples: Sequence[Mapping[str, Any]], reqs: Sequence[Request], min_gap: timedelta = timedelta(minutes=5),
              max_gap: timedelta = timedelta(minutes=25)) -> list[dict[str, Any]]:
    """Consecutive sample pairs in the same session window (the reset unchanged, 10 min of
    slack) with a session rise >= 0, each with the API dollars (read / write / io) of every
    request between them and the weekly rise. Only intervals with tokens ("active") are kept."""
    pts = []
    for row in samples:
        t = _row_time(row)
        sp, sr = _meter(row, "session")
        wp, _ = _meter(row, "weekly")
        if t and sp is not None and sr is not None and wp is not None:
            pts.append((t, sp, sr, wp))
    pts.sort(key=lambda p: p[0])
    out = []
    i = 0
    for (ta, sa, ra, wa), (tb, sb, rb, wb) in zip(pts, pts[1:]):
        if not (min_gap <= tb - ta <= max_gap) or abs((rb - ra).total_seconds()) > 600 or sb < sa:
            continue
        while i < len(reqs) and reqs[i].ts <= ta:
            i += 1
        j = i
        f = {"read": 0.0, "write": 0.0, "io": 0.0}
        while j < len(reqs) and reqs[j].ts <= tb:
            for k, v in reqs[j].dollars().items():
                f[k] += v
            j += 1
        if sum(f.values()) > 0:
            out.append({"from": ta, "to": tb, "d_session": sb - sa, "d_weekly": wb - wa, **f})
    return out


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. FitError if singular."""
    n = len(b)
    mtx = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(mtx[r][c]))
        if abs(mtx[p][c]) < 1e-12:
            raise FitError("the fit's normal equations are singular (too little variation in the data)")
        mtx[c], mtx[p] = mtx[p], mtx[c]
        for r in range(n):
            if r != c:
                f = mtx[r][c] / mtx[c][c]
                mtx[r] = [x - f * y for x, y in zip(mtx[r], mtx[c])]
    return [mtx[i][n] / mtx[i][i] for i in range(n)]


def fit(samples: Sequence[Mapping[str, Any]], reqs: Sequence[Request], prev: Mapping[str, Any], now: datetime,
        main_scans: Sequence[Scan] = (), min_n: int = 50, min_r2: float = 0.5) -> dict[str, Any]:
    """A new model from the samples and the requests: OLS of each interval's session rise on its
    API dollars by kind, with a constant (rounding, unlogged use):
        d_session = a·read$ + b·write$ + c·io$ + k
    -> weights per 1M reference tokens: cache_read = a·price(read), cache_write = b·price(1h write),
    input = c·price(input), output = c·price(output); weekly_per_session = Σ d_weekly / Σ d_session;
    compaction.base_tokens = the median context of the first request after each compaction in
    `main_scans` (else the previous value; summary/hand-off sizes are kept). FitError if fewer
    than min_n intervals, a coefficient <= 0 or R² < min_r2: nothing is stored then."""
    iv = intervals(samples, reqs)
    if len(iv) < min_n:
        raise FitError(f"only {len(iv)} active sample intervals (need {min_n})")
    xs = [[x["read"], x["write"], x["io"], 1.0] for x in iv]
    ys = [float(x["d_session"]) for x in iv]
    cols = list(zip(*xs))
    a = [[sum(p * q for p, q in zip(ci, cj)) for cj in cols] for ci in cols]
    b = [sum(p * y for p, y in zip(ci, ys)) for ci in cols]
    coef = _solve(a, b)
    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - sum(c * x for c, x in zip(coef, row))) ** 2 for row, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if any(c <= 0 for c in coef[:3]):
        raise FitError(f"a non-positive coefficient (session-% per API dollar read/write/io = "
                       f"{coef[0]:.3g}/{coef[1]:.3g}/{coef[2]:.3g}): the data can't separate the token kinds")
    if r2 < min_r2:
        raise FitError(f"R² {r2:.2f} is below {min_r2} (n={len(iv)})")
    ds = sum(x["d_session"] for x in iv if x["d_session"] > 0)
    dw = sum(max(0.0, x["d_weekly"]) for x in iv if x["d_session"] > 0)
    if ds <= 0:
        raise FitError("no session rise in the sample intervals")
    ref = _price(str(prev.get("reference_model") or REFERENCE_MODEL))
    comp = dict(prev["compaction"])
    firsts = []
    for s in main_scans:
        for t in s.compactions:
            nxt = next((r for r in s.requests if r.ts > t), None)
            if nxt is not None:
                firsts.append(nxt.context)
    if firsts:
        comp["base_tokens"] = int(statistics.median(firsts))
    return validate({
        "schema": SCHEMA, "source": f"fit of {len(iv)} 15-min sample intervals", "fitted_at": now.isoformat(),
        "reference_model": prev.get("reference_model") or REFERENCE_MODEL,
        "weights": {"cache_read": _r(coef[0] * ref[P_READ], 4), "cache_write": _r(coef[1] * ref[P_W1H], 3),
                    "input": _r(coef[2] * ref[P_IN], 3), "output": _r(coef[2] * ref[P_OUT], 2)},
        "weekly_per_session": _r(dw / ds, 4), "cache_ttl_min": prev.get("cache_ttl_min", 60),
        "compaction": comp,
        "fit": {"n": len(iv), "r2": _r(r2, 3), "const_pct": _r(coef[3], 3),
                "pct_per_dollar": {"read": _r(coef[0], 4), "write": _r(coef[1], 4), "io": _r(coef[2], 4)},
                "data_from": iv[0]["from"].isoformat(), "data_to": iv[-1]["to"].isoformat(),
                "base_from_compactions": len(firsts)},
    })


def run_fit(days: int, projects_dir: str, main_session: Optional[str], now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=days)
    prev = load()
    reqs = all_requests(projects_dir, since)
    scans = []
    if main_session:
        hits = glob.glob(os.path.join(projects_dir, "*", f"{main_session}.jsonl"))
        scans = [scan(p) for p in hits]
    return fit(read_samples(since), reqs, prev, now, scans)


def summary(m: Mapping[str, Any]) -> str:
    be = break_even(m)
    w = m["weights"]
    return (f"cost model ({m.get('source')}, fitted {m.get('fitted_at')}): per 1M {m['reference_model']} tokens "
            f"cache read {w['cache_read']} / cache write {w['cache_write']} / input {w['input']} / output "
            f"{w['output']} session-%, weekly = session x {m['weekly_per_session']}; break-even {be / 1000:.0f}k tokens "
            f"(a compaction there ~{compaction_cost(m, be):.1f} session-%)")


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="The task-manager's compaction timing and context cost (D-237/D-238).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("model", help="print the cost model in use and the break-even")
    f = sub.add_parser("fit", help="re-fit the cost model from the /usage samples and the transcripts")
    f.add_argument("--days", type=int, default=28, help="data window (default 28 days)")
    f.add_argument("--projects-dir", default=os.environ.get("KEEPALIVE_PROJECTS_DIR",
                                                            os.path.expanduser("~/.claude/projects")))
    f.add_argument("--store", action="store_true", help="store it (the audited action cost_model.set)")
    c = sub.add_parser("context", help="the task-manager's context, cache and per-message costs (JSON)")
    c.add_argument("--session", default=None, help="session id (default: the manager session)")
    c.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    now = datetime.now(UTC)
    try:
        if args.cmd == "model":
            m = load()
            print(json.dumps(m, indent=1))
            print(summary(m))
        elif args.cmd == "fit":
            import afclaude_config
            m = run_fit(args.days, args.projects_dir, afclaude_config.manager_session(), now)
            print(json.dumps(m, indent=1))
            print(summary(m))
            if args.store:
                r = store_model(m)
                print(f"stored ({r.get('status')})")
            else:
                print("not stored (pass --store)")
        else:
            import afclaude_config
            import keepalive
            sid = args.session or afclaude_config.manager_session()
            path = getattr(keepalive, "transcript_path")(sid)
            print(json.dumps(context_block(path, now, days=args.days), indent=1, default=str))
    except FitError as e:
        print(f"fit failed: {e}", file=sys.stderr)
        return 1
    except store.DBError as e:
        print(store.handback_text(e) if e.not_saved else f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
