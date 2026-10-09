import hashlib
import json
import math
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from .api import APIError, TransportError

DAY = 86400
# Conservative interior of the approximately 14-day API window.
MAX_AGE = 13 * DAY


def budget(store, cfg, now):
    attempts = store.rows("SELECT * FROM attempts WHERE sent_at>?", (now - DAY,))
    imported = len(attempts)  # Count attempts, even explicit failures, conservatively.
    keys = {
        (r["assigned_ts"], r["artist"], r["track"])
        for r in store.rows(
            "SELECT assigned_ts,artist,track FROM events WHERE attempts>0"
        )
    }
    live = 0
    for row in store.rows(
        "SELECT * FROM history WHERE timestamp>? AND timestamp<=?", (now - DAY, now)
    ):
        count = row["multiplicity"]
        if (row["timestamp"], row["artist"], row["track"]) in keys:
            count = max(0, count - 1)
        live += count
    return {
        "import_attempts_24h": imported,
        "observed_live_24h": live,
        "remaining_daily_budget": max(
            0, min(cfg.daily_budget - imported, cfg.combined_budget - imported - live)
        ),
    }


def status(store, cfg, now=None):
    now = int(time.time()) if now is None else now
    counts = {
        r["status"]: r["n"]
        for r in store.rows("SELECT status,count(*) n FROM events GROUP BY status")
    }
    eligible = store.rows("SELECT count(*) n FROM events WHERE eligible=1")[0]["n"]
    confirmed_today = store.rows(
        "SELECT count(*) n FROM events WHERE status='confirmed' AND confirmed_at>?",
        (now - DAY,),
    )[0]["n"]
    result = {
        "account": cfg.account,
        "total_events": sum(counts.values()),
        "eligible": eligible,
        **{
            s: counts.get(s, 0)
            for s in ("pending", "submitting", "confirmed", "rejected", "uncertain")
        },
        "imported_last_24h": confirmed_today,
        **budget(store, cfg, now),
    }
    first = store.rows("SELECT min(sent_at) t FROM attempts")[0]["t"]
    recent = store.rows(
        "SELECT count(*) n FROM events WHERE status='confirmed' AND confirmed_at>?",
        (now - 7 * DAY,),
    )[0]["n"]
    capacity = min(
        cfg.daily_budget, max(0, cfg.combined_budget - result["observed_live_24h"])
    )
    throughput = (
        recent / min(7, (now - first) / DAY)
        if first is not None and now - first >= DAY
        else None
    )
    rate = min(capacity, throughput) if throughput is not None else capacity
    days = math.ceil(result["pending"] / rate) if rate > 0 else None
    result.update(
        estimated_days=days,
        estimated_completion_utc=(
            datetime.fromtimestamp(now, UTC) + timedelta(days=days)
        )
        .date()
        .isoformat()
        if days is not None
        else None,
        estimate_basis="recent throughput and configured capacity"
        if throughput is not None
        else "configured capacity minus observed live listening",
        estimate_excludes_unresolved=counts.get("uncertain", 0)
        + store.rows(
            "SELECT count(*) n FROM events WHERE eligible=1 AND status='rejected'"
        )[0]["n"],
        cooldown_until=store.get("cooldown_until", 0),
        history_last_fetched=store.rows("SELECT max(fetched_at) t FROM history_ranges")[
            0
        ]["t"],
    )
    exclusions = Counter()
    for row in store.rows(
        "SELECT exclusion,count(*) n FROM events WHERE exclusion IS NOT NULL GROUP BY exclusion"
    ):
        for reason in row["exclusion"].split(","):
            exclusions[reason] += row["n"]
    result["excluded_events"] = result["total_events"] - eligible
    result["excluded_by_reason_nonexclusive"] = dict(exclusions)
    result["potential_overlaps"] = store.rows(
        "SELECT count(*) n FROM events WHERE overlap IS NOT NULL"
    )[0]["n"]
    result["history_coverage"] = store.rows(
        "SELECT * FROM history_ranges ORDER BY fetched_at DESC LIMIT 5"
    )
    return result


def select(store, limit, spotify_id=None):
    return store.rows(
        """SELECT * FROM events WHERE status='pending' AND eligible=1 AND overlap IS NULL
        AND (? IS NULL OR spotify_id=?) ORDER BY original_epoch,id LIMIT ?""",
        (spotify_id, spotify_id, limit),
    )


def assign(store, rows, cfg, now):
    occupied = {r["timestamp"] for r in store.rows("SELECT timestamp FROM history")}
    occupied.update(
        r["assigned_ts"]
        for r in store.rows(
            "SELECT assigned_ts FROM events WHERE assigned_ts IS NOT NULL"
        )
    )
    last = store.rows("SELECT max(assigned_ts) t FROM events WHERE attempts>0")[0]["t"]
    cursor = max(now - cfg.timestamp_age_days * DAY, (last + 1) if last else 0)
    with store.conn:
        for row in rows:
            if row["assigned_ts"] is None:
                while cursor in occupied:
                    cursor += 1
                if cursor > now - 60:
                    raise ValueError(
                        "No safe timestamp slots; wait before preparing another batch"
                    )
                row["assigned_ts"] = cursor
                store.conn.execute(
                    "UPDATE events SET assigned_ts=? WHERE id=?", (cursor, row["id"])
                )
                occupied.add(cursor)
                cursor += 1
            if not now - MAX_AGE <= row["assigned_ts"] <= now - 60:
                raise ValueError(
                    "Assigned timestamp expired or too new; use reset-timestamp for conclusively unaccepted events"
                )
    return rows


