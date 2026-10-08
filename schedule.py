"""The schedule: the ONLY window code (docs/dashboard_design.md §6; D-148, D-030, D-033, D-034,
D-202, D-203, D-205).

Settings (actions.SETTINGS, read through afclaude_config.settings(); tests pass a Config):
  window_days    {mon..sun: {start "HH:MM" (30-min grid), n, group} | null}: per weekday, the
                 night's automation window, or none that night (null). Linked days share a group.
  session_hours  the length of one Claude session-limit window (5 h).
  window_tz      the time zone of the start times (Europe/Berlin until the owner's first login
                 sets it from the browser, D-148/D-173).

A window belongs to the weekday it starts on and is [start, start + n x session_hours): the start
is a wall-clock time in window_tz, the length is ABSOLUTE, so every window is n whole session
windows, also on the DST nights (D-148): such a night ends an hour earlier or later on the wall
clock (23:00 x 2 = until 08:00 on the night the clocks go back, until 10:00 on the night they go
forward). A start that does not exist on the wall clock (02:30 on the spring-forward night) is the
same instant as one hour later; an ambiguous one (02:30 on the fall-back night) is the first.

The session-window starts of a window are start + k x session_hours (k < n, absolute): the gate
runs at each of them and never in between (D-202/D-203); AFClaude runs begin only there (and at
the last-stretch slot starts, D-205). A start postponed at a session-window start must begin
before the next session-window start of the same window; at the last one it skips to the next
window (postpone_deadline). The cron runs `keepalive.py --window-start` every 30 minutes
(docker/crontab); it acts only when a session-window start is due (due_session_start: within
CRON_STEP after it), once per start (start_key).

On a spring-forward night two back-to-back windows (Mon 23:00 x 2, Tue 09:00) would overlap by
the lost hour: the later one then begins when the earlier one ends (still n whole sessions).

All functions are tz-aware: they take aware datetimes (any zone) and return UTC datetimes;
`cfg` None = the current settings (one DB read per call: pass a Config to loops).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

UTC = timezone.utc
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_TZ = "Europe/Berlin"
DEFAULT_SESSION_HOURS = 5.0
DEFAULT_START, DEFAULT_N = "23:00", 2          # every night 23:00 x 2 (D-033)
GRID_MINUTES = 30                              # window starts are on the 30-min grid
CRON_STEP = timedelta(minutes=30)              # docker/crontab: --window-start every 30 min
WEEK_MIN = 7 * 24 * 60
LOOKAHEAD = timedelta(days=8)                  # more than a week: every weekday's window once

DayWindow = Optional[Mapping[str, Any]]


@dataclass(frozen=True)
class Config:
    """The three schedule settings."""
    days: Mapping[str, DayWindow]
    session_hours: float = DEFAULT_SESSION_HOURS
    tz: str = DEFAULT_TZ

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def session(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    @classmethod
    def every_day(cls, start: str = DEFAULT_START, n: int = DEFAULT_N, session_hours: float = DEFAULT_SESSION_HOURS,
                  tz: str = DEFAULT_TZ, **per_day: DayWindow) -> Config:
        """The same window every night (one link group "weekly"), per_day overrides (None = off)."""
        days: dict[str, DayWindow] = {d: {"start": start, "n": n, "group": "weekly"} for d in DAYS}
        days.update(per_day)
        return cls(days, float(session_hours), tz)


def load() -> Config:
    """The schedule settings now (afclaude_config.settings: the DB value, else the code default)."""
    import afclaude_config
    s = afclaude_config.settings("window_days", "session_hours", "window_tz")
    return Config(s["window_days"], float(s["session_hours"]), str(s["window_tz"]))


def _cfg(cfg: Config | None) -> Config:
    return cfg if cfg is not None else load()


@dataclass(frozen=True)
class Window:
    """One automation window: [start, end), both UTC; end = start + sessions x session_length."""
    start: datetime
    end: datetime
    sessions: int
    day: str
    group: str
    session_length: timedelta
    tz: str

    def contains(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def session_starts(self) -> list[datetime]:
        return [self.start + k * self.session_length for k in range(self.sessions)]

    def local(self, t: datetime) -> datetime:
        return t.astimezone(ZoneInfo(self.tz))

    def span(self) -> str:
        """'23:00–09:00' on the wall clock of window_tz (08:00 / 10:00 on the DST nights)."""
        return f"{self.local(self.start):%H:%M}–{self.local(self.end):%H:%M}"


def _hm(s: Any) -> tuple[int, int]:
    h, m = (int(x) for x in str(s).split(":"))
    return h, m


def _wall(d: date, hm: tuple[int, int], zone: ZoneInfo) -> datetime:
    """The wall-clock time d hh:mm in `zone` as UTC (fold 0: a missing time = one hour later,
    an ambiguous one = the first)."""
    return datetime.combine(d, time(hm[0], hm[1]), tzinfo=zone).astimezone(UTC)


def _window_on(d: date, cfg: Config) -> Window | None:
    w = cfg.days.get(DAYS[d.weekday()])
    if not w:
        return None
    start = _wall(d, _hm(w["start"]), cfg.zone)
    n = int(w["n"])
    return Window(start, start + n * cfg.session, n, DAYS[d.weekday()], str(w.get("group") or ""), cfg.session,
                  cfg.tz)


def windows(frm: datetime, to: datetime, cfg: Config | None = None) -> list[Window]:
    """The windows that overlap [frm, to), in order."""
    c = _cfg(cfg)
    first = frm.astimezone(c.zone).date() - timedelta(days=2)      # a window is at most 24 h
    last = to.astimezone(c.zone).date() + timedelta(days=1)
    out: list[Window] = []
    d = first
    while d <= last:
        w = _window_on(d, c)
        if w is not None:
            if out and w.start < out[-1].end:      # spring-forward night, back-to-back windows
                s = out[-1].end
                w = Window(s, s + w.sessions * w.session_length, w.sessions, w.day, w.group, w.session_length, w.tz)
            out.append(w)
        d += timedelta(days=1)
    return [w for w in out if w.end > frm and w.start < to]


def current_window(t: datetime, cfg: Config | None = None) -> Window | None:
    """The window `t` is in, else None."""
    return next((w for w in windows(t, t + timedelta(microseconds=1), cfg) if w.contains(t)), None)


def in_window(t: datetime, cfg: Config | None = None) -> bool:
    return current_window(t, cfg) is not None


def next_window(t: datetime, cfg: Config | None = None) -> Window | None:
    """The first window that starts after `t`; None if no weekday has a window."""
    return next((w for w in windows(t, t + LOOKAHEAD, cfg) if w.start > t), None)


def next_window_start(t: datetime, cfg: Config | None = None) -> datetime | None:
    """`t` if inside a window, else the next window's start (None: no window at all)."""
    c = _cfg(cfg)
    if in_window(t, c):
        return t.astimezone(UTC)
    w = next_window(t, c)
    return w.start if w else None


