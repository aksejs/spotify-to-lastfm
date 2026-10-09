# Operations runbook

All commands assume the repository root, an activated `.venv`, and `--config config.local.yaml`, abbreviated below as `si`:

```sh
alias si='spotify-import --config config.local.yaml'
```

Read-only at any time, even while the worker runs: `si status`. Every other command takes the database lock and fails fast while the worker holds it.

## Before the first unattended run

1. `si sync-history --full`: snapshot the whole account history. This reads only.
2. `si status`: check `excluded_by_reason_nonexclusive` and `potential_overlaps`.
3. Live period: if Last.fm already has live scrobbles from Spotify, set `live_scrobbling_since` to the first one (see `config.example.yaml`) and run `si reclassify`. Those plays get the `live_scrobbled_period` reason instead of being queued.
4. Gap review (optional, any time before the queue reaches the live period):
   - `si gap-review export audit/gaps.csv` lists live-period plays with no account scrobble between start−30 s and end+30 s. Most are partial plays that Last.fm's half-track/4-minute rule never scrobbled.
   - Delete the rows you do **not** want imported. Then run `si gap-review include audit/gaps.csv --note "reviewed <date>"`.
   - `--all` also exports plays that have nearby scrobbles. These are usually already scrobbled under a different name, so including them risks duplicates.
5. `si dry-run --online --limit 50`: review exact metadata and assigned timestamps.

## Unattended service

Templates live in `deploy/`. Installing one **is** the authorization for continuous submission; the CLI flag `--approve-continuous ACCOUNT` is embedded in it.

macOS:

```sh
sed -e "s#REPO#$PWD#g" -e "s#ACCOUNT#DivokaSarka#g" deploy/com.spotify-import.plist \
  > ~/Library/LaunchAgents/com.spotify-import.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.spotify-import.plist
tail -f state/worker.log
```

Linux: substitute the same placeholders into `deploy/spotify-import.service`, copy it to `~/.config/systemd/user/`, and run `systemctl --user enable --now spotify-import`. Also run `loginctl enable-linger` so the worker runs without an open session.

After each batch the worker waits `verify_delay_seconds`, reads account history, and records every play as verified or missing. It sends nothing while an earlier batch is unchecked, and it stops for review if more than 10% of a batch (minimum 2 plays) is missing. The worker sleeps while the rolling budget is exhausted, verifies uncertain events every `poll_seconds`, and snapshots the database every `backup_interval_hours` (`state/import-backups/`). A Mac that sleeps simply resumes later. The budget is wall-clock based.

## Finding the real daily limit

Last.fm doesn't publish the number. Probe it in steps, at least 24 hours apart:

1. Set `daily_budget` to the step (e.g. 1000, then 1500, 2000, 2500, 3000) and `combined_budget` to about 300 more, so your live listening doesn't throttle the test.
2. `si run --approve-continuous ACCOUNT --once` sends up to the budget, checks every batch, and exits.
3. `si status` → `daily_limit_hits`. It stays empty until Last.fm refuses with code 5. Then it shows how many scrobbles were accepted in the 24 h before (imports + live) and since UTC midnight. That number is the limit; which of the two totals matches shows how Last.fm counts.
4. After a hit, refused plays keep their timestamps and wait 24 h. Settle at about 80% of the measured total minus your usual live listening. **Exceeding the limit also blocks your live scrobbles until it resets.**

## Pause and resume

- Pause: `launchctl bootout gui/$(id -u)/com.spotify-import` (Linux: `systemctl --user stop spotify-import`). A stop that lands during an HTTP request leaves that batch `submitting`. The next start marks it `uncertain` and the worker reconciles it from history; it is never replayed blindly.
- Resume: the same `bootstrap`/`start` command. No other state needs touching.
- Manual foreground run instead: `si run --approve-continuous DivokaSarka`, stop with Ctrl-C.

## Change the daily budget

Edit `daily_budget` / `combined_budget` in `config.local.yaml`, then restart the worker. Budget settings are not part of the preparation policy, so no reclassify is needed. Already-assigned timestamps are unaffected.

## Restart after a crash or reboot

Just start again. On startup, `submitting` rows become `uncertain` and the lock is reacquired. Pending assignments older than the 13-day window stop the run with an explicit error. Check them with `si status`, then use `reset-timestamp` (below) only for events that were never sent.

## Reconcile uncertain events

1. `si verify` reads account history around each assigned timestamp. A unique exact timestamp/artist/track match confirms the event.
2. Still uncertain? Look at Last.fm around `assigned_ts` (see `si export-audit`). Then:
   - visible on Last.fm: `si resolve EVENT_ID confirm --note "seen at <url/time>"`
   - certainly absent, and you accept a possible duplicate: `si resolve EVENT_ID retry --accept-duplicate-risk --note "..."`
   - leave it: uncertain events stay quarantined forever and are never resent.

## Accepted but missing scrobbles

Last.fm sometimes accepts a scrobble (code 0, counted as accepted) and still doesn't store it. `status` shows these as `confirmed_missing`. The worker re-checks them every `poll_seconds`, and a late arrival clears the mark.

1. `si verify` re-reads history for all sent plays.
2. Still missing after an hour: `si resend-missing --note "absent after re-check"`, or pass specific event IDs. It re-reads history once more, then requeues only plays that are still absent, with a new timestamp. The old timestamp stays reserved and is recorded in `decisions`.
3. If the worker stopped because many plays went missing, look at the account history first. Repeated drops point to a systematic problem (for example, name filtering). Change nothing until the cause is understood.

## Diagnose rejected tracks

`si export-audit audit/events.csv` and filter `status=rejected` with `attempts>0`. `response` holds Last.fm's ignored code/message:

- Code 1/2 (artist/track ignored by Last.fm's filters): submitted metadata is immutable, so retrying the same event gets the same answer. Add an `overrides` entry keyed by `spotify_track_id` and run `si reclassify`. That fixes later, never-attempted plays of the same track only. Already-rejected events stay recorded as rejected; repairing them is not supported yet. Find bad metadata *before* it is sent by reading the dry-run preview.
- Code 3/4 (timestamp too old/new): the assignment expired. Use `si reset-timestamp EVENT_ID --note "..."` (allowed only for conclusively unaccepted events).
- Code 5 (daily limit): no action; the worker cools down for 24 h.

Events with `attempts=0` and `status=rejected` are the excluded ones; `exclusion` names the reason.

## Backups and restore

- Ad hoc: `si backup state/manual-YYYYMMDD.sqlite`. This uses the SQLite backup API and is safe while paused. Never copy `import.sqlite` alone while a WAL file exists.
- Automatic: the worker keeps the last `backup_keep` snapshots in `state/import-backups/`.
- Restore: pause the worker. Inspect the backup separately with `spotify-import --db state/import-backups/auto-….sqlite --config config.local.yaml status`, then move it over `state/import.sqlite`.
- **A backup forgets submissions made after it was taken.** After restoring, run `si sync-history` and `si verify` before resuming, and compare attempt counts with the newer `worker.log`. Plays sent after the snapshot are `pending` again in the restored copy and would be scrobbled twice.

## What is kept for audit

Original Spotify JSON (read only, hashed in `sources`), `state/import.sqlite` (every event, attempt, decision, and history snapshot), `audit/*` exports, and `config.local.yaml`. Keep all of them until the migration is finished and verified.
