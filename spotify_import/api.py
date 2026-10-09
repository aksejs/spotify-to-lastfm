"""HTTPS form-body API transport; deliberately no automatic POST retries."""

import json
import os
import time
from collections import Counter
from hashlib import md5
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class APIError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = int(code)
        self.retry_after = retry_after
        super().__init__(f"Last.fm API error {self.code}")


class TransportError(Exception):
    """A write may have reached the server. Never replay on this exception."""


def signature(params, secret):
    # ASCII parameter ordering, as in upstream sign_call; protocol mandates MD5.
    message = "".join(
        k + str(params[k])
        for k in sorted(params)
        if k not in ("format", "callback", "api_sig")
    )
    return md5((message + secret).encode("utf-8")).hexdigest()


def credentials(path):
    values = {}
    path = Path(path)
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError("Secrets file must have mode 600")
        values = json.loads(path.read_text())
    keys = ("LASTFM_API_KEY", "LASTFM_API_SECRET", "LASTFM_SESSION_KEY")
    return {k: os.environ.get(k) or values.get(k, "") for k in keys}


def save_credentials(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(values, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class LastFM:
    def __init__(self, values, interval=1.0):
        self.values = values
        self.interval = interval
        self.last_request = 0.0

    def call(self, method, params=None, *, signed=False, session=False):
        if not self.values.get("LASTFM_API_KEY"):
            raise ValueError("Supply LASTFM_API_KEY locally")
        body = {
            "method": method,
            "api_key": self.values["LASTFM_API_KEY"],
            **(params or {}),
        }
        if session:
            if not self.values.get("LASTFM_SESSION_KEY"):
                raise ValueError("Run auth or supply LASTFM_SESSION_KEY locally")
            body["sk"] = self.values["LASTFM_SESSION_KEY"]
        if signed:
            if not self.values.get("LASTFM_API_SECRET"):
                raise ValueError("Supply LASTFM_API_SECRET locally")
            body["api_sig"] = signature(body, self.values["LASTFM_API_SECRET"])
        body["format"] = "json"
        # Keep ALL parameters out of URLs and exception messages.
        request = Request(
            "https://ws.audioscrobbler.com/2.0/",
            data=urlencode(body).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "spotify-history-import/0.1",
            },
        )
        time.sleep(max(0, self.last_request + self.interval - time.monotonic()))
        self.last_request = time.monotonic()
        try:
            try:
                response = urlopen(request, timeout=30)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                payload = response.read(4_000_001)
                retry = response.headers.get("Retry-After", "0")
            if len(payload) > 4_000_000:
                raise TransportError("Oversized API response; outcome unknown")
            data = json.loads(payload)
            if isinstance(data, dict) and "error" in data:
                raise APIError(data["error"], int(retry) if retry.isdigit() else 0)
            if status != 200 or not isinstance(data, dict):
                raise TransportError("HTTP failure without a conclusive API response")
            return data
        except APIError:
            raise
        except (URLError, OSError, ValueError, TypeError):
            raise TransportError(
                "Network or malformed API response; outcome unknown"
            ) from None

    def check_account(self, account):
        result = self.call("user.getInfo", signed=True, session=True)
        if result.get("user", {}).get("name", "").casefold() != account.casefold():
            raise ValueError(
                "Authenticated Last.fm account does not match configured account"
            )

    def submit(self, rows):
        params = {}
        for i, row in enumerate(rows):
            for key in ("artist", "track", "album"):
                params[f"{key}[{i}]"] = row[key]
            params[f"timestamp[{i}]"] = str(row["assigned_ts"])
        return self.call("track.scrobble", params, signed=True, session=True)

    def history(self, account, start, end):
        result = Counter()
        page = 1
        expected = None
        while True:
            data = self.call(
                "user.getRecentTracks",
                {
                    "user": account,
                    "from": max(0, start - 1),
                    "to": end + 1,
                    "limit": 200,
                    "page": page,
                },
                signed=bool(self.values.get("LASTFM_SESSION_KEY")),
                session=bool(self.values.get("LASTFM_SESSION_KEY")),
            )
            try:
                recent = data["recenttracks"]
                attrs = recent["@attr"]
                pages, total = int(attrs["totalPages"]), int(attrs["total"])
                if attrs.get("user", account).casefold() != account.casefold():
                    raise ValueError("wrong user")
                if int(attrs.get("page", page)) != page:
                    raise ValueError("wrong page")
                if expected is None:
                    expected = (pages, total)
                if expected != (pages, total):
                    raise ValueError("history changed during pagination")
                tracks = recent.get("track", [])
                if isinstance(tracks, dict):
                    tracks = [tracks]
                for track in tracks:
                    if track.get("@attr", {}).get("nowplaying") == "true":
                        continue
                    ts = int(track["date"]["uts"])
                    artist = track["artist"]["#text"]
                    name = track["name"]
                    album = track.get("album", {}).get("#text", "")
                    result[(ts, artist, name, album)] += 1
                if page >= pages:
                    break
                page += 1
                if page > 100000:
                    raise ValueError("too many pages")
            except (KeyError, TypeError, ValueError):
                raise TransportError(
                    "Incomplete or unstable history response; retry read later"
                ) from None
        if sum(result.values()) != expected[1]:
            raise TransportError("History pagination count mismatch; retry read later")
        return Counter(
            {key: count for key, count in result.items() if start <= key[0] <= end}
        )
