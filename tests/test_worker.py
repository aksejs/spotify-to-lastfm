"""Unattended worker: Telegram, reports, halts, automatic resends. No real network."""

from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch

import test_importer
from test_importer import FakeAPI, NOW, event
from spotify_import.cli import main, run
from spotify_import.engine import assign, select, submit
from spotify_import.history import sync, verify
from spotify_import.worker import (
    MAX_AUTO_RESENDS,
    Notifier,
    auto_resend,
    maybe_report,
    needs_review,
    resume,
    serve,
    tick,
)


class Recorder(Notifier):
    def __init__(self):
        super().__init__({})
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


class Stop(Exception):
    pass


def sleeper(limit):
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= limit:
            raise Stop()

    return sleep, calls


# Imported as a module attribute so discovery does not rerun the base tests here.
class WorkerTest(test_importer.ImporterTest):
    def setUp(self):
        super().setUp()
        self.no_network = patch(
            "spotify_import.api.urlopen",
            side_effect=AssertionError("Real network forbidden in tests"),
        )
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.notifier = Recorder()

    def dropped(self, count=1):
        self.seed([event(master_metadata_track_name=f"T{i}") for i in range(count)])
        sync(self.store, FakeAPI(), self.cfg, 0, NOW, NOW)
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        api = FakeAPI(drop={r["assigned_ts"] for r in rows})
        submit(self.store, api, self.cfg, rows, NOW)
        verify(self.store, api, self.cfg, NOW + 60)
        return api, rows

    def test_telegram_send_hides_token_and_never_raises(self):
        notifier = Notifier(
            {"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "42"}
        )
        with (
            patch("spotify_import.worker.urlopen", side_effect=OSError("down")),
            redirect_stdout(StringIO()) as out,
        ):
            self.assertFalse(notifier.send("hello"))
        self.assertNotIn("SECRET", out.getvalue())

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return b'{"ok": true}'

        with (
            patch("spotify_import.worker.urlopen", return_value=Response()) as call,
            redirect_stdout(StringIO()),
        ):
            self.assertTrue(notifier.send("привет"))
        body = call.call_args.args[0].data.decode()
        self.assertIn("chat_id=42", body)

    def test_unconfigured_notifier_only_prints(self):
        with redirect_stdout(StringIO()) as out:
            self.assertFalse(Notifier({}).send("local only"))
        self.assertIn("local only", out.getvalue())

    def test_report_follows_each_finished_sending_session(self):
        self.seed([event(master_metadata_track_name=f"T{i}") for i in range(3)])
        sync(self.store, FakeAPI(), self.cfg, 0, NOW, NOW)
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        api = FakeAPI()
        submit(self.store, api, self.cfg, rows, NOW)
        verify(self.store, api, self.cfg, NOW + 60)
        # Still inside the session: no report yet.
        self.assertFalse(maybe_report(self.store, self.cfg, self.notifier, NOW + 120))
        self.assertTrue(maybe_report(self.store, self.cfg, self.notifier, NOW + 900))
        self.assertIn("отправка завершена", self.notifier.messages[0])
        self.assertIn("Отправлено: 3, сохранено: 3", self.notifier.messages[0])
        # Nothing new sent: no second report.
        self.assertFalse(maybe_report(self.store, self.cfg, self.notifier, NOW + 3600))

    def test_session_report_names_listening_period_and_profile_dates(self):
        self.seed(
            [
                event(ts="2020-01-01T00:05:00Z", master_metadata_track_name="A"),
                event(ts="2020-01-31T12:00:00Z", master_metadata_track_name="B"),
            ]
        )
        sync(self.store, FakeAPI(), self.cfg, 0, NOW, NOW)
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        api = FakeAPI()
        submit(self.store, api, self.cfg, rows, NOW)
        verify(self.store, api, self.cfg, NOW + 60)
        cfg = test_importer.replace(self.cfg, account_timezone="Europe/Warsaw")
        self.assertTrue(maybe_report(self.store, cfg, self.notifier, NOW + 900))
        text = self.notifier.messages[0]
        self.assertIn("Прослушивания: 01.01.2020 – 31.01.2020", text)
        # NOW - 2 days = 13 Jan 2027 08:00 UTC: one profile date.
        self.assertIn("В профиле на дату: 13.01.2027", text)

    def test_tick_runs_while_a_restart_waits_out_a_pause(self):
        import test_workflow

        self.seed()
        sync(self.store, FakeAPI(), self.cfg, 0, NOW, NOW)
        with self.store.conn:
            self.store.set("cooldown_until", NOW + 300)
        api = FakeAPI()
        api.check_account = lambda account: None
        ticks = []
        clock = test_workflow.Clock()
        args = SimpleNamespace(approve_plan=None, approve_continuous=self.cfg.account)
        with (
            patch("spotify_import.cli.time.time", clock.time),
            patch("spotify_import.cli.time.sleep", clock.sleep),
            redirect_stdout(StringIO()),
        ):
            run(self.store, api, self.cfg, args, tick=ticks.append)
        # Ticked during the pause, before the account check and first send.
        self.assertLess(ticks[0], NOW + 300)
        self.assertEqual(api.calls, 1)

    def test_daily_sign_of_life_only_when_nothing_was_sent(self):
        self.seed()
        # NOW is 08:00 UTC = 09:00 Warsaw in January.
        cfg = test_importer.replace(
            self.cfg, account_timezone="Europe/Warsaw", report_hour=21
        )
        self.assertFalse(maybe_report(self.store, cfg, self.notifier, NOW))
        evening = NOW + 12 * 3600
        self.assertTrue(maybe_report(self.store, cfg, self.notifier, evening))
        self.assertFalse(maybe_report(self.store, cfg, self.notifier, evening + 600))
        self.assertIn("сводка", self.notifier.messages[0])
        self.assertTrue(maybe_report(self.store, cfg, self.notifier, evening + 86400))

    def test_auto_resend_waits_an_hour_and_stops_after_limit(self):
        api, rows = self.dropped()
        self.assertEqual(
            auto_resend(self.store, api, self.cfg, NOW + 120)["requeued"], 0
        )
        later = NOW + 7200
        for attempt in range(MAX_AUTO_RESENDS):
            self.assertEqual(
                auto_resend(self.store, api, self.cfg, later)["requeued"], 1
            )
            again = assign(self.store, select(self.store, 50), self.cfg, later)
            api.drop = {again[0]["assigned_ts"]}
            submit(self.store, api, self.cfg, again, later)
            verify(self.store, api, self.cfg, later + 60)
            later += 7200
        self.assertEqual(auto_resend(self.store, api, self.cfg, later)["requeued"], 0)
        self.assertEqual(len(needs_review(self.store)), 1)

    def test_tick_reports_exhausted_resends_once(self):
        api, rows = self.dropped()
        with self.store.conn:
            for _ in range(MAX_AUTO_RESENDS):
                self.store.conn.execute(
                    "INSERT INTO decisions(event_id,time,action,note) VALUES (?,?,?,?)",
                    (rows[0]["id"], NOW, "resend", "{}"),
                )
        with redirect_stdout(StringIO()):
            tick(self.store, api, self.cfg, self.notifier, NOW + 7200)
            tick(
                self.store,
                api,
                self.cfg,
                self.notifier,
                NOW + 7200 + self.cfg.poll_seconds,
            )
        alerts = [m for m in self.notifier.messages if "не сохраняет" in m]
        self.assertEqual(len(alerts), 1)

    def serve_with(self, fake_run, limit=3):
        sleep, calls = sleeper(limit)
        args = SimpleNamespace(approve_plan=None, approve_continuous=self.cfg.account)
        with self.assertRaises(Stop), redirect_stdout(StringIO()):
            serve(
                self.store,
                FakeAPI(),
                self.cfg,
                args,
                self.notifier,
                fake_run,
                sleep=sleep,
                clock=lambda: NOW,
            )
        return calls

    def test_halt_persists_until_resume_and_survives_restart(self):
        self.seed()
        runs = []

        def failing(*a, **k):
            runs.append(1)
            raise ValueError("Accepted scrobbles are missing; stopped for review")

        self.serve_with(failing)
        self.assertEqual(len(runs), 1)  # Halted after the first failure only.
        self.assertIn("missing", self.store.get("halted")["reason"])
        self.assertTrue(any("остановлен" in m for m in self.notifier.messages))
        # A restarted worker stays halted and never calls run.
        self.serve_with(failing)
        self.assertEqual(len(runs), 1)
        with self.assertRaises(ValueError):
            run(
                self.store,
                FakeAPI(),
                self.cfg,
                SimpleNamespace(approve_plan=None, approve_continuous=self.cfg.account),
            )
        resume(self.store, "checked history", NOW)
        self.assertIsNone(self.store.get("halted"))
        self.assertEqual(len(self.store.get("halt_history")), 1)

    def test_transient_errors_wait_instead_of_halting(self):
        from spotify_import.api import APIError, TransportError

        self.seed()
        errors = iter([TransportError("x"), APIError(8)])

        def flaky(*a, **k):
            raise next(errors)

        self.serve_with(flaky, limit=2)
        self.assertIsNone(self.store.get("halted"))

    def test_finished_queue_reports_once_and_idles(self):
        self.seed([])  # Nothing to send.
        calls = []

        def done(*a, **k):
            calls.append(1)

        self.serve_with(done, limit=2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("завершён" in m for m in self.notifier.messages))

    def test_cli_resume_works_while_worker_holds_the_lock(self):
        self.store.conn.close()
        args = ["--db", str(self.root / "state.sqlite")]
        from spotify_import.db import Store

        store = Store(self.root / "state.sqlite")
        with store.conn:
            store.set("halted", {"reason": "test", "at": NOW})
        with store.lock(), redirect_stdout(StringIO()) as out:
            self.assertEqual(main(args + ["resume", "--note", "ok"]), 0)
        self.assertIn("resumed_from", out.getvalue())
        store.conn.close()
        self.store = Store(self.root / "state.sqlite")
        self.assertIsNone(self.store.get("halted"))
        self.assertIn('"halted"', json.dumps(list(_status_keys(self.store, self.cfg))))


def _status_keys(store, cfg):
    from spotify_import.engine import status

    return status(store, cfg, int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())).keys()


for name in list(test_importer.ImporterTest.__dict__):
    if name.startswith("test_"):
        setattr(WorkerTest, name, None)
del name
