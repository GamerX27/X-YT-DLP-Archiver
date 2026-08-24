# YT-DLP Downloader

A self-hosted web downloader powered by **yt-dlp**. Paste a URL, pick a quality, and a rule-based planner selects the right format and sorts files into tidy folders  automatically. No cloud, no API keys, no tracking.

---

## Features

| | |
|---|---|
| **Web UI** | Dark-themed single-page app with real-time progress over WebSocket |
| **Video downloads** | 720p / 1080p / 1440p / 4K  H.264 preferred at ≤ 1080p, best available above |
| **Audio / YouTube Music** | Detects `music.youtube.com` automatically; downloads best-quality MP3 with cover art, title, artist, and album embedded |
| **Smart codec selection** | H.264 for ≤ 1080p (no re-encode on most devices); AV1/VP9 for 4K |
| **Rule-based format planning** | A pure-Python planner picks the format string per URL from resolution/codec rules |
| **Auto folder sorting** | Files land in `Channel/`, `Channel/Playlist/`, or `Channel/Videos/` |
| **Metadata embedding** | MP4/M4A: mutagen MP4 tags. MP3: mutagen ID3 tags (cover art, title, artist, album) |
| **Playlist archive** | Skips already-downloaded videos across runs using a per-playlist `.yt-dlp-archive` file |
| **Cancel & cleanup** | Cancelling a download kills yt-dlp immediately and deletes partial files |
| **Retry** | One-click retry for failed or cancelled tasks |
| **Network resilience** | Automatically retries up to 10 times on transient network errors (10 s between attempts) |
| **Jellyfin integration** | Pick a library, browse existing subfolders, and trigger a library scan after each download |
| **Playlist reordering** | After a Jellyfin TV playlist download, file mtimes are normalised to match playlist order |
| **Fully self-hosted** | Everything runs in a single container  nothing leaves your network |

---

## Architecture

Everything runs inside **one container**: FastAPI serves the UI and runs yt-dlp, with a small rule-based planner deciding the format string and destination folder from yt-dlp's own metadata.

```
Browser
  │  HTTP / WebSocket
  ▼
┌──────────────────────────────────┐
│         ytdlp-downloader         │  network_mode: host → port 3050
│                                  │
│  FastAPI + yt-dlp                │
│       │  metadata                │
│       ▼                          │
│  FormatPlanner (pure Python)     │
│       │  format string + folder  │
│       ▼                          │
│  yt-dlp executes plan            │
└──────────────────────────────────┘
           │
           ▼
  /media/{Channel}/{...}
```

`network_mode: host` means the container shares the host's network stack. yt-dlp runs identically to running it directly on the host, which avoids YouTube bot-detection that blocks container IPs.

---

## Quick Start

1. **Create your environment file:**
   ```bash
   cp .env.example .env
   # Set the JELLYFIN_* variables if you want the Monitor tab (see below)
   ```

2. **Start:**
   ```bash
   docker compose up -d --build
   ```

3. **Open the UI**:
   ```
   http://localhost:3050
   ```

   Watch progress:
   ```bash
   docker compose logs -f
   ```

   Ready when you see:
   ```
   INFO:     Application startup complete.
   ```

See [`DEPLOY.md`](DEPLOY.md) for the full setup guide, troubleshooting, and day-to-day commands.

---

## Configuration

All configuration is in `.env`. Copy `.env.example` to get started.

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Simultaneous yt-dlp jobs |
| `PUID` / `PGID` | `1000` | User/group ID for ownership of downloaded files |
| `JELLYFIN_URL` | *(optional)* | Full URL of your Jellyfin server, e.g. `http://192.168.1.10:8096` |
| `JELLYFIN_API_KEY` | *(optional)* | API key from Jellyfin → Admin → API Keys |
| `JELLYFIN_MEDIA_PATH` | *(optional)* | Host path that covers all Jellyfin libraries  mounted at the same path inside the container |
| `YOUTUBE_PLAYER_CLIENT` | *(unset  yt-dlp's default client selection)* | Pin yt-dlp to specific YouTube player client(s) for extraction, comma-separated, e.g. `web` or `android,web`. Set this if downloads start failing with `HTTP Error 403: Forbidden` or silently cap at a lower resolution than requested |
| `YTDLP_BRANCH` | `stable` | `stable` re-checks for a new yt-dlp release weekly; `nightly` checks daily and installs pre-release builds use this at your own risk |

An ad-hoc download always saves straight to your browser as a file  there's no server-side media folder to configure. Leave the three `JELLYFIN_*` variables empty to disable Jellyfin integration; the Jellyfin destination picker and the whole Monitor tab won't appear in the UI, since a monitor has no browser to hand a finished file to and needs a real Jellyfin library to write into.

---

## How It Works

1. **Paste a URL.** For `music.youtube.com` the UI switches to Audio mode automatically.
2. **yt-dlp probes the URL** to fetch metadata: title, channel, playlist info, available resolutions and codecs.
3. **`FormatPlanner` computes a plan** from that metadata: a format string, output template, and destination folder, using fixed resolution/codec rules (see [`webui/format_planner.py`](webui/format_planner.py)).
4. **yt-dlp executes the plan.** Progress streams to the browser in real time.
5. **Post-processing:** thumbnail and metadata are embedded via mutagen; files are moved to the final destination.
6. **Jellyfin scan** is triggered automatically if a Jellyfin library was selected.

---

## Output Structure

### Video

```
/mnt/Media/
├── Linus Tech Tips/
│   └── The Best CPU Ever Made [dQw4w9WgXcQ].mp4
│
├── MrBeast/
│   └── Epic Playlist/
│       ├── 01 - First Video [aB1cD2eF3gH].mp4
│       └── 02 - Second Video [iJ4kL5mN6oP].mp4
│
└── SomeChannel/
    └── Videos/
        └── 2024-03-15 - Random Upload [yZ0aB1cD2eF].mp4
```

| URL type | Layout |
|---|---|
| Single video | `Channel/Title [ID].mp4` |
| Playlist | `Channel/Playlist/01 - Title [ID].mp4` |
| Channel | `Channel/Videos/YYYY-MM-DD - Title [ID].mp4` |

### Audio / Music

```
/mnt/Media/Music/
├── Radiohead/
│   ├── OK Computer/
│   │   ├── 01 - Paranoid Android.mp3
│   │   └── 02 - Karma Police.mp3
│   └── Creep.mp3
```

| URL type | Layout |
|---|---|
| Single track | `Artist/Title.mp3` |
| Album (playlist) | `Artist/Album/01 - Title.mp3` |

MP3 files have cover art (APIC), title (TIT2), artist (TPE1/TPE2), and album (TALB) embedded.

### Jellyfin

When a Jellyfin library is selected the folder structure follows the library type:

| Library type | Layout |
|---|---|
| Movies | `Channel/Title (Year).mp4` |
| TV Shows | `Channel/Playlist/01 - Title [ID].mp4` |
| Music | `Artist/Album/01 - Title.mp3` |
| Home Videos / other | `Channel/Playlist/01 - Title.mp4` |

---

## Format Planning Rules

All download planning lives in [`webui/format_planner.py`](webui/format_planner.py)  Powerd by Python. To change format selection, codec preferences, or folder layout, edit the methods there directly (`_format_from_resolution`, `_folder_from_summary`, `_folder_for_jellyfin`). Changes take effect on container restart.
