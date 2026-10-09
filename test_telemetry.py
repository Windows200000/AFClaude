#!/usr/bin/env python3
"""Offline tests for the telemetry in the DB (dashboard phase 2c, D-161, telemetry.py): the
tables (accounts, append-only rows immutable by trigger, records with a version), the
actions.py appenders (validation, dedupe, stale marks), the importer (counts, idempotency,
the import mark, files untouched), the readers' DB-vs-file parity (before the import: the
file; after: the same rows from the DB) and the producers' dual-write (only for the live
files next to the DB; a DB failure never sinks the producer). Each test uses its own temp DB
and data dir; never touches the live data/."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import store  # noqa: E402
import actions  # noqa: E402
import telemetry  # noqa: E402
import pacing  # noqa: E402
import limit_ratio  # noqa: E402
import run_metrics  # noqa: E402
import stage_eta  # noqa: E402
import usage_sampler  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


def _read(path):
    with open(path) as fh:
        return fh.read()


def sample(i, stale=False, pct=None):
    t = T0 + timedelta(minutes=15 * i)
    return {"at": t.isoformat(), "tag": "cron",
            "usage": {"fetched_at": str(t), "stale": stale,
                      "session": {"percent": float(pct if pct is not None else i % 50),
                                  "resets_at": str(t + timedelta(hours=2))},
                      "weekly": {"percent": float(i // 10), "resets_at": "2026-10-08 17:00:00+00:00"}},
            "pad": "x" * (100 + i % 7)}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="afclaude-telemetry-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old_db = store.DB_PATH
        store.DB_PATH = os.path.join(self.dir, "afclaude.db")
        self.addCleanup(setattr, store, "DB_PATH", self.old_db)
        store.connect(store.DB_PATH, create=True).close()
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        pacing._TAIL_CACHE.clear()

    def path(self, name):
        return os.path.join(self.dir, name)

    def write_lines(self, name, rows, **kw):
        lines = [json.dumps(r, **kw) for r in rows]
        with open(self.path(name), "w") as fh:
            fh.write("".join(x + "\n" for x in lines))
        return lines

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class Schema(Base):
    def test_additive_schema_keeps_the_version(self):
        self.assertEqual(store.SCHEMA_VERSION, 4)     # additive: an older watcher must not see a newer DB
        self.assertEqual(store.get_meta(self.conn, "schema_version"), "4")
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(store.TELEMETRY_APPEND + store.TELEMETRY_DOCS + ("accounts",)) <= names)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT id FROM accounts")], [("default",)])

    def test_existing_db_gains_the_tables_on_connect(self):
        self.conn.close()
        raw = sqlite3.connect(store.DB_PATH)
        for t in store.TELEMETRY_APPEND + store.TELEMETRY_DOCS + ("accounts",):
            raw.execute(f"DROP TABLE {t}")
        raw.commit()
        raw.close()
        self.conn = store.connect()
        self.assertEqual(self.count("usage_samples"), 0)
        self.assertEqual(self.count("accounts"), 1)

    def test_every_appender_kind_has_a_file_and_a_table(self):
        self.assertEqual(set(actions.APPENDERS), set(telemetry.FILES))
        self.assertEqual({a.table for a in actions.APPENDERS.values()},
                         set(store.TELEMETRY_APPEND + store.TELEMETRY_DOCS))


class Appenders(Base):
    def test_append_dedupes_and_marks_stale(self):
        line = json.dumps(sample(1, stale=True))
        self.assertTrue(actions.append(self.conn, "usage.sample", line, actor="runner:sampler", via="runner"))
        self.assertFalse(actions.append(self.conn, "usage.sample", line + "\n", actor="runner:sampler", via="runner"))
        actions.append(self.conn, "usage.sample", json.dumps(sample(2)), actor="runner:sampler", via="runner")
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM usage_samples ORDER BY id")]
        self.assertEqual([r["stale"] for r in rows], [1, 0])
        self.assertEqual(rows[0]["payload"], line)                     # stored as written
        self.assertEqual(rows[0]["ts"], "2026-10-05T00:15:00Z")         # normalised, sortable
        self.assertEqual((rows[0]["actor"], rows[0]["via"], rows[0]["account_id"]),
                         ("runner:sampler", "runner", "default"))
        self.assertTrue(rows[0]["recorded_at"])

    def test_old_sample_rows_are_stale_by_their_fetch_age(self):
        r = sample(1)
        r["usage"].pop("stale")
        r["usage"]["fetched_at"] = "2026-10-01 00:00:00+00:00"            # days older than the sample
        actions.append(self.conn, "usage.sample", json.dumps(r), actor="runner:sampler", via="runner")
        self.assertEqual(self.conn.execute("SELECT stale FROM usage_samples").fetchone()[0], 1)

    def test_append_only_rows_are_immutable(self):
        actions.append(self.conn, "usage.haiku", json.dumps({"at": "2026-10-05T00:00:00+00:00"}),
                       actor="runner:sampler", via="runner")
        for sql in ("UPDATE haiku_judgements SET stale=1", "DELETE FROM haiku_judgements"):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                self.conn.execute(sql)
        self.assertEqual(self.count("haiku_judgements"), 1)

    def test_records_upsert_with_version_and_are_never_deleted(self):
        run = {"run_id": "r1", "start": "2026-10-05T02:00:05+00:00", "final": False}
        kw = dict(actor="runner:run_metrics", via="runner")
        self.assertEqual(actions.put_record(self.conn, "usage.run", json.dumps(run), **kw), "inserted")
        self.assertEqual(actions.put_record(self.conn, "usage.run", json.dumps(run), **kw), "same")
        run["final"] = True
        self.assertEqual(actions.put_record(self.conn, "usage.run", json.dumps(run), **kw), "updated")
        r = dict(self.conn.execute("SELECT * FROM usage_runs").fetchone())
        self.assertEqual((r["doc_key"], r["final"], r["version"], r["ts"]), ("r1", 1, 1, "2026-10-05T02:00:05Z"))
        with self.assertRaisesRegex(sqlite3.DatabaseError, "never deleted"):
            self.conn.execute("DELETE FROM usage_runs")

    def test_validation(self):
        ok = json.dumps({"at": "2026-10-05T00:00:00+00:00"})
        bad = [dict(kind="nope"), dict(kind="usage.run"), dict(actor="evil"), dict(via="web"),
               dict(account_id="other"), dict(line="not json"), dict(line="[1, 2]"), dict(line="{}\n{}"),
               dict(line=7)]
        for b in bad:
            args = dict(kind="usage.forecast", line=ok, actor="runner:pacing", via="runner", account_id="default")
            args.update(b)
            with self.subTest(b), self.assertRaises(ValueError):
                actions.append(self.conn, args.pop("kind"), args.pop("line"), **args)
        with self.assertRaises(ValueError):
            actions.put_record(self.conn, "usage.forecast", ok, actor="runner:pacing", via="runner")
        with self.assertRaises(ValueError):   # a record needs its key
            actions.put_record(self.conn, "usage.run", ok, actor="runner:pacing", via="runner")
        self.assertEqual(self.count("forecast_log"), 0)

    def test_append_many_counts_invalid_lines(self):
        r = actions.append_many(self.conn, "usage.series", [json.dumps({"at": "2026-10-05T00:00:00Z"}), "{broken",
                                                           json.dumps({"at": "2026-10-05T00:00:00Z"})],
                                actor="runner:import", via="runner")
        self.assertEqual(r, {"inserted": 1, "duplicate": 1, "invalid": 1})

    def test_telemetry_ts(self):
        self.assertEqual(store.telemetry_ts("2026-10-09 21:14:12.412000+00:00"), "2026-10-09T21:14:12.412Z")
        self.assertEqual(store.telemetry_ts("2026-10-09T21:14Z"), "2026-10-09T21:14:00Z")
        self.assertEqual(store.telemetry_ts("2026-10-09T23:14:00+02:00"), "2026-10-09T21:14:00Z")
        self.assertEqual(store.telemetry_ts("garbage"), "garbage")
        self.assertEqual(store.telemetry_ts(None), "")


class Importer(Base):
    def fixtures(self):
        self.samples = self.write_lines("samples.jsonl", [sample(i, stale=(i == 3)) for i in range(40)])
        with open(self.path("samples.jsonl"), "a") as fh:
            fh.write("\n{\"at\": \"2026-10-05T23:00:00+00:00\", \"usage\": {\"sess")   # a partial last line
        self.write_lines("weekly_series.jsonl", [{"at": sample(i)["at"], "weekly_pct": 1.0} for i in range(5)])
        self.write_lines("session_windows.jsonl", [{"window_end": f"2026-10-0{i}T05:00:00+00:00", "ratio": 0.2}
                                                   for i in range(5, 8)])
        self.write_lines("forecast_log.jsonl", [{"at": "2026-10-05T02:00:00+00:00", "forecast_user": 9.5}])
        self.write_lines("haiku.jsonl", [{"at": sample(1)["at"], "skipped": "x"}] * 2)   # an identical duplicate
        self.write_lines("usage_reports.jsonl", [{"at": "2026-10-05T03:00:00+00:00", "session": "s"}])
        self.write_lines("run_usage.jsonl", [{"at": "2026-10-05T02:10:00+00:00", "stale": True}])
        self.write_lines("stage_eta_log.jsonl", [{"at": "2026-10-05T03:00Z", "stages": []}], separators=(",", ":"))
        self.write_lines("afclaude_runs.jsonl", [{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "final": True},
                                                 {"run_id": "b", "start": "2026-10-06T02:00:00+00:00"}],
                         ensure_ascii=False)
        with open(self.path("weekly_cycles.json"), "w") as fh:
            json.dump({"2026-10-08T17:00:00+00:00": {"reset_at": "2026-10-08T17:00:00+00:00", "samples": 3}}, fh,
                      indent=1)
        with open(self.path("user_model.json"), "w") as fh:
            json.dump({"schema": 1, "fitted_at": "2026-10-01"}, fh)

    def snapshot(self):
        out = {}
        for n in os.listdir(self.dir):
            if not n.startswith("afclaude.db"):
                with open(self.path(n), "rb") as fh:
                    out[n] = fh.read()
        return out

    def test_import_counts_idempotency_and_files_untouched(self):
        self.fixtures()
        before = self.snapshot()
        r1 = telemetry.import_all()
        by = {k["table"]: k for k in r1["kinds"]}
        self.assertEqual(by["usage_samples"]["file_rows"], 40)
        self.assertEqual(by["usage_samples"]["skipped_lines"], 1)       # the partial line: counted, not stored
        self.assertEqual(by["usage_samples"]["inserted"], 40)
        self.assertEqual(by["haiku_judgements"]["inserted"], 1)
        self.assertEqual(by["haiku_judgements"]["duplicate"], 1)
        for t in store.TELEMETRY_APPEND + store.TELEMETRY_DOCS:
            self.assertEqual(by[t]["status"], "ok", t)
            self.assertEqual(by[t]["missing"], 0, t)
        self.assertEqual(r1["counts"], {"usage_samples": 40, "usage_weekly_series": 5, "usage_session_windows": 3,
                                        "forecast_log": 1, "haiku_judgements": 1, "usage_reports": 1,
                                        "usage_run_readings": 1, "stage_eta_log": 1, "weekly_cycles": 1,
                                        "usage_runs": 2, "user_model": 1})
        self.assertEqual(self.conn.execute("SELECT SUM(stale) FROM usage_samples").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT stale FROM usage_run_readings").fetchone()[0], 1)
        r2 = telemetry.import_all()
        for k in r2["kinds"]:
            self.assertEqual(k.get("inserted"), 0, k["table"])
            self.assertFalse(k.get("updated"), k["table"])
        self.assertEqual(r2["counts"], r1["counts"])
        self.assertEqual(self.snapshot(), before)                       # never changes a file
        mark = json.loads(store.get_meta(self.conn, telemetry.MARK + "usage_samples"))
        self.assertEqual((mark["file_rows"], mark["db_rows"]), (40, 40))

    def test_import_picks_up_new_rows_and_changed_records(self):
        self.fixtures()
        telemetry.import_all()
        with open(self.path("samples.jsonl"), "a") as fh:
            fh.write("\n" + json.dumps(sample(50)) + "\n")
        self.write_lines("afclaude_runs.jsonl", [{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "final": True},
                                                 {"run_id": "b", "start": "2026-10-06T02:00:00+00:00", "final": True}],
                         ensure_ascii=False)
        by = {k["table"]: k for k in telemetry.import_all()["kinds"]}
        self.assertEqual((by["usage_samples"]["inserted"], by["usage_samples"]["db_rows"]), (1, 41))
        self.assertEqual((by["usage_runs"]["updated"], by["usage_runs"]["same"]), (1, 1))

    def test_missing_file_is_reported_not_marked(self):
        r = telemetry.import_all(kinds=["usage.forecast"])
        self.assertEqual(r["kinds"][0]["status"], "no file")
        self.assertIsNone(store.get_meta(self.conn, telemetry.MARK + "forecast_log"))

    def test_store_cli(self):
        self.fixtures()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(store.main(["import-telemetry", "--db", store.DB_PATH]), 0)
        res = json.loads(out.getvalue())
        self.assertEqual(res["counts"]["usage_samples"], 40)


class Readers(Base):
    def setUp(self):
        super().setUp()
        self.lines = self.write_lines("samples.jsonl", [sample(i) for i in range(300)])

    def test_file_until_imported_then_db(self):
        p = self.path("samples.jsonl")
        self.assertIsNone(telemetry.db_lines(p))                        # not imported: the file
        actions.append(self.conn, "usage.sample", self.lines[0], actor="runner:sampler", via="runner")
        self.assertIsNone(telemetry.db_lines(p))                        # a dual-written row alone: still the file
        telemetry.import_all(kinds=["usage.sample"])
        self.assertEqual(telemetry.db_lines(p), self.lines)
        other = os.path.join(tempfile.mkdtemp(dir=self.dir), "samples.jsonl")   # not the live file
        shutil.copy(p, other)
        self.assertIsNone(telemetry.db_lines(other))

    def test_db_and_file_give_the_same_rows(self):
        p = self.path("samples.jsonl")
        alt_dir = tempfile.mkdtemp(dir=self.dir)
        alt = os.path.join(alt_dir, "samples.jsonl")                    # same content, read from the file
        shutil.copy(p, alt)
        telemetry.import_all(kinds=["usage.sample"])
        for mb in (5000, 20000, 77777, 10 ** 7):
            pacing._TAIL_CACHE.clear()
            with self.subTest(max_bytes=mb):
                self.assertEqual(pacing.tail_rows(p, mb), pacing.tail_rows(alt, mb))
                self.assertEqual(telemetry.db_lines(p, max_bytes=mb),
                                 [json.dumps(r) for r in pacing.tail_rows(alt, mb)])
        self.assertEqual(limit_ratio.load_samples(p), limit_ratio.load_samples(alt))
        self.assertEqual(limit_ratio.load_samples(p), limit_ratio.load_samples(p, db=False))
        since = T0 + timedelta(hours=50)
        got = [json.loads(x)["at"] for x in telemetry.lines(p, since=since)]
        self.assertEqual(got[0], since.isoformat())

    def test_tail_cache_follows_the_table(self):
        p = self.path("samples.jsonl")
        telemetry.import_all(kinds=["usage.sample"])
        n = len(pacing.tail_rows(p, 10 ** 7))
        actions.append(self.conn, "usage.sample", json.dumps(sample(400)), actor="runner:sampler", via="runner")
        self.assertEqual(len(pacing.tail_rows(p, 10 ** 7)), n + 1)

    def test_windows_runs_forecast_parity(self):
        wins = [{"window_end": f"2026-10-0{i}T05:00:00+00:00", "ratio": 0.1 * i} for i in range(5, 9)]
        self.write_lines("session_windows.jsonl", wins)
        self.write_lines("afclaude_runs.jsonl", [{"run_id": "a", "start": "2026-10-05T02:00:00+00:00"},
                                                 {"run_id": "b", "start": "2026-10-06T02:00:00+00:00"}],
                         ensure_ascii=False)
        self.write_lines("forecast_log.jsonl", [{"at": "2026-10-05T02:00:00+00:00", "reset": "2026-10-06T17:00:00+00:00",
                                                 "forecast_user": 9.5}])
        w = self.path("session_windows.jsonl")
        r = self.path("afclaude_runs.jsonl")
        before = (limit_ratio.load_windows(w), run_metrics.read_rows(r))
        telemetry.import_all()
        self.assertEqual(limit_ratio.load_windows(w), before[0])
        self.assertEqual(run_metrics.read_rows(r), before[1])
        self.assertEqual([json.loads(x) for x in telemetry.lines(self.path("forecast_log.jsonl"))][0]["forecast_user"], 9.5)
        # a backfill's recomputed window is a new row in the append-only table: the last one wins
        changed = dict(wins[1], ratio=0.99)
        actions.append(self.conn, "usage.session_window", json.dumps(changed), actor="cli", via="cli")
        got = limit_ratio.load_windows(w)
        self.assertEqual(len(got), 4)
        self.assertEqual(got[1]["ratio"], 0.99)

    def test_unreadable_db_falls_back_to_the_file(self):
        p = self.path("samples.jsonl")
        telemetry.import_all(kinds=["usage.sample"])
        self.conn.close()
        with open(store.DB_PATH, "r+b") as fh:                          # a broken DB file
            fh.write(b"garbage" * 100)
        self.assertIsNone(telemetry.db_lines(p))
        self.assertEqual(len(limit_ratio.load_samples(p)), 300)
        self.conn = sqlite3.connect(":memory:")


class DualWrite(Base):
    def test_sampler_append_writes_the_live_file_and_the_db(self):
        live = self.path("samples.jsonl")
        row = sample(1)
        row["when"] = T0                                               # a datetime: default=str in both copies
        usage_sampler.append(live, row)
        line = _read(live).rstrip("\n")
        self.assertEqual(self.conn.execute("SELECT payload FROM usage_samples").fetchone()[0], line)
        usage_sampler.append(self.path("haiku.jsonl"), {"at": row["at"], "skipped": "x"})
        self.assertEqual(self.count("haiku_judgements"), 1)
        other = os.path.join(tempfile.mkdtemp(dir=self.dir), "samples.jsonl")
        usage_sampler.append(other, sample(2))                           # not the live file: no DB row
        self.assertEqual(self.count("usage_samples"), 1)

    def test_producers(self):
        rows = [sample(i) for i in range(0, 60)]
        limit_ratio.append_new_windows(rows, path=self.path("session_windows.jsonl"), now=T0 + timedelta(days=1))
        n = len(_read(self.path("session_windows.jsonl")).splitlines())
        self.assertGreater(n, 0)
        self.assertEqual(self.count("usage_session_windows"), n)
        d = {"t0": T0, "w0": 5.0, "forecast": 12.345, "go": True}
        pacing.record_forecast(d, T0 + timedelta(days=3), path=self.path("forecast_log.jsonl"))
        self.assertEqual(self.count("forecast_log"), 1)
        run_metrics.write_rows([{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "note": "ü"}],
                               self.path("afclaude_runs.jsonl"))
        run_metrics.write_rows([{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "note": "ü", "final": True}],
                               self.path("afclaude_runs.jsonl"))
        r = dict(self.conn.execute("SELECT * FROM usage_runs").fetchone())
        self.assertEqual((r["final"], r["version"]), (1, 1))
        self.assertEqual(r["payload"], _read(self.path("afclaude_runs.jsonl")).rstrip("\n"))
        res = {"status": "ok", "assumptions": {}, "stages": []}
        self.assertTrue(stage_eta.record(res, path=self.path("stage_eta_log.jsonl"), now=T0))
        self.assertEqual(self.count("stage_eta_log"), 1)
        with open(self.path("weekly_cycles.json"), "w") as fh:
            json.dump({"2026-10-08T17:00:00+00:00": {"reset_at": "2026-10-08T17:00:00+00:00", "samples": 1}}, fh)
        self.assertTrue(telemetry.sync_file("usage.weekly_cycle"))
        self.assertTrue(telemetry.sync_file("usage.weekly_cycle"))
        self.assertEqual(self.conn.execute("SELECT version FROM weekly_cycles").fetchone()[0], 0)   # unchanged
        # after an import the readers see the dual-written rows too
        telemetry.import_all()
        self.assertEqual(len(limit_ratio.load_windows(self.path("session_windows.jsonl"))), n)

    def test_db_failure_never_sinks_the_producer(self):
        self.conn.close()
        os.remove(store.DB_PATH)                                         # no DB: connect() refuses to create one
        live = self.path("forecast_log.jsonl")
        self.assertTrue(pacing.record_forecast({"t0": T0, "w0": 1.0, "forecast": 2.0}, T0 + timedelta(days=1),
                                               path=live))
        self.assertEqual(len(_read(live).splitlines()), 1)                  # the file has the row
        self.assertFalse(os.path.exists(store.DB_PATH))
        self.assertFalse(telemetry.record("usage.forecast", _read(live), live))
        self.conn = sqlite3.connect(":memory:")


if __name__ == "__main__":
    unittest.main()
