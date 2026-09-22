#!/usr/bin/env bash
# Watchover on one server, entirely in Docker, built from this source tree. No GitHub, no registry, no Python on the host.
#
#   ./watchover.sh install          first start: writes .env (random passwords / keys), builds the image, starts PostgreSQL + dashboard
#   ./watchover.sh update           new source tree copied over this folder: rebuild the image, recreate the containers, keep the data
#   ./watchover.sh start|stop|restart|status|logs [service]
#   ./watchover.sh mcp on|off       our MCP server as a third container (MCP_API_KEY from .env)
#   ./watchover.sh legacy on|off    the former Streamlit interface as an extra container (:8502) while teams move over
#   ./watchover.sh edge on NAME     Caddy in front: https://NAME for the company network (TLS from Caddy's CA, or ./certs/watchover.crt+key);
#                                   NAME may be a DNS name, an IP or a Bonjour name like watchover.local (announced by the Mac itself)
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

# sizing follows the machine: every CPU Docker can see and most of its memory, as caps (not reservations) - 10 on a laptop,
# 32 or 64 on a server. Written on every install/update/start unless WATCHOVER_SIZING=manual keeps your own numbers.
size_env() {
  [ "$(env_get WATCHOVER_SIZING)" = "manual" ] && return 0
  local ncpu mem_b mem_gb llm_mem tr_mem
  ncpu=$(docker info --format '{{.NCPU}}' 2>/dev/null | tr -dc '0-9'); [ -n "$ncpu" ] && [ "$ncpu" -ge 1 ] || ncpu=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
  mem_b=$(docker info --format '{{.MemTotal}}' 2>/dev/null | tr -dc '0-9'); [ -n "$mem_b" ] && [ "$mem_b" -ge 1 ] || mem_b=$((8 * 1024 * 1024 * 1024))
  mem_gb=$((mem_b / 1073741824)); [ "$mem_gb" -ge 2 ] || mem_gb=2
  llm_mem=$((mem_gb * 80 / 100)); [ "$llm_mem" -ge 4 ] || llm_mem=$(( mem_gb < 4 ? mem_gb : 4 ))
  tr_mem=$((mem_gb * 60 / 100));  [ "$tr_mem" -ge 4 ]  || tr_mem=$(( mem_gb < 4 ? mem_gb : 4 ))
  env_set WATCHOVER_SIZING auto
  env_set WATCHOVER_CPUS "$(( ncpu < 4 ? ncpu : 4 ))";     env_set WATCHOVER_MEMORY "$(( mem_gb < 4 ? mem_gb : 4 ))g"
  env_set WATCHOVER_PG_CPUS "$(( ncpu < 2 ? ncpu : 2 ))";  env_set WATCHOVER_PG_MEMORY "$(( mem_gb < 2 ? mem_gb : 2 ))g"
  env_set WATCHOVER_LLM_CPUS "$ncpu";   env_set WATCHOVER_LLM_THREADS "$ncpu";                       env_set WATCHOVER_LLM_MEMORY "${llm_mem}g"
  env_set WATCHOVER_TRAIN_CPUS "$ncpu"; env_set WATCHOVER_TRAIN_THREADS "$(( ncpu > 1 ? ncpu - 1 : 1 ))"; env_set WATCHOVER_TRAIN_MEMORY "${tr_mem}g"
  say "sizing: Docker sees $ncpu CPUs / ${mem_gb} GB -> LLM $ncpu cpu ${llm_mem}g, trainer $ncpu cpu ${tr_mem}g, dashboard $(( ncpu < 4 ? ncpu : 4 )) cpu  (WATCHOVER_SIZING=manual in .env keeps your own)"
}

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
  size_env
  chmod 600 "$ENV_FILE"
  mkdir -p datasets backups
}

