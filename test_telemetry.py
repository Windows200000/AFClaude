#!/usr/bin/env python3
"""Offline tests for the telemetry in the DB (dashboard phase 2c, D-161, telemetry.py): the
tables (accounts, append-only rows immutable by trigger, records with a version), the
actions.py appenders (validation, dedupe, stale marks), the importer of the legacy files
(counts, idempotency, the import mark, files untouched, imported kinds retired, --catch-up),
the readers (a never-imported legacy file until the first write imports it, then the DB; an
unreadable DB gives no rows, never the file), the producers writing the DB only (no file;
side paths are files), DB failures (the rows in the fallback file, the producer goes on) and
the CLI (rows, user-model get|set). Each test uses its own temp DB and data dir; never touches
the live data/."""
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
            if t == "usage_reviews":                                    # born in the DB: no legacy file
                self.assertNotIn(t, by)
                continue
            self.assertEqual(by[t]["status"], "ok", t)
            self.assertEqual(by[t]["missing"], 0, t)
        self.assertEqual(r1["counts"], {"usage_samples": 40, "usage_weekly_series": 5, "usage_session_windows": 3,
                                        "forecast_log": 1, "haiku_judgements": 1, "usage_reports": 1,
                                        "usage_run_readings": 1, "stage_eta_log": 1, "weekly_cycles": 1,
                                        "usage_runs": 2, "user_model": 1, "usage_reviews": 0})
        self.assertEqual(self.conn.execute("SELECT SUM(stale) FROM usage_samples").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT stale FROM usage_run_readings").fetchone()[0], 1)
        r2 = telemetry.import_all()                                     # imported kinds are retired
        self.assertEqual({k["status"] for k in r2["kinds"]}, {telemetry.RETIRED})
        self.assertEqual(r2["counts"], r1["counts"])
        r3 = telemetry.import_all(catch_up=True)                        # append-only again: nothing new
        for k in r3["kinds"]:
            if k["table"] in store.TELEMETRY_DOCS:
                self.assertEqual(k["status"], telemetry.RETIRED, k["table"])
            else:
                self.assertEqual((k["status"], k["inserted"]), ("ok", 0), k["table"])
        self.assertEqual(r3["counts"], r1["counts"])
        self.assertEqual(self.snapshot(), before)                       # never changes a file
        mark = json.loads(store.get_meta(self.conn, telemetry.MARK + "usage_samples"))
        self.assertEqual((mark["file_rows"], mark["db_rows"]), (40, 40))

    def test_catch_up_adds_rows_and_never_reverts_records(self):
        self.fixtures()
        telemetry.import_all()
        with open(self.path("samples.jsonl"), "a") as fh:
            fh.write("\n" + json.dumps(sample(50)) + "\n")
        run_metrics.write_rows([{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "final": True, "n": 2}],
                               self.path("afclaude_runs.jsonl"))         # a newer record in the DB
        by = {k["table"]: k for k in telemetry.import_all(catch_up=True)["kinds"]}
        self.assertEqual((by["usage_samples"]["inserted"], by["usage_samples"]["db_rows"]), (1, 41))
        self.assertEqual(by["usage_runs"]["status"], telemetry.RETIRED)
        self.assertEqual(telemetry.read_records("usage.run", self.path("afclaude_runs.jsonl"))[0]["n"], 2)

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

    def test_legacy_file_until_imported_then_db(self):
        p = self.path("samples.jsonl")
        self.assertIsNone(telemetry.db_lines(p))                        # never imported: the legacy file
        actions.append(self.conn, "usage.sample", self.lines[0], actor="runner:sampler", via="runner")
        self.assertIsNone(telemetry.db_lines(p))                        # a row written around telemetry.py: still
        before = _read(p)
        self.assertTrue(telemetry.record("usage.sample", json.dumps(sample(300)), p))   # the first write imports
        self.assertEqual(telemetry.db_lines(p), self.lines + [json.dumps(sample(300))])
        self.assertEqual(_read(p), before)                              # the legacy file is never written
        other = os.path.join(tempfile.mkdtemp(dir=self.dir), "samples.jsonl")   # not the live path: a side file
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

    def test_unreadable_db_gives_no_rows_never_the_file(self):
        p = self.path("samples.jsonl")
        telemetry.import_all(kinds=["usage.sample"])
        self.assertEqual(len(pacing.tail_rows(p, 10 ** 7)), 300)
        self.conn.close()
        with open(store.DB_PATH, "r+b") as fh:                          # a broken DB file
            fh.write(b"garbage" * 100)
        self.assertEqual(telemetry.db_lines(p), [])
        self.assertEqual(limit_ratio.load_samples(p), [])
        self.assertEqual(pacing.tail_rows(p, 10 ** 7), [])              # not the cached rows either
        self.assertEqual(telemetry.read_records("usage.run"), [])
        self.conn = sqlite3.connect(":memory:")


class Writes(Base):
    def files(self):
        return sorted(n for n in os.listdir(self.dir) if not n.startswith("afclaude.db"))

    def test_sampler_append_writes_the_db_only(self):
        live = self.path("samples.jsonl")
        row = sample(1)
        row["when"] = T0                                               # a datetime: default=str
        self.assertTrue(usage_sampler.append(live, row))
        self.assertEqual(self.conn.execute("SELECT payload FROM usage_samples").fetchone()[0],
                         json.dumps(row, default=str))
        usage_sampler.append(self.path("haiku.jsonl"), {"at": row["at"], "skipped": "x"})
        self.assertEqual(self.count("haiku_judgements"), 1)
        self.assertEqual(self.files(), [])                              # no file
        other = os.path.join(tempfile.mkdtemp(dir=self.dir), "samples.jsonl")
        usage_sampler.append(other, sample(2))                           # a side path: its file, no DB row
        self.assertEqual(self.count("usage_samples"), 1)
        self.assertEqual(len(_read(other).splitlines()), 1)

    def test_producers(self):
        rows = [sample(i) for i in range(0, 60)]
        _, new = limit_ratio.append_new_windows(rows, path=self.path("session_windows.jsonl"), now=T0 + timedelta(days=1))
        self.assertGreater(len(new), 0)
        self.assertEqual(self.count("usage_session_windows"), len(new))
        _, again = limit_ratio.append_new_windows(rows, path=self.path("session_windows.jsonl"),
                                                  now=T0 + timedelta(days=1))
        self.assertEqual(again, [])                                     # the writer's check reads the DB
        self.assertEqual(len(limit_ratio.backfill_windows(self.path("samples.jsonl"),
                                                          self.path("session_windows.jsonl"))), len(new))
        d = {"t0": T0, "w0": 5.0, "forecast": 12.345, "go": True}
        self.assertTrue(pacing.record_forecast(d, T0 + timedelta(days=3), path=self.path("forecast_log.jsonl")))
        self.assertEqual(self.count("forecast_log"), 1)
        run_metrics.write_rows([{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "note": "ü"}],
                               self.path("afclaude_runs.jsonl"))
        run_metrics.write_rows([{"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "note": "ü", "final": True}],
                               self.path("afclaude_runs.jsonl"))
        r = dict(self.conn.execute("SELECT * FROM usage_runs").fetchone())
        self.assertEqual((r["final"], r["version"]), (1, 1))
        self.assertEqual(r["payload"], json.dumps({"run_id": "a", "start": "2026-10-05T02:00:00+00:00", "note": "ü",
                                                   "final": True}, ensure_ascii=False))
        self.assertEqual(run_metrics.read_rows(self.path("afclaude_runs.jsonl"))[0]["final"], True)
        res = {"status": "ok", "assumptions": {}, "stages": []}
        self.assertTrue(stage_eta.record(res, path=self.path("stage_eta_log.jsonl"), now=T0))
        self.assertFalse(stage_eta.record(res, path=self.path("stage_eta_log.jsonl"), now=T0))   # once a day (DB)
        self.assertEqual(self.count("stage_eta_log"), 1)
        cycles = {"2026-10-08T17:00:00+00:00": {"reset_at": "2026-10-08T17:00:00+00:00", "samples": 1}}
        self.assertTrue(telemetry.write_cycles(cycles))
        self.assertTrue(telemetry.write_cycles(telemetry.read_cycles()))
        self.assertEqual(self.conn.execute("SELECT version FROM weekly_cycles").fetchone()[0], 0)   # unchanged
        self.assertEqual(telemetry.read_cycles(self.path("weekly_cycles.json")), cycles)
        self.assertEqual(self.files(), [])                              # no producer wrote a file
        for t in ("usage_session_windows", "forecast_log", "usage_runs", "stage_eta_log", "weekly_cycles"):
            self.assertIsNotNone(store.get_meta(self.conn, telemetry.MARK + t), t)   # marked by the first write

    def test_first_write_imports_a_never_imported_legacy_file(self):
        old = self.write_lines("forecast_log.jsonl", [{"at": "2026-10-04T02:00:00+00:00", "forecast_user": 1.0}])
        self.assertEqual(telemetry.lines(self.path("forecast_log.jsonl")), old)   # read-only legacy
        pacing.record_forecast({"t0": T0, "w0": 1.0, "forecast": 2.0}, T0 + timedelta(days=1),
                               path=self.path("forecast_log.jsonl"))
        got = telemetry.lines(self.path("forecast_log.jsonl"))
        self.assertEqual((len(got), got[0]), (2, old[0]))
        self.assertEqual(_read(self.path("forecast_log.jsonl")), old[0] + "\n")   # untouched
        mark = json.loads(store.get_meta(self.conn, telemetry.MARK + "forecast_log"))
        self.assertEqual(mark["file_rows"], 1)

    def test_db_failure_records_the_rows_and_never_sinks_the_producer(self):
        self.conn.close()
        os.remove(store.DB_PATH)                                         # no DB: connect() refuses to create one
        live = self.path("forecast_log.jsonl")
        self.assertFalse(pacing.record_forecast({"t0": T0, "w0": 1.0, "forecast": 2.0}, T0 + timedelta(days=1),
                                                path=live))
        self.assertFalse(os.path.exists(live))                          # no file instead
        self.assertFalse(os.path.exists(store.DB_PATH))
        self.assertFalse(run_metrics.write_rows([{"run_id": "a", "start": "2026-10-05T02:00:00+00:00"}],
                                                self.path("afclaude_runs.jsonl")))
        failed = [e for e in store.fallback_events(store.fallback_path()) if e.get("event") == "failed"]
        self.assertEqual([(e["action"], e["params"]["kind"]) for e in failed],
                         [("telemetry.append", "usage.forecast"), ("telemetry.put", "usage.run")])
        self.assertEqual(json.loads(failed[0]["params"]["lines"][0])["forecast_user"], 2.0)   # kept for a replay
        self.assertEqual(telemetry.lines(live), [])
        self.conn = sqlite3.connect(":memory:")


class Cli(Base):
    def run_cli(self, *argv, stdin=None):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                from unittest import mock
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    rc = telemetry.main(list(argv))
            else:
                rc = telemetry.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_rows(self):
        for i in range(5):
            telemetry.record("usage.sample", json.dumps(sample(i)))
        rc, out, _ = self.run_cli("rows", "usage.sample", "--last", "2")
        self.assertEqual((rc, [json.loads(x)["at"] for x in out.splitlines()]), (0, [sample(3)["at"], sample(4)["at"]]))

    def test_user_model_get_set(self):
        import usage_model
        self.assertEqual(self.run_cli("user-model", "get")[0], 1)       # none yet
        doc = {"schema": "afclaude.user_model/1", "weeks_of_data": 5,
               "envelope_weekly_pct_by_hours": {"1": 3, "168": 40}}
        rc, out, err = self.run_cli("user-model", "set", stdin=json.dumps(doc))
        self.assertEqual((rc, json.loads(out)["status"]), (0, "inserted"), err)
        self.assertEqual(json.loads(self.run_cli("user-model", "get")[1]), doc)
        self.assertEqual(telemetry.read_user_model(), doc)
        P, env, src = usage_model.load_params(self.path("user_model.json"))   # the live path: the DB
        self.assertIn("5 closed weeks", src)
        doc2 = dict(doc, weeks_of_data=6)
        self.assertEqual(self.run_cli("user-model", "set", stdin=json.dumps(doc2))[0], 0)
        audit = store.audit_log(self.conn, target_type="user_model")
        self.assertEqual([a["action"] for a in audit], ["user_model.set"] * 2)
        self.assertEqual((audit[0]["before"]["doc"]["weeks_of_data"], audit[0]["after"]["doc"]["weeks_of_data"]), (5, 6))
        self.assertEqual(self.run_cli("user-model", "set", stdin="[1]")[0], 1)   # not an object
        self.assertFalse(os.path.exists(self.path("user_model.json")))


if __name__ == "__main__":
    unittest.main()
