"""Shared local settings for AFClaude (data/afclaude.json, optional, gitignored).
The dashboard will edit these later (schema v4 settings). Defaults live here."""
import json
import os
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("AFCLAUDE_CONFIG", os.path.join(HERE, "data", "afclaude.json"))
SESSION_LENGTH = timedelta(hours=5)          # one Claude session-limit window
DEFAULTS = {"last_mile_hours": SESSION_LENGTH.total_seconds() / 3600,
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
