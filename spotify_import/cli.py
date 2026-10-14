import argparse
import csv
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from .api import APIError, LastFM, TransportError, credentials, save_credentials
from .config import load_config
from .db import Store
from .engine import (
    DAY,
    assign,
    backoff,
    budget,
    dry_run,
    load_plan,
    reset_timestamp,
    resolve,
    select,
    status,
    submit,
)
from .history import gaps, overlaps, resend, sync, verify
from .prepare import prepare, reclassify
from .worker import Notifier, resume, serve


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser():
    p = argparse.ArgumentParser(
        description="Durable Spotify history importer. Default commands never scrobble."
    )
    p.add_argument("--db", default="state/import.sqlite")
    p.add_argument("--config")
    p.add_argument("--secrets", default="state/secrets.json")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("auth")
    prep = commands.add_parser("prepare")
    prep.add_argument("folder")
    commands.add_parser("reclassify")
    commands.add_parser("status")
    dry = commands.add_parser("dry-run")
    dry.add_argument("--limit", type=int, default=10)
    dry.add_argument("--spotify-track-id")
    dry.add_argument(
        "--online",
        action="store_true",
        help="Read account history for timestamp collision checks",
    )
    run = commands.add_parser("run")
    approval = run.add_mutually_exclusive_group(required=True)
    approval.add_argument(
        "--approve-plan",
        help="Submit only the exact dry-run plan identified by this hash",
    )
    approval.add_argument(
        "--approve-continuous",
        metavar="ACCOUNT",
        help="Explicitly authorize ongoing migration for this account",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="With --approve-continuous: exit when the rolling budget is used up",
    )
    commands.add_parser("verify")
    hist = commands.add_parser("sync-history")
    hist.add_argument("--full", action="store_true")
    export = commands.add_parser("export-audit")
    export.add_argument("path")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    hist_export = commands.add_parser("export-history")
    hist_export.add_argument("path")
    backup = commands.add_parser("backup")
    backup.add_argument("path")
    decision = commands.add_parser("resolve")
    decision.add_argument("event_id")
    decision.add_argument(
        "action", choices=["confirm", "keep", "exclude", "include", "retry"]
    )
    decision.add_argument("--note", required=True)
    decision.add_argument("--accept-duplicate-risk", action="store_true")
    review = commands.add_parser(
        "gap-review", help="Review live-period plays that Last.fm may have missed"
    )
    review_steps = review.add_subparsers(dest="step", required=True)
    gap_export = review_steps.add_parser("export")
    gap_export.add_argument("path")
    gap_export.add_argument(
        "--all",
        action="store_true",
        help="Also list plays with a scrobble nearby (likely already scrobbled)",
    )
    gap_include = review_steps.add_parser(
        "include", help="Queue every event ID in a reviewed export"
    )
    gap_include.add_argument("path")
    gap_include.add_argument("--note", required=True)
    serve = commands.add_parser(
        "serve", help="Unattended worker: continuous run, reports, halts, resends"
    )
    serve.add_argument("--approve-continuous", metavar="ACCOUNT", required=True)
    resume_cmd = commands.add_parser("resume", help="Clear a halt after review")
    resume_cmd.add_argument("--note", required=True)
    commands.add_parser("notify-test", help="Send a Telegram test message")
    commands.add_parser(
        "telegram-chats", help="List chats that wrote to the bot (find the chat id)"
    )
    again = commands.add_parser(
        "resend-missing",
        help="Requeue accepted scrobbles that account history still lacks",
    )
    again.add_argument("event_ids", nargs="*", help="Default: all recorded as missing")
    again.add_argument("--note", required=True)
    reset = commands.add_parser("reset-timestamp")
    reset.add_argument("event_id")
    reset.add_argument("--note", required=True)
    return p


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def authenticate(api, cfg, path):
    token = api.call("auth.getToken", signed=True)["token"]
    print("Open this official authorization URL in your browser:")
    print(
        "https://www.last.fm/api/auth/?"
        + urlencode({"api_key": api.values["LASTFM_API_KEY"], "token": token})
    )
    input("After authorizing the application, press Enter: ")
    session = api.call("auth.getSession", {"token": token}, signed=True)["session"]
    if session["name"].casefold() != cfg.account.casefold():
        raise ValueError(
            "Authorized account differs from configured account; session not saved"
        )
    save_credentials(path, {**api.values, "LASTFM_SESSION_KEY": session["key"]})
    print("Session saved securely; no scrobbles submitted.")


