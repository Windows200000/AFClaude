"""Login rate limits, the arithmetic (phase 6a W6, docs/phase6a_plan.md §3.6; design §9.5's
`auth_login_rate`: 5 failures / 15 min per account, 20 per IP, then exponential backoff;
repeated failures raise an alert).

Per key (an account or a client IP): fewer than `max_failures` failures in the last `window_s`
→ the next attempt is allowed. At or past the limit the key waits `base_s · 2^(n − max_failures)`
after its latest failure, n counted over `horizon_s` (24 h), capped at `cap_s` (1 h); counting
over the longer horizon is what makes the backoff grow, since the window's own count drops as
failures age out of it. The base, cap and horizon are not in the design (it says "exponential
backoff"): *interpretation, 8a checks it.* A login checks both keys and waits for the longer.

Pure: the failure times (from `auth_failures`, W5's table) and the time come in. A failure time
after `now` (another container's clock slightly ahead) still counts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Rule:
    max_failures: int
    window_s: float = 15 * 60
    base_s: float = 60
    cap_s: float = 60 * 60
    horizon_s: float = 24 * 60 * 60


ACCOUNT = Rule(max_failures=5)
IP = Rule(max_failures=20)


def _since(failures: Iterable[float], now: float, seconds: float) -> list[float]:
    return sorted(ts for ts in failures if ts > now - seconds)


def retry_after(failures: Iterable[float], now: float, rule: Rule) -> float:
    """Seconds until the key may try again; 0 when it may now."""
    recent = _since(failures, now, max(rule.horizon_s, rule.window_s))
    if sum(1 for ts in recent if ts > now - rule.window_s) < rule.max_failures:
        return 0.0
    over = min(len(recent) - rule.max_failures, 32)
    wait = min(rule.cap_s, rule.base_s * (1 << over))
    return max(0.0, recent[-1] + wait - now)


def reached_limit(failures: Iterable[float], now: float, rule: Rule) -> bool:
    """True right after the failure that brings the window's count to the limit: the moment to
    raise the alert, once per crossing."""
    return len(_since(failures, now, rule.window_s)) == rule.max_failures
