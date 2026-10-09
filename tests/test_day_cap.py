"""Per-day cap: Last.fm silently drops scrobbles beyond ~750 per calendar day."""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import test_importer
import test_workflow
from test_importer import FakeAPI, NOW, event
from spotify_import.cli import run
from spotify_import.config import Config
from spotify_import.engine import DAY, assign, select
from spotify_import.history import sync

# NOW is 2027-01-15 08:00 UTC; the cursor starts two days earlier.
JAN13 = NOW - 2 * DAY - 8 * 3600
JAN14 = JAN13 + DAY


# Imported as a module attribute so discovery does not rerun the base tests here.
class DayCapTest(test_workflow.WorkflowTest):
    def setUp(self):
        super().setUp()
        self.cfg = replace(
            self.cfg, max_scrobbles_per_day=3, timestamp_spacing_seconds=10
        )

    def fresh(self, count):
        return [r for r in select(self.store, 50) if r["assigned_ts"] is None][:count]

    def live(self, *timestamps):
        api = FakeAPI()
        for ts in timestamps:
            api.tracks[(ts, "Live", f"Song {ts}", "")] += 1
        sync(self.store, api, self.cfg, 0, NOW, NOW)

    def test_full_day_moves_to_next_midnight(self):
        self.seed([event() for _ in range(5)])
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        times = [r["assigned_ts"] for r in rows]
        self.assertTrue(all(JAN13 <= t < JAN14 for t in times[:3]))
        self.assertEqual(times[3:], [JAN14, JAN14 + 10])

    def test_live_plays_count_toward_the_cap(self):
        self.live(JAN13 + 3600, JAN13 + 7200)
        self.seed([event() for _ in range(2)])
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        self.assertLess(rows[0]["assigned_ts"], JAN14)
        self.assertEqual(rows[1]["assigned_ts"], JAN14)

    def test_unfinished_day_is_never_used(self):
        self.seed([event() for _ in range(8)])
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW, partial=True)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["assigned_ts"] < JAN14 + DAY for r in rows))
        with self.assertRaises(ValueError):
            assign(self.store, select(self.store, 50), self.cfg, NOW)

    def test_account_timezone_day_is_also_capped(self):
        # 23:10-23:30 UTC on Jan 13 is already Jan 14 in Warsaw (UTC+1).
        self.live(JAN14 - 3000, JAN14 - 2400, JAN14 - 1800)
        self.seed([event()])
        utc_only = assign(self.store, select(self.store, 1), self.cfg, NOW)
        self.assertEqual(utc_only[0]["assigned_ts"], JAN14)
        with self.store.conn:
            self.store.conn.execute("UPDATE events SET assigned_ts=NULL")
        warsaw = replace(self.cfg, account_timezone="Europe/Warsaw")
        # Jan 14 Warsaw is full; Jan 15 Warsaw has not ended yet.
        self.assertEqual(
            assign(self.store, select(self.store, 1), warsaw, NOW, partial=True), []
        )

    def test_assigned_and_unchecked_imports_count_but_dropped_do_not(self):
        self.seed([event() for _ in range(4)])
        first = assign(self.store, self.fresh(3), self.cfg, NOW)
        self.assertEqual(
            assign(self.store, self.fresh(1), self.cfg, NOW)[0]["assigned_ts"],
            JAN14,
        )
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE events SET assigned_ts=NULL WHERE assigned_ts=?", (JAN14,)
            )
            self.store.conn.execute(
                """UPDATE events SET status='confirmed',attempts=1,missing_at=?
                WHERE id=?""",
                (NOW, first[0]["id"]),
            )
        # Last.fm dropped one, so Jan 13 has room again.
        again = assign(self.store, self.fresh(1), self.cfg, NOW)
        self.assertLess(again[0]["assigned_ts"], JAN14)

    def test_preassigned_row_on_overfull_day_is_refused(self):
        self.seed([event()])
        rows = assign(self.store, select(self.store, 1), self.cfg, NOW)
        self.live(rows[0]["assigned_ts"] + 1, rows[0]["assigned_ts"] + 2, JAN13 + 5)
        with self.assertRaises(ValueError):
            assign(self.store, select(self.store, 1), self.cfg, NOW)

    def run_once(self, api, cfg):
        api.check_account = lambda account: None
        args = SimpleNamespace(
            approve_plan=None, approve_continuous=cfg.account, once=True
        )
        clock = test_workflow.Clock()
        with (
            patch("spotify_import.cli.time.time", clock.time),
            patch("spotify_import.cli.time.sleep", clock.sleep),
            redirect_stdout(StringIO()) as out,
        ):
            run(self.store, api, cfg, args)
        return out.getvalue()

    def test_once_stops_when_no_day_has_room(self):
        self.seed([event() for _ in range(10)])
        self.full_snapshot()
        api = FakeAPI()
        output = self.run_once(api, replace(self.cfg, batch_size=4))
        self.assertIn("No day with room", output)
        sent = self.store.rows("SELECT assigned_ts FROM attempts")
        self.assertEqual(len(sent), 6)
        self.assertEqual(
            self.store.rows(
                "SELECT count(*) n FROM events WHERE missing_at IS NOT NULL"
            )[0]["n"],
            0,
        )

    def test_continuous_waits_for_the_next_day_to_finish(self):
        self.seed([event() for _ in range(8)])
        self.full_snapshot()
        api = FakeAPI()
        clock = self.scheduler(api, replace(self.cfg, batch_size=4))
        self.assertEqual(
            self.store.rows("SELECT count(*) n FROM events WHERE status='confirmed'")[
                0
            ]["n"],
            8,
        )
        self.assertGreater(clock.slept, 18 * 3600)

    def test_backfill_starts_at_oldest_allowed_day_and_moves_forward(self):
        cfg = replace(self.cfg, timestamp_age_days=12)
        self.seed([event() for _ in range(7)])
        first = assign(self.store, self.fresh(4), cfg, NOW)
        second = assign(self.store, self.fresh(3), cfg, NOW)
        times = [r["assigned_ts"] for r in first + second]
        self.assertEqual(times[0], NOW - 12 * DAY)
        self.assertEqual(times, sorted(times))
        day = [(t - (NOW - 12 * DAY - 8 * 3600)) // DAY for t in times]
        self.assertEqual(day, [0, 0, 0, 1, 1, 1, 2])

    def test_whole_window_is_reread_once_per_poll_interval(self):
        from spotify_import.cli import refresh_for_write

        cfg = replace(self.cfg, timestamp_age_days=12)
        api = FakeAPI()
        ranges = []
        history = api.history
        api.history = lambda account, start, end: (
            ranges.append(end - start) or history(account, start, end)
        )
        refresh_for_write(self.store, api, cfg, [], NOW)
        refresh_for_write(self.store, api, cfg, [], NOW + 60)
        refresh_for_write(self.store, api, cfg, [], NOW + cfg.poll_seconds + 1)
        self.assertEqual(ranges, [13 * DAY, DAY, 13 * DAY])

    def test_invalid_timezone_and_cap(self):
        with self.assertRaises(ValueError):
            Config(account_timezone="Mars/Olympus")
        with self.assertRaises(ValueError):
            Config(max_scrobbles_per_day=0)


for base in (test_importer.ImporterTest, test_workflow.WorkflowTest):
    for name in list(base.__dict__):
        if name.startswith("test_"):
            setattr(DayCapTest, name, None)
del base, name
