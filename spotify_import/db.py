import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 hash TEXT PRIMARY KEY, name TEXT NOT NULL, bytes INTEGER NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_paths(path TEXT PRIMARY KEY, hash TEXT NOT NULL REFERENCES sources(hash));
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, source_hash TEXT NOT NULL REFERENCES sources(hash), position INTEGER NOT NULL,
 original_ts TEXT, original_epoch INTEGER, raw_json TEXT NOT NULL, spotify_id TEXT,
 artist TEXT NOT NULL, track TEXT NOT NULL, album TEXT NOT NULL,
 eligible INTEGER NOT NULL, exclusion TEXT, assigned_ts INTEGER UNIQUE,
 status TEXT NOT NULL CHECK(status IN ('pending','submitting','confirmed','rejected','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, last_attempt INTEGER, response TEXT, confirmed_at INTEGER,
 overlap TEXT, UNIQUE(source_hash,position));
CREATE INDEX IF NOT EXISTS queue ON events(status,eligible,original_epoch);
CREATE INDEX IF NOT EXISTS spotify_id ON events(spotify_id);
CREATE TABLE IF NOT EXISTS attempts(
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES events(id),
 sent_at INTEGER NOT NULL, assigned_ts INTEGER NOT NULL, outcome TEXT NOT NULL, response TEXT);
CREATE INDEX IF NOT EXISTS attempt_time ON attempts(sent_at);
CREATE TABLE IF NOT EXISTS decisions(
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, time INTEGER NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history(
 timestamp INTEGER NOT NULL, artist TEXT NOT NULL, track TEXT NOT NULL, album TEXT NOT NULL,
 multiplicity INTEGER NOT NULL, PRIMARY KEY(timestamp,artist,track,album));
CREATE INDEX IF NOT EXISTS history_time ON history(timestamp);
CREATE TABLE IF NOT EXISTS history_ranges(start INTEGER NOT NULL, end INTEGER NOT NULL, fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, config_hash TEXT NOT NULL,
 events TEXT NOT NULL, online INTEGER NOT NULL);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        version = self.get("schema_version")
        if version not in (None, 1):
            raise ValueError("Unsupported database schema")
        with self.conn:
            self.set("schema_version", 1)

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
        )

    @contextmanager
    def lock(self):
        with open(str(self.path) + ".lock.local", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Another importer command holds the database lock"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def bind(self, account):
        old = self.get("account")
        if old and old.casefold() != account.casefold():
            raise ValueError("Database belongs to another Last.fm account")
        with self.conn:
            self.set("account", account)

    def recover(self):
        with self.conn:
            self.conn.execute(
                "UPDATE events SET status='uncertain', response='interrupted submission' WHERE status='submitting'"
            )
            self.conn.execute(
                "UPDATE attempts SET outcome='uncertain' WHERE outcome='submitting'"
            )

    def backup(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise ValueError("Backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(destination) as target:
            self.conn.backup(target)
