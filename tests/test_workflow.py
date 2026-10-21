"""CLI and unattended-operation tests. All networking is replaced by fakes."""

from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_importer
from test_importer import FakeAPI, NOW, event
from spotify_import.api import LastFM, TransportError
from spotify_import.cli import main, run
from spotify_import.db import Store
from spotify_import.engine import assign, budget, select, submit
from spotify_import.history import overlaps, sync
from spotify_import.prepare import reclassify


class Clock:
    def __init__(self):
        self.now = NOW
        self.slept = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(1, seconds)
        self.slept += seconds


# Reuse fixture helpers, but not the base class's test methods a second time.
# Imported as a module attribute so discovery does not rerun the base tests here.
class WorkflowTest(test_importer.ImporterTest):
    def setUp(self):
        super().setUp()
        self.no_network = patch(
            "spotify_import.api.urlopen",
            side_effect=AssertionError("Real network forbidden in tests"),
        )
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def scheduler(self, api, cfg=None, plan=None):
        api.check_account = getattr(api, "check_account", lambda account: None)
        args = SimpleNamespace(
            approve_plan=plan, approve_continuous=None if plan else self.cfg.account
        )
        clock = Clock()
        with (
            patch("spotify_import.cli.time.time", clock.time),
            patch("spotify_import.cli.time.sleep", clock.sleep),
            redirect_stdout(StringIO()),
        ):
            run(self.store, api, cfg or self.cfg, args)
        return clock

    def full_snapshot(self):
        sync(self.store, FakeAPI(), self.cfg, 0, NOW, NOW)

    def test_scheduler_crosses_rolling_reset_without_user_action(self):
        self.seed([event() for _ in range(13)])
        self.full_snapshot()
        cfg = replace(self.cfg, daily_budget=5, combined_budget=5, batch_size=2)
        api = FakeAPI()
        clock = self.scheduler(api, cfg)
        self.assertEqual(
            len([r for r in self.rows() if r["status"] == "confirmed"]), 13
        )
        self.assertGreaterEqual(clock.slept, 2 * 86400)
        times = [r["sent_at"] for r in self.store.rows("SELECT sent_at FROM attempts")]
        for t in times:
            self.assertLessEqual(sum(t - 86400 < sent <= t for sent in times), 5)
        self.assertTrue(list(self.root.glob("state-backups/auto-*.sqlite")))

    def test_wakes_when_the_rolling_budget_frees_not_at_the_next_poll(self):
        self.seed([event(master_metadata_track_name=f"T{i}") for i in range(4)])
        self.full_snapshot()
        cfg = replace(
            self.cfg, daily_budget=2, combined_budget=2, batch_size=2, poll_seconds=3600
        )
        self.scheduler(FakeAPI(), cfg)
        sent = sorted(
            {r["sent_at"] for r in self.store.rows("SELECT sent_at FROM attempts")}
        )
        self.assertEqual(len(sent), 2)
        self.assertLess(sent[1] - sent[0], 86400 + 120)

    def test_successful_history_read_resets_failure_streak(self):
        self.seed([event(master_metadata_track_name=f"T{i}") for i in range(2)])
        self.full_snapshot()
        cfg = replace(self.cfg, daily_budget=1, combined_budget=1, batch_size=1)
        submit(
            self.store,
            FakeAPI(),
            cfg,
            assign(self.store, select(self.store, 1), cfg, NOW),
            NOW,
        )
        with self.store.conn:
            self.store.set("consecutive_failures", 7)
        api = FakeAPI()
        api.check_account = lambda account: None
        args = SimpleNamespace(
            approve_plan=None, approve_continuous=cfg.account, once=True
        )
        with (
            patch("spotify_import.cli.time.time", Clock().time),
            redirect_stdout(StringIO()),
        ):
            run(self.store, api, cfg, args)
        self.assertEqual(self.store.get("consecutive_failures"), 0)

    def test_startup_outage_recovers_without_writes_until_authenticated(self):
        self.seed()
        self.full_snapshot()
        api = FakeAPI()
        api.check_account = __import__("unittest.mock", fromlist=["Mock"]).Mock(
            side_effect=[TransportError("offline"), None]
        )
        clock = self.scheduler(api)
        self.assertGreaterEqual(clock.slept, 60)
        self.assertEqual(api.calls, 1)
        self.assertEqual(api.check_account.call_count, 2)

    def test_uncertain_batch_is_quarantined_while_remaining_queue_continues(self):
        self.seed([event() for _ in range(4)])
        self.full_snapshot()
        api = FakeAPI()
        original_submit = api.submit
        calls = []

        def send(rows):
            calls.append([r["id"] for r in rows])
            if len(calls) == 1:
                raise TransportError("response lost")
            return original_submit(rows)

        api.submit = send
        self.scheduler(api, replace(self.cfg, batch_size=2))
        self.assertEqual(
            Counter(r["status"] for r in self.rows()), Counter(uncertain=2, confirmed=2)
        )
        self.assertFalse(set(calls[0]) & set(calls[1]))

    def test_continuous_requires_full_history(self):
        self.seed()
        with self.assertRaisesRegex(ValueError, "sync-history"):
            self.scheduler(FakeAPI())

    def test_changed_policy_blocks_submission(self):
        self.seed()
        self.full_snapshot()
        with self.assertRaisesRegex(ValueError, "Preparation policy"):
            self.scheduler(FakeAPI(), replace(self.cfg, exclude_skipped=True))

    def test_auto_backup_retention_never_prunes_manual_backups(self):
        cfg = replace(self.cfg, backup_keep=2)
        self.seed()
        folder = self.root / "state-backups"
        folder.mkdir()
        manual = folder / "manual.sqlite"
        manual.write_bytes(b"keep")
        for day in range(4):
            self.store.automatic_backup(cfg, NOW + day * 86400)
        self.assertEqual(len(list(folder.glob("auto-*.sqlite"))), 2)
        self.assertTrue(manual.exists())
        latest = Store(sorted(folder.glob("auto-*.sqlite"))[-1])
        self.assertEqual(
            latest.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        self.assertEqual(len(latest.rows("SELECT * FROM events")), 2)
        latest.conn.close()

    def test_210_days_with_restarts_budget_and_no_repeated_event_ids(self):
        self.seed([event() for _ in range(1050)])
        cfg = replace(self.cfg, daily_budget=5, combined_budget=5, batch_size=5)
        api = FakeAPI()
        for day in range(210):
            now = NOW + day * (86400 + 60)
            rows = assign(self.store, select(self.store, 5), cfg, now)
            self.assertEqual(len(rows), 5)
            submit(self.store, api, cfg, rows, now)
            self.assertEqual(budget(self.store, cfg, now)["remaining_daily_budget"], 0)
            if day % 10 == 0:
                self.store.conn.close()
                self.store = Store(self.root / "state.sqlite")
                self.store.recover()
        self.assertTrue(
            all(r["status"] == "confirmed" and r["attempts"] == 1 for r in self.rows())
        )
        self.assertEqual(len({r["assigned_ts"] for r in self.rows()}), 1050)
        self.assertEqual(api.calls, 210)

    def test_manual_overlap_keep_and_exclude_survive_refresh_and_reclassify(self):
        from spotify_import.engine import resolve

        self.seed()
        row = self.rows()[0]
        api = FakeAPI()
        api.tracks[(row["original_epoch"], row["artist"], row["track"], "")] = 1
        sync(self.store, api, self.cfg, 0, NOW, NOW)
        overlaps(self.store)
        resolve(self.store, row["id"], "keep", "Reviewed distinct play", NOW)
        other = self.rows()[1]
        resolve(self.store, other["id"], "exclude", "Reviewed existing play", NOW)
        overlaps(self.store)
        reclassify(self.store, self.cfg)
        self.assertIsNone(
            self.store.rows("SELECT overlap FROM events WHERE id=?", (row["id"],))[0][
                "overlap"
            ]
        )
        self.assertEqual(
            self.store.rows("SELECT exclusion FROM events WHERE id=?", (other["id"],))[
                0
            ]["exclusion"],
            "manual_overlap",
        )

    def test_private_history_sends_session_authentication(self):
        api = LastFM(
            {
                "LASTFM_API_KEY": "key",
                "LASTFM_API_SECRET": "secret",
                "LASTFM_SESSION_KEY": "session",
            }
        )
        with patch.object(
            api,
            "call",
            return_value={
                "recenttracks": {
                    "@attr": {"totalPages": "0", "total": "0"},
                    "track": [],
                }
            },
        ) as call:
            self.assertEqual(api.history("user", 0, 100), Counter())
        self.assertTrue(call.call_args.kwargs["signed"])
        self.assertTrue(call.call_args.kwargs["session"])

    def test_cli_prepare_preview_export_is_offline_and_idempotent(self):
        fixture = Path(__file__).parent / "fixtures" / "endsong_synthetic.json"
        self.source.joinpath(fixture.name).write_bytes(fixture.read_bytes())
        args = ["--db", str(self.root / "cli.sqlite")]
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(args + ["prepare", str(self.source)]), 0)
        self.assertIn("events_added", output.getvalue())
        with redirect_stdout(StringIO()) as output:
            main(args + ["prepare", str(self.source)])
        self.assertIn('"events_added": 0', output.getvalue())
        with redirect_stdout(StringIO()) as output:
            main(args + ["dry-run", "--limit", "2"])
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["status"]["total_events"], 4)
        self.assertEqual(len(preview["preview"]), 2)
        self.assertIn("UNAVAILABLE", preview["overlap_check"])
        export = self.root / "audit.json"
        with redirect_stdout(StringIO()):
            main(args + ["export-audit", "--format", "json", str(export)])
        self.assertEqual(len(json.loads(export.read_text())["events"]), 4)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(args + ["run"])


# unittest normally inherits base tests; keep only the additional workflow cases.
for name in list(test_importer.ImporterTest.__dict__):
    if name.startswith("test_"):
        setattr(WorkflowTest, name, None)
