#!/usr/bin/env python3
"""usage_split.py: the pure core of goal 12's usage split (docs/usage_split_plan.md §0, §3.1, §3.2, steps S2/S3;
D-138, D-140, D-266, D-308; mypy --strict, D-209). No DB, no files, no clock: the sampler (S2/S3) feeds it
transcript entries, typed-message rows and cost-model records as plain data.

  - `driver_of()`: who drove a session over time (auto = AFClaude typed or launched the prompt, owner =
    anything else that is a real prompt). A prompt matches a `typed_messages` row (same session, equal hash
    of the whitespace-normalised text, sent at most MATCH_WINDOW before the entry) -> that row's driver; no
    match -> owner. `driver_of_prefix()` is the history matcher for transcripts older than the record: the
    keep-alive prefix list decides (owner_words.KEEPALIVE_PREFIXES, a delivered inbox message is the owner's).
    Both return a `DriverTrack` carrying its `source` ("typed" | "prefix": never mixed silently);
    `DriverTrack.at(ts)` is the driver in force at a time, which is what a subagent's request (its own
    transcript) takes from its parent session.
  - `cost_of()`: one request's modeled cost in session-% from compact_timing's cost model (`per_token`, the
    API-price features by token kind, the 5-min and 1-h cache writes apart). A model with no price raises
    `UnpricedModel` (D-235): no default price.
  - `category_of()`: session class x driver -> the §0 table's category. `other_devices` is the residual no
    local transcript explains; it never comes out of `category_of`.

Real prompt = a non-sidechain, non-meta user entry with text that is not a tool result, an interrupt marker,
a compaction summary, a local command or a task notification, and whose origin kind is human or absent. A
`peer` or `task-notification` origin, a compaction summary and a local command (`/compact`, `<command-...>`,
`<local-command-...>`) never change the driver. An origin kind this module doesn't know raises
`SplitError` (D-235: new kinds must be classified, not guessed).
"""
from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compact_timing  # noqa: E402
import owner_words  # noqa: E402

UTC = timezone.utc
MATCH_WINDOW = timedelta(minutes=10)     # a typed row matches an entry at most this long after the send (§3.1)
SOURCE_TYPED = "typed"
SOURCE_PREFIX = "prefix"
HUMAN_ORIGINS = (None, "human")          # origin kinds of a prompt somebody typed
NOT_A_PROMPT_ORIGINS = ("peer", "task-notification")   # delivered by Claude Code, not typed by a driver
# Text starts of the entries that are local commands or notifications, not prompts (the sampler's list).
NOT_A_PROMPT_TEXT = ("<local-command", "<command-", "<system-reminder", "<task-notification",
                     owner_words.INTERRUPT_PREFIX, "/compact")

Driver = Literal["auto", "owner"]
SessionClass = Literal["afclaude", "owner"]
Category = Literal["run", "continued", "design", "user_local", "other_devices"]
DRIVERS: tuple[Driver, ...] = ("auto", "owner")
CATEGORIES: tuple[Category, ...] = ("run", "continued", "design", "user_local", "other_devices")
_CATEGORY: dict[tuple[str, str], Category] = {("afclaude", "auto"): "run", ("owner", "auto"): "continued",
                                              ("afclaude", "owner"): "design", ("owner", "owner"): "user_local"}


class SplitError(ValueError):
    """The split can't be computed from this input (the message says why); the sample is aborted, not guessed."""


class UnpricedModel(SplitError):
    """A request's model has no price in compact_timing.PRICES: add it, then recompute (D-235)."""


@dataclass(frozen=True)
class TypedRow:
    """One `typed_messages` row (§3.1): a message AFClaude typed or launched with."""
    session: str
    sent_at: datetime
    driver: Driver
    text_hash: str


@dataclass(frozen=True)
class DriverChange:
    """The driver of a session from `at` on; `via` = typed (a matching row), unmatched (no row: the owner's)
    or prefix (the history matcher); `uuid` = the entry's uuid, for diagnostics."""
    at: datetime
    driver: Driver
    via: str
    uuid: str = ""


@dataclass(frozen=True)
class DriverTrack:
    """A session's driver changes, oldest first, and where they came from. `initial` is the driver in force
    before the first change (the sampler's carried-over state), None = unknown."""
    source: str
    changes: tuple[DriverChange, ...]
    initial: Optional[Driver] = None

    def at(self, ts: datetime) -> Driver:
        """The driver in force at `ts` (the latest change at or before it). Unknown -> SplitError (D-235)."""
        driver = self.initial
        for c in self.changes:
            if c.at > ts:
                break
            driver = c.driver
        if driver is None:
            raise SplitError(f"no driver known at {ts.isoformat()}: no prompt before it and no carried-over driver")
        return driver


# ---------------------------------------------------------------- the typed-message hash

