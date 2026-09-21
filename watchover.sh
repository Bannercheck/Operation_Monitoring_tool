#!/usr/bin/env bash
# Watchover on one server, entirely in Docker, built from this source tree. No GitHub, no registry, no Python on the host.
#
#   ./watchover.sh install          first start: writes .env (random passwords / keys), builds the image, starts PostgreSQL + dashboard
#   ./watchover.sh update           new source tree copied over this folder: rebuild the image, recreate the containers, keep the data
#   ./watchover.sh start|stop|restart|status|logs [service]
#   ./watchover.sh mcp on|off       our MCP server as a third container (MCP_API_KEY from .env)
#   ./watchover.sh api on|off       the HTTP API + new React interface (:8000, OpenAPI docs at /api/docs)
#   ./watchover.sh backup           PostgreSQL dump + data volume -> ./backups/watchover-YYYYMMDD-HHMM.tgz
#   ./watchover.sh restore FILE     bring a backup back (containers are stopped meanwhile)
#   ./watchover.sh user ...         account management, e.g.  user list | user add ops@firma.com --admin | user password EMAIL | user unlock EMAIL
#   ./watchover.sh migrate          copy old SQLite files (knowledge.db / actions.db / playbook.db in ./data or the volume) into PostgreSQL
#   ./watchover.sh save|load FILE   export the built images to a .tgz (build on a machine with internet), import them on an air-gapped server
#   ./watchover.sh shell            a shell inside the dashboard container
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=.env
COMPOSE="docker compose"

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
need_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Linux:  curl -fsSL https://get.docker.com | sudo sh   (then: sudo usermod -aG docker \$USER and sign in again). macOS / Windows: Docker Desktop."
  docker info >/dev/null 2>&1 || die "Docker is installed but not reachable (daemon stopped, or your user is not in the 'docker' group)."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing (docker compose version)."
}
rand() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"; else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi; }
version() { sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1; }
git_rev() { (git rev-parse --short HEAD 2>/dev/null) || echo "-"; }
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -1; }
env_set() { if grep -q "^$1=" "$ENV_FILE" 2>/dev/null; then sed -i.bak "s|^$1=.*|$1=$2|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"; else printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; fi; }
host_ip() { local ip; ip=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$ip" ] || ip=$(ipconfig getifaddr en0 2>/dev/null); [ -n "$ip" ] || ip=$(ipconfig getifaddr en1 2>/dev/null); echo "${ip:-localhost}"; }   # Linux, then macOS (Wi-Fi / Ethernet)

write_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp .env.docker.example "$ENV_FILE"
    say ".env written from .env.docker.example"
  fi
  env_set WATCHOVER_IMAGE watchover
  env_set WATCHOVER_TAG "$(version)"
  env_set GIT_REV "$(git_rev)"
  [ -n "$(env_get POSTGRES_PASSWORD)" ] && [ "$(env_get POSTGRES_PASSWORD)" != "change-me" ] || { env_set POSTGRES_PASSWORD "$(rand 24)"; say "POSTGRES_PASSWORD generated"; }
  [ -n "$(env_get MCP_API_KEY)" ] || { sed -i.bak 's/^#MCP_API_KEY=.*$//' "$ENV_FILE" 2>/dev/null; rm -f "$ENV_FILE.bak"; env_set MCP_API_KEY "$(rand 24)"; say "MCP_API_KEY generated"; }
  chmod 600 "$ENV_FILE"
  mkdir -p datasets backups
}

build() {
  say "building image watchover:$(version) from this source tree (first time: a few minutes, needs internet for PyPI)"
  $COMPOSE build --build-arg GIT_REV="$(git_rev)" --build-arg VERSION="$(version)" dashboard
}

profiles() { local p=""; [ "$(env_get WATCHOVER_MCP)" = "1" ] && p="$p --profile mcp"; [ "$(env_get WATCHOVER_API)" = "1" ] && p="$p --profile api"; echo "$p"; }
up() { $COMPOSE $(profiles) up -d --remove-orphans; }

banner() {
  local ip; ip=$(host_ip)
  cat <<TXT

  Watchover v$(version) is running.
    Dashboard      http://$ip:$(env_get WATCHOVER_UI_PORT || echo 8501)     (same machine: http://localhost:$(env_get WATCHOVER_UI_PORT || echo 8501))
    Agents post to http://$ip:$(env_get WATCHOVER_LIVE_PORT || echo 8600)/ingest
$( [ "$(env_get WATCHOVER_MCP)" = "1" ] && printf '    MCP server     http://%s:%s/mcp   (Authorization: Bearer <MCP_API_KEY in .env>)\n' "$ip" "$(env_get WATCHOVER_MCP_PORT || echo 8765)" )
$( [ "$(env_get WATCHOVER_API)" = "1" ] && printf '    New interface  http://%s:%s   (API docs: /api/docs)\n' "$ip" "$(env_get WATCHOVER_API_PORT || echo 8000)" )
    First sign-in: admin@watchover.local with the initial password you were given; a new password is required at once.
    Open the firewall for $(env_get WATCHOVER_UI_PORT || echo 8501) and $(env_get WATCHOVER_LIVE_PORT || echo 8600) if the server has one (ufw allow 8501,8600/tcp).
    Status: ./watchover.sh status     Logs: ./watchover.sh logs     Backup: ./watchover.sh backup

TXT
}

