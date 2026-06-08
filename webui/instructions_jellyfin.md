# Jellyfin Playlist Episode Date Ordering

You correct file modification dates so Jellyfin displays playlist episodes in the right order.

## Problem

YouTube uploaders sometimes upload playlist episodes out of chronological order. When Jellyfin sorts by file modification date, episodes appear out of sequence. You fix this.

## Input

A JSON array, **pre-sorted by playlist index ascending**:

```json
[
  {
    "filename": "01 - Episode Title.mp4",
    "playlist_index": 1,
    "upload_date": "YYYY-MM-DD"
  }
]
```

## Rules

1. **Correct order = playlist index order** — the number prefix in the filename defines the intended sequence.
2. **Preserve dates when already in order** — if upload dates are already strictly ascending by playlist index, output them unchanged.
3. **Normalize out-of-order dates** — for each episode (in playlist order), if its upload date is not strictly after the previous episode's assigned date, set it to `previous_date + 1 day`.
4. **Never assign a future date** — dates must be ≤ today.
5. Keep the exact `filename` string — do not change it.

## Output

Return a JSON object with an `episodes` key. No prose, no markdown fences:

```json
{
  "episodes": [
    {"filename": "01 - Episode Title.mp4", "new_date": "YYYY-MM-DD"},
    ...
  ]
}
```

## Examples

**Already ordered → keep as-is:**

Input:
```json
[
  {"filename": "01 - Pilot.mp4",       "playlist_index": 1, "upload_date": "2023-01-05"},
  {"filename": "02 - Chapter Two.mp4", "playlist_index": 2, "upload_date": "2023-01-12"}
]
```
Output:
```json
{
  "episodes": [
    {"filename": "01 - Pilot.mp4",       "new_date": "2023-01-05"},
    {"filename": "02 - Chapter Two.mp4", "new_date": "2023-01-12"}
  ]
}
```

**Out of order → normalize:**

Input:
```json
[
  {"filename": "01 - Intro.mp4",  "playlist_index": 1, "upload_date": "2024-06-12"},
  {"filename": "02 - Part 2.mp4", "playlist_index": 2, "upload_date": "2023-06-01"},
  {"filename": "03 - Finale.mp4", "playlist_index": 3, "upload_date": "2024-07-01"}
]
```
Output (ep2 date was before ep1, so it becomes ep1+1 day; ep3 is already after corrected ep2):
```json
{
  "episodes": [
    {"filename": "01 - Intro.mp4",  "new_date": "2024-06-12"},
    {"filename": "02 - Part 2.mp4", "new_date": "2024-06-13"},
    {"filename": "03 - Finale.mp4", "new_date": "2024-07-01"}
  ]
}
```

---

## Music Libraries (Jellyfin `music` type)

When `jellyfin_library_type` is `"music"` (set automatically for `music.youtube.com` URLs), the backend uses a different folder structure optimised for Jellyfin's music scanner:

| Content | Folder | Output template |
|---|---|---|
| Single track | `Artist/` | `%(title)s.%(ext)s` |
| Album (playlist) | `Artist/AlbumName/` | `%(playlist_index)02d - %(title)s.%(ext)s` |

- **Artist** is sourced from the `artist` metadata field (YouTube Music provides this), falling back to the channel name.
- **Album** is the playlist title.
- Files are `.mp3` (best quality VBR, converted by FFmpeg).
- Cover art, artist, album, and title tags are embedded as ID3 tags.

The date-ordering step (correcting mtimes) is **skipped** for music libraries — Jellyfin sorts music by track number, not date.
```
