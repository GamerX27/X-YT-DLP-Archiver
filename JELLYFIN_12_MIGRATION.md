# Jellyfin 12.0 migration notes

Research done 2026-09-11, ahead of migrating the Jellyfin server this app talks
to from 10.x to 12.0. **Conclusion: no code changes are required.** This file
exists so that conclusion (and how it was verified) doesn't have to be
re-derived at migration time.

## Where this app touches the Jellyfin API

Only two call sites, both in `backend/jellyfin.py`:

- `_headers()` — sends `Authorization: MediaBrowser Token="<JELLYFIN_API_KEY>"`
- `get_libraries()` — `GET /Library/VirtualFolders`
- `refresh_library()` — `POST /Library/Refresh`

`backend/routes/jellyfin.py` just exposes these over `/api/jellyfin/status`
and `/api/jellyfin/libraries`; no direct Jellyfin calls there.

## What changed in 12.0 that could have broken this

Jellyfin 12.0's headline breaking change is removing **legacy authorization**:
`EnableLegacyAuthorization` defaults to `false` now (was already opt-out since
10.11 via [PR #13306](https://github.com/jellyfin/jellyfin/pull/13306), forced
off for everyone in 12.0 via
[PR #15559](https://github.com/jellyfin/jellyfin/pull/15559)). That removes:

- Headers: `X-Emby-Token`, `X-MediaBrowser-Token`, `X-Emby-Authorization`
- Query param: `api_key=` (lowercase spelling, deprecated since the `ApiKey`
  param was added in 10.8)
- URL prefixes: `/emby/*` and `/mediabrowser/*`

**This app was never affected** — `_headers()` already uses the modern,
non-deprecated scheme: `Authorization: MediaBrowser Token="..."` against a
plain `{JELLYFIN_URL}/...` path with no `/emby` or `/mediabrowser` prefix.
Confirmed against the current-vs-legacy breakdown in
[nielsvanvelzen's Jellyfin API auth gist](https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f)
(he's the Jellyfin dev who wrote the deprecation PRs).

## Endpoint compatibility, verified against v12.0 source

Pulled straight from the `jellyfin/jellyfin` repo at the `v12.0` tag rather
than trusting summarized docs (see caveat below):

- **`POST /Library/Refresh`** — `Jellyfin.Api/Controllers/LibraryController.cs`,
  `RefreshLibrary()`. No request body, still returns `204 No Content`, still
  just calls `_libraryManager.ValidateMediaLibrary(...)`. Unchanged from what
  `refresh_library()` expects.
- **`GET /Library/VirtualFolders`** —
  `Jellyfin.Api/Controllers/LibraryStructureController.cs`,
  `GetVirtualFolders()` → `ActionResult<IEnumerable<VirtualFolderInfo>>`.
  Still a bare JSON array (not paginated/wrapped). `VirtualFolderInfo`
  (`MediaBrowser.Model/Entities/VirtualFolderInfo.cs`) still has `Name`,
  `Locations` (`string[]`), `CollectionType`, `ItemId` — exactly the fields
  `get_libraries()` reads.
- **`CollectionType` enum** (`Jellyfin.Data/Enums/CollectionType.cs`) — the
  values this app maps in `_TYPE_LABEL`
  (`movies`, `tvshows`, `music`, `musicvideos`, `homevideos`, `books`,
  `photos`) are all still present with the same lowercase serialization.

### Caveat: don't trust the mintlify-hosted "Jellyfin Server" API docs

An unofficial mintlify mirror
(`jellyfin-jellyfin.mintlify.app/api/media/library`) describes
`GET /Library/VirtualFolders` as returning a paginated
`{ Items: [...], TotalRecordCount }` wrapper with fields renamed to `Id` and
`Path`. **That's wrong** — it doesn't match the actual 12.0 controller/model
source (see above). If this comes up again during the real migration, go to
the source (`gh api repos/jellyfin/jellyfin/...` at the target tag) rather
than that site.

## Other 12.0 breaking changes (checked, not applicable to this app)

These are real 12.0 changes but don't touch anything this app calls:

- Removed endpoints: `POST /Users/{userId}/EasyPassword`,
  `GET /Items/{itemId}/CriticReviews`, `GET /Environment/NetworkShares`,
  `POST /System/MediaEncoder/Path`, `GET /LiveTv/Recordings/Groups/{groupId}`,
  `GET /QuickConnect/Initiate` (must use POST now)
- `GetItems` behavior/async changes — not called by this app
- Global subtitle config removed (now per-library) — not touched
- Server now targets .NET 10 — only matters for Jellyfin *plugins*, this app
  is an external HTTP client
- DB schema migration is one-way/non-reversible, FFmpeg 8.1 + newer
  NVIDIA/Intel driver requirements — operational concerns for the Jellyfin
  server itself, not this app

## What to actually do at migration time

Given the above, migrating the Jellyfin server to 12.0 should need **zero**
changes here. As a sanity check once it's done:

1. `curl http://localhost:3050/api/jellyfin/libraries` — confirms
   `GET /Library/VirtualFolders` still authenticates and parses correctly.
2. Run one real download to a Jellyfin library and confirm the post-download
   scan fires (`POST /Library/Refresh` in `backend/jellyfin.py`'s
   `refresh_library()`, called from `process_task()` in
   `backend/pipeline.py`) and the file actually shows up in Jellyfin.
3. If `JELLYFIN_API_KEY` was created a long time ago, no action needed —
   API keys aren't affected by the legacy-authorization removal (only the
   header/query-param *format* was deprecated, not API keys themselves).

If any of the above actually breaks, the first thing to check is whether the
`JELLYFIN_URL` in `.env` accidentally includes an `/emby` or `/mediabrowser`
suffix, or whether `EnableLegacyAuthorization` was ever relied on some other
way — otherwise re-diff `backend/jellyfin.py` against the then-current
`LibraryStructureController.cs` / `LibraryController.cs` in the target
Jellyfin release tag.

## Sources

- https://jellyfin.org/posts/jellyfin-release-12.0/
- https://github.com/jellyfin/jellyfin/releases/tag/v12.0
- https://github.com/jellyfin/jellyfin/pull/15559
- https://github.com/jellyfin/jellyfin/pull/13306
- https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f
- `jellyfin/jellyfin` repo source at the `v12.0` tag (`LibraryController.cs`,
  `LibraryStructureController.cs`, `VirtualFolderInfo.cs`,
  `CollectionType.cs`)