def current_or_next(t: datetime, cfg: Config | None = None) -> Window | None:
    c = _cfg(cfg)
    return current_window(t, c) or next_window(t, c)


def session_starts(window: Window) -> list[datetime]:
    """start + k x session_hours, k < n (absolute)."""
    return window.session_starts()


def session_slots(after: datetime, before: datetime, cfg: Config | None = None) -> list[tuple[datetime, Window]]:
    """Every session-window start s with after < s < before, with its window, in order (across
    days: nights of different lengths, nights without a window)."""
    return [(s, w) for w in windows(after, before, cfg) for s in w.session_starts() if after < s < before]


def latest_session_start(t: datetime, cfg: Config | None = None) -> datetime | None:
    """The latest session-window start <= t of the window `t` is in; None outside a window."""
    w = current_window(t, cfg)
    if w is None:
        return None
    return [s for s in w.session_starts() if s <= t][-1]


def next_session_start(t: datetime, cfg: Config | None = None) -> datetime | None:
    """The first session-window start strictly after t (this window's next one, or a later
    window's first); None if no weekday has a window."""
    slots = session_slots(t, t + LOOKAHEAD, cfg)
    return slots[0][0] if slots else None


def postpone_deadline(t: datetime, cfg: Config | None = None) -> datetime:
    """A start postponed at the session-window start s of `t` (the latest one <= t) must begin
    before this: the next session-window start of the same window, which checks the gate itself,
    so the run still ends by the window end. At the last start it is s itself (no postponement:
    skip to the next window, D-202/D-203); outside a window `t`."""
    w = current_window(t, cfg)
    if w is None:
        return t.astimezone(UTC)
    starts = w.session_starts()
    s = [x for x in starts if x <= t][-1]
    later = [x for x in starts if x > s]
    return later[0] if later else s


