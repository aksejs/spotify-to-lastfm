"""Live-scrobbling cutoff and gap review. All networking is replaced by fakes."""

import csv
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
import json

import yaml

import test_importer
from test_importer import FakeAPI, NOW, event
from spotify_import.cli import main
from spotify_import.config import Config, load_config
from spotify_import.engine import resolve, select, status
from spotify_import.history import gaps, overlaps, sync
from spotify_import.prepare import prepare, reclassify

CUTOFF = "2020-01-01T00:00:00Z"
BEFORE = "2019-12-31T23:00:00Z"


# Imported as a module attribute so discovery does not rerun the base tests here.
class LivePeriodTest(test_importer.ImporterTest):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, live_scrobbling_since=CUTOFF)

    def snapshot(self, tracks=()):
        api = FakeAPI()
        for key in tracks:
            api.tracks[key] += 1
        sync(self.store, api, self.cfg, 0, NOW, NOW)

    def cli(self, *argv):
        config = self.root / "config.yaml"
        config.write_text(yaml.safe_dump({"live_scrobbling_since": CUTOFF}))
        base = ["--db", str(self.root / "state.sqlite"), "--config", str(config)]
        self.store.conn.close()
        with redirect_stdout(StringIO()) as out:
            code = main(base + list(argv))
        self.store = type(self.store)(self.root / "state.sqlite")
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_cutoff_excludes_with_reason_and_keeps_earlier_plays(self):
        self.seed([event(ts=BEFORE), event(), event(ms_played=0)])
        rows = self.rows()
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["exclusion"], None)
        reasons = sorted(r["exclusion"] for r in rows[1:])
        self.assertEqual(
            reasons, ["live_scrobbled_period", "too_short,live_scrobbled_period"]
        )
        state = status(self.store, self.cfg, NOW)
        self.assertEqual(state["total_events"], 3)
        self.assertEqual(
            state["excluded_by_reason_nonexclusive"]["live_scrobbled_period"], 2
        )
        self.assertEqual([r["original_ts"] for r in select(self.store, 50)], [BEFORE])

    def test_policy_unset_matches_databases_prepared_before_option(self):
        self.assertNotIn("live_scrobbling_since", Config().preparation_policy())
        self.assertIn("live_scrobbling_since", self.cfg.preparation_policy())

    def test_unquoted_yaml_timestamp_and_invalid_values(self):
        path = self.root / "c.yaml"
        path.write_text("live_scrobbling_since: 2025-11-11T20:58:40Z\n")
        cfg = load_config(path)
        self.assertEqual(
            cfg.live_cutoff(),
            int(datetime(2025, 11, 11, 20, 58, 40, tzinfo=UTC).timestamp()),
        )
        json.dumps(cfg.preparation_policy())
        for bad in ("yesterday", "2025-11-11T20:58:40"):
            with self.assertRaises(ValueError):
                Config(live_scrobbling_since=bad)

    def test_reclassify_restores_overlap_quarantine(self):
        self.cfg = Config()
        self.seed([event(), event(ts=BEFORE)])
        known = self.rows()[0]
        self.snapshot([(known["original_epoch"], known["artist"], known["track"], "")])
        self.assertEqual(overlaps(self.store), 1)
        reclassify(self.store, self.cfg)
        # Reclassification must not release a known duplicate for submission.
        self.assertNotIn(known["id"], [r["id"] for r in select(self.store, 50)])
        self.assertEqual(len(select(self.store, 50)), 1)

    def test_reclassify_applies_cutoff_to_existing_database(self):
        self.cfg, live = Config(), self.cfg
        self.seed([event(ts=BEFORE), event()])
        with self.assertRaises(ValueError):
            prepare(self.store, self.source, live)
        reclassify(self.store, live)
        self.assertEqual(len(select(self.store, 50)), 1)

    def test_gap_review_lists_only_unscrobbled_plays(self):
        self.seed([event(), event(ts="2020-01-01T01:00:00Z")])
        first, second = self.rows()
        # Live scrobble of the first play at its start time, under another name.
        start = first["original_epoch"] - 240
        self.snapshot([(start, "AL90", "Radical Dances", "")])
        found = {r["id"]: r for r in gaps(self.store)}
        self.assertIn("Radical Dances", found[first["id"]]["nearby_scrobbles"])
        self.assertEqual(found[second["id"]]["nearby_scrobbles"], "")

    def test_gap_review_requires_full_history(self):
        self.seed([event()])
        with self.assertRaises(ValueError):
            gaps(self.store)

    def test_gap_include_round_trip_survives_reclassify(self):
        self.seed([event(), event(ts="2020-01-01T01:00:00Z"), event(ts=BEFORE)])
        live = self.rows()[1]
        self.snapshot([(live["original_epoch"] - 240, "AL-90", "other", "")])
        path = self.root / "gaps.csv"
        result = self.cli("gap-review", "export", str(path))
        self.assertEqual(result["exported_candidates"], 1)
        with path.open(newline="", encoding="utf-8") as f:
            listed = [r["id"] for r in csv.DictReader(f)]
        self.assertNotIn(live["id"], listed)
        result = self.cli("gap-review", "include", str(path), "--note", "Reviewed")
        self.assertEqual(result["included"], 1)
        # Re-applying the same reviewed file is harmless.
        result = self.cli("gap-review", "include", str(path), "--note", "Reviewed")
        self.assertEqual(result["already_included"], 1)
        reclassify(self.store, self.cfg)
        queued = {r["id"] for r in select(self.store, 50)}
        self.assertTrue(set(listed) <= queued)
        self.assertNotIn(live["id"], queued)
        decisions = self.store.rows("SELECT * FROM decisions WHERE action='include'")
        self.assertEqual(len(decisions), 1)

    def test_gap_include_rejects_unreviewable_ids_atomically(self):
        self.seed([event(), event(ts=BEFORE), event(ms_played=0)])
        self.snapshot()
        ids = [r["id"] for r in self.rows()]
        path = self.root / "bad.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id"])
            writer.writeheader()
            writer.writerows({"id": i} for i in ids)
        from spotify_import.cli import include_gaps

        with self.assertRaises(ValueError):
            include_gaps(self.store, path, "note")
        self.assertEqual(self.store.rows("SELECT * FROM decisions"), [])

    def test_include_only_for_live_period_exclusions(self):
        self.seed([event(ms_played=0)])
        with self.assertRaises(ValueError):
            resolve(self.store, self.rows()[0]["id"], "include", "note", NOW)


for name in list(test_importer.ImporterTest.__dict__):
    if name.startswith("test_"):
        setattr(LivePeriodTest, name, None)
