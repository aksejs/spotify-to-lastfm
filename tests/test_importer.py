import json
import sqlite3
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from spotify_import.api import APIError, LastFM, TransportError, signature
from spotify_import.config import Config
from spotify_import.db import Store
from spotify_import.engine import (
    DAY,
    assign,
    budget,
    dry_run,
    load_plan,
    reset_timestamp,
    resolve,
    select,
    status,
    submit,
)
from spotify_import.history import overlaps, sync, verify
from spotify_import.prepare import prepare, reclassify

NOW = 1800000000
TRACK_ID = "1nU1x5TSTtj0zXYtQZ7tQd"


def event(**changes):
    value = {
        "ts": "2020-01-01T00:05:00Z",
        "ms_played": 240000,
        "master_metadata_album_artist_name": "AL-90",
        "master_metadata_track_name": "Радикальные Танцы",
        "master_metadata_album_album_name": "Album",
        "spotify_track_uri": "spotify:track:" + TRACK_ID,
        "skipped": False,
    }
    return {**value, **changes}


def response(rows, codes=None):
    codes = codes or [0] * len(rows)
    items = [
        {
            "timestamp": str(row["assigned_ts"]),
            "artist": {"#text": row["artist"], "corrected": "0"},
            "track": {"#text": row["track"], "corrected": "0"},
            "album": {"#text": row["album"]},
            "ignoredMessage": {"code": str(code), "#text": ""},
        }
        for row, code in zip(rows, codes)
    ]
    return {
        "scrobbles": {
            "@attr": {
                "accepted": str(codes.count(0)),
                "ignored": str(len(codes) - codes.count(0)),
            },
            "scrobble": items if len(items) > 1 else items[0],
        }
    }


class FakeAPI:
    def __init__(self, codes=None, error=None, drop=(), display=None):
        self.codes = codes
        self.error = error
        self.tracks = Counter()
        self.calls = 0
        # Accepted but never stored, as Last.fm occasionally does.
        self.drop = set(drop)
        # Last.fm's own capitalization in history, e.g. str.upper.
        self.display = display or (lambda name: name)

    def submit(self, rows):
        self.calls += 1
        if self.error:
            raise self.error
        codes = self.codes or [0] * len(rows)
        for row, code in zip(rows, codes):
            if code == 0 and row["assigned_ts"] not in self.drop:
                key = (row["assigned_ts"], self.display(row["artist"]))
                self.tracks[(*key, self.display(row["track"]), row["album"])] += 1
        return response(rows, self.codes)

    def history(self, account, start, end):
        return Counter({k: v for k, v in self.tracks.items() if start <= k[0] <= end})


class ImporterTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.store = Store(self.root / "state.sqlite")
        self.cfg = Config()
        self.store.bind(self.cfg.account)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def seed(self, events=None, name="endsong_0.json"):
        path = self.source / name
        path.write_text(
            json.dumps(
                events if events is not None else [event(), event()], ensure_ascii=False
            )
        )
        prepare(self.store, self.source, self.cfg)
        return path

    def rows(self):
        return self.store.rows("SELECT * FROM events ORDER BY original_epoch,id")

    def assigned(self):
        return assign(self.store, select(self.store, 50), self.cfg, NOW)

    def test_idempotent_preserves_repeated_plays_and_raw(self):
        p = self.seed()
        original = p.read_bytes()
        identities = [r["id"] for r in self.rows()]
        self.assertNotEqual(*identities)
        self.assertEqual(prepare(self.store, self.source, self.cfg)["events_added"], 0)
        self.assertEqual(identities, [r["id"] for r in self.rows()])
        self.assertEqual(original, p.read_bytes())
        self.assertEqual(json.loads(self.rows()[0]["raw_json"]), event())
        (self.source / "endsong_copy.json").write_bytes(original)
        self.assertEqual(prepare(self.store, self.source, self.cfg)["events_added"], 0)

    def test_changed_source_rolls_back(self):
        p = self.seed()
        p.write_text(json.dumps([event(ms_played=5)]))
        with self.assertRaises(ValueError):
            prepare(self.store, self.source, self.cfg)
        self.assertEqual(len(self.rows()), 2)

    def test_invalid_whole_file_atomic(self):
        (self.source / "endsong_a.json").write_text(json.dumps([event()]))
        (self.source / "endsong_b.json").write_text("{")
        with self.assertRaises(ValueError):
            prepare(self.store, self.source, self.cfg)
        self.assertEqual(self.rows(), [])

    def test_filter_all_reasons_no_loss(self):
        self.seed(
            [
                event(ms_played=0),
                event(master_metadata_track_name=None),
                event(ts="bad"),
                event(skipped=True),
                event(master_metadata_album_album_name=None),
                None,
            ]
        )
        state = status(self.store, self.cfg, NOW)
        self.assertEqual(state["total_events"], 6)
        self.assertEqual(state["eligible"], 2)
        self.assertEqual(state["excluded_events"], 4)
        self.assertIn("invalid_record", state["excluded_by_reason_nonexclusive"])

    def test_video_recorded_as_excluded(self):
        self.seed([event()], name="Streaming_History_Video_2026.json")
        self.assertEqual(self.rows()[0]["exclusion"], "video_history")

    def test_overrides_and_reclassify(self):
        self.seed([event()])
        self.cfg = replace(
            self.cfg,
            overrides=[{"spotify_track_id": TRACK_ID, "track": "радикальные танцы"}],
        )
        with self.assertRaises(ValueError):
            prepare(self.store, self.source, self.cfg)
        reclassify(self.store, self.cfg)
        self.assertEqual(self.rows()[0]["track"], "радикальные танцы")
        self.assertEqual(
            json.loads(self.rows()[0]["raw_json"])["master_metadata_track_name"],
            "Радикальные Танцы",
        )
        self.assertEqual(
            dry_run(self.store, self.cfg, now=NOW)["preview"][0]["track"],
            "радикальные танцы",
        )

    def test_success_and_singleton(self):
        self.seed([event()])
        submit(self.store, FakeAPI(), self.cfg, self.assigned(), NOW)
        self.assertEqual(self.rows()[0]["status"], "confirmed")
        self.assertEqual(self.rows()[0]["attempts"], 1)

    def test_partial_acceptance_ignored_metadata(self):
        self.seed()
        submit(self.store, FakeAPI([0, 2]), self.cfg, self.assigned(), NOW)
        self.assertEqual([r["status"] for r in self.rows()], ["confirmed", "rejected"])
        self.assertIn("ignoredMessage", self.rows()[1]["response"])

    def test_http_200_all_ignored(self):
        self.seed()
        submit(self.store, FakeAPI([1, 2]), self.cfg, self.assigned(), NOW)
        self.assertTrue(all(r["status"] == "rejected" for r in self.rows()))

    def test_daily_limit_keeps_timestamp_and_stops(self):
        self.seed()
        rows = self.assigned()
        submit(self.store, FakeAPI([0, 5]), self.cfg, rows, NOW)
        self.assertEqual(self.store.get("cooldown_until"), NOW + DAY + 60)
        self.assertEqual(self.rows()[1]["status"], "pending")
        self.assertEqual(self.rows()[1]["assigned_ts"], rows[1]["assigned_ts"])
        self.assertEqual(
            submit(self.store, FakeAPI(), self.cfg, select(self.store, 50), NOW + 1), 0
        )

    def test_transient_explicit_errors_safe_retry(self):
        self.seed()
        rows = self.assigned()
        submit(self.store, FakeAPI(error=APIError(16)), self.cfg, rows, NOW)
        self.assertEqual([r["status"] for r in self.rows()], ["pending", "pending"])
        self.assertEqual(self.store.get("cooldown_until"), NOW + 60)
        submit(self.store, FakeAPI(), self.cfg, select(self.store, 50), NOW + 61)
        self.assertEqual(
            [r["assigned_ts"] for r in rows], [r["assigned_ts"] for r in self.rows()]
        )
        self.assertEqual(self.rows()[0]["attempts"], 2)

    def test_rate_limit_retry_after(self):
        self.seed()
        submit(
            self.store, FakeAPI(error=APIError(29, 600)), self.cfg, self.assigned(), NOW
        )
        self.assertEqual(self.store.get("cooldown_until"), NOW + 600)

    def test_auth_block_does_not_reject_events(self):
        self.seed()
        submit(self.store, FakeAPI(error=APIError(9)), self.cfg, self.assigned(), NOW)
        self.assertEqual(self.store.get("auth_blocked"), 9)
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_invalid_parameter_rejected(self):
        self.seed()
        submit(self.store, FakeAPI(error=APIError(6)), self.cfg, self.assigned(), NOW)
        self.assertEqual(self.rows()[0]["status"], "rejected")

    def test_timeout_after_server_acceptance_reconciles(self):
        self.seed()
        rows = self.assigned()
        api = FakeAPI(error=TransportError("timeout"))
        api.tracks = Counter(
            {(r["assigned_ts"], r["artist"], r["track"], r["album"]): 1 for r in rows}
        )
        submit(self.store, api, self.cfg, rows, NOW)
        self.assertTrue(all(r["status"] == "uncertain" for r in self.rows()))
        self.assertEqual(select(self.store, 50), [])
        self.assertEqual(verify(self.store, api, self.cfg, NOW + 600)["verified"], 2)
        self.assertEqual(api.calls, 1)

    def test_absence_never_retries_uncertain(self):
        self.seed()
        api = FakeAPI(error=TransportError("timeout"))
        submit(self.store, api, self.cfg, self.assigned(), NOW)
        self.assertEqual(
            verify(self.store, api, self.cfg, NOW + 600)["uncertain_remaining"], 2
        )
        self.assertEqual(select(self.store, 50), [])
        with self.assertRaises(ValueError):
            reset_timestamp(self.store, self.rows()[0]["id"], "missing", NOW)

    def test_correction_or_duplicate_match_stays_uncertain(self):
        self.seed()
        rows = self.assigned()
        api = FakeAPI(error=TransportError("timeout"))
        submit(self.store, api, self.cfg, rows, NOW)
        # Another track at our second is not ours; duplicates stay ambiguous.
        api.tracks[(rows[0]["assigned_ts"], "Corrected", "Other Track", "")] = 1
        api.tracks[
            (rows[1]["assigned_ts"], rows[1]["artist"], rows[1]["track"], "")
        ] = 2
        self.assertEqual(
            verify(self.store, api, self.cfg, NOW + 600)["uncertain_remaining"], 2
        )

    def test_uncertain_with_rewritten_artist_reconciles_by_track(self):
        # Real case: response lost; Last.fm stored "Чёрный Лукич" and
        # "Гражданская Оборона" for "Черный Лукич" / "Grazhdanskaya Oborona".
        self.seed(
            [
                event(master_metadata_album_artist_name="Черный Лукич"),
                event(
                    master_metadata_album_artist_name="Grazhdanskaya Oborona",
                    master_metadata_track_name="После нас",
                ),
            ]
        )
        rows = self.assigned()
        api = FakeAPI(error=TransportError("garbled"))
        submit(self.store, api, self.cfg, rows, NOW)
        shown = {
            "Черный Лукич": "Чёрный Лукич",
            "Grazhdanskaya Oborona": "Гражданская Оборона",
        }
        for row in rows:
            api.tracks[(row["assigned_ts"], shown[row["artist"]], row["track"], "")] = 1
        result = verify(self.store, api, self.cfg, NOW + 600)
        self.assertEqual(result["uncertain_remaining"], 0)

    def test_crash_after_request_before_database_commit(self):
        self.seed()
        rows = self.assigned()
        with patch("spotify_import.engine.outcome", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                submit(self.store, FakeAPI(), self.cfg, rows, NOW)
        self.assertEqual(self.rows()[0]["status"], "submitting")
        self.store.conn.close()
        self.store = Store(self.root / "state.sqlite")
        self.store.recover()
        self.assertEqual(self.rows()[0]["status"], "uncertain")
        self.assertEqual(select(self.store, 50), [])

    def test_transaction_failure_rolls_back_whole_response(self):
        self.seed()
        rows = self.assigned()
        self.store.conn.execute(
            """CREATE TRIGGER simulated_crash BEFORE UPDATE OF status ON events
         WHEN NEW.status='confirmed' AND NEW.id='"""
            + rows[1]["id"]
            + "' BEGIN SELECT RAISE(ABORT,'crash'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            submit(self.store, FakeAPI(), self.cfg, rows, NOW)
        self.assertTrue(all(r["status"] == "submitting" for r in self.rows()))
        self.store.recover()
        self.assertTrue(all(r["status"] == "uncertain" for r in self.rows()))

    def test_restart_partial_batch_never_replays_success(self):
        self.seed([event(), event(), event()])
        rows = self.assigned()
        submit(self.store, FakeAPI(), self.cfg, rows[:2], NOW)
        self.store.recover()
        self.assertEqual(len(select(self.store, 50)), 1)

    def test_inconsistent_response_is_uncertain(self):
        self.seed()
        rows = self.assigned()
        api = FakeAPI()
        bad = response(rows)
        bad["scrobbles"]["@attr"]["accepted"] = "1"
        with patch.object(api, "submit", return_value=bad):
            submit(self.store, api, self.cfg, rows, NOW)
        self.assertEqual(self.rows()[0]["status"], "uncertain")

    def test_reordered_response_is_matched_by_timestamp(self):
        # Real case 2026-10-21: Last.fm listed the batch in another order.
        self.seed([event(master_metadata_track_name=f"T{i}") for i in range(3)])
        rows = self.assigned()
        api = FakeAPI()
        answer = response(rows, [0, 1, 0])
        answer["scrobbles"]["scrobble"].reverse()
        with patch.object(api, "submit", return_value=answer):
            submit(self.store, api, self.cfg, rows, NOW)
        states = {r["id"]: r["status"] for r in self.rows()}
        self.assertEqual(
            [states[r["id"]] for r in rows], ["confirmed", "rejected", "confirmed"]
        )

    def test_response_with_unknown_timestamp_is_uncertain(self):
        self.seed()
        rows = self.assigned()
        api = FakeAPI()
        bad = response(rows)
        bad["scrobbles"]["scrobble"][0]["timestamp"] = "1"
        with patch.object(api, "submit", return_value=bad):
            submit(self.store, api, self.cfg, rows, NOW)
        self.assertTrue(all(r["status"] == "uncertain" for r in self.rows()))

    def test_stored_garbled_response_is_reprocessed(self):
        from spotify_import.engine import GARBLED

        self.seed([event(master_metadata_track_name=f"T{i}") for i in range(3)])
        rows = self.assigned()
        answer = response(rows, [0, 1, 0])
        answer["scrobbles"]["scrobble"].reverse()
        api = FakeAPI(error=TransportError("simulate an old garbled verdict"))
        submit(self.store, api, self.cfg, rows, NOW)
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE attempts SET response=?",
                (GARBLED + json.dumps(answer, ensure_ascii=False),),
            )
        verify(self.store, FakeAPI(), self.cfg, NOW + 600)
        states = {r["id"]: r["status"] for r in self.rows()}
        self.assertEqual(
            [states[r["id"]] for r in rows], ["confirmed", "rejected", "confirmed"]
        )
        outcomes = {
            r["outcome"] for r in self.store.rows("SELECT outcome FROM attempts")
        }
        self.assertEqual(outcomes, {"confirmed", "rejected"})

    def test_rolling_budget_and_live_listening(self):
        self.seed()
        rows = self.assigned()
        submit(self.store, FakeAPI(), self.cfg, rows, NOW)
        api = FakeAPI()
        api.tracks = Counter({(NOW - 1, "Other", "Live", ""): 3})
        sync(self.store, api, self.cfg, NOW - DAY, NOW, NOW)
        self.assertEqual(
            budget(self.store, self.cfg, NOW)["remaining_daily_budget"], 495
        )
        self.assertEqual(
            budget(self.store, self.cfg, NOW + DAY)["remaining_daily_budget"], 500
        )

    def test_import_history_not_double_counted(self):
        self.seed()
        rows = self.assigned()
        # Recent timestamps would otherwise be counted as both live and imports.
        with self.store.conn:
            for i, row in enumerate(rows):
                self.store.conn.execute(
                    "UPDATE events SET assigned_ts=? WHERE id=?",
                    (NOW - 100 + i, row["id"]),
                )
        rows = select(self.store, 50)
        submit(self.store, FakeAPI(), self.cfg, rows, NOW)
        api = FakeAPI()
        api.tracks = Counter(
            {(r["assigned_ts"], r["artist"], r["track"], r["album"]): 1 for r in rows}
        )
        sync(self.store, api, self.cfg, NOW - DAY, NOW, NOW)
        self.assertEqual(
            budget(self.store, self.cfg, NOW)["remaining_daily_budget"], 498
        )

    def test_timestamp_boundaries_and_collisions(self):
        self.seed()
        api = FakeAPI()
        api.tracks[(NOW - 2 * DAY, "someone", "other", "")] = 1
        sync(self.store, api, self.cfg, NOW - 2 * DAY, NOW, NOW)
        rows = self.assigned()
        self.assertEqual(rows[0]["assigned_ts"], NOW - 2 * DAY + 1)
        self.assertLess(rows[1]["assigned_ts"], NOW)
        with self.assertRaises(ValueError):
            assign(self.store, rows, self.cfg, NOW + 14 * DAY)

    def test_collision_after_dry_run_blocks_write(self):
        self.seed()
        rows = self.assigned()
        api = FakeAPI()
        api.tracks[(rows[0]["assigned_ts"], "other", "track", "")] = 1
        sync(self.store, api, self.cfg, rows[0]["assigned_ts"], NOW, NOW)
        with self.assertRaises(ValueError):
            submit(self.store, api, self.cfg, rows, NOW)
        self.assertEqual(api.calls, 0)

    def test_overlap_end_and_start_candidate_preserves_events(self):
        self.seed()
        row = self.rows()[0]
        api = FakeAPI()
        api.tracks[(row["original_epoch"] - 240, row["artist"], row["track"], "")] = 1
        sync(self.store, api, self.cfg, 0, NOW, NOW)
        self.assertEqual(overlaps(self.store), 2)
        self.assertEqual(select(self.store, 50), [])
        self.assertEqual(len(self.rows()), 2)
        resolve(self.store, row["id"], "keep", "Reviewed distinct event", NOW)
        self.assertEqual(len(select(self.store, 50)), 1)

    def test_plan_stable_restart_and_configuration_binding(self):
        self.seed()
        plan = dry_run(self.store, self.cfg, now=NOW)
        rows = load_plan(self.store, self.cfg, plan["plan_id"], NOW + 1)
        self.assertEqual(len(rows), 2)
        submit(self.store, FakeAPI(), self.cfg, rows, NOW)
        self.assertEqual(load_plan(self.store, self.cfg, plan["plan_id"], NOW + 1), [])
        with self.assertRaises(ValueError):
            load_plan(
                self.store, replace(self.cfg, daily_budget=400), plan["plan_id"], NOW
            )

    def test_account_binding_and_backup(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.store.bind("SomeoneElse")
        self.store.backup(self.root / "backup.sqlite")
        backup = Store(self.root / "backup.sqlite")
        self.assertEqual(len(backup.rows("SELECT * FROM events")), 2)
        backup.conn.close()

    def test_exclusive_writer_lock(self):
        other = Store(self.root / "state.sqlite")
        with self.store.lock(), self.assertRaises(ValueError):
            with other.lock():
                pass
        other.conn.close()


class APITest(unittest.TestCase):
    def test_signature_sorting_excludes_format(self):
        import hashlib

        self.assertEqual(
            signature(
                {"artist[1]": "one", "artist[10]": "ten", "format": "json"}, "secret"
            ),
            hashlib.md5(b"artist[10]tenartist[1]onesecret").hexdigest(),
        )

    def test_timeout_is_ambiguous_no_retry(self):
        api = LastFM(
            {
                "LASTFM_API_KEY": "key",
                "LASTFM_API_SECRET": "secret",
                "LASTFM_SESSION_KEY": "session",
            }
        )
        with patch(
            "spotify_import.api.urlopen", side_effect=TimeoutError("secret URL")
        ) as request:
            with self.assertRaises(TransportError) as error:
                api.call("track.scrobble", signed=True, session=True)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(
            request.call_args.args[0].full_url, "https://ws.audioscrobbler.com/2.0/"
        )
        self.assertIn(b"sk=session", request.call_args.args[0].data)

    def test_history_pagination_and_nowplaying(self):
        api = LastFM({"LASTFM_API_KEY": "key"})

        def page(n, tracks):
            return {
                "recenttracks": {
                    "@attr": {"page": str(n), "totalPages": "2", "total": "2"},
                    "track": tracks,
                }
            }

        def track(ts):
            return {
                "date": {"uts": str(ts)},
                "artist": {"#text": "a"},
                "name": "t",
                "album": {"#text": ""},
            }

        with patch.object(
            api,
            "call",
            side_effect=[
                page(1, [{"@attr": {"nowplaying": "true"}}, track(100)]),
                page(2, [track(101)]),
            ],
        ):
            self.assertEqual(sum(api.history("user", 0, 200).values()), 2)

    def test_incomplete_history_fails_closed(self):
        api = LastFM({"LASTFM_API_KEY": "key"})
        with patch.object(
            api,
            "call",
            return_value={
                "recenttracks": {
                    "@attr": {"totalPages": "1", "total": "2"},
                    "track": [],
                }
            },
        ):
            with self.assertRaises(TransportError):
                api.history("user", 0, 200)


if __name__ == "__main__":
    unittest.main()
