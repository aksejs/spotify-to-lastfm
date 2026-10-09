"""Spotify field mapping and chronological ordering adapted from upstream convert.py."""

import hashlib
import json
from datetime import datetime
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_event(raw, cfg, video=False):
    reasons = []
    if not isinstance(raw, dict):
        raw = {"invalid_record": raw}
        reasons.append("invalid_record")
    ts = raw.get("ts")
    try:
        date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError()
        epoch = int(date.timestamp())
    except (AttributeError, TypeError, ValueError, OverflowError):
        epoch = None
        reasons.append("invalid_timestamp")
    uri = raw.get("spotify_track_uri") or ""
    sid = uri.removeprefix("spotify:track:") if isinstance(uri, str) else ""
    metadata = dict(
        artist=raw.get("master_metadata_album_artist_name") or "",
        track=raw.get("master_metadata_track_name") or "",
        album=raw.get("master_metadata_album_album_name") or "",
    )
    for override in cfg.overrides:
        if override["spotify_track_id"] == sid:
            metadata.update(
                {k: v for k, v in override.items() if k != "spotify_track_id"}
            )
    if video:
        reasons.append("video_history")
    if raw.get("episode_name") or raw.get("spotify_episode_uri"):
        reasons.append("podcast")
    if any(
        not isinstance(metadata[k], str) or not metadata[k].strip()
        for k in ("artist", "track")
    ):
        reasons.append("missing_metadata")
    ms = raw.get("ms_played")
    if type(ms) not in (int, float) or not 0 <= ms < 10**12:
        reasons.append("invalid_play_duration")
    elif ms < cfg.minimum_played_ms:
        reasons.append("too_short")
    if cfg.exclude_skipped and raw.get("skipped") is True:
        reasons.append("skipped")
    return (
        ts if isinstance(ts, str) else None,
        epoch,
        sid,
        {k: v if isinstance(v, str) else "" for k, v in metadata.items()},
        reasons,
    )


def prepare(store, folder, cfg):
    root = Path(folder).resolve()
    files = sorted(
        {
            p
            for pattern in (
                "endsong_*.json",
                "Streaming_History_Audio_*.json",
                "Streaming_History_Video_*.json",
            )
            for p in root.rglob(pattern)
        }
    )
    if not files:
        raise ValueError("No extended Spotify history files found")
    policy = cfg.preparation_policy()
    old = store.get("preparation_policy")
    if old is not None and old != policy:
        raise ValueError(
            "Preparation policy changed; use reclassify for unsubmitted events"
        )
    added = 0
    # One transaction: malformed JSON or a changed source rolls back this entire preparation.
    with store.conn:
        store.set("preparation_policy", policy)
        for path in files:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            prior = store.rows(
                "SELECT hash FROM source_paths WHERE path=?", (str(path),)
            )
            if prior and prior[0]["hash"] != digest:
                raise ValueError(
                    f"Source changed: {path.name}; use immutable originals"
                )
            if store.rows("SELECT hash FROM sources WHERE hash=?", (digest,)):
                store.conn.execute(
                    "INSERT OR IGNORE INTO source_paths VALUES (?,?)",
                    (str(path), digest),
                )
                continue
            data = json.loads(content)
            if not isinstance(data, list):
                raise ValueError(f"{path.name} must contain a JSON array")
            store.conn.execute(
                "INSERT INTO sources VALUES (?,?,?,?)",
                (digest, path.name, len(content), len(data)),
            )
            store.conn.execute(
                "INSERT INTO source_paths VALUES (?,?)", (str(path), digest)
            )
            video = path.name.startswith("Streaming_History_Video_")
            for position, raw in enumerate(data):
                ts, epoch, sid, meta, reasons = parse_event(raw, cfg, video)
                identity = hashlib.sha256(
                    f"{digest}:{position}:{canonical(raw)}".encode()
                ).hexdigest()
                store.conn.execute(
                    """INSERT INTO events
                    (id,source_hash,position,original_ts,original_epoch,raw_json,spotify_id,
                     artist,track,album,eligible,exclusion,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identity,
                        digest,
                        position,
                        ts,
                        epoch,
                        canonical(raw),
                        sid,
                        meta["artist"],
                        meta["track"],
                        meta["album"],
                        not reasons,
                        ",".join(reasons) or None,
                        "rejected" if reasons else "pending",
                    ),
                )
                added += 1
    return {"files_found": len(files), "events_added": added}


def reclassify(store, cfg):
    # Final submitted metadata is immutable. Preserve originals and every decision.
    rows = store.rows("""SELECT e.*,s.name FROM events e JOIN sources s ON s.hash=e.source_hash
                         WHERE attempts=0 AND status IN ('pending','rejected')
                         AND (exclusion IS NULL OR exclusion!='manual_overlap')""")
    with store.conn:
        for row in rows:
            _, _, _, meta, reasons = parse_event(
                json.loads(row["raw_json"]),
                cfg,
                row["name"].startswith("Streaming_History_Video_"),
            )
            store.conn.execute(
                """UPDATE events SET artist=?,track=?,album=?,eligible=?,exclusion=?,
                status=?,assigned_ts=NULL,overlap=NULL WHERE id=?""",
                (
                    meta["artist"],
                    meta["track"],
                    meta["album"],
                    not reasons,
                    ",".join(reasons) or None,
                    "rejected" if reasons else "pending",
                    row["id"],
                ),
            )
        store.set("preparation_policy", cfg.preparation_policy())
        store.conn.execute("DELETE FROM plans")
    return len(rows)
