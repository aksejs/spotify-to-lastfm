"""Post-send history checks, missing scrobbles, and resends. Networking is faked."""

import json
from dataclasses import replace

import test_importer
import test_workflow
from test_importer import FakeAPI, NOW, event
from spotify_import.api import TransportError
from spotify_import.db import Store
from spotify_import.engine import DAY, assign, dry_run, select, status, submit
from spotify_import.history import resend, verify


# Imported as a module attribute so discovery does not rerun the base tests here.
class VerificationTest(test_workflow.WorkflowTest):
    def sent(self, api, count=2):
        self.seed([event() for _ in range(count)])
        self.full_snapshot()
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        submit(self.store, api, self.cfg, rows, NOW)
        return rows

    def by_status(self):
        state = status(self.store, self.cfg, NOW)
        return {
            k: state[f"confirmed_{k}"] for k in ("verified", "unchecked", "missing")
        }

    def test_lastfm_capitalization_still_verifies(self):
        api = FakeAPI(display=str.upper)
        self.sent(api)
        result = verify(self.store, api, self.cfg, NOW + 60)
        self.assertEqual(result["verified"], 2)
        self.assertEqual(
            self.by_status(), {"verified": 2, "unchecked": 0, "missing": 0}
        )

    def test_corrected_names_from_response_verify(self):
        api = FakeAPI()
        row = self.sent(api, 1)[0]
        stored = json.loads(self.rows()[0]["response"])
        stored["artist"]["#text"] = "Corrected Artist"
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE events SET response=?", (json.dumps(stored),)
            )
        api.tracks.clear()
        api.tracks[(row["assigned_ts"], "corrected artist", row["track"], "")] = 1
        self.assertEqual(verify(self.store, api, self.cfg, NOW + 60)["verified"], 1)

    def test_lastfm_renamed_scrobbles_verify_but_other_tracks_do_not(self):
        # Real renames seen in batch 2, reported with corrected="0".
        cases = [
            ("DIIV", "How Long Have You Known", "DIIV", "How Long Have You Known?"),
            (
                "Gorillaz",
                "Some Kind of Nature (feat. Lou Reed)",
                "Gorillaz",
                "Some Kind of Nature",
            ),
            ("The Make-Up", "Joy of Sound", "Make Up", "Joy of Sound"),
            (
                "Гражданская Оборона",
                "Всё идёт по плану",
                "гражданская оборона",
                "Всё Идёт По Плану",
            ),
            ("DIIV", "Doused", "DIIV", "Oshin"),
        ]
        self.seed(
            [
                event(
                    master_metadata_album_artist_name=artist,
                    master_metadata_track_name=track,
                )
                for artist, track, _, _ in cases
            ]
        )
        self.full_snapshot()
        api = FakeAPI()
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        api.drop = {r["assigned_ts"] for r in rows}
        submit(self.store, api, self.cfg, rows, NOW)
        stored = {(r["artist"], r["track"]): r for r in rows}
        for artist, track, shown_artist, shown_track in cases:
            ts = stored[(artist, track)]["assigned_ts"]
            api.tracks[(ts, shown_artist, shown_track, "")] = 1
        result = verify(self.store, api, self.cfg, NOW + 60)
        self.assertEqual(result["verified"], 4)
        self.assertEqual(result["missing_ids"], [stored[("DIIV", "Doused")]["id"]])

    def test_accepted_but_dropped_is_recorded_missing_not_replayed(self):
        api = FakeAPI()
        self.seed([event(), event()])
        self.full_snapshot()
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        api.drop = {rows[1]["assigned_ts"]}
        submit(self.store, api, self.cfg, rows, NOW)
        result = verify(self.store, api, self.cfg, NOW + 60)
        self.assertEqual(result["missing_ids"], [rows[1]["id"]])
        self.assertEqual(
            self.by_status(), {"verified": 1, "unchecked": 0, "missing": 1}
        )
        # Still confirmed (Last.fm accepted it) and never selected again by itself.
        self.assertEqual(select(self.store, 50), [])
        # Appearing later (Last.fm lag) clears the missing mark.
        api.tracks[
            (rows[1]["assigned_ts"], rows[1]["artist"], rows[1]["track"], "")
        ] = 1
        verify(self.store, api, self.cfg, NOW + 120, unresolved=True)
        self.assertEqual(self.by_status()["verified"], 2)

    def test_every_batch_checked_before_next_send(self):
        self.seed([event() for _ in range(6)])
        self.full_snapshot()
        api = FakeAPI()
        reads = []
        history = api.history
        api.history = lambda *a: reads.append(api.calls) or history(*a)
        clock = self.scheduler(api, replace(self.cfg, batch_size=2))
        self.assertEqual(api.calls, 3)
        # A history read follows each send, before the next one.
        for sent in (1, 2, 3):
            self.assertIn(sent, reads)
        self.assertEqual(
            self.by_status(), {"verified": 6, "unchecked": 0, "missing": 0}
        )
        self.assertGreaterEqual(clock.slept, 3 * self.cfg.verify_delay_seconds)

    def test_unchecked_batch_blocks_writes_until_history_readable(self):
        api = FakeAPI()
        self.sent(api)  # Simulates a crash after acceptance, before the check.
        self.seed([event(ts="2020-01-02T00:00:00Z")], name="endsong_1.json")
        history = api.history
        failures = iter([TransportError("offline")])

        def flaky(*args):
            error = next(failures, None)
            if error:
                self.assertEqual(api.calls, 1)  # Nothing new sent while unreadable.
                raise error
            return history(*args)

        api.history = flaky
        self.scheduler(api)
        self.assertEqual(api.calls, 2)
        self.assertEqual(self.by_status()["verified"], 3)

    def test_isolated_drop_continues_but_pattern_stops(self):
        self.seed([event() for _ in range(4)])
        self.full_snapshot()
        api = FakeAPI()
        start = NOW - self.cfg.timestamp_age_days * DAY
        api.drop = {start}
        self.scheduler(api, replace(self.cfg, batch_size=2))
        self.assertEqual(api.calls, 2)
        self.assertEqual(self.by_status()["missing"], 1)

        self.seed(
            [event(ts="2021-01-01T00:00:00Z") for _ in range(4)], "endsong_1.json"
        )
        api.drop = set(range(NOW - 3 * DAY, NOW))
        with self.assertRaises(ValueError):
            self.scheduler(api, replace(self.cfg, batch_size=2))
        self.assertEqual(api.calls, 3)

    def test_manual_plan_is_checked_after_sending(self):
        self.seed()
        self.full_snapshot()
        plan = dry_run(self.store, self.cfg, now=NOW)["plan_id"]
        api = FakeAPI(display=str.lower)
        self.scheduler(api, plan=plan)
        self.assertEqual(
            self.by_status(), {"verified": 2, "unchecked": 0, "missing": 0}
        )

    def test_resend_requires_age_and_fresh_absence_then_gets_new_timestamp(self):
        api = FakeAPI()
        self.seed([event(), event()])
        self.full_snapshot()
        rows = assign(self.store, select(self.store, 50), self.cfg, NOW)
        lost = rows[0]
        api.drop = {lost["assigned_ts"]}
        submit(self.store, api, self.cfg, rows, NOW)
        verify(self.store, api, self.cfg, NOW + 60)
        with self.assertRaises(ValueError):
            resend(self.store, api, self.cfg, [], "too soon", NOW + 120)
        with self.assertRaises(ValueError):
            resend(self.store, api, self.cfg, [rows[1]["id"]], "verified", NOW + 7200)
        result = resend(self.store, api, self.cfg, [], "absent twice", NOW + 7200)
        self.assertEqual(result["requeued"], 1)
        queued = select(self.store, 50)
        self.assertEqual([r["id"] for r in queued], [lost["id"]])
        again = assign(self.store, queued, self.cfg, NOW + 7200)[0]
        self.assertNotEqual(again["assigned_ts"], lost["assigned_ts"])
        decision = self.store.rows("SELECT * FROM decisions WHERE action='resend'")[0]
        self.assertEqual(
            json.loads(decision["note"])["old_timestamp"], lost["assigned_ts"]
        )

    def test_resend_skips_scrobbles_that_appeared_meanwhile(self):
        api = FakeAPI()
        rows = self.sent(api, 1)
        api.tracks.clear()
        verify(self.store, api, self.cfg, NOW + 60)
        api.tracks[
            (rows[0]["assigned_ts"], rows[0]["artist"], rows[0]["track"], "")
        ] = 1
        result = resend(self.store, api, self.cfg, [], "late", NOW + 7200)
        self.assertEqual(result, {"requeued": 0, "found_on_recheck": 1})
        self.assertEqual(self.rows()[0]["status"], "confirmed")

    def test_timestamp_spacing(self):
        self.seed([event() for _ in range(3)])
        cfg = replace(self.cfg, timestamp_spacing_seconds=60)
        rows = assign(self.store, select(self.store, 50), cfg, NOW)
        times = [r["assigned_ts"] for r in rows]
        self.assertEqual([b - a for a, b in zip(times, times[1:])], [60, 60])
        with self.assertRaises(ValueError):
            replace(self.cfg, timestamp_spacing_seconds=0)

    def test_schema_v1_database_upgrades_in_place(self):
        self.seed()
        with self.store.conn:
            for column in ("verified_at", "missing_at"):
                self.store.conn.execute(f"ALTER TABLE events DROP COLUMN {column}")
            self.store.set("schema_version", 1)
        self.store.conn.close()
        self.store = Store(self.root / "state.sqlite")
        self.assertEqual(self.store.get("schema_version"), 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertIsNone(self.rows()[0]["verified_at"])


for base in (test_importer.ImporterTest, test_workflow.WorkflowTest):
    for name in list(base.__dict__):
        if name.startswith("test_"):
            setattr(VerificationTest, name, None)
del base, name
