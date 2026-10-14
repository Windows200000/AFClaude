#!/usr/bin/env python3
"""Offline tests for compact_timing.py (D-237/D-238): the break-even from the cost model, the model
in the DB (cost_model.set, audited; the built-in one without a stored one; never a silent fall back
on a bad one), the transcript reading (requests, re-reads, owner prompts, compactions), the
trigger decision, the quickview block and the re-fit from the /usage samples."""
import io
import json
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB)
import actions  # noqa: E402
import compact_timing as ct  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
_N = [0]


def req(ts, ctx, write=0, out=400, model="claude-opus-5-5", rid=None, sidechain=False):
    _N[0] += 1
    return {"type": "assistant", "timestamp": ts.isoformat(), "requestId": rid or f"r{_N[0]}", "isSidechain": sidechain,
            "message": {"model": model, "usage": {"input_tokens": 2, "cache_creation_input_tokens": write,
                                                  "cache_read_input_tokens": ctx - write - 2, "output_tokens": out,
                                                  "cache_creation": {"ephemeral_1h_input_tokens": write}}}}


def prompt(ts, text, kind="human", **kw):
    e = {"type": "user", "timestamp": ts.isoformat(), "message": {"role": "user", "content": text}, **kw}
    if kind:
        e["origin"] = {"kind": kind}
    return e


def write(path, entries):
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def reset_model():
    conn = store.connect()
    try:
        with store.transaction(conn):
            conn.execute("UPDATE cost_model SET payload=? WHERE doc_key='model'", (json.dumps(ct.BUILTIN),))
    finally:
        conn.close()


class BreakEven(unittest.TestCase):
    def test_the_builtin_break_even(self):
        """D-238: ≈180k with the evaluation's weights; at C* a compaction costs exactly what one
        later re-read of C* instead of the compacted base saves (the formula's definition)."""
        m = ct.builtin()
        be = ct.break_even(m)
        self.assertAlmostEqual(be, 176_869, delta=50)
        base = m["compaction"]["base_tokens"]
        self.assertAlmostEqual(ct.compaction_cost(m, be), ct.reread_cost(m, be - base), places=4)
        self.assertAlmostEqual(ct.compaction_cost(m, be), 1.83, places=2)          # "≈2 session-%"
        self.assertAlmostEqual(ct.reread_cost(m, 650_000), 9.945, places=3)        # the eval: 650k ≈ 10 %
        self.assertAlmostEqual(ct.weekly(m, 10.0), 1.57)

    def test_it_moves_with_the_weights(self):
        m = ct.builtin()
        m["weights"]["cache_write"] = 30.0                                          # dearer re-reads: earlier
        self.assertLess(ct.break_even(m), ct.break_even(ct.builtin()))
        m = ct.builtin()
        m["compaction"]["summary_tokens"] = 30_000                                  # dearer compaction: later
        self.assertGreater(ct.break_even(m), ct.break_even(ct.builtin()))

    def test_other_models_are_scaled_by_price(self):
        m = ct.builtin()
        k, h = ct.per_token(m), ct.per_token(m, "claude-haiku-4-5-20251001")
        self.assertAlmostEqual(h["read"] / k["read"], 0.1 / 0.2)
        self.assertAlmostEqual(h["write_1h"] / k["write_1h"], 2.0 / 8.0)
        self.assertAlmostEqual(k["write_5m"] / k["write_1h"], 5.0 / 8.0)
        with self.assertRaisesRegex(ct.FitError, "no price for model 'claude-unknown'"):
            ct.per_token(m, "claude-unknown")

    def test_validate(self):
        for bad, msg in (([], "a JSON object"), (dict(ct.BUILTIN, schema="x"), "schema"),
                         (dict(ct.BUILTIN, weights={"cache_read": 1}), "weights.cache_write"),
                         (dict(ct.BUILTIN, weights=dict(ct.BUILTIN["weights"], cache_read=-1)), "weights.cache_read"),
                         (dict(ct.BUILTIN, weights=dict(ct.BUILTIN["weights"], cache_write=0.5)), "above its cache read"),
                         (dict(ct.BUILTIN, compaction={}), "compaction.base_tokens"),
                         (dict(ct.BUILTIN, reference_model="gpt"), "reference_model"),
                         (dict(ct.BUILTIN, weekly_per_session=True), "weekly_per_session")):
            with self.assertRaisesRegex(ValueError, msg):
                ct.validate(bad)