def dry_run(store, cfg, limit=10, spotify_id=None, online=False, now=None):
    now = int(time.time()) if now is None else now
    rows = assign(store, select(store, limit, spotify_id), cfg, now)
    public = [
        {
            k: r[k]
            for k in (
                "id",
                "original_ts",
                "spotify_id",
                "artist",
                "track",
                "album",
                "assigned_ts",
            )
        }
        for r in rows
    ]
    payload = json.dumps(public, sort_keys=True, ensure_ascii=False)
    plan = hashlib.sha256((payload + cfg.fingerprint() + str(now)).encode()).hexdigest()
    with store.conn:
        store.conn.execute(
            "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?)",
            (plan, now, cfg.fingerprint(), payload, online),
        )
    duplicate_groups = store.rows("""SELECT count(*) n FROM
        (SELECT raw_json FROM events GROUP BY raw_json HAVING count(DISTINCT source_hash)>1)""")[
        0
    ]["n"]
    return {
        "plan_id": plan,
        "online_collision_check": online,
        "identical_record_groups_across_files_review_only": duplicate_groups,
        "overlap_check": "only fetched ranges; review history_coverage"
        if store.rows("SELECT * FROM history_ranges LIMIT 1")
        else "UNAVAILABLE: no Last.fm snapshot",
        "status": status(store, cfg, now),
        "preview": public,
    }


def load_plan(store, cfg, plan, now):
    found = store.rows("SELECT * FROM plans WHERE id=?", (plan,))
    if not found or found[0]["config_hash"] != cfg.fingerprint():
        raise ValueError("Unknown plan or configuration changed; run dry-run again")
    rows = []
    for preview in json.loads(found[0]["events"]):
        actual = store.rows("SELECT * FROM events WHERE id=?", (preview["id"],))[0]
        if any(actual[k] != v for k, v in preview.items()):
            raise ValueError("Plan changed; run dry-run again")
        if actual["status"] == "pending":
            if actual["overlap"]:
                raise ValueError("Plan has unresolved overlap candidates")
            if not now - MAX_AGE <= actual["assigned_ts"] <= now - 60:
                raise ValueError("Plan timestamp outside safe window")
            rows.append(actual)
    return rows


def backoff(store, now, floor=0):
    failures = store.get("consecutive_failures", 0) + 1
    delay = min(21600, 60 * 2 ** min(failures - 1, 9))
    store.set("consecutive_failures", failures)
    store.set(
        "cooldown_until", max(store.get("cooldown_until", 0), now + max(delay, floor))
    )


def outcome(store, batch, rows, states, now, metadata=None):
    # All event, attempt and cooldown updates commit together.
    with store.conn:
        for key, value in (metadata or {}).items():
            store.set(key, value)
        for row, (state, reason) in zip(rows, states):
            store.conn.execute(
                "UPDATE events SET status=?,response=?,confirmed_at=? WHERE id=?",
                (state, reason, now if state == "confirmed" else None, row["id"]),
            )
            store.conn.execute(
                "UPDATE attempts SET outcome=?,response=? WHERE batch=? AND event_id=?",
                (state, reason, batch, row["id"]),
            )