def export_audit(store, path, fmt):
    rows = store.rows("SELECT * FROM events ORDER BY original_epoch,id")
    if fmt == "json":
        write_json(
            path,
            {
                "account": store.get("account"),
                "events": rows,
                "sources": store.rows("SELECT * FROM sources"),
                "attempts": store.rows("SELECT * FROM attempts"),
                "decisions": store.rows("SELECT * FROM decisions"),
                "preparation_policy": store.get("preparation_policy"),
            },
        )
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("x", newline="", encoding="utf-8") as f:
            fields = [x[1] for x in store.conn.execute("PRAGMA table_info(events)")]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return {"exported_events": len(rows), "path": str(path)}


def export_gaps(store, path, everything):
    rows = [r for r in gaps(store) if everything or not r["nearby_scrobbles"]]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id",
                "original_ts",
                "ms_played",
                "artist",
                "track",
                "album",
                "nearby_scrobbles",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return {"exported_candidates": len(rows), "path": str(path)}


def include_gaps(store, path, note):
    with Path(path).open(newline="", encoding="utf-8") as f:
        ids = list(dict.fromkeys(row["id"] for row in csv.DictReader(f)))
    included = {
        r["event_id"]
        for r in store.rows("SELECT event_id FROM decisions WHERE action='include'")
    }
    pending = [i for i in ids if i not in included]
    # Validate the whole reviewed file before changing any event.
    valid = {
        r["id"]
        for r in store.rows(
            "SELECT id FROM events WHERE attempts=0 AND exclusion='live_scrobbled_period'"
        )
    }
    invalid = [i for i in pending if i not in valid]
    if invalid:
        raise ValueError(
            f"{len(invalid)} IDs are not excluded live-period events, e.g. {invalid[0]}"
        )
    for event_id in pending:
        resolve(store, event_id, "include", note)
    # An included play that exactly matches account history is quarantined again.
    return {
        "included": len(pending),
        "already_included": len(ids) - len(pending),
        "potential_overlaps": overlaps(store),
    }


def refresh_for_write(store, api, cfg, rows, now):
    start = min(
        [now - DAY] + [r["assigned_ts"] for r in rows if r["assigned_ts"] is not None]
    )
    # The per-day cap needs every play on the days the cursor may use. Finished
    # days rarely change, so the whole window is re-read only every poll_seconds;
    # batches are verified individually anyway.
    if now - store.get("last_window_sync", 0) >= cfg.poll_seconds:
        start = min(start, now - (cfg.timestamp_age_days + 1) * DAY)
        sync(store, api, cfg, start, now, now)
        with store.conn:
            store.set("last_window_sync", now)
        return
    sync(store, api, cfg, start, now, now)