class StoredModel(unittest.TestCase):
    def tearDown(self):
        reset_model()

    def test_builtin_without_a_stored_one_then_the_stored_one_audited(self):
        def audits():
            conn = store.connect()
            try:
                return [tuple(r) for r in conn.execute(
                    "SELECT action, actor FROM audit_log WHERE action='cost_model.set' ORDER BY id").fetchall()]
            finally:
                conn.close()
        self.assertEqual(ct.load()["source"], ct.BUILTIN["source"])                # none stored, or reset
        before = audits()
        m = ct.builtin()
        m.update(source="test fit", weights=dict(m["weights"], cache_write=20.0))
        r = ct.store_model(m)
        self.assertIn(r["status"], ("inserted", "updated"))
        got = ct.load()
        self.assertEqual((got["source"], got["weights"]["cache_write"]), ("test fit", 20.0))
        self.assertEqual(audits()[len(before):], [("cost_model.set", "cli")])
        with self.assertRaisesRegex(ValueError, "weights.output"):
            ct.store_model(dict(m, weights=dict(m["weights"], output=0)))
        self.assertEqual(ct.load()["source"], "test fit")                         # the bad one was not stored

    def test_a_broken_stored_model_raises(self):
        """Never a silent fall back to the built-in model (D-235): a stored model that doesn't
        validate (written around the action) is an error."""
        ct.store_model(ct.builtin())
        conn = store.connect()
        try:
            with store.transaction(conn):
                conn.execute("UPDATE cost_model SET payload=? WHERE doc_key='model'", (json.dumps({"schema": "x"}),))
        finally:
            conn.close()
        with self.assertRaisesRegex(ValueError, "schema"):
            ct.load()

    def test_the_cli_prints_the_model(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ct.main(["model"]), 0)
        self.assertIn("break-even 177k tokens", out.getvalue())


class Transcript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.jsonl")
        self.m = ct.builtin()
        ct._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan(self):
        r1 = req(T0 + timedelta(seconds=5), 300_000, write=295_000, rid="A")
        write(self.path, [
            prompt(T0, "first question"),
            dict(r1, message=dict(r1["message"], usage=dict(r1["message"]["usage"], output_tokens=10))),
            r1,                                                                   # the same request: deduped
            {"type": "user", "timestamp": (T0 + timedelta(seconds=6)).isoformat(),
             "message": {"content": [{"type": "tool_result", "content": "x"}]}},  # not a prompt
            req(T0 + timedelta(seconds=9), 301_000, write=1000),
            prompt(T0 + timedelta(minutes=5), "[keep-alive] A new run starts"),
            prompt(T0 + timedelta(minutes=6), "<task-notification>done</task-notification>", kind="task-notification"),
            prompt(T0 + timedelta(minutes=7), "[Request interrupted by user]", kind=None),
            req(T0 + timedelta(minutes=8), 50_000, sidechain=True),               # a sidechain: skipped
            {"type": "system", "subtype": "compact_boundary", "timestamp": (T0 + timedelta(minutes=9)).isoformat()},
            prompt(T0 + timedelta(minutes=9), "This session is being continued", isCompactSummary=True),
            {"type": "assistant", "timestamp": T0.isoformat(), "message": {"model": "<synthetic>", "usage": {}}},
            "not json",
        ])
        with open(self.path, "a") as fh:
            fh.write('{"type": "user", "timest')                                 # a half-written line
        s = ct.scan(self.path)
        self.assertEqual([(r.context, r.output, r.reread) for r in s.requests],
                         [(300_000, 400, True), (301_000, 400, False)])
        self.assertEqual([p.kind for p in s.prompts], ["owner", "keepalive", "other", "summary"])
        self.assertEqual(s.compactions, [T0 + timedelta(minutes=9)])
        st = ct.context_state(s, self.m, T0 + timedelta(minutes=10))
        self.assertEqual((st["context"], st["estimated"]), (57_000, True))     # compacted since: the base
        self.assertFalse(st["above"])

    def test_context_state(self):
        write(self.path, [prompt(T0, "q"), req(T0 + timedelta(seconds=3), 250_000, write=2000)])
        st = ct.context_state(ct.scan(self.path), self.m, T0 + timedelta(minutes=20))
        self.assertEqual((st["context"], st["estimated"], st["warm"], st["above"]), (250_400, False, True, True))
        self.assertEqual(st["cache_expires_at"], T0 + timedelta(minutes=60, seconds=3))
        self.assertAlmostEqual(st["minutes_left"], 40.05, places=2)
        self.assertAlmostEqual(st["reread_cost"], 250_400 * 15.3e-6, places=6)
        self.assertEqual(ct.context_state(ct.Scan(), self.m, T0)["context"], None)

    def test_owner_messages_and_the_daily_split(self):
        write(self.path, [
            prompt(T0, "morning question"),                                       # after a long gap: a re-read
            req(T0 + timedelta(seconds=5), 600_000, write=600_000 - 2),
            req(T0 + timedelta(seconds=30), 601_000, write=1000),
            prompt(T0 + timedelta(minutes=10), "[keep-alive] continue"),
            req(T0 + timedelta(minutes=10, seconds=5), 602_000, write=1000),
            prompt(T0 + timedelta(minutes=20), "and this?"),                      # warm
            req(T0 + timedelta(minutes=20, seconds=5), 603_000, write=1000),
        ])
        s = ct.scan(self.path)
        msgs = ct.owner_messages(s, self.m, T0 - timedelta(days=1))
        self.assertEqual([(x["reread"], x["reread_tokens"], x["requests"], x["gap_min"]) for x in msgs],
                         [(False, 0, 1, 10), (True, 599_998, 2, None)])           # newest first, no text
        self.assertNotIn("question", json.dumps(msgs))
        first = sum(sum(r.cost(self.m).values()) for r in s.requests[:2])
        self.assertAlmostEqual(msgs[1]["session_pct"], round(first, 2))
        self.assertAlmostEqual(msgs[1]["weekly_pct"], round(first * 0.157, 3))
        days = ct.daily_split(s, self.m, T0 - timedelta(days=1))
        self.assertEqual(len(days), 1)
        d = days[0]
        self.assertEqual((d["day"], d["requests"], d["n_rereads"]), ("2026-10-13", 4, 1))
        self.assertAlmostEqual(d["rereads"], round(599_998 * 15.3e-6, 1))
        self.assertAlmostEqual(d["total"], round(d["rereads"] + d["warm_reads"] + d["writes"] + d["output"], 1), places=0)
        b = ct.context_block(self.path, T0 + timedelta(minutes=30), self.m)
        self.assertEqual(set(b), {"live", "messages", "days", "model"})
        self.assertEqual(b["live"]["context"], 603_400)
        self.assertEqual(b["model"]["break_even"], ct.break_even(self.m))
        self.assertEqual(ct.context_block(None, T0, self.m), {"error": "no transcript of the task-manager session"})


