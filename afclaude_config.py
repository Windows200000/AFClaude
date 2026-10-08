"""Shared local settings for AFClaude (data/afclaude.json, optional, gitignored).
The dashboard will edit these later (schema v4 settings). Defaults live here."""
from __future__ import annotations

import json
import math
import os
from datetime import time as dtime, timedelta
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("AFCLAUDE_CONFIG", os.path.join(HERE, "data", "afclaude.json"))
SESSION_LENGTH = timedelta(hours=5)          # one Claude session-limit window
# Automation window (Europe/Berlin wall clock): the weekly default of the per-weekday
# windows in docs/dashboard_design.md §4.2.1 (owner decision 29.09.2026: 23:00-09:00,
# 2 session windows of 5 h). It may span midnight; the end is start + hours on the wall
# clock, so a DST night still ends at 09:00 (it is then 11 h or 9 h long in real time).
DEFAULTS = {"last_mile_hours": "auto",   # "auto" = pacing.last_mile_hours(), or a number of hours
            "window_start": "23:00",
            "window_hours": 10,
            "reserve_threshold": None,  # the night gate's weekly-% threshold: None / "auto" = one
                                        # session window left (pacing.dynamic_threshold), or a % (50..99)
            "usage_model": "pacing"}    # weekly budget model: "pacing" (pacing.py) | "linear"
USAGE_MODELS = ("pacing", "linear")
RESERVE_THRESHOLD_RANGE = (50.0, 99.0)


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            cfg.update(json.load(fh) or {})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


# ---- machine-specific values (dashboard design §7.3), in the same local file.
# Committed template with placeholders: afclaude.example.json. Generic code reads host
# names, identities and session ids from here instead of hardcoding them. The LEGACY_*
# values are what the code had hardcoded before; they apply only while the local file
# does not set the key, so a checkout without data/afclaude.json behaves as before.
# Tunables the dashboard edits (window, budget, pause) live in the DB settings table
# (actions.SETTINGS); this file only supplies their local defaults (last_mile_hours ...).
EXAMPLE_FILE = os.path.join(HERE, "afclaude.example.json")
LEGACY_MANAGER_SESSION = "f2897285-dd97-49d9-b29a-2334b4753dee"
LEGACY_TRUST_ROOT = "/mnt/BlockVolume/Claude"


def get(key: str, default: Any = None) -> Any:
    """One value of the local file (or DEFAULTS); `default` if neither has it or it is null."""
    v = load().get(key)
    return default if v is None else v


def _text(key: str, legacy: str) -> str:
    v = get(key)
    return v.strip() if isinstance(v, str) and v.strip() else legacy


def manager_session() -> str:
    """Session id of the AFClaude manager (keep-alive target; the dispatcher never touches it)."""
    return _text("manager_session", LEGACY_MANAGER_SESSION)


def trust_root():
    """Only directories below this get the trust-dialog mark / dispatcher task starts."""
    return _text("trust_root", LEGACY_TRUST_ROOT)


def last_mile_setting() -> str | float:
    """The last stretch before the weekly reset (the only end-of-week setting): "auto"
    (default: min(ceil(session windows of quota left), 2) x SESSION_LENGTH, see pacing.py)
    or a number of hours >= 0 (0 = off). Anything invalid means "auto"."""
    v = load().get("last_mile_hours", DEFAULTS["last_mile_hours"])
    if v is None or (isinstance(v, str) and v.strip().lower() == "auto"):
        return "auto"
    try:
        h = float(v)
    except (TypeError, ValueError):
        return "auto"
    return max(h, 0.0) if math.isfinite(h) and not isinstance(v, bool) else "auto"


def usage_model():
    """Which weekly budget model keepalive.budget_eval() uses: "pacing" (pacing.py, default:
    the original linear rule evolved into a forecast-driven plan) or "linear" (the original
    rule unchanged, also the error fallback). An unknown value (also the retired "reserve"
    and "budget") means the default."""
    v = str(load().get("usage_model", DEFAULTS["usage_model"])).strip().lower()
    return v if v in USAGE_MODELS else DEFAULTS["usage_model"]


def _pct(v, rng):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not rng[0] <= v <= rng[1]:
        return None
    return float(v)


def reserve_threshold_setting() -> tuple[str | float, str]:
    """The night gate's threshold (pacing.py): -> ("auto", "dynamic") or (pct, source).
    data/afclaude.json reserve_threshold: "auto" (default: one session window left,
    100 - the measured full-session weekly cost) or a % in 50..99. Backward compatibility:
    without reserve_threshold, a valid legacy week_target (the old 80..95 setting) is the
    override. Anything invalid means "auto"."""
    cfg = load()
    v = cfg.get("reserve_threshold")
    if v is not None and not (isinstance(v, str) and v.strip().lower() == "auto"):
        p = _pct(v, RESERVE_THRESHOLD_RANGE)
        return ("auto", "dynamic") if p is None else (p, "override")
    if v is None:
        p = _pct(cfg.get("week_target"), RESERVE_THRESHOLD_RANGE)
        if p is not None:
            return p, "override (legacy week_target)"
    return "auto", "dynamic"


def reserve_threshold_value() -> str | float:
    """The setting as one value for the settings store: "auto" or a %."""
    return reserve_threshold_setting()[0]


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
