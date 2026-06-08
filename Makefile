# ──────────────────────────────────────────────────────────────
#  YouTube Downloader — management commands
#  Usage: make <target>
# ──────────────────────────────────────────────────────────────

CONTAINER  := ytdlp-downloader
COMPOSE    := docker compose

.PHONY: help up down restart rebuild logs status health shell \
        update-yt-dlp clean-tmp purge

# ── Default ───────────────────────────────────────────────────

help:
	@echo ""
	@echo "  make up              Build and start in background"
	@echo "  make down            Stop and remove containers"
	@echo "  make restart         Stop then start (no rebuild)"
	@echo "  make rebuild         Full stop, rebuild image, start"
	@echo ""
	@echo "  make logs            Follow live logs (Ctrl+C to stop)"
	@echo "  make status          Show container status + health"
	@echo "  make health          Query both service health endpoints"
	@echo "  make shell           Open a bash shell inside container"
	@echo ""
	@echo "  make update-yt-dlp   Upgrade yt-dlp inside the running container"
	@echo "  make clean-tmp       Remove orphaned temp download files"
	@echo "  make purge           Remove containers AND volumes (loses models!)"
	@echo ""

# ── Lifecycle ─────────────────────────────────────────────────

up:
	@[ -f .env ] || { echo "ERROR: .env not found — copy .env.example first"; exit 1; }
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

rebuild:
	$(COMPOSE) down
	$(COMPOSE) up -d --build

# ── Observability ─────────────────────────────────────────────

logs:
	$(COMPOSE) logs -f --tail=200

status:
	@echo "── Container ──────────────────────────────────────────"
	$(COMPOSE) ps
	@echo ""
	@echo "── Resource usage ─────────────────────────────────────"
	@docker stats $(CONTAINER) --no-stream --format \
	  "CPU: {{.CPUPerc}}   RAM: {{.MemUsage}}   Net: {{.NetIO}}   Disk: {{.BlockIO}}"

health:
	@echo "── Web API (/api/health) ──────────────────────────────"
	@docker exec $(CONTAINER) curl -sf http://localhost:3050/api/health \
	  | python3 -m json.tool || echo "  Web API not reachable"
	@echo ""
	@echo "── Ollama (/api/tags) ─────────────────────────────────"
	@docker exec $(CONTAINER) curl -sf http://localhost:11434/api/tags \
	  | python3 -m json.tool || echo "  Ollama not reachable"

shell:
	docker exec -it $(CONTAINER) bash

# ── Maintenance ───────────────────────────────────────────────

update-yt-dlp:
	@echo "Upgrading yt-dlp inside the running container..."
	docker exec $(CONTAINER) /app/venv/bin/pip install --upgrade yt-dlp yt-dlp-ejs
	@echo "Restarting web server to pick up the new version..."
	$(COMPOSE) restart

clean-tmp:
	@echo "Removing orphaned temp download files..."
	docker exec $(CONTAINER) find /app/tmp_downloads -mindepth 1 -maxdepth 1 -type d \
	  -mmin +120 -exec rm -rf {} + 2>/dev/null && echo "Done" || echo "Nothing to clean"

purge:
	@echo "WARNING: this will delete all Docker volumes including Ollama models."
	@read -p "Type YES to confirm: " c && [ "$$c" = "YES" ]
	$(COMPOSE) down -v
