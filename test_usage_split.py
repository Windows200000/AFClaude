#!/usr/bin/env python3
"""Tests for usage_split.py (goal 12 S2/S3 core; D-138, D-235): driver_of (typed-message matcher: whitespace
tolerance, other session, older than 10 min; peer / task-notification / compaction entries; a subagent takes its
parent's driver at its time), the prefix matcher on a synthetic transcript fixture, category_of, cost_of (cost
model reuse, five token kinds, an unpriced model raises). Synthetic data only.

    python3 -m unittest test_usage_split
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import compact_timing
import usage_split as us

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "testdata", "usage_split_transcript.jsonl")
SID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
UTC = timezone.utc


def t(hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 10, 20, h, m, s, tzinfo=UTC)


def fixture() -> list:
    with open(FIXTURE) as f:
        return [json.loads(line) for line in f if line.strip()]


def prompt(n: int, hms: str, text: str, **kw) -> dict:
    e = {"type": "user", "uuid": f"p{n}", "timestamp": f"2026-10-20T{hms}.000Z", "origin": {"kind": "human"},
         "message": {"role": "user", "content": text}}
    e.update(kw)
    return e


def row(text: str, hms: str, driver: str = "auto", session: str = SID) -> us.TypedRow:
    return us.TypedRow(session, t(hms), driver, us.text_hash(text))  # type: ignore[arg-type]


def seq(track: us.DriverTrack) -> list:
    return [(c.at.strftime("%H:%M:%S"), c.driver) for c in track.changes]


class PrefixMatcher(unittest.TestCase):
    def test_fixture_history(self):
        track = us.driver_of_prefix(fixture())
        self.assertEqual(track.source, "prefix")
        # keep-alive text = auto; owner prompt = owner; the inbox delivery (owner, no change), tool result,
        # task notification, peer, /compact, summary, local output, interrupt, sidechain: no change;
        # a pasted AFClaude prompt = auto
        self.assertEqual(seq(track), [("10:00:00", "auto"), ("10:30:00", "owner"), ("11:00:00", "auto")])
        self.assertTrue(all(c.via == "prefix" for c in track.changes))

    def test_inbox_message_is_the_owners(self):
        track = us.driver_of_prefix([prompt(1, "10:00:00", "[keep-alive] go"),
                                     prompt(2, "10:10:00", "[AFClaude inbox #3] hello")])
        self.assertEqual(seq(track), [("10:00:00", "auto"), ("10:10:00", "owner")])

    def test_initial_driver_kept(self):
        track = us.driver_of_prefix([prompt(1, "10:00:00", "[keep-alive] go")], initial="auto")
        self.assertEqual(track.changes, ())
        self.assertEqual(track.at(t("12:00:00")), "auto")


class TypedMatcher(unittest.TestCase):
    def test_fixture_typed(self):
        rows = [row("[keep-alive] Continue the run: budget 20%.", "09:59:58"),
                row("[AFClaude usage review] Go ahead.", "10:59:59"),
                row("[AFClaude inbox #4] Please also check the dashboard.", "10:39:59", "owner")]
        track = us.driver_of(SID, fixture(), rows)
        self.assertEqual(track.source, "typed")
        # the unmatched "Hi, what is the status?" is the owner's; a pasted wrapper hashes like the plain text
        self.assertEqual(seq(track), [("10:00:00", "auto"), ("10:30:00", "owner"), ("11:00:00", "auto")])
        self.assertEqual([c.via for c in track.changes], ["typed", "unmatched", "typed"])

    def test_whitespace_tolerant(self):
        rows = [row("[keep-alive]   Continue\n  the run.\n", "09:59:00")]
        track = us.driver_of(SID, [prompt(1, "10:00:00", "[keep-alive] Continue the\trun.")], rows)
        self.assertEqual(seq(track), [("10:00:00", "auto")])

    def test_other_session_refused(self):
        rows = [row("[keep-alive] go", "09:59:00", session=OTHER)]
        track = us.driver_of(SID, [prompt(1, "10:00:00", "[keep-alive] go")], rows)
        self.assertEqual((track.changes[0].driver, track.changes[0].via), ("owner", "unmatched"))

    def test_older_than_ten_minutes_refused(self):
        entry = [prompt(1, "10:00:00", "[keep-alive] go")]
        self.assertEqual(us.driver_of(SID, entry, [row("[keep-alive] go", "09:49:59")]).changes[0].driver, "owner")
        self.assertEqual(us.driver_of(SID, entry, [row("[keep-alive] go", "09:50:00")]).changes[0].driver, "auto")

    def test_sent_after_the_entry_refused(self):
        track = us.driver_of(SID, [prompt(1, "10:00:00", "[keep-alive] go")], [row("[keep-alive] go", "10:00:01")])
        self.assertEqual(track.changes[0].driver, "owner")

    def test_a_row_matches_once(self):
        entries = [prompt(1, "10:00:00", "go on"), prompt(2, "10:02:00", "go on")]
        track = us.driver_of(SID, entries, [row("go on", "09:59:00")], initial="owner")
        self.assertEqual(seq(track), [("10:00:00", "auto"), ("10:02:00", "owner")])

    def test_latest_row_wins(self):
        rows = [row("go on", "09:55:00", "auto"), row("go on", "09:59:00", "owner")]
        track = us.driver_of(SID, [prompt(1, "10:00:00", "go on")], rows)
        self.assertEqual(track.changes[0].driver, "owner")

    def test_non_prompts_never_change_the_driver(self):
        rows = [row("[keep-alive] go", "09:59:00")]
        noise = [
            prompt(2, "10:01:00", "<task-notification>x</task-notification>", origin={"kind": "task-notification"}),
            prompt(3, "10:02:00", "from a peer", origin={"kind": "peer"}),
            prompt(4, "10:03:00", "summary", origin=None, isCompactSummary=True),
            prompt(5, "10:04:00", "/compact Keep this", origin=None),
            prompt(6, "10:05:00", "<command-name>/model</command-name>", origin=None),
            prompt(7, "10:06:00", [{"type": "tool_result", "tool_use_id": "x", "content": "r"}], origin=None),
            prompt(8, "10:07:00", "sidechain words", isSidechain=True),
            prompt(9, "10:08:00", "meta words", isMeta=True),
            {"type": "assistant", "timestamp": "2026-10-20T10:09:00.000Z", "message": {}},
        ]
        track = us.driver_of(SID, [prompt(1, "10:00:00", "[keep-alive] go")] + noise, rows)
        self.assertEqual(seq(track), [("10:00:00", "auto")])

    def test_unknown_origin_raises(self):
        with self.assertRaises(us.SplitError):
            us.driver_of(SID, [prompt(1, "10:00:00", "hi", origin={"kind": "scheduled"})], [])

    def test_unreadable_time_and_order_raise(self):
        with self.assertRaises(us.SplitError):
            us.driver_of(SID, [prompt(1, "10:00:00", "hi", timestamp="soon")], [])
        with self.assertRaises(us.SplitError):
            us.driver_of(SID, [prompt(1, "10:10:00", "hi"), prompt(2, "10:00:00", "[keep-alive] x")], [])


class SubagentDriver(unittest.TestCase):
    def test_takes_the_parents_driver_at_its_time(self):
        track = us.driver_of_prefix(fixture())
        self.assertEqual(track.at(t("10:00:00")), "auto")
        self.assertEqual(track.at(t("10:29:59")), "auto")
        self.assertEqual(track.at(t("10:30:00")), "owner")
        self.assertEqual(track.at(t("10:59:59")), "owner")
        self.assertEqual(track.at(t("23:00:00")), "auto")

    def test_unknown_driver_raises(self):
        track = us.driver_of_prefix([prompt(1, "10:00:00", "[keep-alive] go")])
        with self.assertRaises(us.SplitError):
            track.at(t("09:00:00"))
        self.assertEqual(us.driver_of_prefix([], initial="owner").at(t("09:00:00")), "owner")


class Categories(unittest.TestCase):
    def test_table(self):
        self.assertEqual(us.category_of("afclaude", "auto"), "run")
        self.assertEqual(us.category_of("owner", "auto"), "continued")
        self.assertEqual(us.category_of("afclaude", "owner"), "design")
        self.assertEqual(us.category_of("owner", "owner"), "user_local")
        self.assertEqual(us.CATEGORIES[-1], "other_devices")
        with self.assertRaises(us.SplitError):
            us.category_of("nobody", "auto")  # type: ignore[arg-type]


class Cost(unittest.TestCase):
    model = compact_timing.builtin()

    def req(self, model="claude-opus-5-5", **kw) -> compact_timing.Request:
        return compact_timing.Request(ts=t("10:00:00"), model=model, **kw)

    def test_reference_model_by_kind(self):
        r = self.req(input=1000, write=2000, write_5m=0, read=100_000, output=500)
        w = self.model["weights"]
        want = (1000 * w["input"] + 2000 * w["cache_write"] + 100_000 * w["cache_read"] + 500 * w["output"]) / 1e6
        self.assertAlmostEqual(us.cost_of(self.model, r), want)

    def test_five_minute_write_priced_apart(self):
        one_h = us.cost_of(self.model, self.req(write=1_000_000))
        five = us.cost_of(self.model, self.req(write=1_000_000, write_5m=1_000_000))
        self.assertAlmostEqual(five / one_h, 5.0 / 8.0)

    def test_other_model_scaled_by_prices(self):
        opus = us.cost_of(self.model, self.req(output=1_000_000))
        haiku = us.cost_of(self.model, self.req("claude-haiku-5-5", output=1_000_000))
        self.assertAlmostEqual(haiku / opus, 0.5 / 20.0)

    def test_empty_request_costs_nothing(self):
        self.assertEqual(us.cost_of(self.model, self.req()), 0.0)

    def test_unpriced_model_raises(self):
        with self.assertRaises(us.UnpricedModel) as cm:
            us.cost_of(self.model, self.req("claude-mystery-9", output=10))
        self.assertIn("claude-mystery-9", str(cm.exception))
        self.assertIsInstance(cm.exception, us.SplitError)

    def test_from_a_transcript_entry(self):
        got = compact_timing.request_of(fixture()[1])
        assert got is not None
        # 10 in, 2000 1-h writes, 50000 reads, 300 out on the reference model
        w = self.model["weights"]
        want = (10 * w["input"] + 2000 * w["cache_write"] + 50_000 * w["cache_read"] + 300 * w["output"]) / 1e6
        self.assertAlmostEqual(us.cost_of(self.model, got[1]), want)


class Hash(unittest.TestCase):
    def test_normalised(self):
        self.assertEqual(us.text_hash(" a \n b\t c "), us.text_hash("a b c"))
        self.assertNotEqual(us.text_hash("a b"), us.text_hash("a  c"))
        self.assertEqual(us.text_hash('\n\n<pasted_content id="1">\nhello  there\n</pasted_content id="1">\n'),
                         us.text_hash("hello there"))


if __name__ == "__main__":
    unittest.main()
