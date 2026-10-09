import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml


@dataclass
class Config:
    account: str = "DivokaSarka"
    daily_budget: int = 500
    combined_budget: int = 500
    batch_size: int = 50
    minimum_played_ms: int = 30000
    exclude_skipped: bool = False
    timestamp_age_days: int = 2
    request_interval_seconds: float = 1.0
    poll_seconds: int = 3600
    backup_interval_hours: int = 24
    backup_keep: int = 7
    # UTC instant from which the account already has live scrobbles of these plays.
    live_scrobbling_since: str | None = None
    overrides: list = field(default_factory=list)

    def __post_init__(self):
        if not self.account or not isinstance(self.account, str):
            raise ValueError("account is required")
        for key in (
            "daily_budget",
            "combined_budget",
            "batch_size",
            "minimum_played_ms",
            "timestamp_age_days",
            "poll_seconds",
            "backup_interval_hours",
            "backup_keep",
        ):
            if type(getattr(self, key)) is not int:
                raise ValueError(f"{key} must be an integer")
        if (
            not 1 <= self.batch_size <= 50
            or min(self.daily_budget, self.combined_budget) < 1
        ):
            raise ValueError("budgets must be positive; batch_size must be 1..50")
        if not 1 <= self.timestamp_age_days <= 12 or self.minimum_played_ms < 0:
            raise ValueError("timestamp_age_days must be 1..12; minimum_played_ms >= 0")
        if (
            type(self.request_interval_seconds) not in (int, float)
            or not math.isfinite(self.request_interval_seconds)
            or self.poll_seconds < 60
            or self.request_interval_seconds < 0.2
        ):
            raise ValueError("poll_seconds >= 60; request_interval_seconds >= 0.2")
        if self.backup_interval_hours < 1 or self.backup_keep < 1:
            raise ValueError("backup_interval_hours and backup_keep must be positive")
        if type(self.exclude_skipped) is not bool or not isinstance(
            self.overrides, list
        ):
            raise ValueError("invalid exclusion/override configuration")
        if isinstance(self.live_scrobbling_since, datetime):
            # Unquoted YAML timestamps arrive as datetimes; store canonical text.
            if self.live_scrobbling_since.tzinfo is None:
                raise ValueError("live_scrobbling_since needs an explicit UTC offset")
            self.live_scrobbling_since = self.live_scrobbling_since.isoformat()
        if self.live_scrobbling_since is not None:
            self.live_cutoff()
        seen = set()
        for override in self.overrides:
            if not isinstance(override, dict) or set(override) - {
                "spotify_track_id",
                "artist",
                "track",
                "album",
            }:
                raise ValueError("invalid override fields")
            key = override.get("spotify_track_id")
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError("override needs a unique spotify_track_id")
            if any(not isinstance(v, str) or not v.strip() for v in override.values()):
                raise ValueError("override values must be nonempty strings")
            seen.add(key)

    def live_cutoff(self):
        if self.live_scrobbling_since is None:
            return None
        try:
            date = datetime.fromisoformat(
                str(self.live_scrobbling_since).replace("Z", "+00:00")
            )
        except ValueError:
            date = None
        if date is None or date.tzinfo is None:
            raise ValueError("live_scrobbling_since must be an ISO-8601 UTC timestamp")
        return int(date.timestamp())

    def preparation_policy(self):
        keys = ["minimum_played_ms", "exclude_skipped", "overrides"]
        # Omitted when unset so databases prepared before this option stay valid.
        if self.live_scrobbling_since is not None:
            keys.append("live_scrobbling_since")
        return {k: asdict(self)[k] for k in keys}

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


def load_config(path):
    return (
        Config(**(yaml.safe_load(Path(path).read_text()) or {})) if path else Config()
    )
