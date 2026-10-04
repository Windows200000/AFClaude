#!/usr/bin/env python3
"""Stale usage data: one rule shared by keepalive.py, usage_sampler.py, pacing.py and limit_ratio.py.

Background (night 03.10 -> 04.10.2026): `claude -p /usage` printed only the cost summary for
about 34 h and left ~/.claude.json cachedUsageUtilization untouched. The sampler kept recording
the frozen weekly 29% every 15 min, and the window start held all night without an alert.

Rule: usage whose cache was fetched more than USAGE_STALE_AFTER before the time it is used is
stale. The sampler keeps such rows (D-130: don't drop data) but marks them `usage.stale: true`;
the forecast / ratio code ignores the meters of stale rows. Rows written before the flag existed
are recognised by the same age test (sample time minus usage.fetched_at), so the 34 h of frozen
rows already in data/samples.jsonl are ignored without rewriting the file.
"""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
USAGE_STALE_AFTER = timedelta(minutes=45)   # 3 missed sampler runs (every 15 min)


def ts(x):
    """datetime | ISO string (also 'YYYY-MM-DD HH:MM:SS+00:00' and 'Z') -> aware datetime or None."""
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=UTC)
    if not x:
        return None
    try:
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def age(fetched_at, now):
    """now - fetched_at, or None if either is missing / unparsable."""
    f, n = ts(fetched_at), ts(now)
    return None if f is None or n is None else n - f


def is_stale(fetched_at, now, max_age=USAGE_STALE_AFTER):
    """True if the cache was fetched more than max_age before `now`, or has no fetch time."""
    a = age(fetched_at, now)
    return a is None or a > max_age


def row_stale(row):
    """A sampler row whose usage meters must not be used: flagged `usage.stale`, or (older rows)
    sampled more than USAGE_STALE_AFTER after the cache was fetched. Rows without usage, or
    without a fetch time, are not 'stale' (they carry no meters to misuse)."""
    if not isinstance(row, dict):
        return False
    u = row.get("usage")
    if not isinstance(u, dict):
        return False
    if u.get("stale") is True:
        return True
    a = age(u.get("fetched_at"), row.get("at"))
    return a is not None and a > USAGE_STALE_AFTER


def row_usage(row):
    """The row's usage dict, or {} if it is stale (so its weekly / session meters are absent)."""
    if row_stale(row):
        return {}
    u = row.get("usage") if isinstance(row, dict) else None
    return u if isinstance(u, dict) else {}