class Decide(unittest.TestCase):
    """D-238's triggers on a context state."""
    def state(self, ctx=300_000, idle=timedelta(minutes=55)):
        last = T0
        return {"context": ctx, "last_request_at": last, "cache_expires_at": last + timedelta(hours=1),
                "break_even": 177_000}, last + idle

    def test_matrix(self):
        st, now = self.state()
        d = ct.decide(now, st, run_using=False, run_ended=False, busy=None)
        self.assertEqual((d.due, d.trigger, d.key), (True, "idle", "compact-" + T0.isoformat()))
        st, now = self.state(ctx=150_000)
        self.assertIn("below the break-even 177k", ct.decide(now, st, run_using=False, run_ended=True, busy=None).reason)
        st, now = self.state()
        self.assertEqual(ct.decide(now, st, run_using=True, run_ended=False, busy=None).reason,
                         "a keep-alive run is using the session")
        self.assertEqual(ct.decide(now, st, run_using=False, run_ended=False, busy="a turn is running").reason,
                         "a turn is running")
        st, now = self.state(idle=timedelta(minutes=10))
        d = ct.decide(now, st, run_using=False, run_ended=True, busy=None)
        self.assertEqual((d.due, d.trigger), (True, "run-end"))                    # the run end: at once
        d = ct.decide(now, st, run_using=False, run_ended=False, busy=None)
        self.assertFalse(d.due)
        self.assertIn("idle 10 min: compacts at", d.reason)
        st, now = self.state(idle=timedelta(minutes=49, seconds=59))
        self.assertFalse(ct.decide(now, st, run_using=False, run_ended=False, busy=None).due)
        st, now = self.state(idle=timedelta(minutes=50))
        self.assertTrue(ct.decide(now, st, run_using=False, run_ended=False, busy=None).due)
        for idle in (timedelta(minutes=57, seconds=1), timedelta(minutes=61)):    # within SEND_MARGIN / expired
            st, now = self.state(idle=idle)
            d = ct.decide(now, st, run_using=False, run_ended=True, busy=None)
            self.assertEqual((d.due, d.missed), (False, True))
        self.assertIn("no request", ct.decide(T0, {"context": None, "break_even": 1}, run_using=False,
                                              run_ended=False, busy=None).reason)