def text_hash(text: str) -> str:
    """sha256 of the prompt text with whitespace normalised (runs of whitespace = one space, trimmed) and a
    `<pasted_content>` wrapper removed first: what a tmux-typed long text looks like in the transcript matches
    the text that was sent. The recorder (S2) must hash a sent message with this function."""
    return hashlib.sha256(" ".join(owner_words.unwrap_paste(text).split()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- real prompts

def _when(e: Mapping[str, Any]) -> datetime:
    v = e.get("timestamp")
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            t = None
        if t is not None:
            return t if t.tzinfo else t.replace(tzinfo=UTC)
    raise SplitError(f"a prompt entry {e.get('uuid')!r} has no readable timestamp")


def real_prompt(e: Mapping[str, Any]) -> Optional[str]:
    """The text of a transcript entry that is a real prompt (see the module doc), else None."""
    if e.get("type") != "user" or e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
        return None
    text = owner_words.prompt_text(e)
    if text is None or not text.strip() or text.lstrip().startswith(NOT_A_PROMPT_TEXT):
        return None
    origin = e.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind in NOT_A_PROMPT_ORIGINS:
        return None
    if kind not in HUMAN_ORIGINS:
        raise SplitError(f"entry {e.get('uuid')!r}: unknown origin kind {kind!r} (classify it in usage_split)")
    return text


def prefix_driver(text: str) -> Driver:
    """The history matcher (§3.1): AFClaude's texts start with a keep-alive prefix, except the owner's inbox
    message it delivers; everything else is the owner's."""
    t = owner_words.unwrap_paste(text).lstrip()
    if t.startswith(owner_words.INBOX_PREFIX):
        return "owner"
    return "auto" if t.startswith(owner_words.KEEPALIVE_PREFIXES) else "owner"


def _track(entries: Iterable[Mapping[str, Any]], initial: Optional[Driver], source: str,
           decide: Callable[[datetime, str], tuple[Driver, str]]) -> DriverTrack:
    changes: list[DriverChange] = []
    cur = initial
    for e in entries:
        text = real_prompt(e)
        if text is None:
            continue
        at = _when(e)
        if changes and at < changes[-1].at:
            raise SplitError(f"entries out of time order at {at.isoformat()}")
        driver, via = decide(at, text)
        if driver != cur:
            changes.append(DriverChange(at, driver, via, str(e.get("uuid") or "")))
            cur = driver
    return DriverTrack(source, tuple(changes), initial)


class _TypedMatcher:
    def __init__(self, session: str, rows: Iterable[TypedRow]) -> None:
        # only this session's rows can match (a row of another session is refused); each row matches once
        self.rows = sorted((r for r in rows if r.session == session), key=lambda r: r.sent_at)
        self.used: set[int] = set()

    def __call__(self, at: datetime, text: str) -> tuple[Driver, str]:
        h = text_hash(text)
        for i in range(len(self.rows) - 1, -1, -1):          # the latest send before the entry first
            r = self.rows[i]
            if i in self.used or r.sent_at > at or at - r.sent_at > MATCH_WINDOW or r.text_hash != h:
                continue
            self.used.add(i)
            return r.driver, SOURCE_TYPED
        return "owner", "unmatched"


class _PrefixMatcher:
    def __call__(self, at: datetime, text: str) -> tuple[Driver, str]:
        return prefix_driver(text), SOURCE_PREFIX


def driver_of(session: str, entries: Iterable[Mapping[str, Any]], typed: Iterable[TypedRow],
              initial: Optional[Driver] = None) -> DriverTrack:
    """The driver changes of `session`'s main transcript `entries` (file order, sidechain entries skipped)
    against the `typed` rows (any sessions; only this session's can match). Each real prompt matches the
    latest unused row with an equal hash sent at most MATCH_WINDOW before it (never one sent after it) and
    takes that row's driver; no match = owner. `initial` = the driver in force before the first entry."""
    return _track(entries, initial, SOURCE_TYPED, _TypedMatcher(session, typed))


def driver_of_prefix(entries: Iterable[Mapping[str, Any]], initial: Optional[Driver] = None) -> DriverTrack:
    """The same for history from before the typed-message record: the prompt-prefix list decides
    (`prefix_driver`); the track's source is "prefix", never to be mixed with a typed one."""
    return _track(entries, initial, SOURCE_PREFIX, _PrefixMatcher())


# ---------------------------------------------------------------- category and cost

def category_of(session_class: SessionClass, driver: Driver) -> Category:
    """§0: AFClaude's session driven by AFClaude = run, the owner's session driven by AFClaude = continued,
    AFClaude's session driven by the owner = design, the owner's session driven by the owner = user_local."""
    cat = _CATEGORY.get((session_class, driver))
    if cat is None:
        raise SplitError(f"no category for session class {session_class!r} and driver {driver!r}")
    return cat


def cost_of(model: Mapping[str, Any], r: compact_timing.Request) -> float:
    """One request's cost in session-%: the cost model's per-token weights by kind for the request's model
    (`compact_timing.Request.cost`: cache reads, input, output, 1-h and 5-min cache writes). A model with no
    price raises UnpricedModel; nothing is defaulted."""
    try:
        return float(sum(r.cost(model).values()))
    except compact_timing.FitError as err:
        raise UnpricedModel(f"request at {r.ts.isoformat()} ({r.model}): {err}") from err

