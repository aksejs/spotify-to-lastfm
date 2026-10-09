import hashlib
import json
from dataclasses import asdict, dataclass, field
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
        if self.poll_seconds < 60 or self.request_interval_seconds < 0.2:
            raise ValueError("poll_seconds >= 60; request_interval_seconds >= 0.2")
        if type(self.exclude_skipped) is not bool or not isinstance(
            self.overrides, list
        ):
            raise ValueError("invalid exclusion/override configuration")
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

    def preparation_policy(self):
        return {
            k: asdict(self)[k]
            for k in ("minimum_played_ms", "exclude_skipped", "overrides")
        }

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


def load_config(path):
    return (
        Config(**(yaml.safe_load(Path(path).read_text()) or {})) if path else Config()
    )
