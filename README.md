# Spotify history → Last.fm

A work-in-progress Python fork of [joinemm/spotify-to-lastfm](https://github.com/joinemm/spotify-to-lastfm), adding durable SQLite state, reviewed submission plans, and conservative recovery from ambiguous API outcomes.

**Current checkpoint:** automated tests and small live submissions have been exercised. Long-running migration validation and the full recovery runbook remain unfinished. No scheduled job is installed by this repository. The original scripts are retained for reference; the legacy `scrobble.py` submission entry point is disabled.

## Install

Requires Python 3.11+ on macOS or Linux. With uv:

```sh
uv sync --frozen
source .venv/bin/activate
cp config.example.yaml config.local.yaml
```

Alternatively, create a virtual environment and install `pip install -e .`. The uv lockfile pins the supported importer's dependencies. Historical upstream dependencies and instructions are preserved in `docs/UPSTREAM_REQUIREMENTS.txt` and `docs/UPSTREAM_README.md`; do not use those for this importer.

Edit the account, filtering rules, budgets, and reviewed metadata overrides in `config.local.yaml`. Supply `--config config.local.yaml` consistently on every command. The defaults/example currently target the account for which this fork was developed; change the account before preparing another person's history.

## Credentials

Supply `LASTFM_API_KEY`, `LASTFM_API_SECRET`, and optionally `LASTFM_SESSION_KEY` through the environment or `state/secrets.json`:

```json
{
  "LASTFM_API_KEY": "",
  "LASTFM_API_SECRET": "",
  "LASTFM_SESSION_KEY": ""
}
```

Set file permissions to `600`. With key and secret configured, obtain the session key using the official browser authorization flow:

```sh
spotify-import --config config.local.yaml auth
```

The authenticated account must match the configured account. Credentials, source histories, SQLite state, backups, and local audit exports must stay out of Git. Authentication uses HTTPS form bodies; session keys are never printed. Authenticated history reads also work when account history requires a session.

## Prepare and review

```sh
spotify-import --config config.local.yaml prepare /path/to/spotify-history
spotify-import --config config.local.yaml status
spotify-import --config config.local.yaml dry-run --limit 10
spotify-import --config config.local.yaml sync-history --full
spotify-import --config config.local.yaml export-history audit/lastfm-history.json
spotify-import --config config.local.yaml dry-run --online --limit 10
```

Input files are read only. Supported names are `endsong_*.json`, `Streaming_History_Audio_*.json`, and `Streaming_History_Video_*.json`. Video and podcast records are recorded as excluded. Every record retains its original JSON. IDs combine the source content hash, array position, and event contents; byte-identical file copies are treated as aliases, while repeated listens within a file remain separate events. Changed files at a previously imported path are rejected.

Default eligibility requires valid timestamps, artist/track metadata, and at least 30,000 milliseconds played. Skipped events are retained if they meet that threshold; missing albums are allowed. **This threshold is an import policy, not proof of Last.fm's half-track/four-minute listening criterion:** Spotify exports do not provide full track duration. Configure filtering explicitly. Exclusion reasons may overlap, so reason counts need not sum to the excluded-event total.

A dry run assigns and stores exact timestamps and outputs a plan hash. It never submits. Without a history snapshot, overlap checks are reported unavailable. Overlap matching compares track/artist and original end or estimated start time within two seconds; these are review candidates, not automatic deduplication. Differently named tracks and incomplete account history can evade matching. Cross-file identical records are reported for review without being discarded.

## Submit only a reviewed plan

Only after approving the exact preview:

```sh
spotify-import --config config.local.yaml run --approve-plan PLAN_HASH
spotify-import --config config.local.yaml verify
spotify-import --config config.local.yaml export-audit audit/events.csv
spotify-import --config config.local.yaml export-audit --format json audit/full-audit.json
spotify-import --config config.local.yaml backup state/checkpoint.sqlite
```

Previously confirmed events are not submitted again. All writes inspect batch counts, response timestamps, and individual ignored codes. A timeout or interrupted submission becomes `uncertain`, never an automatic replay. Verification requires a unique exact timestamp/artist/track match. Missing tracks, duplicates, and service-side naming corrections can require manual review. Last.fm does not provide guaranteed exactly-once delivery.

A default rolling 24-hour budget counts 500 import attempts, including failures and uncertain attempts. A second combined budget also subtracts observed live listening. This is a conservative local policy, not a statement of Last.fm's actual daily limit. Independent clients can submit additional plays after a history check. Daily-limit responses pause for at least 24 hours; explicit temporary API refusals back off and retain assigned timestamps.

New timestamps start two days before submission, use distinct seconds, and avoid known occupied timestamps. Original timestamps remain in the database. Uncertain or confirmed events cannot have their timestamps reset. The safe submission window is conservatively bounded to 13 days; expired pending assignments stop for review.

## Continuous mode and recovery

After a full history snapshot, overlap review, and explicit separate authorization, continuous mode is available:

```sh
spotify-import --config config.local.yaml run --approve-continuous YOUR_CONFIGURED_ACCOUNT
```

It runs in the foreground, sleeps when the budget is exhausted, and periodically reconciles uncertain events. It is not yet installed as a launchd/systemd service. Stop with Ctrl-C and resume with the same database and command. Only one mutating importer process may hold the database lock. Read-only `status` remains available while it runs.

Pause the worker before exporting or backing up. Use `backup`, which uses SQLite's backup API; do not copy only a live database file while omitting its WAL. Restore to a separate path and inspect using `--db`. **A stale backup can forget submissions made after the backup:** do not resume it until later attempt records and account history have been reconciled. Restoring files alone cannot make forgotten submissions safe to replay.

`resolve EVENT_ID confirm|keep|exclude|retry --note EVIDENCE` records manual decisions. `retry` additionally requires `--accept-duplicate-risk`. Absence from history is not proof that an uncertain request failed. `reset-timestamp EVENT_ID --note EVIDENCE` is restricted to conclusively unaccepted events. `reclassify` applies changed filtering/overrides only to never-attempted events and invalidates existing plans; submitted metadata remains immutable.

Remaining work includes full-scale history/recovery validation, a more complete rejection-repair workflow, and deployment/runbook documentation. Keep unresolved events and their audit records until reviewed.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic data and mocked APIs; they never submit real scrobbles. Coverage includes partial acceptance, ignored records, rate limits, timeouts after server acceptance, transactional failures, restart recovery, repeated inputs/listens, Unicode overrides, timestamp boundaries, account isolation, history pagination, and overlaps.

See [the upstream audit](docs/AUDIT.md) for design decisions and limitations. Original MIT license and attribution are retained.
