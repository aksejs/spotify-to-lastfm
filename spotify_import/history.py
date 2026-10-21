import json
import re
import time
import unicodedata
from collections import Counter

from .engine import reprocess_garbled


def sync(store, api, cfg, start, end, now=None):
    now = int(time.time()) if now is None else now
    tracks = api.history(cfg.account, start, end)
    # A failed/incomplete fetch never replaces the last complete snapshot.
    with store.conn:
        store.conn.execute(
            "DELETE FROM history WHERE timestamp BETWEEN ? AND ?", (start, end)
        )
        store.conn.executemany(
            "INSERT INTO history VALUES (?,?,?,?,?)",
            [(*key, count) for key, count in tracks.items()],
        )
        store.conn.execute(
            "INSERT INTO history_ranges VALUES (?,?,?)", (start, end, now)
        )
    return tracks


def overlaps(store):
    # Spotify ts describes the end; compare both end and estimated start, within 2 seconds.
    # Candidate only: never automatically discard a listening event.
    history = {}
    for row in store.rows("SELECT * FROM history"):
        history.setdefault(
            (row["artist"].casefold(), row["track"].casefold()), []
        ).append(row["timestamp"])
    import bisect

    for values in history.values():
        values.sort()
    candidates = 0
    with store.conn:
        for row in store.rows("""SELECT * FROM events WHERE eligible=1 AND attempts=0
            AND id NOT IN (SELECT event_id FROM decisions WHERE action='keep')"""):
            raw = json.loads(row["raw_json"])
            start = row["original_epoch"] - int(raw.get("ms_played", 0) / 1000)
            times = history.get((row["artist"].casefold(), row["track"].casefold()), [])
            hits = set()
            for target in (start, row["original_epoch"]):
                i = bisect.bisect_left(times, target - 2)
                hits.update(t for t in times[i : i + 5] if abs(t - target) <= 2)
            note = json.dumps(sorted(hits)) if hits else None
            store.conn.execute(
                "UPDATE events SET overlap=? WHERE id=?", (note, row["id"])
            )
            candidates += bool(hits)
    return candidates


def simplify(name):
    """Comparable form of a name that Last.fm may have tidied up on its side.

    Drops case, bracketed parts such as "(feat. X)", a leading "The", and
    punctuation: "The Make-Up" -> "makeup", "How Long Have You Known?" ->
    "howlonghaveyouknown". Only used for the scrobble at our exact timestamp.
    """
    name = unicodedata.normalize("NFKC", name).casefold().replace("ё", "е")
    full = "".join(ch for ch in name if ch.isalnum())
    name = re.sub(r"[(\[].*?[)\]]", " ", name).strip()
    name = re.sub(r"^the\s+", "", name)
    return "".join(ch for ch in name if ch.isalnum()) or full


def same(a, b):
    a, b = simplify(a), simplify(b)
    return a == b or bool(a and b and (a in b or b in a))