case "${1:-}" in
  install)
    need_docker; write_env; build; up
    say "waiting for the containers to become healthy"
    for _ in $(seq 1 60); do
      if $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^dashboard healthy'; then break; fi; sleep 2
    done
    $COMPOSE ps; banner ;;
  update)
    need_docker; write_env; build; up; docker image prune -f >/dev/null; say "updated to v$(version); data volumes untouched"; $COMPOSE ps ;;
  start)   need_docker; write_env; up; $COMPOSE ps ;;
  stop)    need_docker; $COMPOSE $(profiles) stop ;;
  restart) need_docker; $COMPOSE $(profiles) restart "${2:-}" ;;
  status)  need_docker; $COMPOSE $(profiles) ps; docker stats --no-stream $($COMPOSE $(profiles) ps -q) 2>/dev/null || true ;;
  logs)    need_docker; $COMPOSE $(profiles) logs -f --tail 200 "${2:-dashboard}" ;;
  mcp)
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_MCP 1; up; say "MCP server on: http://$(host_ip):$(env_get WATCHOVER_MCP_PORT || echo 8765)/mcp  key: $(env_get MCP_API_KEY)" ;;
      off) $COMPOSE --profile mcp stop mcp; $COMPOSE --profile mcp rm -f mcp; env_set WATCHOVER_MCP 0; say "MCP server off" ;;
      *) die "usage: ./watchover.sh mcp on|off" ;; esac ;;
  api)
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_API 1; up; say "new interface + API on: http://$(host_ip):$(env_get WATCHOVER_API_PORT || echo 8000)  (docs: /api/docs)" ;;
      off) $COMPOSE --profile api stop api; $COMPOSE --profile api rm -f api; env_set WATCHOVER_API 0; say "HTTP API off" ;;
      *) die "usage: ./watchover.sh api on|off" ;; esac ;;
  backup)
    need_docker; mkdir -p backups; stamp=$(date +%Y%m%d-%H%M); out="backups/watchover-$stamp.tgz"; tmp=$(mktemp -d)
    $COMPOSE exec -T postgres pg_dump -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)" | gzip > "$tmp/db.sql.gz"
    docker run --rm -v "$($COMPOSE config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes"]["watchover-data"]["name"])' 2>/dev/null || echo watchover_watchover-data)":/data -v "$tmp":/out alpine tar czf /out/data.tgz -C /data .
    cp "$ENV_FILE" "$tmp/env"; tar czf "$out" -C "$tmp" .; rm -rf "$tmp"; say "backup written: $out ($(du -h "$out" | cut -f1))" ;;
  restore)
    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh restore backups/watchover-….tgz"; tmp=$(mktemp -d); tar xzf "$2" -C "$tmp"
    $COMPOSE $(profiles) stop dashboard mcp api 2>/dev/null || $COMPOSE stop dashboard
    $COMPOSE up -d postgres; sleep 5
    say "restoring the database"; $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" "$(env_get POSTGRES_DB || echo watchover)"
    gunzip -c "$tmp/db.sql.gz" | $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)"
    say "restoring settings, secrets and snapshots"; docker run --rm -v "watchover_watchover-data":/data -v "$tmp":/in alpine sh -c 'rm -rf /data/* && tar xzf /in/data.tgz -C /data'
    rm -rf "$tmp"; up; say "restored from $2"; $COMPOSE ps ;;
  user)    need_docker; shift; $COMPOSE exec dashboard python -m watchover.useradmin "$@" ;;
  migrate) need_docker; shift; [ -d data ] && docker cp data/. watchover:/data/ 2>/dev/null || true; $COMPOSE exec dashboard python -m watchover.migrate --source /data "$@" ;;
  save)    need_docker; out="${2:-watchover-images-$(version).tgz}"; docker save "watchover:$(version)" postgres:16-alpine | gzip > "$out"; say "images saved to $out (copy it with this folder to the air-gapped server, then: ./watchover.sh load $out && ./watchover.sh install)" ;;
  load)    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh load watchover-images-X.Y.tgz"; gunzip -c "$2" | docker load; say "images loaded; ./watchover.sh install skips the build when the image tag matches" ;;
  shell)   need_docker; $COMPOSE exec dashboard bash ;;
  version) echo "Watchover v$(version) ($(git_rev))" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