class Fit(unittest.TestCase):
    """The re-fit (the monthly usage review): known weights are recovered from synthetic samples."""
    TRUE = {"read": 3.0, "write": 1.9, "io": 3.0, "const": 0.5}       # session-% per API dollar

    def data(self, n=120, seed=1):
        rnd = random.Random(seed)
        samples, reqs = [], []
        t = T0
        pct, wpct = 0.0, 10.0
        reset = (T0 + timedelta(hours=200)).isoformat().replace("T", " ")
        samples.append(self.sample(t, pct, wpct, reset))
        for i in range(n):
            reads, writes, outs = rnd.randint(0, 3_000_000), rnd.randint(0, 300_000), rnd.randint(0, 40_000)
            r = ct.Request(ts=t + timedelta(minutes=7), model="claude-opus-5-5", read=reads, write=writes,
                           output=outs, input=10)
            reqs.append(r)
            f = r.dollars()
            rise = (self.TRUE["read"] * f["read"] + self.TRUE["write"] * f["write"] + self.TRUE["io"] * f["io"]
                    + self.TRUE["const"] + rnd.gauss(0, 0.05))
            pct += rise
            wpct += rise * 0.16
            t += timedelta(minutes=15)
            samples.append(self.sample(t, pct, wpct, reset))
        return samples, reqs

    @staticmethod
    def sample(t, pct, wpct, reset):
        return {"at": t.isoformat(), "usage": {"fetched_at": t.isoformat().replace("T", " "),
                                               "session": {"percent": round(pct, 3), "resets_at": reset},
                                               "weekly": {"percent": round(wpct, 3), "resets_at": reset}}}

    def test_recovers_the_weights(self):
        samples, reqs = self.data()
        sc = ct.Scan(requests=[ct.Request(ts=T0 + timedelta(hours=1, seconds=1), model="claude-opus-5-5", read=55_000,
                                          input=10)], compactions=[T0 + timedelta(hours=1)])
        m = ct.fit(samples, reqs, ct.builtin(), T0 + timedelta(days=2), [sc])
        w = m["weights"]
        self.assertAlmostEqual(w["cache_read"], 3.0 * 0.2, delta=0.02)
        self.assertAlmostEqual(w["cache_write"], 1.9 * 8, delta=0.3)
        self.assertAlmostEqual(w["output"], 3.0 * 20, delta=1.0)
        self.assertAlmostEqual(w["input"], 3.0 * 4, delta=0.3)
        self.assertAlmostEqual(m["weekly_per_session"], 0.16, delta=0.005)
        self.assertGreater(m["fit"]["r2"], 0.99)
        self.assertAlmostEqual(m["fit"]["const_pct"], 0.5, delta=0.05)
        self.assertEqual((m["fit"]["n"], m["compaction"]["base_tokens"]), (120, 55_010))
        self.assertEqual(m["compaction"]["summary_tokens"], 12_000)                # kept
        ct.validate(m)

    def test_refuses_a_poor_fit(self):
        samples, reqs = self.data(n=20)
        with self.assertRaisesRegex(ct.FitError, "only 20 active sample intervals"):
            ct.fit(samples, reqs, ct.builtin(), T0)
        samples, reqs = self.data()
        for s in samples:                                                          # the meters say nothing
            s["usage"]["session"]["percent"] = 5.0
        with self.assertRaises(ct.FitError):
            ct.fit(samples, reqs, ct.builtin(), T0)
        samples, reqs = self.data()
        reqs[3].model = "claude-unknown"
        with self.assertRaisesRegex(ct.FitError, "no price for model"):
            ct.fit(samples, reqs, ct.builtin(), T0)

    def test_intervals_skip_resets_gaps_and_drops(self):
        reset = "2026-10-20 10:00:00+00:00"
        other = "2026-10-21 10:00:00+00:00"
        s = [self.sample(T0, 10, 1, reset), self.sample(T0 + timedelta(minutes=15), 12, 1, reset),
             self.sample(T0 + timedelta(minutes=30), 1, 1, other),                 # a session reset
             self.sample(T0 + timedelta(minutes=90), 3, 1, other),                 # a gap
             self.sample(T0 + timedelta(minutes=105), 2, 1, other)]                # a drop
        reqs = [ct.Request(ts=T0 + timedelta(minutes=m), model="claude-opus-5-5", read=1000) for m in (5, 20, 95, 100)]
        iv = ct.intervals(s, reqs)
        self.assertEqual([(x["from"], x["d_session"]) for x in iv], [(T0, 2.0)])

    def test_read_samples_skips_stale_rows(self):
        conn = store.connect()
        try:
            for i, stale in enumerate((False, True)):
                row = self.sample(T0 + timedelta(minutes=15 * i), 10 + i, 1, "2026-10-20 10:00:00+00:00")
                row["usage"]["stale"] = stale
                actions.append(conn, "usage.sample", json.dumps(row), actor="runner:sampler", via="runner")
            got = ct.read_samples(T0 - timedelta(minutes=1), conn)
        finally:
            conn.close()
        self.assertEqual([r["usage"]["session"]["percent"] for r in got], [10])


if __name__ == "__main__":
    unittest.main()
