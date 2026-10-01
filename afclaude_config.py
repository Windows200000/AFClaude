"""Shared local settings for AFClaude (data/afclaude.json, optional, gitignored).
The dashboard will edit these later (schema v4 settings). Defaults live here."""
import json
import os
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("AFCLAUDE_CONFIG", os.path.join(HERE, "data", "afclaude.json"))
SESSION_LENGTH = timedelta(hours=5)          # one Claude session-limit window
DEFAULTS = {"last_mile_hours": SESSION_LENGTH.total_seconds() / 3600}


def load():
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


def get(key, default=None):
    """One value of the local file (or DEFAULTS); `default` if neither has it or it is null."""
    v = load().get(key)
    return default if v is None else v


def _text(key, legacy):
    v = get(key)
    return v.strip() if isinstance(v, str) and v.strip() else legacy


def manager_session():
    """Session id of the AFClaude manager (keep-alive target; the dispatcher never touches it)."""
    return _text("manager_session", LEGACY_MANAGER_SESSION)


def trust_root():
    """Only directories below this get the trust-dialog mark / dispatcher task starts."""
    return _text("trust_root", LEGACY_TRUST_ROOT)


def last_mile():
    """Period before the weekly reset in which the 90% projection no longer blocks
    (the last-mile rule). timedelta(0) = off."""
    try:
        h = float(load().get("last_mile_hours", DEFAULTS["last_mile_hours"]))
    except (TypeError, ValueError):
        h = DEFAULTS["last_mile_hours"]
    return timedelta(hours=max(h, 0.0))
