#!/usr/bin/env python3
"""Offline tests for schedule.py, the only window code (docs/dashboard_design.md §6; D-148,
D-030/D-033/D-034, D-202/D-203). Explicit Configs; the settings path through a temp DB."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import pacing  # noqa: E402
import schedule  # noqa: E402

UTC = timezone.utc
DEF = schedule.Config.every_day()            # every night 23:00 x 2 x 5 h, Europe/Berlin
H5 = timedelta(hours=5)


def Z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def berlin(t):
    return t.astimezone(DEF.zone).strftime("%a %d.%m. %H:%M %Z")


class DefaultWindow(unittest.TestCase):
    """23:00 x 2: 23:00-09:00 Berlin, spanning midnight; DST nights are 10 h absolute (D-148)."""
    cases = [
        ("2026-09-25T20:59:59Z", False), ("2026-09-25T21:00:00Z", True),     # 22:59:59 / 23:00 CEST
        ("2026-09-25T22:00:00Z", True), ("2026-09-26T06:59:59Z", True),      # 00:00 / 08:59:59 CEST
        ("2026-09-26T07:00:00Z", False), ("2026-09-26T10:00:00Z", False),    # 09:00 / 12:00 CEST
        ("2026-10-26T21:59:59Z", False), ("2026-10-26T22:00:00Z", True),     # CET
        ("2026-10-27T07:59:59Z", True), ("2026-10-27T08:00:00Z", False),     # 08:59:59 / 09:00 CET
        # DST end night (25.10.2026, 03:00 CEST -> 02:00 CET): 23:00 CEST + 10 h = 08:00 CET
        ("2026-10-24T20:59:59Z", False), ("2026-10-24T21:00:00Z", True),
        ("2026-10-25T00:30:00Z", True), ("2026-10-25T01:30:00Z", True),      # 02:30 CEST, 02:30 CET
        ("2026-10-25T06:59:59Z", True), ("2026-10-25T07:00:00Z", False),     # 07:59:59 / 08:00 CET
        # DST start night (28.03.2027, 02:00 CET -> 03:00 CEST): 23:00 CET + 10 h = 10:00 CEST
        ("2027-03-27T21:59:59Z", False), ("2027-03-27T22:00:00Z", True),
        ("2027-03-28T00:59:59Z", True), ("2027-03-28T01:00:00Z", True),      # 01:59:59 CET, 03:00 CEST
        ("2027-03-28T07:59:59Z", True), ("2027-03-28T08:00:00Z", False),     # 09:59:59 / 10:00 CEST
    ]

    def test_in_window(self):
        for ts, want in self.cases:
            with self.subTest(ts=ts):
                self.assertEqual(schedule.in_window(Z(ts), DEF), want)

    def test_window_and_session_starts(self):
        for now, start, end, starts in [
            ("2026-09-29T21:30:00Z", "2026-09-29T21:00:00Z", "2026-09-30T07:00:00Z",
             ["2026-09-29T21:00:00Z", "2026-09-30T02:00:00Z"]),                  # CEST: 23:00, 04:00
            ("2026-10-27T06:00:00Z", "2026-10-26T22:00:00Z", "2026-10-27T08:00:00Z",
             ["2026-10-26T22:00:00Z", "2026-10-27T03:00:00Z"]),                  # CET: 23:00, 04:00
            ("2026-10-24T23:00:00Z", "2026-10-24T21:00:00Z", "2026-10-25T07:00:00Z",
             ["2026-10-24T21:00:00Z", "2026-10-25T02:00:00Z"]),                  # DST end: 23:00, 03:00 CET
            ("2027-03-28T01:30:00Z", "2027-03-27T22:00:00Z", "2027-03-28T08:00:00Z",
             ["2027-03-27T22:00:00Z", "2027-03-28T03:00:00Z"]),                  # DST start: 23:00, 05:00 CEST
            ("2026-12-31T22:30:00Z", "2026-12-31T22:00:00Z", "2027-01-01T08:00:00Z",
             ["2026-12-31T22:00:00Z", "2027-01-01T03:00:00Z"]),                  # across the year
        ]:
            with self.subTest(now=now):
                w = schedule.current_window(Z(now), DEF)
                self.assertEqual((w.start, w.end), (Z(start), Z(end)))
                self.assertEqual(w.end - w.start, 2 * H5)                    # N whole sessions, always
                self.assertEqual(schedule.session_starts(w), [Z(s) for s in starts])
                self.assertEqual(schedule.session_starts(w)[-1] + H5, w.end)
        self.assertEqual(schedule.current_window(Z("2026-10-24T21:00:00Z"), DEF).span(), "23:00–08:00")
        self.assertEqual(schedule.current_window(Z("2027-03-27T22:00:00Z"), DEF).span(), "23:00–10:00")
        self.assertEqual(schedule.current_window(Z("2026-09-29T21:00:00Z"), DEF).span(), "23:00–09:00")

    def test_next_window(self):
        for now, start in [
            ("2026-09-26T10:00:00Z", "2026-09-26T21:00:00Z"),  # 12:00 CEST -> 23:00 CEST today
            ("2026-09-26T07:00:00Z", "2026-09-26T21:00:00Z"),  # 09:00 CEST (window just ended)
            ("2026-10-24T12:00:00Z", "2026-10-24T21:00:00Z"),  # DST end night starts in CEST
            ("2026-10-25T12:00:00Z", "2026-10-25T22:00:00Z"),  # the evening after is CET
            ("2027-03-27T12:00:00Z", "2027-03-27T22:00:00Z"),  # DST start night starts in CET
            ("2027-03-28T12:00:00Z", "2027-03-28T21:00:00Z"),  # the evening after is CEST
        ]:
            with self.subTest(now=now):
                self.assertEqual(schedule.next_window_start(Z(now), DEF), Z(start))
                self.assertEqual(schedule.next_window(Z(now), DEF).start, Z(start))
        inside = Z("2026-09-25T21:30:00Z")
        self.assertEqual(schedule.next_window_start(inside, DEF), inside)
        self.assertEqual(schedule.next_window(inside, DEF).start, Z("2026-09-26T21:00:00Z"))   # the next one

    def test_latest_next_and_postpone_deadline(self):
        sun = Z("2026-10-04T21:00:00Z")                       # Sun 23:00 CEST
        s2 = sun + H5                                         # Mon 04:00
        self.assertEqual(schedule.latest_session_start(sun + timedelta(hours=2), DEF), sun)
        self.assertEqual(schedule.latest_session_start(s2 + timedelta(hours=4), DEF), s2)
        self.assertIsNone(schedule.latest_session_start(sun - timedelta(hours=1), DEF))
        self.assertEqual(schedule.next_session_start(sun - timedelta(hours=1), DEF), sun)
        self.assertEqual(schedule.next_session_start(sun, DEF), s2)            # strictly after
        self.assertEqual(schedule.next_session_start(s2 + timedelta(hours=4, minutes=18), DEF),
                         sun + timedelta(days=1))                              # 08:18 -> Mon 23:00
        # D-202/D-203: until the next start of the same window; the last start: none (itself)
        self.assertEqual(schedule.postpone_deadline(sun, DEF), s2)
        self.assertEqual(schedule.postpone_deadline(sun + timedelta(hours=1), DEF), s2)
        self.assertEqual(schedule.postpone_deadline(s2, DEF), s2)
        noon = Z("2026-10-05T10:00:00Z")
        self.assertEqual(schedule.postpone_deadline(noon, DEF), noon)          # outside: no postponement
        # generalised (D-203 addendum): any start, any number of sessions
        three = schedule.Config.every_day("21:00", 3)
        w = schedule.current_window(Z("2026-10-04T19:00:00Z"), three)          # Sun 21:00 CEST x 3
        a, b, c = schedule.session_starts(w)
        self.assertEqual((b - a, c - b, w.end - c), (H5, H5, H5))
        self.assertEqual([schedule.postpone_deadline(x, three) for x in (a, b, c)], [b, c, c])

    def test_dst_postpone_deadline(self):
        fall = Z("2026-10-24T21:00:00Z")
        self.assertEqual(schedule.postpone_deadline(fall, DEF), Z("2026-10-25T02:00:00Z"))   # 03:00 CET
        spring = Z("2027-03-27T22:00:00Z")
        self.assertEqual(schedule.postpone_deadline(spring, DEF), Z("2027-03-28T03:00:00Z"))  # 05:00 CEST

    def test_keys(self):
        """One dedup key per session-window start: the window's end date (its night), -s2 ..."""
        for m, key in [("2026-09-29T21:00:05Z", "window-start-2026-09-30"),
                       ("2026-09-30T01:59:59Z", "window-start-2026-09-30"),
                       ("2026-09-30T02:00:00Z", "window-start-2026-09-30-s2"),
                       ("2026-09-30T06:59:59Z", "window-start-2026-09-30-s2"),
                       ("2026-09-30T10:00:00Z", "window-start-2026-10-01"),          # outside: the next
                       ("2026-10-25T01:59:59Z", "window-start-2026-10-25"),          # DST end
                       ("2026-10-25T02:00:00Z", "window-start-2026-10-25-s2"),
                       ("2027-03-28T02:59:59Z", "window-start-2027-03-28"),          # DST start
                       ("2027-03-28T03:00:00Z", "window-start-2027-03-28-s2")]:
            with self.subTest(m=m):
                self.assertEqual(schedule.start_key(Z(m), DEF), key)
        self.assertEqual(schedule.night(Z("2026-09-29T22:00:00Z"), DEF), "2026-09-30")


