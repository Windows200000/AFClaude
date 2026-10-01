"""Shared local settings for AFClaude (data/afclaude.json, optional, gitignored).
The dashboard will edit these later (schema v4 settings). Defaults live here."""
import json
import os
from datetime import time as dtime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("AFCLAUDE_CONFIG", os.path.join(HERE, "data", "afclaude.json"))
SESSION_LENGTH = timedelta(hours=5)          # one Claude session-limit window
# Automation window (Europe/Berlin wall clock): the weekly default of the per-weekday
# windows in docs/dashboard_design.md §4.2.1 (owner decision 29.09.2026: 23:00-09:00,
# 2 session windows of 5 h). It may span midnight; the end is start + hours on the wall
# clock, so a DST night still ends at 09:00 (it is then 11 h or 9 h long in real time).
DEFAULTS = {"last_mile_hours": SESSION_LENGTH.total_seconds() / 3600,
            "window_start": "23:00",
            "window_hours": 10,
            "usage_model": "reserve"}   # weekly budget model: "reserve" (usage_model.py) | "linear"
USAGE_MODELS = ("reserve", "linear")


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            cfg.update(json.load(fh) or {})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def last_mile():
    """Period before the weekly reset in which the 90% projection no longer blocks
    (the last-mile rule). timedelta(0) = off."""
    try:
        h = float(load().get("last_mile_hours", DEFAULTS["last_mile_hours"]))
    except (TypeError, ValueError):
        h = DEFAULTS["last_mile_hours"]
    return timedelta(hours=max(h, 0.0))


def usage_model():
    """Which weekly budget model keepalive.budget_decision() uses: "reserve" (the reserve
    envelope model in usage_model.py, default) or "linear" (the old projection rule).
    An unknown value means the default."""
    v = str(load().get("usage_model", DEFAULTS["usage_model"])).strip().lower()
    return v if v in USAGE_MODELS else DEFAULTS["usage_model"]


def _hhmm(s):
    h, m = (int(x) for x in str(s).split(":"))
    return dtime(h, m)


def window():
    """-> (start, end) as Berlin wall-clock times (end exclusive; end < start = spans
    midnight). window_start "HH:MM", window_hours 0 < h < 24 (whole minutes); an invalid
    value falls back to the default for both, so a typo never shifts only one edge."""
    cfg = load()
    try:
        start = _hhmm(cfg.get("window_start", DEFAULTS["window_start"]))
        hours = float(cfg.get("window_hours", DEFAULTS["window_hours"]))
        if not 1 <= round(hours * 60) <= 24 * 60 - 1:
            raise ValueError(hours)
    except (TypeError, ValueError):
        start, hours = _hhmm(DEFAULTS["window_start"]), float(DEFAULTS["window_hours"])
    end_min = (start.hour * 60 + start.minute + round(hours * 60)) % (24 * 60)
    return start, dtime(end_min // 60, end_min % 60)
