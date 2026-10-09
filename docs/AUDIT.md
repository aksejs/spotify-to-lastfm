# Upstream audit — 2026-10-09

Audited upstream revision: `ff836b3` (2026-03-16), from [joinemm/spotify-to-lastfm](https://github.com/joinemm/spotify-to-lastfm). GitHub reported the repository unarchived and last pushed on March 16, 2026. It received recent Spotify-format and Python-version updates; this is a small script-based project, not a durable migration service.

- `convert.py` reads Spotify metadata, filters short events, sorts by timestamp, and writes daily JSON chunks. Its current discovery patterns omit `endsong_*.json`. It retains skipped plays above its threshold, excludes missing albums, and optionally drops events after a cutoff. Original records and exclusion reasons are not durably stored. Naive datetime parsing can interpret UTC-looking strings in local time.
- `scrobble.py` assigns timestamps starting 13 days ago with one-minute spacing, in batches of 50. Re-running creates new timestamps and resubmits the input. There is no database, attempt ledger, checkpoint, rolling budget, reconciliation, or automatic schedule.
- It prints accepted/ignored counts and ignored messages, but does not persist per-event outcomes. It raises HTTP errors before interpreting their API bodies. Errors do not drive a durable retry policy; singleton response handling is also fragile.
- Authentication uses `auth.getMobileSession` with username/password. Although HTTPS is used, the request passes parameters in the URL query rather than the POST body, exposing sensitive values to URL logging. The new CLI uses browser token/session authentication and verifies the account before writes.
- The upstream README recommends 2,600/day and describes an approximate 2,800 limit. Those assumptions are unsuitable for this migration. The new budgets are configurable, with a conservative default of 500.
- `pip-audit` reported advisories affecting upstream's pinned aiohttp, multidict, and idna versions on the audit date. Counts contained duplicated advisory records and do not establish exploitability in these scripts. The supported CLI uses standard-library transport and PyYAML, with locked dependencies; that dependency audit reported no known vulnerabilities at the time checked.

The fork preserves the repository, license, original scripts, and copies of the upstream README/dependency list. Spotify field mapping, chronological ordering, and Last.fm signature ordering are retained as concepts. The stateful delivery layer is new because upstream had no corresponding tested mechanism to reuse. No upstream automated test suite was present.

[Scrubbler 2](https://github.com/Scrubbler-Dev/Scrubbler-2) was reviewed as an alternative: its current README describes a .NET 10/Uno desktop host with separately installed account and scrobble plugins. It is not a drop-in headless Python implementation. No GPL-licensed implementation code was copied. This was an architecture/README comparison, not an exhaustive audit of its plugins.

Primary protocol references:

- [track.scrobble](https://www.last.fm/api/show/track.scrobble): batch size, signing, timestamps, and ignored codes.
- [user.getRecentTracks](https://www.last.fm/api/show/user.getRecentTracks): bounded, paginated history reads.
- [Desktop authentication](https://www.last.fm/api/desktopauth): token, browser approval, and session exchange.
- [Scrobbling protocol](https://www.last.fm/api/scrobbling): response inspection, listening criteria, and error handling.

The linked 2022 community Google document could not be retrieved during this audit and was not used as authoritative protocol guidance. The moderator permission described by the user is treated as supplied context, not independently verified correspondence.

Current limitations: no exactly-once guarantee, no complete historical matching across metadata differences, no unattended service installed, and no full migration/recovery trial yet. The approximately 14-day acceptance window is treated conservatively; the code stops expired assignments instead of silently changing uncertain submissions.