def names(row):
    """Submitted names plus any names Last.fm returned for this scrobble."""
    result = {(row["artist"], row["track"])}
    try:
        response = json.loads(row["response"] or "")
        result.add((response["artist"]["#text"], response["track"]["#text"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return result


def verify(store, api, cfg, now=None, unresolved=False, ids=None):
    """Look up accepted/uncertain scrobbles in account history and record the result.

    Last.fm may display its own capitalization, so names compare case-insensitively.
    A confirmed event that is absent is recorded as missing; absence never
    authorizes an automatic replay.
    """
    now = int(time.time()) if now is None else now
    store.recover()
    # Answers once judged garbled may be readable now (e.g. reordered batches).
    reprocess_garbled(store, now)
    rows = store.rows(
        "SELECT * FROM events WHERE status IN ('uncertain','confirmed') AND assigned_ts IS NOT NULL"
        + (" AND (status='uncertain' OR verified_at IS NULL)" if unresolved else "")
    )
    if ids is not None:
        rows = [r for r in rows if r["id"] in set(ids)]
    verified = absent = 0
    missing = []
    # Group by UTC day to avoid fetching years of unrelated account history.
    groups = {}
    for row in rows:
        groups.setdefault(row["assigned_ts"] // 86400, []).append(row)
    for group in groups.values():
        found = sync(
            store,
            api,
            cfg,
            min(r["assigned_ts"] for r in group),
            max(r["assigned_ts"] for r in group),
            now,
        )
        by_ts = Counter()
        for key, count in found.items():
            by_ts[key[0]] += count
        with store.conn:
            for row in group:
                wanted = names(row)
                # Last.fm may swap an artist to another script (Bi-2 -> Би-2) while
                # reporting no correction. An accepted scrobble alone at our exact
                # second therefore needs only artist OR track to agree.
                # Uncertain events need the track itself to agree: Last.fm also
                # rewrites artists (Черный Лукич -> Чёрный Лукич,
                # Grazhdanskaya Oborona -> Гражданская Оборона). Our second was
                # free right before sending, so the sole scrobble there is ours.
                loose = row["status"] == "confirmed"
                matches = sum(
                    count
                    for (ts, artist, track, album), count in found.items()
                    if ts == row["assigned_ts"]
                    and any(
                        (same(artist, a) or same(track, t)) if loose else same(track, t)
                        for a, t in wanted
                    )
                )
                if matches == 1 and by_ts[row["assigned_ts"]] == 1:
                    verified += 1
                    if row["status"] == "uncertain":
                        store.conn.execute(
                            "UPDATE events SET status='confirmed',confirmed_at=?,response=? WHERE id=?",
                            (
                                now,
                                "Matched unique exact timestamp/artist/track in account history",
                                row["id"],
                            ),
                        )
                        store.conn.execute(
                            "UPDATE attempts SET outcome='confirmed' WHERE event_id=? AND outcome='uncertain'",
                            (row["id"],),
                        )
                    store.conn.execute(
                        "UPDATE events SET verified_at=?,missing_at=NULL WHERE id=?",
                        (now, row["id"]),
                    )
                elif row["status"] == "confirmed" and not matches:
                    absent += 1
                    if row["verified_at"] is None:
                        missing.append(row["id"])
                        store.conn.execute(
                            "UPDATE events SET missing_at=coalesce(missing_at,?) WHERE id=?",
                            (now, row["id"]),
                        )
                # Absence/corrections/duplicates NEVER authorize replay.
    return {
        "checked": len(rows),
        "verified": verified,
        "not_observed_confirmed": absent,
        "missing_ids": missing,
        "uncertain_remaining": store.rows(
            "SELECT count(*) n FROM events WHERE status='uncertain'"
        )[0]["n"],
    }


def resend(store, api, cfg, event_ids, note, now=None, min_age=3600):
    """Requeue accepted scrobbles that account history still lacks, with new timestamps."""
    now = int(time.time()) if now is None else now
    if not note.strip():
        raise ValueError("Evidence note required")
    rows = store.rows(
        """SELECT * FROM events WHERE status='confirmed' AND verified_at IS NULL
        AND missing_at IS NOT NULL"""
    )
    if event_ids:
        rows = [r for r in rows if r["id"] in set(event_ids)]
        if len(rows) != len(set(event_ids)):
            raise ValueError("Only events recorded as missing can be resent")
    if any(now - r["missing_at"] < min_age for r in rows):
        raise ValueError("Missing for under an hour; Last.fm may still be catching up")
    if not rows:
        return {"requeued": 0}
    # A fresh read immediately before requeueing; anything now visible is kept.
    verify(store, api, cfg, now, ids=[r["id"] for r in rows])
    still = store.rows(
        f"""SELECT * FROM events WHERE verified_at IS NULL AND status='confirmed'
        AND id IN ({",".join("?" * len(rows))})""",
        [r["id"] for r in rows],
    )
    with store.conn:
        for row in still:
            store.conn.execute(
                """UPDATE events SET status='pending',assigned_ts=NULL,confirmed_at=NULL,
                missing_at=NULL,response=? WHERE id=?""",
                ("Accepted but absent from account history; requeued", row["id"]),
            )
            store.conn.execute(
                "INSERT INTO decisions(event_id,time,action,note) VALUES (?,?,?,?)",
                (
                    row["id"],
                    now,
                    "resend",
                    json.dumps({"old_timestamp": row["assigned_ts"], "note": note}),
                ),
            )
    return {"requeued": len(still), "found_on_recheck": len(rows) - len(still)}


def gaps(store, margin=30):
    """Live-period plays with no account scrobble while they were playing.

    Review aid only: a nearby scrobble may be this play under another name, and an
    empty window can still mean the account history snapshot is incomplete.
    """
    latest = store.rows(
        "SELECT max(original_epoch) t FROM events WHERE exclusion='live_scrobbled_period'"
    )[0]["t"]
    if latest is None:
        return []
    if not store.rows(
        "SELECT 1 FROM history_ranges WHERE start=0 AND end>=?", (latest,)
    ):
        raise ValueError(
            "Gap review needs a full history snapshot: sync-history --full"
        )
    import bisect

    history = store.rows("SELECT * FROM history ORDER BY timestamp")
    times = [row["timestamp"] for row in history]
    result = []
    # Only events whose sole exclusion is the live period would otherwise be eligible.
    for row in store.rows("""SELECT * FROM events WHERE attempts=0 AND status='rejected'
            AND exclusion='live_scrobbled_period' ORDER BY original_epoch,id"""):
        raw = json.loads(row["raw_json"])
        start = row["original_epoch"] - int(raw.get("ms_played", 0) / 1000)
        lo = bisect.bisect_left(times, start - margin)
        hi = bisect.bisect_right(times, row["original_epoch"] + margin)
        nearby = [
            f"{h['timestamp']} {h['artist']} — {h['track']}" for h in history[lo:hi]
        ]
        result.append(
            {
                "id": row["id"],
                "original_ts": row["original_ts"],
                "ms_played": raw.get("ms_played"),
                "artist": row["artist"],
                "track": row["track"],
                "album": row["album"],
                "nearby_scrobbles": " | ".join(nearby),
            }
        )
    return result
