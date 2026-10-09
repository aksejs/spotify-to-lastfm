import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
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
from .history import overlaps, sync, verify
from .prepare import prepare, reclassify


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
    decision.add_argument("action", choices=["confirm", "keep", "exclude", "retry"])
    decision.add_argument("--note", required=True)
    decision.add_argument("--accept-duplicate-risk", action="store_true")
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


def refresh_for_write(store, api, cfg, rows, now):
    start = min(
        [now - DAY] + [r["assigned_ts"] for r in rows if r["assigned_ts"] is not None]
    )
    # Empty queue needs the planned assignment window as well as today's live plays.
    start = min(start, now - cfg.timestamp_age_days * DAY)
    sync(store, api, cfg, start, now, now)


def run(store, api, cfg, args):
    continuous = bool(args.approve_continuous)
    if continuous and args.approve_continuous != cfg.account:
        raise ValueError("--approve-continuous must exactly match configured account")
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
    api.check_account(cfg.account)
    with store.conn:
        store.set("auth_blocked", None)
    while True:
        now = int(time.time())
        if store.get("auth_blocked"):
            raise ValueError("Authentication blocked; correct credentials and restart")
        cooldown = store.get("cooldown_until", 0)
        if now < cooldown:
            if not continuous:
                output({"paused_until": cooldown})
                return
            time.sleep(min(60, cooldown - now))
            continue
        if continuous and now - store.get("last_auto_verify", 0) >= cfg.poll_seconds:
            try:
                result = verify(store, api, cfg, now, only_uncertain=True)
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
            if not continuous:
                output(
                    {
                        "message": "Rolling budget exhausted; restart same plan later",
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
        assign(store, rows, cfg, now)
        count = submit(store, api, cfg, rows, now)
        output({"attempted": count, "status": status(store, cfg)})
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
    try:
        cfg = load_config(args.config)
        store = Store(args.db)
        if args.command == "status":
            output(status(store, cfg))
            return
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
    return 0
