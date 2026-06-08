FROM archlinux:latest

# Full system update then install everything from the official Arch repos:
#   yt-dlp        — https://archlinux.org/packages/extra/any/yt-dlp/
#   yt-dlp-ejs    — https://archlinux.org/packages/extra/any/yt-dlp-ejs/
#   ollama        — https://archlinux.org/packages/extra/x86_64/ollama/
#   ffmpeg        — video merging / re-encoding
#   python        — runtime for the web app
#   python-pip    — needed to install web framework deps into the venv
RUN pacman -Syu --noconfirm && \
    pacman -S --noconfirm \
        yt-dlp \
        yt-dlp-ejs \
        ffmpeg \
        ollama \
        python \
        python-pip \
        python-jinja \
        python-aiofiles \
        curl \
    && pacman -Scc --noconfirm

WORKDIR /app

# Venv with --system-site-packages so the pacman-installed yt_dlp module
# is importable from inside the venv without duplicating it via pip.
RUN python -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/tmp_downloads

COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3050

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -sf http://localhost:3050/api/health || exit 1

# docker-compose uses init:true to inject Docker's built-in init process,
# so no tini/dumb-init needed here.
CMD ["/entrypoint.sh"]