def submit(store, api, cfg, rows, now=None):
    now = int(time.time()) if now is None else now
    if not rows:
        return 0
    if (
        len(rows) > cfg.batch_size
        or len(rows) > budget(store, cfg, now)["remaining_daily_budget"]
    ):
        raise ValueError("Batch exceeds available rolling budget")
    if store.get("cooldown_until", 0) > now:
        return 0
    # Check both DB and current account snapshot immediately before reserving a write.
    for row in rows:
        current = store.rows("SELECT * FROM events WHERE id=?", (row["id"],))[0]
        if current != row or current["status"] != "pending" or current["overlap"]:
            raise ValueError("Event state changed; regenerate the plan")
        if not now - MAX_AGE <= row["assigned_ts"] <= now - 60:
            raise ValueError("Assigned timestamp outside safe window")
        if store.rows("SELECT 1 FROM history WHERE timestamp=?", (row["assigned_ts"],)):
            raise ValueError(
                "Timestamp collision in account history; inspect before resetting timestamp"
            )
    batch = uuid.uuid4().hex
    with store.conn:
        for row in rows:
            store.conn.execute(
                "UPDATE events SET status='submitting',attempts=attempts+1,last_attempt=? WHERE id=?",
                (now, row["id"]),
            )
            store.conn.execute(
                "INSERT INTO attempts(batch,event_id,sent_at,assigned_ts,outcome) VALUES (?,?,?,?,'submitting')",
                (batch, row["id"], now, row["assigned_ts"]),
            )
    try:
        response = api.submit(rows)
    except APIError as error:
        code = error.code
        if code in (11, 16, 29):
            state = "pending"  # Explicit whole-request refusal, safe to retry same timestamp.
            with store.conn:
                backoff(store, now, error.retry_after)
        elif code in (4, 9, 10, 13, 26):
            state = "pending"
            with store.conn:
                store.set("auth_blocked", code)
        elif code == 8:
            state = "uncertain"  # Generic failure gives too little evidence for replay.
        else:
            state = "rejected"
        outcome(
            store,
            batch,
            rows,
            [(state, json.dumps({"api_error": code}))] * len(rows),
            now,
        )
        return len(rows)
    except TransportError:
        outcome(
            store,
            batch,
            rows,
            [("uncertain", "Transport failure; server acceptance unknown")] * len(rows),
            now,
        )
        with store.conn:
            backoff(store, now)
        return len(rows)
    try:
        result = response["scrobbles"]
        items = result["scrobble"]
        items = [items] if isinstance(items, dict) else items
        codes = [int(item["ignoredMessage"]["code"]) for item in items]
        accepted, ignored = (
            int(result["@attr"]["accepted"]),
            int(result["@attr"]["ignored"]),
        )
        if (
            len(items) != len(rows)
            or accepted != codes.count(0)
            or ignored != len(rows) - accepted
        ):
            raise ValueError("inconsistent counts")
        # Timestamp equality verifies order even if the service corrects metadata.
        if any(
            int(item["timestamp"]) != row["assigned_ts"]
            for item, row in zip(items, rows)
        ):
            raise ValueError("response order/timestamps mismatch")
        states = []
        for item, code in zip(items, codes):
            safe = {
                k: item[k]
                for k in ("track", "artist", "album", "timestamp", "ignoredMessage")
                if k in item
            }
            safe['batch_counts'] = {'accepted': accepted, 'ignored': ignored}
            state = (
                "confirmed" if code == 0 else ("pending" if code == 5 else "rejected")
            )
            states.append((state, json.dumps(safe, ensure_ascii=False)))
    except (KeyError, ValueError, TypeError):
        outcome(
            store,
            batch,
            rows,
            [("uncertain", "Malformed/inconsistent batch response")] * len(rows),
            now,
        )
        with store.conn:
            backoff(store, now)
        return len(rows)
    metadata = {'consecutive_failures': 0}
    if 5 in codes:
        metadata['cooldown_until'] = now + DAY + 60
    outcome(store, batch, rows, states, now, metadata)
    return len(rows)


def resolve(store, event_id, action, note, now=None):
    now = int(time.time()) if now is None else now
    rows = store.rows("SELECT * FROM events WHERE id=?", (event_id,))
    if not rows or not note.strip():
        raise ValueError("Valid event ID and evidence note required")
    row = rows[0]
    with store.conn:
        if action == "confirm" and row["status"] == "uncertain":
            store.conn.execute(
                "UPDATE events SET status='confirmed',confirmed_at=?,response=? WHERE id=?",
                (now, note, event_id),
            )
        elif action == "exclude" and row["attempts"] == 0:
            store.conn.execute(
                "UPDATE events SET status='rejected',eligible=0,exclusion='manual_overlap' WHERE id=?",
                (event_id,),
            )
        elif action == "keep" and row["attempts"] == 0:
            store.conn.execute("UPDATE events SET overlap=NULL WHERE id=?", (event_id,))
        elif (
            action == "retry"
            and row["status"] in ("uncertain", "rejected")
            and row["eligible"]
        ):
            store.conn.execute(
                "UPDATE events SET status='pending',response=? WHERE id=?",
                (note, event_id),
            )
        else:
            raise ValueError("Resolution not valid for current state")
        store.conn.execute(
            "INSERT INTO decisions(event_id,time,action,note) VALUES (?,?,?,?)",
            (event_id, now, action, note),
        )


def reset_timestamp(store, event_id, note, now=None):
    now = int(time.time()) if now is None else now
    row = store.rows("SELECT * FROM events WHERE id=?", (event_id,))
    if not row or not note.strip():
        raise ValueError("Event ID and evidence note required")
    row = row[0]
    ambiguous = store.rows(
        "SELECT 1 FROM attempts WHERE event_id=? AND outcome IN ('uncertain','submitting','confirmed')",
        (event_id,),
    )
    if row["status"] not in ("pending", "rejected") or ambiguous or not row["eligible"]:
        raise ValueError(
            "Cannot change timestamp of an accepted or ambiguous submission"
        )
    with store.conn:
        store.conn.execute(
            "UPDATE events SET assigned_ts=NULL,status='pending' WHERE id=?",
            (event_id,),
        )
        store.conn.execute(
            "INSERT INTO decisions(event_id,time,action,note) VALUES (?,?,?,?)",
            (
                event_id,
                now,
                "reset-timestamp",
                json.dumps({"old_timestamp": row["assigned_ts"], "note": note}),
            ),
        )
