#!/usr/bin/env bash
# Watchover on one server, entirely in Docker, built from this source tree. No GitHub, no registry, no Python on the host.
#
#   ./watchover.sh install          first start: writes .env (random passwords / keys), builds the image, starts PostgreSQL + dashboard
#   ./watchover.sh update           new source tree copied over this folder: rebuild the image, recreate the containers, keep the data
#   ./watchover.sh start|stop|restart|status|logs [service]
#   ./watchover.sh mcp on|off       our MCP server as a third container (MCP_API_KEY from .env)
#   ./watchover.sh legacy on|off    the former Streamlit interface as an extra container (:8502) while teams move over
#   ./watchover.sh edge on HOST     Caddy in front: https://HOST for the company network (TLS from Caddy's CA, or ./certs/watchover.crt+key)
#   ./watchover.sh edge off|cert    stop it | export the CA root certificate colleagues install once (./certs/watchover-root.crt)
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
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//'; }   # inline comments and trailing blanks dropped
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

profiles() { local p=""; [ "$(env_get WATCHOVER_MCP)" = "1" ] && p="$p --profile mcp"; [ "$(env_get WATCHOVER_LEGACY)" = "1" ] && p="$p --profile legacy"; [ "$(env_get WATCHOVER_EDGE)" = "1" ] && p="$p --profile edge"; echo "$p"; }
edge_url() { local h p; h=$(env_get WATCHOVER_EDGE_HOST); p=$(env_get WATCHOVER_EDGE_HTTPS_PORT || echo 443); [ "$p" = "443" ] && echo "https://$h" || echo "https://$h:$p"; }
up() { $COMPOSE $(profiles) up -d --remove-orphans; }

banner() {
  local ip; ip=$(host_ip)
  cat <<TXT

  Watchover v$(version) is running.
    Dashboard      http://$ip:$(env_get WATCHOVER_UI_PORT || echo 8501)     (same machine: http://localhost:$(env_get WATCHOVER_UI_PORT || echo 8501); API docs at /api/docs)
    Agents post to http://$ip:$(env_get WATCHOVER_LIVE_PORT || echo 8600)/ingest
$( [ "$(env_get WATCHOVER_MCP)" = "1" ] && printf '    MCP server     http://%s:%s/mcp   (Authorization: Bearer <MCP_API_KEY in .env>)\n' "$ip" "$(env_get WATCHOVER_MCP_PORT || echo 8765)" )
$( [ "$(env_get WATCHOVER_LEGACY)" = "1" ] && printf '    Legacy (Streamlit) http://%s:%s\n' "$ip" "$(env_get WATCHOVER_LEGACY_PORT || echo 8502)" )
$( [ "$(env_get WATCHOVER_EDGE)" = "1" ] && printf '    Company network  %s   (agents: %s/ingest; CA root for colleagues: ./watchover.sh edge cert)\n' "$(edge_url)" "$(edge_url)" )
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
  legacy)
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_LEGACY 1; up; say "legacy Streamlit interface on: http://$(host_ip):$(env_get WATCHOVER_LEGACY_PORT || echo 8502)" ;;
      off) $COMPOSE --profile legacy stop legacy; $COMPOSE --profile legacy rm -f legacy; env_set WATCHOVER_LEGACY 0; say "legacy interface off" ;;
      *) die "usage: ./watchover.sh legacy on|off" ;; esac ;;
  edge)
    need_docker; write_env
    [ -z "$(env_get WATCHOVER_UI_PORT)" ] || env_set WATCHOVER_UI_PORT "$(env_get WATCHOVER_UI_PORT | sed 's/^127\.0\.0\.1://')"   # undo the v1.13 localhost binding
    case "${2:-}" in
      on)
        host="${3:-$(env_get WATCHOVER_EDGE_HOST)}"; [ -n "$host" ] || host=$(host_ip)
        env_set WATCHOVER_EDGE 1; env_set WATCHOVER_EDGE_HOST "$host"
        [ -n "$(env_get WATCHOVER_EDGE_TLS)" ] || env_set WATCHOVER_EDGE_TLS internal
        [ -n "$(env_get WATCHOVER_EDGE_HTTPS_PORT)" ] || env_set WATCHOVER_EDGE_HTTPS_PORT 443
        [ -n "$(env_get WATCHOVER_EDGE_HTTP_PORT)" ] || env_set WATCHOVER_EDGE_HTTP_PORT 80
        if [ -f certs/watchover.crt ] && [ -f certs/watchover.key ]; then env_set WATCHOVER_EDGE_TLS "/certs/watchover.crt /certs/watchover.key"; say "company certificate found in ./certs"; fi
        mkdir -p certs
        env_set COMPOSE_FILE "docker-compose.yml:docker-compose.edge.yml"      # plain http port not published while the edge is on (Caddy uses the compose network)
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard edge
        say "edge on: $(edge_url)   (http://$(env_get WATCHOVER_EDGE_HOST) redirects; plain :8501 is no longer published)"
        if [ "$(env_get WATCHOVER_EDGE_TLS)" = "internal" ]; then say "colleagues install the CA root once: ./watchover.sh edge cert  ->  ./certs/watchover-root.crt"; fi ;;
      off)
        $COMPOSE --profile edge stop edge; $COMPOSE --profile edge rm -f edge; env_set WATCHOVER_EDGE 0
        sed -i.bak '/^COMPOSE_FILE=/d' "$ENV_FILE"; rm -f "$ENV_FILE.bak"
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard; say "edge off: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)" ;;
      cert)
        mkdir -p certs; docker cp watchover-edge:/data/caddy/pki/authorities/local/root.crt certs/watchover-root.crt
        say "CA root: ./certs/watchover-root.crt  — macOS: Keychain Access › System › import, set to Always Trust; Windows: certutil -addstore -f Root watchover-root.crt; Linux agents: --ca watchover-root.crt" ;;
      *) die "usage: ./watchover.sh edge on [host] | off | cert" ;; esac ;;
  api)     say "the API now runs inside the dashboard container: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)/api/docs" ;;
  backup)
    need_docker; mkdir -p backups; stamp=$(date +%Y%m%d-%H%M); out="backups/watchover-$stamp.tgz"; tmp=$(mktemp -d)
    $COMPOSE exec -T postgres pg_dump -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)" | gzip > "$tmp/db.sql.gz"
    docker run --rm -v "$($COMPOSE config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes"]["watchover-data"]["name"])' 2>/dev/null || echo watchover_watchover-data)":/data -v "$tmp":/out alpine tar czf /out/data.tgz -C /data .
    cp "$ENV_FILE" "$tmp/env"; tar czf "$out" -C "$tmp" .; rm -rf "$tmp"; say "backup written: $out ($(du -h "$out" | cut -f1))" ;;
  restore)
    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh restore backups/watchover-….tgz"; tmp=$(mktemp -d); tar xzf "$2" -C "$tmp"
    $COMPOSE $(profiles) stop dashboard mcp legacy 2>/dev/null || $COMPOSE stop dashboard
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