build() {
  say "building image watchover:$(version) from this source tree (first time: a few minutes, needs internet for PyPI)"
  $COMPOSE build --build-arg GIT_REV="$(git_rev)" --build-arg VERSION="$(version)" dashboard
  [ "$(env_get WATCHOVER_LLM)" = "1" ] && $COMPOSE --profile llm build trainer || true
}

profiles() { local p=""; [ "$(env_get WATCHOVER_MCP)" = "1" ] && p="$p --profile mcp"; [ "$(env_get WATCHOVER_LEGACY)" = "1" ] && p="$p --profile legacy"; [ "$(env_get WATCHOVER_EDGE)" = "1" ] && p="$p --profile edge"; [ "$(env_get WATCHOVER_LLM)" = "1" ] && p="$p --profile llm"; echo "$p"; }
# a *.local name on macOS: announce it on the LAN with Bonjour (dns-sd proxy record) so colleagues' Macs, iPhones and Windows 10+ resolve it
mdns_start() {
  mdns_stop
  command -v dns-sd >/dev/null 2>&1 || return 0
  local n ip; ip=$(host_ip)
  for n in $(echo "$1" | tr ',' ' '); do
    case "$n" in *.local)
      nohup dns-sd -P "$n" _https._tcp local 443 "$n" "$ip" >/dev/null 2>&1 &
      echo $! >> .edge-mdns.pid
      say "Bonjour: $n -> $ip announced (dns-sd, stops with edge off or reboot; run 'edge on' again after a reboot)" ;;
    esac
  done
}
mdns_stop() { if [ -f .edge-mdns.pid ]; then while read -r pid; do kill "$pid" 2>/dev/null || true; done < .edge-mdns.pid; rm -f .edge-mdns.pid; fi; }
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
$( [ "$(env_get WATCHOVER_LLM)" = "1" ] && printf '    Local LLM        Ollama on the compose network (models: %s); manage with ./watchover.sh llm\n' "$(env_get WATCHOVER_LLM_MODELS || echo 'qwen2.5:7b-instruct qwen2.5:3b-instruct bge-m3')" )
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
  llm)
    need_docker; write_env
    models="$(env_get WATCHOVER_LLM_MODELS)"; models="${models:-qwen2.5:7b-instruct qwen2.5:3b-instruct bge-m3}"
    case "${2:-}" in
      on)   # starts the bundled CPU Ollama; never touches an LLM connection you already configured (LLM page / .env)
        env_set WATCHOVER_LLM 1
        if [ -z "$(env_get LLM_BASE_URL)" ]; then
          env_set LLM_PROVIDER ollama; env_set LLM_BASE_URL "http://ollama:11434"
          [ -n "$(env_get LLM_MODEL)" ] || env_set LLM_MODEL "qwen2.5:7b-instruct"
          [ -n "$(env_get LLM_EMBED_MODEL)" ] || env_set LLM_EMBED_MODEL "bge-m3"
          say "no LLM connection was configured: the dashboard will use the bundled Ollama (http://ollama:11434)"
        else
          say "existing LLM connection kept ($(env_get LLM_BASE_URL)); the bundled Ollama is an extra option at http://ollama:11434 (LLM › Connection)"
        fi
        say "building the trainer image (torch CPU, transformers, peft; first time a few minutes)"; $COMPOSE --profile llm build trainer
        up
        say "waiting for the Ollama service to become healthy"
        for _ in $(seq 1 60); do $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^ollama healthy' && break; sleep 3; done
        for m in $models; do say "pulling $m (CPU inference; the 7B model is a few GB, first time is slow)"; $COMPOSE exec -T ollama ollama pull "$m" || say "could not pull $m (check internet / disk)"; done
        $COMPOSE exec -T ollama ollama list || true
        say "bundled Ollama on (chat/review: qwen2.5:7b, fast: 3b, embeddings: bge-m3). Models to download: .env WATCHOVER_LLM_MODELS" ;;
      use)   # switch the dashboard's chat model (any model your configured Ollama serves, e.g. watchover-ops); nothing else changes
        [ -n "${3:-}" ] || die "usage: ./watchover.sh llm use <model>"
        env_set LLM_MODEL "$3"; $COMPOSE $(profiles) up -d --no-deps dashboard; say "LLM_MODEL=$3 (switch back any time with llm use <model> or in LLM › Connection)" ;;
      pull)
        [ -n "${3:-}" ] || die "usage: ./watchover.sh llm pull <model>"
        $COMPOSE exec -T ollama ollama pull "$3" ;;
      list) $COMPOSE exec -T ollama ollama list ;;
      export)   # the company's memory as chat-format JSONL for scripts/train_lora.py
        out="${3:-training.jsonl}"; $COMPOSE exec -T dashboard python -m watchover.training export --out /tmp/training.jsonl --lang "$(env_get WATCHOVER_LANG || echo tr)" \
          && docker cp watchover:/tmp/training.jsonl "$out" && say "training data -> $out" ;;
      import)   # a trained adapter folder (Modelfile + adapter/) -> the watchover-ops model inside the Ollama container
        [ -d "${3:-}" ] && [ -f "$3/Modelfile" ] || die "usage: ./watchover.sh llm import <folder with Modelfile and adapter/>"
        name="${4:-watchover-ops}"
        docker cp "$3" watchover-ollama:/tmp/watchover-ops && $COMPOSE exec -T ollama sh -c "cd /tmp/watchover-ops && ollama create $name -f Modelfile" \
          && say "model $name created next to your existing models (nothing switched). Try it: ./watchover.sh llm use $name  ·  quality gate: python scripts/train_lora.py --gate --model $name" ;;
      off)
        $COMPOSE --profile llm stop trainer ollama; $COMPOSE --profile llm rm -f trainer ollama; env_set WATCHOVER_LLM 0
        [ "$(env_get LLM_BASE_URL)" = "http://ollama:11434" ] && env_set LLM_BASE_URL ""      # only undo what 'llm on' set itself
        say "bundled Ollama off (downloaded models kept in the watchover-ollama volume; other LLM connections untouched)" ;;
      *) die "usage: ./watchover.sh llm on|off|use <model>|pull <model>|list|export [file]|import <dir> [name]" ;; esac ;;
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
        hosts="${3:-$(env_get WATCHOVER_EDGE_HOSTS)}"; hosts="${hosts:-$(env_get WATCHOVER_EDGE_HOST)}"; [ -n "$hosts" ] || hosts=$(host_ip)
        hosts=$(echo "$hosts" | tr ' ' ',' | tr -s ',' | sed 's/^,//; s/,$//')
        host="${hosts%%,*}"
        case ",$hosts," in *,"$(host_ip)",*) ;; *) hosts="$hosts,$(host_ip)" ;; esac      # the IP is always on the certificate too
        env_set WATCHOVER_EDGE 1; env_set WATCHOVER_EDGE_HOST "$host"; env_set WATCHOVER_EDGE_HOSTS "$(echo "$hosts" | sed 's/,/, /g')"
        mdns_start "$hosts"
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
        $COMPOSE --profile edge stop edge; $COMPOSE --profile edge rm -f edge; env_set WATCHOVER_EDGE 0; mdns_stop
        sed -i.bak '/^COMPOSE_FILE=/d' "$ENV_FILE"; rm -f "$ENV_FILE.bak"
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard; say "edge off: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)" ;;
      cert)
        mkdir -p certs; docker cp watchover-edge:/data/caddy/pki/authorities/local/root.crt certs/watchover-root.crt
        say "CA root: ./certs/watchover-root.crt  — macOS: Keychain Access › System › import, set to Always Trust; Windows: certutil -addstore -f Root watchover-root.crt; Linux agents: --ca watchover-root.crt" ;;
      *) die "usage: ./watchover.sh edge on [name[,name2,ip]] | off | cert     e.g.  edge on watchover.local" ;; esac ;;
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
