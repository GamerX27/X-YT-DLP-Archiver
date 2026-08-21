# Deno is required by yt-dlp-ejs to solve YouTube's JS challenges.
# Pulling the official prebuilt binary avoids a curl|sh install script.
FROM denoland/deno:bin AS deno

FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --upgrade -r requirements.txt

COPY . .

RUN mkdir -p /app/tmp_downloads

COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3050

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:3050/api/health || exit 1

# docker-compose uses init:true to inject Docker's built-in init process,
# so no tini/dumb-init needed here.
CMD ["/entrypoint.sh"]
