"""AFClaude settings accessor + local machine identity (D-146, docs/dashboard_design.md §7.4).

Settings: everything the dashboard can change (window, budget, pacing, dispatcher timings)
lives in the DB `settings` table, typed in actions.SETTINGS with its code default (D-169).
Every runner reads it through ONE accessor, setting(name): the saved value, else the code
default (DB > code default; no file and no env overrides a setting). A long-running runner
re-reads per loop (keepalive.reload_window). If the DB can't be read, setting() logs it once
and returns the code default (the full DB error path is phase 2a step 3, §7.8 / D-171).

Machine identity stays in the local file data/afclaude.json (gitignored; or $AFCLAUDE_CONFIG;
template afclaude.example.json): manager_session, trust_root, the dashboard block.

One-time import (phase 2a): tunables still found in data/afclaude.json, data/dispatcher.json
or (the pacing keys) data/user_model.json are imported into the DB on first use through
actions.py (action setting.import, audit actor runner:import, D-154) and then removed from
the file; identity keys stay. Idempotent: a file without tunables is not touched, a key the
DB already has keeps the DB value. `python3 afclaude_config.py [import]` shows the effective
settings (or runs the import now).
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("AFCLAUDE_CONFIG", os.path.join(HERE, "data", "afclaude.json"))
# the other files whose tunables are imported once (same paths as dispatcher.py / pacing.py)
DISPATCHER_CONFIG_FILE = os.path.join(os.environ.get("DISPATCHER_DATA_DIR", os.path.join(HERE, "data")),
                                      "dispatcher.json")
USER_MODEL_FILE = os.path.join(os.environ.get("AFCLAUDE_DATA_DIR", os.path.join(HERE, "data")), "user_model.json")
SESSION_LENGTH = timedelta(hours=5)          # one Claude session-limit window (code constant; 2b: session_hours)
BERLIN = ZoneInfo("Europe/Berlin")           # the runners' wall clock until schedule.py (2b) reads window_tz
USAGE_MODELS = ("pacing", "linear")
RESERVE_THRESHOLD_RANGE = (50.0, 99.0)


# ---- logging: runners point LOG at their own log; each distinct problem is logged once per
# process (until a read works again), so a 30-s loop does not flood the log.

LOG: Optional[Callable[[str], Any]] = None
_LOGGED: set = set()


def _log(msg: str) -> None:
    if msg in _LOGGED:
        return
    _LOGGED.add(msg)
    try:
        if LOG is not None:
            LOG(msg)
        else:
            sys.stderr.write(f"[{datetime.now(BERLIN):%Y-%m-%d %H:%M:%S %Z}] {msg}\n")
    except Exception:   # noqa: BLE001 - logging must never take a runner down
        pass


# ---- the settings accessor

def _db_path() -> str:
    import store
    return store.DB_PATH


def _read_conn() -> sqlite3.Connection | None:
    """A read-only connection to the existing DB (never creates it, never runs the schema
    init). None if there is no DB file."""
    path = _db_path()
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=rw", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def settings(*names: str) -> dict[str, Any]:
    """{name: effective value} for the given settings (all of actions.SETTINGS if none),
    read in one connection: the saved value, else the code default. Never raises for a DB
    problem: it logs it and uses the code defaults. An unknown name is a ValueError."""
    import actions
    keys = list(names) or list(actions.SETTINGS)
    specs = {k: actions._setting_spec(k) for k in keys}
    _import_once()
    out = {k: s.default() for k, s in specs.items()}
    try:
        conn = _read_conn()
    except sqlite3.Error as e:
        _log(f"settings: database {_db_path()} unreadable ({type(e).__name__}: {e}); using the code defaults")
        return out
    if conn is None:
        _log(f"settings: no database at {_db_path()}; using the code defaults")
        return out
    try:
        for k in keys:
            value, source = actions.effective_setting(conn, k)
            if source.startswith("invalid"):
                _log(f"settings: the saved {k} is {source}; using the code default {value!r}")
            out[k] = value
    except (sqlite3.Error, ValueError) as e:   # ValueError: a row with broken JSON
        _log(f"settings: database {_db_path()} unreadable ({type(e).__name__}: {e}); using the code defaults")
        return {k: s.default() for k, s in specs.items()}
    finally:
        conn.close()
    _LOGGED.clear()                     # healthy again: a new problem is logged again
    return out


def setting(name: str) -> Any:
    """THE accessor for one setting: the DB value, else the code default (see settings())."""
    return settings(name)[name]


# ---- typed views the runners use

def last_mile_setting() -> str | float:
    """The last stretch before the weekly reset: "auto" (default: min(ceil(session windows of
    quota left), 2) x SESSION_LENGTH, see pacing.py) or a number of hours >= 0 (0 = off)."""
    v = setting("last_mile_hours")
    return "auto" if v == "auto" else float(v)


def usage_model() -> str:
    """Which weekly budget model keepalive.budget_eval() uses: "pacing" (pacing.py, default)
    or "linear" (the original rule, also pacing's error fallback)."""
    return str(setting("usage_model"))


def reserve_threshold_setting() -> tuple[str | float, str]:
    """The night gate's threshold (pacing.py): -> ("auto", "dynamic") (default: one session
    window left, 100 - the measured full-session weekly cost) or (pct in 50..99, "override")."""
    v = setting("reserve_threshold")
    return ("auto", "dynamic") if v == "auto" else (float(v), "override")


def reserve_threshold_value() -> str | float:
    """The setting as one value: "auto" or a %."""
    return reserve_threshold_setting()[0]


def pacing_params() -> dict[str, Any]:
    """pacing.py's yield / guard parameters: {idle_min, min_gap, session_cap, last_mile_yield}."""
    s = settings("pacing_idle_min", "pacing_min_gap", "pacing_session_cap", "pacing_last_mile_yield")
    return {"idle_min": s["pacing_idle_min"], "min_gap": s["pacing_min_gap"],
            "session_cap": s["pacing_session_cap"], "last_mile_yield": s["pacing_last_mile_yield"]}


def window_for(days: dict, session_hours: float, now: datetime | None = None) -> tuple[dtime, dtime]:
    """(start, end) Berlin wall-clock times of one automation window of window_days: the one
    `now` is in, else the next one to start (end exclusive; end < start = spans midnight; the
    end is start + n x session_hours on the wall clock). The runners still know one daily
    window (per-weekday windows and DST-exact lengths are schedule.py, phase 2b). No window on
    any day -> (start, start) = empty: never inside."""
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    week = 7 * 1440
    now_m = now.weekday() * 1440 + now.hour * 60 + now.minute
    best = None
    for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
        w = days.get(d)
        if not w:
            continue
        h, m = (int(x) for x in str(w["start"]).split(":"))
        length = min(int(round(w["n"] * session_hours * 60)), 1439)   # a 24-h window can't be told from none
        s = i * 1440 + h * 60 + m
        if any(s + k <= now_m < s + k + length for k in (-week, 0)):
            rank = -1                                       # the window we are in
        else:
            rank = (s - now_m) % week                       # minutes until it starts
        if best is None or rank < best[0]:
            best = (rank, h * 60 + m, length)
    if best is None:
        return dtime(0, 0), dtime(0, 0)
    start, end = best[1], (best[1] + best[2]) % 1440
    return dtime(start // 60, start % 60), dtime(end // 60, end % 60)


def window(now: datetime | None = None) -> tuple[dtime, dtime]:
    """The automation window from the window_days and session_hours settings (window_for)."""
    s = settings("window_days", "session_hours")
    return window_for(s["window_days"], s["session_hours"], now)


# ---- machine identity (dashboard design §7.3), the local file. Committed template with
# placeholders: afclaude.example.json. The LEGACY_* values are what the code had hardcoded
# before; they apply only while the local file does not set the key.
EXAMPLE_FILE = os.path.join(HERE, "afclaude.example.json")
LEGACY_MANAGER_SESSION = "f2897285-dd97-49d9-b29a-2334b4753dee"
LEGACY_TRUST_ROOT = "/mnt/BlockVolume/Claude"


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def load() -> dict[str, Any]:
    """The local file (machine identity), {} if missing or unreadable."""
    return _read_json(CONFIG_FILE)


def identity(key: str, default: Any = None) -> Any:
    """One value of the local file; `default` if it doesn't have it or it is null."""
    v = load().get(key)
    return default if v is None else v


def _text(key: str, legacy: str) -> str:
    v = identity(key)
    return v.strip() if isinstance(v, str) and v.strip() else legacy


def manager_session() -> str:
    """Session id of the AFClaude manager (keep-alive target; the dispatcher never touches it)."""
    return _text("manager_session", LEGACY_MANAGER_SESSION)


def trust_root() -> str:
    """Only directories below this get the trust-dialog mark / dispatcher task starts."""
    return _text("trust_root", LEGACY_TRUST_ROOT)


# ---- the one-time import of file tunables (phase 2a)

AFCLAUDE_TUNABLES = ("window_start", "window_hours", "last_mile_hours", "reserve_threshold", "week_target",
                     "usage_model")
DISPATCHER_TUNABLES = {"idle_cleanup_hours": "cleanup_idle_hours",
                       "finished_grace_minutes": "cleanup_finished_grace_minutes",
                       "verify_minutes": "stall_verify_minutes", "take_over_idle": "stall_take_over_idle"}
# read by nothing any more (D-205/D-206: no task starts, no caps on approved stalls)
DISPATCHER_OBSOLETE = ("session_usage_stop", "max_concurrent", "max_starts_per_window", "launch_failure_limit",
                       "skip_tasks")
PACING_TUNABLES = {"idle_min": "pacing_idle_min", "min_gap": "pacing_min_gap",
                   "session_cap": "pacing_session_cap", "last_mile_yield": "pacing_last_mile_yield"}


def _map_afclaude(d: dict, session_hours: float) -> tuple[dict, dict, list]:
    """data/afclaude.json -> (values {setting: value}, notes {file key: text}, consumed file keys)."""
    values: dict = {}
    notes: dict = {}
    consumed = [k for k in AFCLAUDE_TUNABLES if k in d]
    if d.get("window_start") is not None or d.get("window_hours") is not None:
        start, hours = d.get("window_start") or "23:00", d.get("window_hours")
        hours = 10 if hours is None else hours
        try:
            import actions
            start = actions._hhmm(start)
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
                raise ValueError(f"window_hours must be a positive number, got {hours!r}")
            n = max(1, int(round(hours / session_hours)))
            if abs(n * session_hours - hours) > 1e-9:
                notes["window_hours"] = (f"{hours:g} h is not whole {session_hours:g}-h session windows "
                                         f"(D-148): imported as {n} x {session_hours:g} h")
            values["window_days"] = {day: {"start": start, "n": n, "group": "weekly"} for day in actions.DAYS}
        except ValueError as e:
            notes["window_start"] = f"window not imported, invalid: {e} (file had {d.get('window_start')!r} " \
                                    f"x {d.get('window_hours')!r} h)"
    for k in ("last_mile_hours", "usage_model"):
        if d.get(k) is not None:
            values[k] = d[k]
    if d.get("reserve_threshold") is not None:
        values["reserve_threshold"] = d["reserve_threshold"]
        if d.get("week_target") is not None:
            notes["week_target"] = f"legacy, dropped: reserve_threshold is set (file had {d['week_target']!r})"
    elif d.get("week_target") is not None:
        values["reserve_threshold"] = d["week_target"]
        notes["week_target"] = f"legacy week_target {d['week_target']!r} imported as reserve_threshold"
    return values, notes, consumed


def _map_dispatcher(d: dict, session_hours: float) -> tuple[dict, dict, list]:
    values = {DISPATCHER_TUNABLES[k]: d[k] for k in DISPATCHER_TUNABLES if d.get(k) is not None}
    notes = {k: f"obsolete (D-205/D-206), dropped (file had {d[k]!r})" for k in DISPATCHER_OBSOLETE if k in d}
    return values, notes, [k for k in list(DISPATCHER_TUNABLES) + list(DISPATCHER_OBSOLETE) if k in d]


def _pacing_objects(d: dict) -> list:
    """The places pacing params live in a user_model.json: the top level and a "pacing" object."""
    return [d] + ([d["pacing"]] if isinstance(d.get("pacing"), dict) else [])


def _map_user_model(d: dict, session_hours: float) -> tuple[dict, dict, list]:
    """As pacing.py read them: from the "pacing" object if there is one, else the top level."""
    objs = _pacing_objects(d)
    src = objs[-1]
    values = {PACING_TUNABLES[k]: src[k] for k in PACING_TUNABLES if src.get(k) is not None}
    notes = {k: f"ignored before (the pacing object has the parameters), dropped (file had {d[k]!r})"
             for k in PACING_TUNABLES if len(objs) > 1 and k in d}
    return values, notes, sorted({k for obj in objs for k in PACING_TUNABLES if k in obj})


def import_sources() -> list:
    """(path, label, mapper, strip) per file; strip(d, consumed) removes the imported keys."""
    def strip_top(d: dict, keys: list) -> None:
        for k in keys:
            d.pop(k, None)

    def strip_pacing(d: dict, keys: list) -> None:
        for obj in _pacing_objects(d):
            for k in keys:
                obj.pop(k, None)
    return [(CONFIG_FILE, "data/afclaude.json", _map_afclaude, strip_top),
            (DISPATCHER_CONFIG_FILE, "data/dispatcher.json", _map_dispatcher, strip_top),
            (USER_MODEL_FILE, "data/user_model.json", _map_user_model, strip_pacing)]


def _write_json(path: str, d: dict) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def import_file_settings() -> list:
    """Import the tunables still in the local files into the DB settings (one setting.import
    per file, actor runner:import), then remove them from the file. -> one result dict per file
    that had any (see actions.setting_import). A file without tunables is not touched; errors
    are logged and leave the file as it is (the next run tries again)."""
    results = []
    for path, label, mapper, strip in import_sources():
        if not os.path.isfile(path) or not mapper(_read_json(path), SESSION_LENGTH.total_seconds() / 3600)[2]:
            continue
        try:
            results.append(_import_one(path, label, mapper, strip))
        except Exception as e:   # noqa: BLE001 - a failed import must not take a runner down
            _log(f"settings import from {label} failed ({type(e).__name__}: {e}); the file is unchanged")
    return results


def _import_one(path: str, label: str, mapper: Callable, strip: Callable) -> dict:
    import actions
    import store
    with open(path + ".import.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)            # two runners starting at once import once
        with open(path) as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        conn = store.connect()
        try:
            values, notes, consumed = mapper(d, actions.get_setting(conn, "session_hours"))
            if not consumed:
                return {"source": label, "imported": {}, "kept": {}, "default": [], "notes": {}}
            res = actions.perform(conn, "setting.import", {"source": label, "values": values, "notes": notes},
                                  actor="runner:import", via="runner")
        finally:
            conn.close()
        strip(d, consumed)
        _write_json(path, d)
    try:
        os.unlink(path + ".import.lock")
    except OSError:
        pass
    _log(f"settings: imported from {label} into the DB: {json.dumps(res['imported'], sort_keys=True)}"
         + (f"; kept the DB values of {sorted(res['kept'])}" if res["kept"] else "")
         + (f"; same as the default: {res['default']}" if res["default"] else "")
         + (f"; notes {json.dumps(res['notes'], sort_keys=True)}" if res["notes"] else "")
         + f"; removed {consumed} from the file")
    return res


_IMPORT_CHECKED = False


def _import_once() -> None:
    """The import on first use: checked once per process."""
    global _IMPORT_CHECKED
    if _IMPORT_CHECKED:
        return
    _IMPORT_CHECKED = True
    import_file_settings()


if __name__ == "__main__":
    if sys.argv[1:] == ["import"]:
        _IMPORT_CHECKED = True
        print(json.dumps(import_file_settings(), indent=1, default=str))
    elif sys.argv[1:]:
        sys.exit("usage: afclaude_config.py [import]")
    else:
        for k, v in settings().items():
            print(f"{k} = {json.dumps(v)}")
        print(f"window (Berlin, now) = {'%s-%s' % tuple(t.strftime('%H:%M') for t in window())}")