def due_session_start(t: datetime, cfg: Config | None = None, step: timedelta = CRON_STEP) -> datetime | None:
    """The session-window start whose check is due at `t` (t within `step` after it: the cron
    fires every CRON_STEP), else None (--window-start then exits at once)."""
    s = latest_session_start(t, cfg)
    return s if s is not None and t - s < step else None


def window_key(window: Window, cfg: Config | None = None) -> str:
    """The window's name: the date (window_tz) it ends on ("night", e.g. 2026-09-30 for Tue 23:00 -
    Wed 09:00); if another window ends on the same date, + its start time (2026-09-30-2300)."""
    c = _cfg(cfg)
    day = window.local(window.end).date()
    same = [w for w in windows(window.end - timedelta(days=2), window.end + timedelta(days=2), c)
            if w.local(w.end).date() == day]
    return day.isoformat() + (f"-{window.local(window.start):%H%M}" if len(same) > 1 else "")


def night(t: datetime, cfg: Config | None = None) -> str:
    """window_key of the window `t` is in, else of the next one ("none": no window at all)."""
    c = _cfg(cfg)
    w = current_or_next(t, c)
    return window_key(w, c) if w else "none"


def start_key(t: datetime, cfg: Config | None = None) -> str:
    """Dedup key of the session-window-start check, one per SESSION-WINDOW START (D-202):
    window-start-<window_key> for its first start, -s2, -s3 ... for the later ones; outside a
    window the next window's first start."""
    c = _cfg(cfg)
    w = current_window(t, c)
    if w is None:
        nw = next_window(t, c)
        return f"window-start-{window_key(nw, c) if nw else 'none'}"
    k = len([s for s in w.session_starts() if s <= t]) - 1
    return f"window-start-{window_key(w, c)}" + (f"-s{k + 1}" if k else "")


def describe(cfg: Config | None = None) -> str:
    """One line: 'every day 23:00 x 2 x 5 h (Europe/Berlin)' or the days."""
    c = _cfg(cfg)
    parts = [f"{d} {w['start']} x {w['n']}" if w else f"{d} off" for d, w in ((d, c.days.get(d)) for d in DAYS)]
    vals = {(w["start"], w["n"]) if w else None for w in (c.days.get(d) for d in DAYS)}
    if len(vals) == 1:
        v = next(iter(vals))
        text = f"every day {v[0]} x {v[1]}" if v else "no window on any day"
    else:
        text = ", ".join(parts)
    return f"{text} x {c.session_hours:g} h ({c.tz})"


def check_overlap(days: Mapping[str, DayWindow], session_hours: float) -> None:
    """Validation for window_days (actions._window_days): no two days' windows overlap on the
    nominal wall clock (the week wraps around). ValueError names the days."""
    spans: list[tuple[str, int, int]] = []
    for i, d in enumerate(DAYS):
        w = days.get(d)
        if w:
            h, m = _hm(w["start"])
            s = i * 1440 + h * 60 + m
            spans.append((d, s, s + int(round(int(w["n"]) * session_hours * 60))))
    for i, (a, sa, ea) in enumerate(spans):
        for b, sb, eb in spans[i + 1:]:
            for k in (-WEEK_MIN, 0, WEEK_MIN):
                if max(sa, sb + k) < min(ea, eb + k):
                    first, second = ((a, ea), b) if sa <= sb + k else ((b, eb + k), a)
                    fw, sw = days[first[0]], days[second]
                    assert fw is not None and sw is not None
                    raise ValueError(f"{first[0]} {fw['start']} x {fw['n']} ends {_fmt_min(first[1])}, so "
                                     f"{second} can't start at {sw['start']} (overlap)")


def _fmt_min(m: int) -> str:
    m %= WEEK_MIN
    return f"{DAYS[m // 1440]} {m % 1440 // 60:02d}:{m % 60:02d}"
