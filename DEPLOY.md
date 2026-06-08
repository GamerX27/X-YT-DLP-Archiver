# Deployment Guide

## Prerequisites

| Requirement | Version | Check |
|---|---|---|
| Docker Engine | 24+ | `docker --version` |
| Docker Compose plugin | v2 | `docker compose version` |
| Free RAM | 2 GB min | `free -h` |
| Free disk | 5 GB min (models + media) | `df -h` |

---

## 1. First-time setup

```bash
# Clone or copy the project folder, then:
cd ytdlp-ollama-webui

# Create your env file
cp .env.example .env
```

Open `.env` and configure it. **All variables below should be present**:

```env
# ── Required ────────────────────────────────────────────────────────────────
MEDIA_PATH=/mnt/Media          # host path for normal (non-Jellyfin) downloads
OLLAMA_MODEL=llama3.2:1b
WEBUI_PORT=3050
MAX_CONCURRENT_DOWNLOADS=2

# ── Jellyfin integration (optional) ─────────────────────────────────────────
# JELLYFIN_URL         — full URL of your Jellyfin server, e.g. http://192.168.1.10:8096
# JELLYFIN_API_KEY     — API key from Jellyfin dashboard → Admin → API Keys
# JELLYFIN_MEDIA_PATH  — the base media path that Jellyfin uses on this host
#                        It is bind-mounted at the SAME path inside the container
#                        so that library paths returned by the Jellyfin API match
#                        exactly what the container can write to.
#                        Example: if Jellyfin has a "Movies" library at
#                        /mnt/Media/Movies, set this to /mnt/Media.
JELLYFIN_URL=http://192.168.1.10:8096
JELLYFIN_API_KEY=your_api_key_here
JELLYFIN_MEDIA_PATH=/mnt/Media
```

> **Note:** `JELLYFIN_MEDIA_PATH` must be the root path that covers all your
> Jellyfin libraries. The container will be able to write to any sub-path
> beneath it (e.g. `/mnt/Media/Movies`, `/mnt/Media/TV Shows`).
> Leave the three `JELLYFIN_*` variables commented out or empty to disable
> the Jellyfin integration entirely — the toggle won't appear in the UI.

Make sure the media directories exist and are writable:

```bash
mkdir -p /mnt/Media
```

---

## 2. Build and start

```bash
make up
```

Or without make:

```bash
docker compose up -d --build
```

**What happens on first boot:**
1. Docker builds the image (~2-3 min — downloads Ubuntu packages, Python deps, Ollama binary)
2. Container starts; Ollama server initialises
3. `llama3.2:1b` model is pulled (~750 MB — takes 1-5 min depending on connection)
4. FastAPI web server starts on port 3050

Watch it happen live:

```bash
make logs
# or
docker compose logs -f
```

You're ready when you see:

```
[entrypoint] Model 'llama3.2:1b' ready
[entrypoint] Starting web server on :3050...
INFO:     Application startup complete.
```

---

## 3. Open the UI

```
http://localhost:3050
```

If running on a remote server replace `localhost` with the server's IP.

---

## 4. Day-to-day commands

```bash
make status          # container health + resource usage
make logs            # live log stream
make health          # query /api/health and Ollama
make shell           # bash inside the container for debugging
make restart         # restart without rebuilding
make rebuild         # rebuild image from scratch and restart
make update-yt-dlp   # upgrade yt-dlp/yt-dlp-ejs without rebuild
make down            # stop everything
```

---

## 5. Monitoring

### Container health state

```bash
docker compose ps
```

The `STATUS` column shows `healthy`, `unhealthy`, or `starting`.
`starting` is normal for up to 3 minutes on first boot (model pull).

```bash
# More detail on why it's unhealthy:
docker inspect ytdlp-downloader \
  --format '{{json .State.Health}}' | python3 -m json.tool
```

### Live logs

```bash
# All logs
make logs

# Filter to errors only
docker compose logs -f | grep -i "error\|exception\|traceback\|failed"

# Ollama-specific lines
docker compose logs -f | grep -i "ollama\|model\|entrypoint"

# Download activity only
docker compose logs -f | grep -i "download\|task\|moving"
```

### Resource usage

```bash
make status

# Continuous live stats (Ctrl+C to stop)
docker stats ytdlp-downloader
```

### Check what Ollama has loaded

```bash
make health
```

---

## 6. Common problems and fixes

### Container is `unhealthy`

```bash
make logs
```

Look for the last error before the web server stopped responding.

---

### `ERROR: .env not found`

```bash
cp .env.example .env
# Edit MEDIA_PATH, then:
make up
```

---

### Port 3050 already in use

Change `WEBUI_PORT` in `.env`:

```env
WEBUI_PORT=8181
```

Then `make rebuild`.

---

### Model pull fails / times out

The container will exit if Ollama doesn't start within 90 s or the model pull fails.

```bash
# Check the error:
make logs

# Pull manually inside the container:
make shell
ollama pull llama3.2:1b
exit
make restart
```

---

### Ollama is running but LLM responses are slow

`llama3.2:1b` runs on CPU. On a slow machine every analysis call takes 10-30 s.
This is normal — the download still starts immediately after the LLM responds.

To skip LLM analysis for testing, the fallback plan in `llm_agent.py` kicks in
automatically if Ollama times out.

---

### Downloads fail with `Sign in to confirm you're not a bot`

YouTube's bot detection. Fixes in order of effort:

1. **Add cookies** — export your browser's YouTube cookies as `cookies.txt`:
   ```bash
   # Copy cookies into the container
   docker cp cookies.txt ytdlp-downloader:/app/cookies.txt
   ```
   Then add to `.env`:
   ```env
   YTDLP_COOKIES=/app/cookies.txt
   ```
   And update `downloader.py` to pass `cookiefile` in the yt-dlp opts.

2. **Add a sleep** — edit `instructions.md` and add `sleep_interval` to `extra_opts`.

3. **Use a PO Token** — see the [yt-dlp PO Token guide](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#po-token-guide).

---

### Media files not appearing at `MEDIA_PATH`

```bash
# Check the volume mount
docker inspect ytdlp-downloader \
  --format '{{range .Mounts}}{{.Source}} → {{.Destination}}{{"\n"}}{{end}}'

# Check permissions
ls -la /mnt/Media   # replace with your path

# Fix permissions if needed
sudo chown -R $USER:$USER /mnt/Media
```

---

### Disk full — temp downloads building up

```bash
make clean-tmp      # removes staging dirs older than 2 hours
```

---

## 7. Updating

### Update yt-dlp only (no rebuild needed)

```bash
make update-yt-dlp
```

### Update everything (new code changes)

```bash
git pull          # if using git
make rebuild
```

### Change the Ollama model

Edit `.env`:

```env
OLLAMA_MODEL=llama3.1
```

Then `make restart` — the entrypoint will pull the new model on next boot.
Old models stay on disk in the `ollama_models` volume; delete them with:

```bash
make shell
ollama rm llama3.2:1b
exit
```

---

## 8. Logs location on host

Docker stores logs at:

```
/var/lib/docker/containers/<id>/<id>-json.log
```

Rotation is configured to `10 MB × 5 files` (50 MB max) in `docker-compose.yml`.

To read them directly:

```bash
docker compose logs --no-color > debug.log
```