def check_sent(store, api, cfg, continuous, now):
    """Read account history for every accepted scrobble not yet checked."""
    ids = [
        r["id"]
        for r in store.rows(
            """SELECT id FROM events WHERE status='confirmed'
            AND verified_at IS NULL AND missing_at IS NULL"""
        )
    ]
    if not ids:
        return
    result = verify(store, api, cfg, now, ids=ids)
    output(
        {
            "post_send_check": {
                "checked": len(ids),
                "verified": result["verified"],
                "missing_ids": result["missing_ids"],
            }
        }
    )
    # Isolated drops are tracked; a pattern of them needs a human before more writes.
    if continuous and len(result["missing_ids"]) > max(1, len(ids) // 10):
        raise ValueError(
            "Accepted scrobbles are missing from account history; stopped for review"
        )


def run(store, api, cfg, args, tick=None):
    continuous = bool(args.approve_continuous)
    if continuous and store.get("halted"):
        raise ValueError(
            "Halted: " + store.get("halted")["reason"] + "; review, then run resume"
        )
    # Continuous until the budget or a cooldown would make it sleep.
    once = continuous and getattr(args, "once", False)
    if continuous and args.approve_continuous != cfg.account:
        raise ValueError("--approve-continuous must exactly match configured account")
    if store.get("preparation_policy") != cfg.preparation_policy():
        raise ValueError(
            "Preparation policy changed; review and run reclassify before submission"
        )
    if continuous:
        latest_source = (
            store.rows("SELECT max(original_epoch) t FROM events")[0]["t"] or 0
        )
        if not store.rows(
            "SELECT 1 FROM history_ranges WHERE start=0 AND end>=?", (latest_source,)
        ):
            raise ValueError(
                "Before continuous importing, run sync-history --full and review overlap candidates"
            )
    # Never begin a POST sequence before authenticated account validation succeeds.
    while True:
        now = int(time.time())
        remaining = store.get("cooldown_until", 0) - now
        if continuous and remaining > 0:
            if once:
                output({"paused_until": store.get("cooldown_until", 0)})
                return
            time.sleep(min(60, remaining))
            continue
        try:
            api.check_account(cfg.account)
            break
        except (TransportError, APIError) as error:
            if not continuous or (
                isinstance(error, APIError) and error.code not in (8, 11, 16, 29)
            ):
                raise
            with store.conn:
                backoff(store, now, getattr(error, "retry_after", 0))
            print(
                "Account validation unavailable; waiting before any write.", flush=True
            )
    with store.conn:
        store.set("auth_blocked", None)
        store.set("last_run_config", asdict(cfg))
    while True:
        now = int(time.time())
        if tick:
            tick(now)
        if store.get("auth_blocked"):
            raise ValueError("Authentication blocked; correct credentials and restart")
        cooldown = store.get("cooldown_until", 0)
        if now < cooldown:
            if not continuous or once:
                output({"paused_until": cooldown})
                return
            time.sleep(min(60, cooldown - now))
            continue
        # Never send while an earlier accepted batch is unchecked (e.g. after a crash).
        try:
            check_sent(store, api, cfg, continuous, now)
        except (TransportError, APIError) as error:
            if not continuous or (
                isinstance(error, APIError) and error.code not in (8, 11, 16, 29)
            ):
                raise
            with store.conn:
                backoff(store, now, getattr(error, "retry_after", 0))
            print("History check failed; backing off before any write.", flush=True)
            continue
        if continuous and now - store.get("last_auto_verify", 0) >= cfg.poll_seconds:
            try:
                result = verify(store, api, cfg, now, unresolved=True)
                with store.conn:
                    store.set("last_auto_verify", now)
                if result["uncertain_remaining"]:
                    print("Unresolved uncertain events remain quarantined.", flush=True)
            except (TransportError, APIError) as error:
                if isinstance(error, APIError) and error.code not in (8, 11, 16, 29):
                    raise
                with store.conn:
                    backoff(store, now, getattr(error, "retry_after", 0))
                continue
        rows = (
            load_plan(store, cfg, args.approve_plan, now)
            if not continuous
            else select(store, cfg.batch_size)
        )
        if not rows:
            output(
                {
                    "message": "No unblocked pending events in this scope",
                    "status": status(store, cfg),
                }
            )
            return
        try:
            refresh_for_write(store, api, cfg, rows, now)
        except (TransportError, APIError) as error:
            if isinstance(error, APIError) and error.code not in (8, 11, 16, 29):
                raise
            with store.conn:
                backoff(store, now, getattr(error, "retry_after", 0))
            if not continuous:
                raise
            print("History read failed; backing off before any write.", flush=True)
            continue
        capacity = budget(store, cfg, now)["remaining_daily_budget"]
        if not capacity:
            if not continuous or once:
                output(
                    {
                        "message": "Rolling budget exhausted; restart later",
                        "status": status(store, cfg),
                    }
                )
                return
            # One history poll per configured interval, no daily manual intervention.
            with store.conn:
                store.set("cooldown_until", now + cfg.poll_seconds)
            print("Rolling budget exhausted; waiting.", flush=True)
            continue
        rows = rows[: min(capacity, cfg.batch_size)]
        if continuous:
            store.automatic_backup(cfg, now)
        rows = assign(store, rows, cfg, now, partial=continuous)
        if not rows:
            # Every finished day in the window is at Last.fm's per-day cap.
            if once:
                output(
                    {
                        "message": "No day with room yet; the next one opens after midnight",
                        "status": status(store, cfg),
                    }
                )
                return
            with store.conn:
                store.set("cooldown_until", now + cfg.poll_seconds)
            print("No day with room under the cap; waiting.", flush=True)
            continue
        count = submit(store, api, cfg, rows, now)
        output({"attempted": count, "status": status(store, cfg)})
        # Timestamp refusals (3 too old, 4 too new) mean the window is misjudged;
        # every following batch would be refused the same way.
        refused = [
            r
            for r in store.rows(
                "SELECT response FROM attempts WHERE sent_at=? AND outcome='rejected'",
                (now,),
            )
            if '"code": "3"' in (r["response"] or "")
            or '"code": "4"' in (r["response"] or "")
        ]
        if refused:
            raise ValueError(
                f"Last.fm refused {len(refused)} timestamps as too old/new; stopped"
            )
        if count and store.rows(
            "SELECT 1 FROM events WHERE status='confirmed' AND verified_at IS NULL AND missing_at IS NULL"
        ):
            time.sleep(cfg.verify_delay_seconds)
            try:
                check_sent(store, api, cfg, continuous, int(time.time()))
            except (TransportError, APIError):
                if not continuous:
                    print(
                        "Batch sent, but the history check failed; run verify.",
                        file=sys.stderr,
                    )
                    raise
                # The guard at the top of the loop retries before any new write.
        if not continuous:
            # Plans may exceed a batch; repeat loads skip already resolved events.
            if store.get("cooldown_until", 0) > int(time.time()) or store.get(
                "auth_blocked"
            ):
                return
            if not load_plan(store, cfg, args.approve_plan, int(time.time())):
                return
        # All requests share API pacing; writes are never concurrently submitted.


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = None
    try:
        cfg = load_config(args.config)
        store = Store(args.db)
        if args.command == "status":
            account = store.get("account")
            if account and account.casefold() != cfg.account.casefold():
                raise ValueError("Database belongs to another Last.fm account")
            output(status(store, cfg))
            return 0
        if args.command == "resume":
            # Lock-free like status: the halted worker keeps holding the lock.
            output({"resumed_from": resume(store, args.note)})
            return 0
        with store.lock():
            store.bind(cfg.account)
            store.recover()
            if args.command == "prepare":
                output(prepare(store, args.folder, cfg))
                output(status(store, cfg))
            elif args.command == "reclassify":
                output({"reclassified": reclassify(store, cfg)})
            elif args.command == "backup":
                store.backup(args.path)
                output({"backup": args.path})
            elif args.command == "export-audit":
                output(export_audit(store, args.path, args.format))
            elif args.command == "export-history":
                write_json(
                    args.path,
                    {
                        "account": cfg.account,
                        "ranges": store.rows("SELECT * FROM history_ranges"),
                        "tracks": store.rows("SELECT * FROM history"),
                    },
                )
            elif args.command == "resolve":
                if args.action == "retry" and not args.accept_duplicate_risk:
                    raise ValueError(
                        "Manual retry needs --accept-duplicate-risk and an evidence note"
                    )
                resolve(store, args.event_id, args.action, args.note)
            elif args.command == "gap-review":
                if args.step == "export":
                    output(export_gaps(store, args.path, args.all))
                else:
                    output(include_gaps(store, args.path, args.note))
            elif args.command == "reset-timestamp":
                reset_timestamp(store, args.event_id, args.note)
            else:
                api = LastFM(credentials(args.secrets), cfg.request_interval_seconds)
                if args.command == "auth":
                    authenticate(api, cfg, args.secrets)
                elif args.command == "dry-run":
                    if not 1 <= args.limit <= 500:
                        raise ValueError("Preview limit must be 1..500")
                    if args.online:
                        now = int(time.time())
                        rows = select(store, args.limit, args.spotify_track_id)
                        refresh_for_write(store, api, cfg, rows, now)
                    output(
                        dry_run(
                            store, cfg, args.limit, args.spotify_track_id, args.online
                        )
                    )
                elif args.command == "sync-history":
                    now = int(time.time())
                    sync(store, api, cfg, 0 if args.full else now - 14 * DAY, now)
                    output(
                        {
                            "potential_overlaps": overlaps(store),
                            "status": status(store, cfg),
                        }
                    )
                elif args.command == "resend-missing":
                    output(resend(store, api, cfg, args.event_ids, args.note))
                elif args.command in ("serve", "notify-test", "telegram-chats"):
                    notifier = Notifier(credentials(args.secrets))
                    if args.command == "telegram-chats":
                        output(notifier.chats())
                    elif args.command == "notify-test":
                        sent = notifier.send(
                            "✅ Тест: импорт Last.fm может писать сюда."
                        )
                        if not sent:
                            raise ValueError("Telegram message not delivered")
                    else:
                        if args.approve_continuous != cfg.account:
                            raise ValueError(
                                "--approve-continuous must exactly match configured account"
                            )
                        # docker stop sends SIGTERM; leave like Ctrl-C between steps.
                        signal.signal(signal.SIGTERM, signal.default_int_handler)
                        serve(
                            store,
                            api,
                            cfg,
                            SimpleNamespace(
                                approve_plan=None,
                                approve_continuous=args.approve_continuous,
                                once=False,
                            ),
                            notifier,
                            run,
                        )
                elif args.command == "verify":
                    output(verify(store, api, cfg))
                elif args.command == "run":
                    run(store, api, cfg, args)
    except (ValueError, OSError, APIError, TransportError) as error:
        # Never emit raw transport exceptions, URLs, credentials, or tracebacks.
        if isinstance(error, OSError):
            print(
                f"Local I/O failure ({type(error).__name__}); inspect file paths and permissions.",
                file=sys.stderr,
            )
        else:
            print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Restart safely with the same database.", file=sys.stderr)
        return 130
    finally:
        if store is not None:
            store.conn.close()
    return 0