class CronFiring(unittest.TestCase):
    """docker/crontab runs --window-start every 30 min; due_session_start acts once per start."""

    def crontab_step(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker", "crontab")) as fh:
            line = next(x for x in fh if x.rstrip().endswith("# AFClaude window-start"))
        minute, hour = line.split()[:2]
        self.assertEqual((minute, hour), ("*/30", "*"))
        self.assertIn("--window-start", line)
        return timedelta(minutes=30)

    def fires(self, cfg, a, b):
        """(start, key) per acting cron run between a and b (UTC, every crontab step)."""
        step, t, out = self.crontab_step(), a, []
        self.assertEqual(step, schedule.CRON_STEP)
        while t < b:
            run = t + timedelta(seconds=5)                                    # supercronic is a little late
            s = schedule.due_session_start(run, cfg)
            if s is not None:
                out.append((s, schedule.start_key(run, cfg)))
            t += step
        return out

    def check(self, cfg, a, b):
        got = self.fires(cfg, a, b)
        want = [s for s, _ in schedule.session_slots(a - timedelta(microseconds=1), b, cfg)]
        self.assertEqual([s for s, _ in got], want)                            # every start, exactly once
        self.assertEqual(len({k for _, k in got}), len(got))                   # one key per start
        return got

    def test_default_both_dst_switches(self):
        for a, b in ((Z("2026-10-20T00:00:00Z"), Z("2026-10-30T00:00:00Z")),
                     (Z("2027-03-24T00:00:00Z"), Z("2027-04-02T00:00:00Z"))):
            self.assertGreaterEqual(len(self.check(DEF, a, b)), 2 * 8)
        # the DST nights: 23:00 and 03:00 CET (fall), 23:00 and 05:00 CEST (spring)
        fall = self.fires(DEF, Z("2026-10-24T12:00:00Z"), Z("2026-10-25T12:00:00Z"))
        self.assertEqual([berlin(s) for s, _ in fall], ["Sat 24.10. 23:00 CEST", "Sun 25.10. 03:00 CET"])
        spring = self.fires(DEF, Z("2027-03-27T12:00:00Z"), Z("2027-03-28T12:00:00Z"))
        self.assertEqual([berlin(s) for s, _ in spring], ["Sat 27.03. 23:00 CET", "Sun 28.03. 05:00 CEST"])

    def test_a_whole_year_per_weekday_and_30_min_grid(self):
        cfg = schedule.Config.every_day("23:00", 2, sat={"start": "20:30", "n": 1, "group": "g1"}, sun=None,
                                        wed={"start": "22:30", "n": 2, "group": "g2"})
        self.check(cfg, Z("2026-01-01T00:00:00Z"), Z("2027-01-01T00:00:00Z"))
        self.check(DEF, Z("2026-01-01T00:00:00Z"), Z("2027-01-01T00:00:00Z"))

    def test_not_due_between_starts(self):
        sun = Z("2026-10-04T21:00:00Z")
        self.assertEqual(schedule.due_session_start(sun + timedelta(seconds=5), DEF), sun)
        self.assertEqual(schedule.due_session_start(sun + timedelta(minutes=29), DEF), sun)
        self.assertIsNone(schedule.due_session_start(sun + timedelta(minutes=30), DEF))
        self.assertIsNone(schedule.due_session_start(sun + timedelta(hours=4, minutes=18), DEF))   # 03:18
        self.assertIsNone(schedule.due_session_start(sun - timedelta(minutes=1), DEF))           # outside


class PerWeekday(unittest.TestCase):
    """window_days: per weekday, link groups, nights without a window (D-030/D-033/D-034)."""
    CFG = schedule.Config.every_day("23:00", 2, sat={"start": "20:00", "n": 1, "group": "g1"}, sun=None)

    def test_different_and_no_windows(self):
        ws = schedule.windows(Z("2026-10-02T12:00:00Z"), Z("2026-10-06T12:00:00Z"), self.CFG)   # Fri..Tue
        self.assertEqual([(w.day, berlin(w.start), berlin(w.end)) for w in ws],
                         [("fri", "Fri 02.10. 23:00 CEST", "Sat 03.10. 09:00 CEST"),
                          ("sat", "Sat 03.10. 20:00 CEST", "Sun 04.10. 01:00 CEST"),
                          ("mon", "Mon 05.10. 23:00 CEST", "Tue 06.10. 09:00 CEST")])            # Sun: off
        sun_noon = Z("2026-10-04T10:00:00Z")
        self.assertFalse(schedule.in_window(sun_noon, self.CFG))
        self.assertEqual(berlin(schedule.next_session_start(sun_noon, self.CFG)), "Mon 05.10. 23:00 CEST")
        sat = Z("2026-10-03T18:30:00Z")                                           # Sat 20:30
        self.assertEqual(schedule.postpone_deadline(sat, self.CFG), Z("2026-10-03T18:00:00Z"))  # one session
        self.assertEqual(berlin(schedule.next_session_start(sat, self.CFG)), "Mon 05.10. 23:00 CEST")
        self.assertEqual(schedule.start_key(sat, self.CFG), "window-start-2026-10-04")

    def test_link_groups(self):
        cfg = schedule.Config({"mon": {"start": "23:00", "n": 2, "group": "work"},
                               "tue": {"start": "23:00", "n": 2, "group": "work"},
                               "wed": {"start": "22:00", "n": 1, "group": "mid"},
                               "thu": {"start": "23:00", "n": 2, "group": "work"},
                               "fri": {"start": "21:30", "n": 3, "group": "wkend"},
                               "sat": {"start": "21:30", "n": 3, "group": "wkend"}, "sun": None})
        ws = schedule.windows(Z("2026-10-05T00:00:00Z"), Z("2026-10-12T00:00:00Z"), cfg)
        self.assertEqual([(w.day, w.group, w.sessions, w.span()) for w in ws],
                         [("mon", "work", 2, "23:00–09:00"), ("tue", "work", 2, "23:00–09:00"),
                          ("wed", "mid", 1, "22:00–03:00"), ("thu", "work", 2, "23:00–09:00"),
                          ("fri", "wkend", 3, "21:30–12:30"), ("sat", "wkend", 3, "21:30–12:30")])
        self.assertEqual(schedule.describe(cfg).split(", ")[-1], "sun off x 5 h (Europe/Berlin)")
        # the actions validator accepts it (links agree, no overlap) and normalises nothing away
        self.assertEqual(actions._window_days(dict(cfg.days)), dict(cfg.days))

    def test_overlap_validation_uses_the_schedule(self):
        days = dict(schedule.Config.every_day().days, tue={"start": "08:00", "n": 1, "group": "x"})
        with self.assertRaisesRegex(ValueError, r"mon 23:00 x 2 ends tue 09:00, so tue can't start at 08:00"):
            actions._window_days(days)
        with self.assertRaisesRegex(ValueError, "overlap"):
            schedule.check_overlap(days, 5.0)
        schedule.check_overlap(dict(days, tue={"start": "09:00", "n": 1, "group": "x"}), 5.0)   # back to back

    def test_back_to_back_on_the_spring_forward_night(self):
        """Sat 23:00 x 2 ends Sun 10:00 CEST on 28.03.2027 (10 h absolute); Sun's 09:00 window
        then begins when Saturday's ends, still one whole session."""
        cfg = schedule.Config.every_day("23:00", 2, sun={"start": "09:00", "n": 1, "group": "s"},
                                        mon=None)
        ws = schedule.windows(Z("2027-03-27T12:00:00Z"), Z("2027-03-28T18:00:00Z"), cfg)
        self.assertEqual([(berlin(w.start), berlin(w.end)) for w in ws],
                         [("Sat 27.03. 23:00 CET", "Sun 28.03. 10:00 CEST"),
                          ("Sun 28.03. 10:00 CEST", "Sun 28.03. 15:00 CEST")])
        ws = schedule.windows(Z("2027-04-03T12:00:00Z"), Z("2027-04-04T18:00:00Z"), cfg)      # a normal week
        self.assertEqual(berlin(ws[1].start), "Sun 04.04. 09:00 CEST")

    def test_missing_and_ambiguous_start_times(self):
        cfg = schedule.Config.every_day("02:30", 1)
        self.assertEqual(schedule.next_window(Z("2027-03-27T12:00:00Z"), cfg).start,
                         Z("2027-03-28T01:30:00Z"))                              # 02:30 missing: 03:30 CEST
        self.assertEqual(schedule.next_window(Z("2026-10-24T12:00:00Z"), cfg).start,
                         Z("2026-10-25T00:30:00Z"))                              # ambiguous: the first (CEST)

    def test_30_min_grid_session_hours_and_time_zone(self):
        cfg = schedule.Config.every_day("22:30", 2, session_hours=4)
        w = schedule.next_window(Z("2026-10-05T12:00:00Z"), cfg)
        self.assertEqual([berlin(s) for s in w.session_starts()], ["Mon 05.10. 22:30 CEST", "Tue 06.10. 02:30 CEST"])
        self.assertEqual(w.span(), "22:30–06:30")
        ny = schedule.Config.every_day(tz="America/New_York")
        w = schedule.next_window(Z("2026-10-05T12:00:00Z"), ny)
        self.assertEqual((w.start, w.end), (Z("2026-10-06T03:00:00Z"), Z("2026-10-06T13:00:00Z")))   # 23:00 EDT
        # New York switches a week after Berlin: its own DST night is 23:00 EDT -> 08:00 EST
        w = schedule.next_window(Z("2026-10-31T12:00:00Z"), ny)
        self.assertEqual(w.span(), "23:00–08:00")

    def test_no_window_at_all(self):
        off = schedule.Config({d: None for d in schedule.DAYS})
        t = Z("2026-10-05T12:00:00Z")
        self.assertEqual(schedule.windows(t, t + timedelta(days=30), off), [])
        self.assertIsNone(schedule.next_window(t, off))
        self.assertIsNone(schedule.next_window_start(t, off))
        self.assertIsNone(schedule.next_session_start(t, off))
        self.assertIsNone(schedule.due_session_start(t, off))
        self.assertEqual((schedule.start_key(t, off), schedule.night(t, off)), ("window-start-none", "none"))
        self.assertEqual(schedule.describe(off), "no window on any day x 5 h (Europe/Berlin)")

    def test_same_end_date_keys_differ(self):
        """Sun 23:00 x 2 and Mon 10:00 x 1 both end on Monday: the key adds the start time."""
        cfg = schedule.Config.every_day("23:00", 2, mon={"start": "10:00", "n": 1, "group": "m"})
        a = schedule.start_key(Z("2026-10-04T21:00:05Z"), cfg)        # Sun 23:00
        b = schedule.start_key(Z("2026-10-05T08:00:05Z"), cfg)        # Mon 10:00
        self.assertEqual((a, b), ("window-start-2026-10-05-2300", "window-start-2026-10-05-1000"))


class NextRunWalk(unittest.TestCase):
    """pacing's next-run walk follows the real session-window starts across days."""

    def test_walk_across_a_no_window_night(self):
        cfg = schedule.Config.every_day("23:00", 2, tue=None)
        reset = Z("2026-10-10T17:00:00Z")                              # Sat 19:00
        now = Z("2026-10-06T10:00:00Z")                                # Tue 12:00: no window tonight
        d = pacing.next_run_core(10, reset, now, 0.2, None, "auto", 0, cfg, None)
        self.assertEqual((d["kind"], berlin(d["at"])), ("night", "Wed 07.10. 23:00 CEST"))
        d = pacing.next_run_core(10, reset, now, 0.2, None, "auto", 0, DEF, None)
        self.assertEqual(berlin(d["at"]), "Tue 06.10. 23:00 CEST")

    def test_nights_of_different_lengths(self):
        """The straight line ends at each start's own window end (Sat: 20:00 x 1 = 01:00)."""
        cfg = PerWeekday.CFG
        reset = Z("2026-10-08T17:00:00Z")                              # Thu 19:00
        now = Z("2026-10-03T12:00:00Z")                                # Sat 14:00
        d = pacing.decide_core(10, reset, Z("2026-10-03T18:00:00Z"), 600.0, None, None, None, True, 0.2,
                               "auto", cfg)
        self.assertIn("window end Sun 04.10. 01:00", d["reason"])
        d = pacing.next_run_core(5, reset, now, 0.2, None, "auto", 0, cfg, None)
        self.assertEqual(berlin(d["at"]), "Sat 03.10. 20:00 CEST")       # 25% <= 80% x 54/168 h
        # 10%: Sat's short night (ends 01:00) fails, Sun has no window: Mon 23:00
        d = pacing.next_run_core(10, reset, now, 0.2, None, "auto", 0, cfg, None)
        self.assertEqual(berlin(d["at"]), "Mon 05.10. 23:00 CEST")
        nothing = schedule.Config({x: None for x in schedule.DAYS})
        d = pacing.next_run_core(10, reset, now, 0.2, None, "auto", 0, nothing, None)
        self.assertEqual((d["kind"], d["at"]), ("after_reset", None))
        self.assertIn("no session-window start", d["reason"])


class Settings(unittest.TestCase):
    """load(): window_days / session_hours / window_tz from the DB (afclaude_config.settings)."""

    def tearDown(self):
        testenv.clear_settings()

    def test_defaults_and_db_values(self):
        testenv.clear_settings()
        self.assertEqual(schedule.load(), DEF)
        days = dict(DEF.days, sun=None, sat={"start": "21:30", "n": 1, "group": "g1"})
        testenv.setcfg(window_days=days, window_tz="Asia/Kolkata", session_hours=4)
        cfg = schedule.load()
        self.assertEqual(cfg, schedule.Config(days, 4.0, "Asia/Kolkata"))
        t = Z("2026-10-03T16:00:00Z")                                   # Sat 21:30 IST
        self.assertTrue(schedule.in_window(t))                          # cfg None: the settings
        self.assertEqual(schedule.current_window(t).end, t + timedelta(hours=4))

    def test_db_unavailable_falls_back_to_the_defaults(self):
        import store
        testenv.setcfg(window_days=dict(DEF.days, mon=None))
        old = store.DB_PATH
        with tempfile.TemporaryDirectory() as d:
            for path in (os.path.join(d, "missing.db"), d):
                store.DB_PATH = path
                try:
                    self.assertEqual(schedule.load(), DEF, path)
                finally:
                    store.DB_PATH = old
        self.assertIsNone(schedule.load().days["mon"])                 # the DB again


if __name__ == "__main__":
    unittest.main()
