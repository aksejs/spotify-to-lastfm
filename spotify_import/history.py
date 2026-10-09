import json
import time
from collections import Counter


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


def verify(store, api, cfg, now=None, only_uncertain=False):
    now = int(time.time()) if now is None else now
    store.recover()
    rows = store.rows(
        "SELECT * FROM events WHERE status IN ('uncertain','confirmed') AND assigned_ts IS NOT NULL"
        + (" AND status='uncertain'" if only_uncertain else "")
    )
    if not rows:
        return {"verified": 0, "uncertain_remaining": 0, "not_observed_confirmed": 0}
    # Group by UTC day to avoid fetching years of unrelated account history.
    groups = {}
    for row in rows:
        groups.setdefault(row["assigned_ts"] // 86400, []).append(row)
    verified = absent = 0
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
                matches = sum(
                    count
                    for (ts, artist, track, album), count in found.items()
                    if ts == row["assigned_ts"]
                    and artist == row["artist"]
                    and track == row["track"]
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
                elif row["status"] == "confirmed":
                    absent += 1
                # Absence/corrections/duplicates NEVER authorize replay.
    return {
        "verified": verified,
        "not_observed_confirmed": absent,
        "uncertain_remaining": store.rows(
            "SELECT count(*) n FROM events WHERE status='uncertain'"
        )[0]["n"],
    }
