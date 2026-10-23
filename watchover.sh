#!/usr/bin/env bash
# Watchover on one server, entirely in Docker, built from this source tree. No GitHub, no registry, no Python on the host.
#
#   ./watchover.sh install          first start: writes .env (random passwords / keys), builds the image, starts PostgreSQL + dashboard
#   ./watchover.sh install --production   the same on a production server: production mode and the go-live checklist
#   ./watchover.sh checklist        the production checklist again: backup timer, start on boot, disk caps, admin password, TLS
#   ./watchover.sh backup-timer on [HH:MM]|off   nightly backup (systemd timer; cron where there is no systemd), default 02:15
#   ./watchover.sh autostart on|off the stack starts with the server (systemd unit; switched on by 'install' with Podman)
#   ./watchover.sh storage show     where the raw logs live (the paths you set, or "not set": the data volume), retention, sizes,
#                                   free space, the archive manifest and the yearly need at the current rate (nothing is proposed)
#   ./watchover.sh storage set [--hot DIR] [--cold DIR] [--hot-hours N] [--cold-days N] [--keep-old]
#                                   put the hot logs / the archive where you say: existing days are copied and verified (sha256),
#                                   the stack switches, the old copies go once it runs (--keep-old keeps them); refused without room
#                                   engine: Docker, or rootful Podman (RHEL): WATCHOVER_ENGINE=docker|podman in .env, detected when unset
#   ./watchover.sh update           new source tree copied over this folder: rebuild the image, recreate the containers, keep the data
#   ./watchover.sh upgrade          git pull of this folder (local edits set aside and put back) + update: the one-line way to a new version
#   ./watchover.sh update all [watchover-kurulum-X.sh] [--check]   (root) everything at once: the server's container packages (Podman,
#                                   crun, netavark, container-selinux, ...) from its repositories / Satellite, then Watchover (the given
#                                   one-file installer; a git install: upgrade). --check lists what would change and changes nothing
#   ./watchover.sh start|stop|restart|status|logs [service]
#   ./watchover.sh start|stop|restart|status all   (root) everything this server runs for Watchover: the containers, the server's own
#                                   agent (watchover-agent, its actuator and self-update timer), start on boot, the nightly backup;
#                                   status all adds versions, disks, the last backup and the LLM
#   ./watchover.sh agent status|start|stop|restart|logs   only the server's own agent (wo-agent, systemd)
#   ./watchover.sh help             the commands in Turkish, grouped by task
#   ./watchover.sh mcp on|off       our MCP server as a third container (MCP_API_KEY from .env)
#   ./watchover.sh otlp on|off      publish the OpenTelemetry ports 4317 (gRPC) / 4318 (HTTP) on the receiver (OTLP/HTTP also on 8600)
#   ./watchover.sh legacy on|off    the former Streamlit interface as an extra container (:8502) while teams move over
#   ./watchover.sh edge on NAME     nginx in front: https://NAME for the company network (TLS from this install's own CA, or ./certs/watchover.crt+key);
#                                   NAME may be a DNS name, an IP or a Bonjour name like watchover.local (announced by the Mac itself)
#   ./watchover.sh edge off|cert    stop it | export the CA root certificate colleagues install once (./certs/watchover-root.crt)
#   ./watchover.sh edge public NAME [LB CIDR...]   one address for the company network: NAME on the load balancer (VPN + firewall
#                                   decide who reaches it); prints what the network team sets up. edge csr [NAME] / edge company ISSUED
#                                   [CHAIN]: a certificate from the company CA (no root to install on anyone's machine); edge check [NAME]
#   ./watchover.sh edge lbtest [stop]   rehearse the load balancer on this server (a test HAProxy on 9443 / 9080 + the checks)
#   ./watchover.sh edge tls 1.2|1.3     TLS 1.2 + 1.3 (default) or TLS 1.3 only on the edge; edge check shows what is accepted
#   ./watchover.sh doctor [--check] the address does not open? checks containers, the edge, the name (Bonjour) and repairs what it can
#   ./watchover.sh backup [--keep N] [--no-logs]  PostgreSQL dump + data volume (--no-logs: without /data/logs) + .env
#                                   -> ./backups/watchover-YYYYMMDD-HHMM.tgz (WATCHOVER_BACKUP_DIR; oldest beyond N=14 removed)
#   ./watchover.sh restore FILE     bring a backup back (containers are stopped meanwhile)
#   ./watchover.sh user ...         account management, e.g.  user list | user add ops@firma.com --admin | user password EMAIL | user unlock EMAIL
#   ./watchover.sh admin-password   the built-in administrator's first password (random per installation, until the first sign-in)
#   ./watchover.sh demo-clean [--dry-run] [--yes]   delete everything the retired simulator left, in every store (counts first; real data stays)
#   ./watchover.sh reset-data [--dry-run] [--yes]   full reset: every analysis store (anomalies, actions / cases, alerts, learned baselines, LLM memory,
#                                                   fine-tunes, history, traces, topology) emptied; accounts, settings, agents, audit logs, real log lines stay
#   ./watchover.sh migrate          copy old SQLite files (knowledge.db / actions.db / playbook.db in ./data or the volume) into PostgreSQL
#   ./watchover.sh save|load FILE   export the built images to a .tgz (build on a machine with internet), import them on an air-gapped server
#   ./watchover.sh bundle [FILE]    everything an air-gapped server needs in one .tar: all images, downloaded Ollama models, training weights, source
#   ./watchover.sh unbundle FILE    on the air-gapped server: load the images and model volumes, mark the install offline (no builds, no pulls)
#   ./watchover.sh setup [--offline]  the one-file installer (sudo bash watchover-kurulum-*.sh: wizard, checks, install); --offline: everything inside, no internet
#   ./watchover.sh llm prepare      cache the training base weights (done by 'bundle' automatically) so the trainer never needs internet
#   ./watchover.sh llm auto|bench   measure the bundled LLM here and pick the chat model (14B when this machine carries 5+ tokens/s, else
#                                   7B; 3B under 2.5): llm on does it after the downloads | tokens/s of one model (llm bench qwen2.5:7b-instruct)
#   ./watchover.sh profile [NAME]   sizing: auto (default: measured from this machine on every start), cpu8 / cpu16 (fixed numbers); no NAME shows it
#   ./watchover.sh shell            a shell inside the dashboard container
#   ./watchover.sh updater on|off|status   in-app updates (Sistem › Güncelleme): upload a zip / bundle, apply, roll back. A separate
#                                   'updater' container is the only one with the engine socket (= root on this host); on by default
#   ./watchover.sh build            only the images of this tree's version (the updater builds a new release with it before switching)
#   ./watchover.sh health [VER] [S] wait up to S seconds (180) until the dashboard answers /api/health with VER (default: this tree)
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE="${WATCHOVER_ENV_FILE:-.env}"      # the updater builds a release tree with the install folder's .env
UPDATER_TAG=4                               # watchover-updater:1 carries tools only (its logic runs from this folder): bump when Dockerfile.updater changes
COMPOSE="docker compose"
CMD="${1:-}"

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
# Container engine. Docker, or rootful Podman as Red Hat ships it: podman.socket (Docker-compatible API) + the Compose v2 binary
# talking to it through DOCKER_HOST, so the compose files and every command below stay the same. Images are built with podman
# build itself (no BuildKit on that socket) under the names Docker would give them. WATCHOVER_ENGINE=docker|podman (environment
# or .env) chooses; unset: a real Docker CLI wins, else Podman when it is installed.
PODMAN_SOCK="${WATCHOVER_PODMAN_SOCK:-/run/podman/podman.sock}"
detect_engine() {
  local e="${WATCHOVER_ENGINE:-$(sed -n 's/^WATCHOVER_ENGINE=//p' "$ENV_FILE" 2>/dev/null | head -1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')}"
  case "$e" in
    docker|podman) echo "$e" ;;
    ""|auto) if type -P docker >/dev/null 2>&1 && ! command docker --version 2>/dev/null | grep -qi podman; then echo docker
             elif type -P podman >/dev/null 2>&1; then echo podman; else echo docker; fi ;;
    *) echo "WATCHOVER_ENGINE must be docker or podman (got '$e')" >&2; return 1 ;;
  esac
}
ENGINE=$(detect_engine) || exit 1
compose_bin() {   # Compose v2 for Podman: standalone binary or the docker-compose-plugin RPM (only Compose, no Docker engine)
  # Watchover's own plugin path first: install-rhel.sh puts the patched Compose of the updater image (or the bundle's) there
  local c; for c in "${WATCHOVER_COMPOSE_BIN:-}" /usr/local/lib/docker/cli-plugins/docker-compose "$(command -v docker-compose 2>/dev/null || true)" \
    /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }; done; return 1
}
ALPINE=alpine; [ "$ENGINE" = podman ] && ALPINE=docker.io/library/alpine:latest    # helper container for volume copies (no short-name prompt)
# every container command of this script goes through here
docker() {
  [ "$ENGINE" = podman ] || { command docker "$@"; return; }
  case "${1:-}" in
    compose) shift; DOCKER_HOST="unix://$PODMAN_SOCK" "$(compose_bin)" "$@" ;;
    info) if [ "${2:-}" = --format ]; then case "${3:-}" in *NCPU*) podman info --format '{{.Host.CPUs}}' ;; *MemTotal*) podman info --format '{{.Host.MemTotal}}' ;; *) podman "$@" ;; esac
          else podman "$@"; fi ;;
    save) shift; podman save -m "$@" ;;                  # several images in one archive (docker save does that by default)
    *) podman "$@" ;;
  esac
}
need_docker() {
  if [ "$ENGINE" = podman ]; then
    command -v podman >/dev/null 2>&1 || die "Podman is not installed: sudo dnf -y install podman   (or: sudo bash scripts/rhel-prepare.sh --engine podman)"
    compose_bin >/dev/null || die "Compose v2 is missing for Podman$([ -f install-rhel.sh ] && [ ! -f /usr/local/bin/watchover ] && echo ' (Watchover is not installed yet: sudo ./install-rhel.sh installs everything, then use the watchover command)'): sudo bash scripts/rhel-prepare.sh --engine podman installs docker-compose-plugin (Compose only, no Docker engine); air-gapped: copy the docker-compose-linux-$(uname -m) binary to /usr/local/lib/docker/cli-plugins/docker-compose (chmod +x)"
    if [ ! -S "$PODMAN_SOCK" ] && [ "$(id -u)" = 0 ] && command -v systemctl >/dev/null 2>&1; then systemctl enable --now podman.socket >/dev/null 2>&1 || true; fi
    [ -S "$PODMAN_SOCK" ] || die "the Podman socket $PODMAN_SOCK is not there: sudo systemctl enable --now podman.socket"
    [ -w "$PODMAN_SOCK" ] || die "Watchover runs on rootful Podman and its socket needs root: sudo ./watchover.sh ${CMD:-start}"
    docker info >/dev/null 2>&1 || die "Podman does not answer (podman info): see journalctl -u podman.socket"
    return 0
  fi
  type -P docker >/dev/null 2>&1 || die "Docker is not installed. Linux:  curl -fsSL https://get.docker.com | sudo sh   (then: sudo usermod -aG docker \$USER and sign in again). macOS / Windows: Docker Desktop. RHEL with Podman: WATCHOVER_ENGINE=podman"
  docker info >/dev/null 2>&1 || die "Docker is installed but not reachable (daemon stopped, or your user is not in the 'docker' group)."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing (docker compose version)."
}
rand() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"; else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi; }
version() { sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1; }
# this tree's commit: git when the folder is a checkout of its own, else .release-rev (written from the zip comment by the updater)
git_rev() { local r=""; [ "$(git rev-parse --show-toplevel 2>/dev/null)" = "$(pwd -P)" ] && r=$(git rev-parse --short HEAD 2>/dev/null); [ -n "$r" ] || { [ -f .release-rev ] && r=$(tr -dc '0-9a-f' < .release-rev | cut -c1-12); }; echo "${r:--}"; }
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//'; }   # inline comments and trailing blanks dropped
# .env is rewritten in place (same file): owner and mode stay when a command runs with sudo (systemd units, Podman)
env_set() { local t; if grep -q "^$1=" "$ENV_FILE" 2>/dev/null; then t=$(sed "s|^$1=.*|$1=$2|" "$ENV_FILE") && printf '%s\n' "$t" > "$ENV_FILE"; else printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; fi; }
env_del() { local t; if grep -q "^$1=" "$ENV_FILE" 2>/dev/null; then t=$(sed "/^$1=/d" "$ENV_FILE") && printf '%s\n' "$t" > "$ENV_FILE"; fi; return 0; }
# this machine's LAN address: Linux 'hostname -I'; macOS the interface of the default route (Wi-Fi, Ethernet or a USB-C adapter), never a
# VPN tunnel (colleagues on the LAN cannot reach a utun address), then en0 / en1
host_ip() {
  local ip if_; ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  if [ -z "$ip" ] && command -v route >/dev/null 2>&1; then
    if_=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
    case "$if_" in utun*|ppp*|ipsec*|gif*|"") ;; *) ip=$(ipconfig getifaddr "$if_" 2>/dev/null) ;; esac
  fi
  [ -n "$ip" ] || ip=$(ipconfig getifaddr en0 2>/dev/null); [ -n "$ip" ] || ip=$(ipconfig getifaddr en1 2>/dev/null); echo "${ip:-localhost}"
}

# sizing follows the machine Docker reports (CPUs, memory), on every install / update / start, as caps (not reservations), with shares
# so that no part can starve another: the dashboard gets most CPUs, the LLM and the trainer a part (a question never slows the screens),
# PostgreSQL a quarter of its memory as shared_buffers. 8 CPU / 64 GB gives exactly the cpu8 profile. WATCHOVER_SIZING=manual (or
# ./watchover.sh profile cpu8|cpu16) keeps fixed numbers instead.
clamp() { local v=$1; [ "$v" -lt "$2" ] && v=$2; [ "$v" -gt "$3" ] && v=$3; echo "$v"; }
size_env() {
  [ "$(env_get WATCHOVER_SIZING)" = "manual" ] && return 0
  local ncpu mem_b mem_gb dcpu dmem rcpu rmem pw spool pcpu pmem psb pcache lcpu lmem tcpu tmem keep
  ncpu=$(docker info --format '{{.NCPU}}' 2>/dev/null | tr -dc '0-9'); [ -n "$ncpu" ] && [ "$ncpu" -ge 1 ] || ncpu=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
  mem_b=$(docker info --format '{{.MemTotal}}' 2>/dev/null | tr -dc '0-9'); [ -n "$mem_b" ] && [ "$mem_b" -ge 1 ] || mem_b=$(awk '/^MemTotal:/ {printf "%d", $2 * 1024}' /proc/meminfo 2>/dev/null); [ -n "$mem_b" ] && [ "$mem_b" -ge 1 ] || mem_b=$((8 * 1024 * 1024 * 1024))
  mem_gb=$((mem_b / 1073741824)); [ "$mem_gb" -ge 2 ] || mem_gb=2
  dcpu=$(clamp $((ncpu - 2)) 2 12);          dmem=$(clamp $((mem_gb / 4)) 4 32)
  rcpu=$(clamp $((ncpu / 4)) 1 4);           rmem=$([ "$mem_gb" -ge 16 ] && echo 2 || echo 1)
  pw=$(clamp $((ncpu * 3 / 8)) 1 6);         spool=$([ "$mem_gb" -ge 32 ] && echo 4096 || echo 2048)
  pcpu=$(clamp $((ncpu / 4)) 1 4);           pmem=$(clamp $((mem_gb / 8)) 2 16)
  psb=$((pmem * 1024 / 4));                  pcache=$(clamp $((mem_gb / 2)) 1 256)
  lcpu=$(clamp $((ncpu / 2)) 1 8);           lmem=$(clamp $((mem_gb * 16 / 100)) 4 16)   # half the CPUs (4 CPUs: 2, not 1: a 7B crawled)
  case "$(env_get LLM_MODEL)" in *14b*) [ "$mem_gb" -lt 48 ] || [ "$lmem" -ge 16 ] || lmem=16 ;; esac   # llm auto chose the 14B: weights ~9 GB + context + embeddings
  tcpu=$(clamp $((ncpu / 2)) 1 8);           tmem=$(clamp $((mem_gb / 5)) 4 16)
  keep=$([ "$mem_gb" -ge 32 ] && echo 24h || echo 30m)
  env_set WATCHOVER_SIZING auto
  env_set WATCHOVER_CPUS "$dcpu";            env_set WATCHOVER_MEMORY "${dmem}g"
  env_set WATCHOVER_RECEIVER_CPUS "$rcpu";   env_set WATCHOVER_RECEIVER_MEMORY "${rmem}g"
  env_set WATCHOVER_PARSE_WORKERS "$pw";     env_set WATCHOVER_SPOOL_MAX_MB "$spool"
  env_set WATCHOVER_PG_CPUS "$pcpu";         env_set WATCHOVER_PG_MEMORY "${pmem}g"
  env_set WATCHOVER_PG_SHARED_BUFFERS "${psb}MB"; env_set WATCHOVER_PG_CACHE "${pcache}GB"
  env_set WATCHOVER_LLM_CPUS "$lcpu";        env_set WATCHOVER_LLM_THREADS "$lcpu"; env_set WATCHOVER_LLM_MEMORY "${lmem}g"
  env_set WATCHOVER_LLM_PARALLEL 1;          env_set WATCHOVER_LLM_KEEPALIVE "$keep"
  env_set WATCHOVER_TRAIN_CPUS "$tcpu";      env_set WATCHOVER_TRAIN_THREADS "$tcpu"; env_set WATCHOVER_TRAIN_MEMORY "${tmem}g"
  say "sizing: Docker sees $ncpu CPUs / ${mem_gb} GB -> dashboard $dcpu cpu ${dmem}g, receiver $rcpu cpu (${pw} parse workers), PostgreSQL $pcpu cpu ${pmem}g, LLM $lcpu cpu ${lmem}g, trainer $tcpu cpu ${tmem}g  (WATCHOVER_SIZING=manual in .env keeps your own)"
}

write_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp .env.docker.example "$ENV_FILE"
    say ".env written from .env.docker.example"
  fi
  # "KEY=   # comment" (older .env.docker.example): Compose reads the comment as KEY's value; nginx refused such a trusted-proxy list
  if grep -qE '^[A-Za-z_][A-Za-z0-9_]*=[[:space:]]+#' "$ENV_FILE" 2>/dev/null; then
    t=$(sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)=[[:space:]]+#.*$/\1=/' "$ENV_FILE") && printf '%s\n' "$t" > "$ENV_FILE"
  fi
  env_set WATCHOVER_IMAGE watchover
  env_set WATCHOVER_TAG "$(version)"
  env_set GIT_REV "$(git_rev)"
  [ -n "$(env_get POSTGRES_PASSWORD)" ] && [ "$(env_get POSTGRES_PASSWORD)" != "change-me" ] || { env_set POSTGRES_PASSWORD "$(rand 24)"; say "POSTGRES_PASSWORD generated"; }
  [ -n "$(env_get MCP_API_KEY)" ] || { sed -i.bak 's/^#MCP_API_KEY=.*$//' "$ENV_FILE" 2>/dev/null; rm -f "$ENV_FILE.bak"; env_set MCP_API_KEY "$(rand 24)"; say "MCP_API_KEY generated"; }
  env_set WATCHOVER_ENGINE "$ENGINE"
  [ -n "$(env_get WATCHOVER_UPDATER)" ] || env_set WATCHOVER_UPDATER 1      # in-app updates on by default; ./watchover.sh updater off
  in_updater || env_set WATCHOVER_INSTALL_DIR "$(pwd -P)"                  # the updater mounts this folder at the same path
  [ -n "$(env_get WATCHOVER_ENGINE_SOCK)" ] || env_set WATCHOVER_ENGINE_SOCK "$(engine_sock)"
  size_env
  compose_files
  chmod 600 "$ENV_FILE"
  mkdir -p datasets backups
}

# Raw logs on disks of their own (a server: e.g. /srv/watchover/hot on NVMe, /srv/watchover/cold on a cheaper volume). Empty =
# inside the data volume (/data/logs/hot|cold) as before. Set: the host directory is bind-mounted at /logs/hot | /logs/cold (the
# paths the OpenShift chart uses) by docker-compose.loghot.yml / .logcold.yml, with :z so SELinux (Podman, or Docker with
# selinux-enabled) labels it for the containers that read logs. Those containers run as UID 10001 / GID 0: the directory is theirs.
LOG_UID=10001
fs_of() { df -P "$1" 2>/dev/null | awk 'NR==2 {print $6}'; }
lower() { echo "$1" | tr 'A-Z' 'a-z'; }
check_log_dir() {   # HOT|COLD PATH: exists (created when root), writable for UID 10001 / GID 0, on a disk of its own
  local k="$1" p="$2" fix u g m ok=0
  in_updater && return 0                                 # host paths are not visible in the updater container; checked when they were set
  case "$p" in /*) ;; *) die "WATCHOVER_LOG_${k}_PATH must be an absolute host directory such as /srv/watchover/$(lower "$k") (got '$p')" ;; esac
  fix="sudo chown -R $LOG_UID:0 $p && sudo chmod 2770 $p"
  if [ ! -d "$p" ]; then
    [ "$(id -u)" = 0 ] || die "WATCHOVER_LOG_${k}_PATH=$p does not exist: sudo mkdir -p $p && $fix"
    mkdir -p "$p" && chown "$LOG_UID:0" "$p" && chmod 2770 "$p" && say "created $p for the $(lower "$k") logs (owner $LOG_UID:0, mode 2770)"
  fi
  read -r u g m <<EOF_STAT
$(stat -c '%u %g %a' "$p" 2>/dev/null || stat -f '%u %g %Lp' "$p")
EOF_STAT
  m="00$m"; m="${m: -3}"                                 # owner / group / other digits (setgid bit dropped)
  [ "$u" = "$LOG_UID" ] && [ $(( ${m:0:1} & 3 )) = 3 ] && ok=1
  [ "$g" = 0 ] && [ $(( ${m:1:1} & 3 )) = 3 ] && ok=1
  [ $(( ${m:2:1} & 3 )) = 3 ] && ok=1
  [ "$ok" = 1 ] || die "WATCHOVER_LOG_${k}_PATH=$p belongs to $u:$g (mode $m) but the containers write as UID $LOG_UID / GID 0: $fix"
  [ "$(fs_of "$p")" = "/" ] && say "note: $p is on the root file system, not a disk of its own (mount a volume there for production)"
  return 0
}
compose_files() {   # COMPOSE_FILE in .env: the base file plus the overrides that apply now (edge on, separate log disks)
  local f="docker-compose.yml" k p
  [ "$(env_get WATCHOVER_EDGE)" = "1" ] && f="$f:docker-compose.edge.yml"
  [ "$(env_get WATCHOVER_OTLP)" = "1" ] && f="$f:docker-compose.otlp.yml"
  for k in HOT COLD; do
    p=$(env_get "WATCHOVER_LOG_${k}_PATH"); [ -n "$p" ] || continue
    check_log_dir "$k" "$p"; f="$f:docker-compose.log$(lower "$k").yml"
  done
  if [ "$f" = docker-compose.yml ]; then env_del COMPOSE_FILE; else env_set COMPOSE_FILE "$f"; fi
}

offline() { [ "$(env_get WATCHOVER_OFFLINE)" = "1" ]; }
have_image() { docker image inspect "$1" >/dev/null 2>&1; }
build() {
  if offline; then   # air-gapped: the images came in with ./watchover.sh unbundle, never build or pull here
    have_image "watchover:$(version)" && have_image "watchover-receiver:$(version)" && have_image "watchover-postgres:$(version)" || die "offline install and image watchover:$(version), watchover-receiver:$(version) or watchover-postgres:$(version) is missing: unbundle the bundle of this version first (./watchover.sh unbundle watchover-bundle-$(version).tar)"
    say "offline install: using the loaded image watchover:$(version) (no build)"
    if updater_on && ! have_image "watchover-updater:$UPDATER_TAG"; then env_set WATCHOVER_UPDATER 0; say "offline install: the bundle has no watchover-updater image, in-app updates stay off (bundle a newer version)"; fi
    return 0
  fi
  refresh_bases
  say "building image watchover:$(version) from this source tree (first time: a few minutes, needs internet for PyPI)"
  build_img dashboard --build-arg GIT_REV="$(git_rev)" --build-arg VERSION="$(version)"
  say "building the receiver (Go, a few seconds)"; build_img receiver
  say "building PostgreSQL (postgres:16-alpine, su-exec for gosu)"; build_img postgres
  if [ "$(env_get WATCHOVER_LLM)" = "1" ]; then build_img trainer; build_img ollama; fi
  if updater_on && ! have_image "watchover-updater:$UPDATER_TAG"; then
    say "building the updater image (once: Docker CLI + Compose, bash, Python)"
    build_img updater || { env_set WATCHOVER_UPDATER 0; say "WARNING: the updater image could not be built; in-app updates stay off (./watchover.sh updater on to retry)"; }
  fi
  return 0
}
# the newest patch of every floating tag before a build (python:3.11-slim, node:22-alpine, nginx:1.30-alpine, postgres:16-alpine,
# ollama): a build reuses the base image it finds locally, so without this a long-lived server kept the first one it ever pulled.
# Failures are only noted (a mirror without one of them); the images already here are used then.
refresh_bases() {
  local b f imgs=""
  for f in Dockerfile Dockerfile.receiver Dockerfile.updater Dockerfile.postgres $([ "$(env_get WATCHOVER_LLM)" = 1 ] && echo Dockerfile.trainer Dockerfile.ollama); do
    [ -f "$f" ] || continue
    imgs="$imgs $(awk '/^ARG [A-Za-z_]+=/ { split(substr($0, 5), a, "="); v[a[1]] = a[2] }
                      /^FROM[ \t]/ { i = $2; for (k in v) gsub("\\$\\{" k "\\}", v[k], i); if (i !~ /\$/) print i }' "$f")"
  done
  imgs="$imgs nginx:1.30-alpine"
  say "refreshing base images (security patches of the floating tags)"
  for b in $(printf '%s\n' $imgs | awk '!s[$0]++'); do docker pull -q "$b" >/dev/null 2>&1 || say "  could not refresh $b (the local copy is used)"; done
}
# one image: Docker through compose; Podman with podman build (Docker image format keeps HEALTHCHECK) under the name Docker gives
# 'watchover:<tag>', which is the name compose asks the Podman socket for
build_img() {
  local svc="$1" f=Dockerfile n=watchover tag k v; tag="$(version)"; shift
  # company proxy (WATCHOVER_HTTP_PROXY / _HTTPS_PROXY / _NO_PROXY in .env, written by install-rhel.sh): apt, apk, pip and npm in
  # the build steps go through it (proxy build args are predefined in Docker and Buildah, nothing of them stays in the image)
  for k in HTTP_PROXY HTTPS_PROXY NO_PROXY; do
    v=$(env_get "WATCHOVER_$k"); if [ -n "$v" ]; then set -- --build-arg "$k=$v" --build-arg "$(echo "$k" | tr 'A-Z' 'a-z')=$v" "$@"; fi
  done
  if [ "$ENGINE" = podman ]; then
    case "$svc" in receiver) f=Dockerfile.receiver; n=watchover-receiver ;; trainer) f=Dockerfile.trainer; n=watchover-trainer ;;
      postgres) f=Dockerfile.postgres; n=watchover-postgres ;; ollama) f=Dockerfile.ollama; n=watchover-ollama ;;
      updater) f=Dockerfile.updater; n=watchover-updater; tag=$UPDATER_TAG; set -- "$@" --build-arg PODMAN=1 ;; esac
    podman build --format docker -f "$f" -t "docker.io/library/$n:$tag" "$@" .
  elif [ "$svc" = trainer ] || [ "$svc" = ollama ]; then $COMPOSE --profile llm build "$@" "$svc"
  elif [ "$svc" = updater ]; then $COMPOSE --profile updater build "$@" updater
  else $COMPOSE build "$@" "$svc"; fi
}
# firewalld (RHEL): open | close the host ports a feature publishes (mcp, otlp). Only as root with firewalld running, else nothing;
# the zone is the one install-rhel.sh used (WATCHOVER_FW_ZONE in .env), else firewalld's default zone. 127.0.0.1:PORT stays closed.
fw_ports() {
  local op="$1" z p opened=""; shift
  [ "$(id -u)" = 0 ] && command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1 || return 0
  z=$(env_get WATCHOVER_FW_ZONE); [ -n "$z" ] || z=$(firewall-cmd --get-default-zone 2>/dev/null) || return 0
  for p in "$@"; do
    case "$p" in 127.*|localhost:*|"") continue ;; esac
    firewall-cmd --permanent --zone="$z" "--$op-port=${p##*:}/tcp" >/dev/null 2>&1 && opened="$opened ${p##*:}/tcp"
    firewall-cmd --zone="$z" "--$op-port=${p##*:}/tcp" >/dev/null 2>&1 || true      # now too: a --reload would drop netavark's runtime rules
  done
  [ -z "$opened" ] || say "firewalld ($z): $([ "$op" = add ] && echo opened || echo closed)$opened"
}
# compose volume name (project prefix) for a volume declared in docker-compose.yml
vol_name() { $COMPOSE config --format json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['volumes']['$1']['name'])" 2>/dev/null || echo "watchover_$1"; }

# the in-app updater (docker-compose.yml service 'updater', profile 'updater'): the only container with the engine socket
in_updater() { [ "${WATCHOVER_IN_UPDATER:-}" = 1 ]; }
updater_on() { [ "$(env_get WATCHOVER_UPDATER)" = "1" ]; }
uprof() { updater_on && echo "--profile updater" || true; }
engine_sock() {   # what the updater mounts: the Podman socket, rootless Docker's, else /var/run/docker.sock (Docker Desktop maps it to its VM)
  local s
  if [ "$ENGINE" = podman ]; then echo "$PODMAN_SOCK"; return; fi
  s="${DOCKER_HOST:-}"; [ -n "$s" ] || s=$(command docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
  case "$(uname -s) $s" in "Linux unix:///run/user/"*) echo "${s#unix://}" ;; *) echo /var/run/docker.sock ;; esac
}
busy_guard() {   # an in-app update owns the stack meanwhile (marker written by the updater, ignored after 3 hours)
  in_updater && return 0
  [ -f .updater/busy ] || return 0
  [ -n "$(find .updater/busy -mmin -180 2>/dev/null)" ] || return 0
  die "an in-app update is running ($(head -c 160 .updater/busy 2>/dev/null)); wait for it (Sistem › Güncelleme) or, if it is stuck, remove .updater/busy"
}
profiles() { local p=""; [ "$(env_get WATCHOVER_MCP)" = "1" ] && p="$p --profile mcp"; [ "$(env_get WATCHOVER_LEGACY)" = "1" ] && p="$p --profile legacy"; [ "$(env_get WATCHOVER_EDGE)" = "1" ] && p="$p --profile edge"; [ "$(env_get WATCHOVER_LLM)" = "1" ] && p="$p --profile llm"; echo "$p"; }
# a *.local name on macOS: announce it on the LAN with Bonjour (dns-sd proxy record) so colleagues' Macs, iPhones and Windows 10+ resolve it.
# With launchd available the record is a LaunchAgent (KeepAlive: survives logout, reboot and 'update'); otherwise a nohup process
# whose pid is kept in .edge-mdns.pid and re-created by up() when it is gone.
mdns_label() { echo "local.watchover.mdns.$(echo "$1" | tr -c 'A-Za-z0-9\n' '-')"; }
mdns_dir() { echo "$HOME/Library/Application Support/Watchover"; }
# The follower the LaunchAgent runs: announces NAME with the Mac's current LAN address and re-announces within 15 s when that address
# changes (another Wi-Fi, a new DHCP lease, waking from sleep, a dock / USB-C Ethernet). A fixed address in the plist was why
# watchover.local stopped resolving after the Mac moved networks. The address in use is kept in <dir>/<label>.ip for 'doctor'.
mdns_follower() {
  cat <<'SH'
#!/bin/bash
# watchover mdns-follow: keep NAME announced on Bonjour with this Mac's current LAN address
NAME="$1"; PORT="${2:-443}"; STATE="$3"; DNSSD="${4:-dns-sd}"
ip_now() {
  local if_ ip; if_=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
  case "$if_" in utun*|ppp*|ipsec*|gif*|"") ;; *) ip=$(ipconfig getifaddr "$if_" 2>/dev/null) ;; esac
  [ -n "$ip" ] || ip=$(ipconfig getifaddr en0 2>/dev/null); [ -n "$ip" ] || ip=$(ipconfig getifaddr en1 2>/dev/null); echo "$ip"
}
pid=""; cur=""
trap '[ -n "$pid" ] && kill "$pid" 2>/dev/null; exit 0' TERM INT
while :; do
  ip=$(ip_now)
  if [ -n "$ip" ] && { [ "$ip" != "$cur" ] || [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; }; then
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && sleep 1
    "$DNSSD" -P "$NAME" _https._tcp local "$PORT" "$NAME" "$ip" >/dev/null 2>&1 &
    pid=$!; cur="$ip"; [ -n "$STATE" ] && echo "$ip" > "$STATE"
    echo "$(date '+%F %T') $NAME -> $ip"
  fi
  sleep "${WATCHOVER_MDNS_EVERY:-15}"
done
SH
}
mdns_start() {
  mdns_stop
  command -v dns-sd >/dev/null 2>&1 || return 0
  local n ip port; ip=$(host_ip); port=$(env_get WATCHOVER_EDGE_HTTPS_PORT); port="${port:-443}"
  for n in $(echo "$1" | tr ',' ' '); do
    case "$n" in *.local)
      if command -v launchctl >/dev/null 2>&1 && [ -n "${HOME:-}" ]; then
        local label dir plist; label=$(mdns_label "$n"); dir=$(mdns_dir); plist="$HOME/Library/LaunchAgents/$label.plist"
        mkdir -p "$HOME/Library/LaunchAgents" "$dir" "$HOME/Library/Logs"
        mdns_follower > "$dir/mdns-follow.sh"; chmod 755 "$dir/mdns-follow.sh"
        cat > "$plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$dir/mdns-follow.sh</string><string>$n</string><string>$port</string><string>$dir/$label.ip</string><string>$(command -v dns-sd)</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/watchover-mdns.log</string><key>StandardErrorPath</key><string>$HOME/Library/Logs/watchover-mdns.log</string>
</dict></plist>
PL
        launchctl unload "$plist" >/dev/null 2>&1 || true; launchctl load -w "$plist" >/dev/null 2>&1 || true
        say "Bonjour: $n -> $ip announced by a LaunchAgent ($plist); it follows this Mac's address when the network changes and stays after reboot and update"
      else
        nohup dns-sd -P "$n" _https._tcp local "$port" "$n" "$ip" >/dev/null 2>&1 &
        echo $! >> .edge-mdns.pid
        echo "$ip" > .edge-mdns.ip
        say "Bonjour: $n -> $ip announced (dns-sd; re-created by install/update/start and when the address changes, lost on reboot: run 'edge on' again)"
      fi ;;
    esac
  done
}
mdns_hosts() { local h; h="$(env_get WATCHOVER_EDGE_HOSTS | tr -d ' ')"; echo "${h:-$(env_get WATCHOVER_EDGE_HOST)}"; }
# the announcement is missing, old (a fixed address from before v1.43), not loaded, or (no launchd) dead / on a stale address while the
# edge is on with a .local name: bring it back. Called by up(), so install, update, upgrade and start all heal it.
mdns_ensure() {
  [ "$(env_get WATCHOVER_EDGE)" = "1" ] || return 0
  command -v dns-sd >/dev/null 2>&1 || return 0
  local hosts n alive=1 plist; hosts="$(mdns_hosts)"
  case "$hosts" in *.local*) ;; *) return 0 ;; esac
  if command -v launchctl >/dev/null 2>&1 && [ -n "${HOME:-}" ]; then
    for n in $(echo "$hosts" | tr ',' ' '); do case "$n" in *.local)
      plist="$HOME/Library/LaunchAgents/$(mdns_label "$n").plist"
      if [ ! -f "$plist" ] || ! grep -q "mdns-follow.sh" "$plist" || [ ! -f "$(mdns_dir)/mdns-follow.sh" ]; then alive=0
      elif ! launchctl list "$(mdns_label "$n")" >/dev/null 2>&1; then launchctl load -w "$plist" >/dev/null 2>&1 || alive=0; fi ;;
    esac; done
  else
    alive=0; if [ -f .edge-mdns.pid ]; then alive=1; while read -r pid; do kill -0 "$pid" 2>/dev/null || alive=0; done < .edge-mdns.pid; fi
    [ "$(cat .edge-mdns.ip 2>/dev/null)" = "$(host_ip)" ] || alive=0
  fi
  [ "$alive" = 1 ] || mdns_start "$hosts"
}
mdns_stop() {
  if [ -f .edge-mdns.pid ]; then while read -r pid; do kill "$pid" 2>/dev/null || true; done < .edge-mdns.pid; rm -f .edge-mdns.pid .edge-mdns.ip; fi
  if command -v launchctl >/dev/null 2>&1 && [ -n "${HOME:-}" ]; then
    local f; for f in "$HOME"/Library/LaunchAgents/local.watchover.mdns.*.plist; do [ -f "$f" ] || continue; launchctl unload "$f" >/dev/null 2>&1 || true; rm -f "$f"; done
  fi
}
# the address a .local name resolves to right now (macOS: the system resolver, which asks mDNS; Linux: getent / avahi)
resolve_ip() {
  local ip=""
  if command -v dscacheutil >/dev/null 2>&1; then ip=$(dscacheutil -q host -a name "$1" 2>/dev/null | awk '/ip_address:/{print $2; exit}' || true); fi
  [ -n "$ip" ] || ip=$(getent hosts "$1" 2>/dev/null | awk '{print $1; exit}' || true)
  [ -n "$ip" ] || { command -v avahi-resolve >/dev/null 2>&1 && ip=$(avahi-resolve -4 -n "$1" 2>/dev/null | awk '{print $2; exit}' || true); }
  echo "$ip"; return 0
}
# doctor: walk the path a browser takes to https://NAME and repair what is safe to repair (containers, Bonjour, the edge);
# --check only reports. Exit status 0 when the address answers at the end.
doctor() {
  local fix=1 bad=0 host port ip ann res svc st code; [ "${1:-}" = "--check" ] && fix=0
  ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
  no() { printf '  \033[31m✗\033[0m %s\n' "$*"; bad=1; }
  hint() { printf '      → %s\n' "$*"; }
  say "Watchover v$(version) doctor$( [ $fix = 0 ] && echo ' (check only)')"
  ip=$(host_ip); ok "this machine's LAN address: $ip"
  local rv; rv=$(running_version)
  if [ "$rv" = "$(version)" ]; then ok "the dashboard runs v$rv (same as this folder)"
  elif [ -n "$rv" ]; then no "the dashboard runs v$rv but this folder is v$(version)"; hint "finish the update: ./watchover.sh update"; fi
  # 1. containers
  local want missing=""; want="dashboard postgres receiver$( [ "$(env_get WATCHOVER_EDGE)" = "1" ] && echo ' edge')"
  svc_up() { echo "$st" | grep -Eq "^$1 running( healthy| starting)? *\$"; }      # running and not unhealthy
  st=$($COMPOSE $(profiles) ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null || true)
  for svc in $want; do svc_up "$svc" || missing="$missing $svc"; done
  if [ -n "$missing" ] && [ $fix = 1 ]; then
    up >/dev/null 2>&1 || true; sleep 8; st=$($COMPOSE $(profiles) ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null || true)
  fi
  for svc in $want; do
    if svc_up "$svc"; then case " $missing " in *" $svc "*) ok "container $svc was down: started again" ;; *) ok "container $svc is up" ;; esac
    else no "container $svc is not running ($(echo "$st" | awk -v s="$svc" '$1 == s { $1 = ""; print; f = 1 } END { if (!f) print "missing" }' | sed 's/^ //'))"
         hint "./watchover.sh logs $svc   shows why"; fi
  done
  # 1b. container names resolve: with Podman a 'firewall-cmd --reload' drops netavark's runtime rule and aardvark-dns (the bridge
  # gateway) becomes unreachable: the edge turns unhealthy, 'failed to resolve host postgres'
  names_ok() { $COMPOSE exec -T dashboard python -c "import socket; socket.getaddrinfo('postgres', 5432)" >/dev/null 2>&1; }
  if svc_up dashboard; then
    if names_ok; then ok "container names resolve (postgres, receiver, watchover)"
    elif [ "$ENGINE" = podman ] && [ $fix = 1 ] && podman network reload --all >/dev/null 2>&1 && names_ok; then
      ok "container names did not resolve (firewall reloaded?): repaired with podman network reload --all"
    else no "container names do not resolve inside the container network"
         hint "sudo podman network reload --all   (after a firewall-cmd --reload); for good: sudo systemctl enable --now netavark-firewalld-reload.service"; fi
  fi
  if [ "$(env_get WATCHOVER_EDGE)" != "1" ]; then
    hint "the edge is off: the address is http://$ip:$(env_get WATCHOVER_UI_PORT || echo 8501)   (for https://watchover.local: ./watchover.sh edge on watchover.local)"
    return $bad
  fi
  host=$(env_get WATCHOVER_EDGE_HOST); port=$(env_get WATCHOVER_EDGE_HTTPS_PORT); port="${port:-443}"
  # 2. the edge answers on this machine (name pinned to 127.0.0.1, so DNS / Bonjour play no part)
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 6 --resolve "$host:$port:127.0.0.1" "https://$host:$port/api/health" 2>/dev/null || true)
  if [ "$code" = "200" ]; then ok "the edge answers https://$host:$port on this machine"
  else
    no "the edge does not answer on this machine (HTTP ${code:-none})"
    if [ $fix = 1 ]; then $COMPOSE $(profiles) up -d --force-recreate dashboard edge >/dev/null 2>&1 || true; sleep 5
      code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 6 --resolve "$host:$port:127.0.0.1" "https://$host:$port/api/health" 2>/dev/null || true)
      [ "$code" = "200" ] && ok "after recreating dashboard + edge it answers" || hint "still not answering: ./watchover.sh logs edge   and   ./watchover.sh logs dashboard"; fi
  fi
  # 3. the name resolves to this machine
  case "$host" in *.local)
    ann=$(cat "$(mdns_dir)/$(mdns_label "$host").ip" 2>/dev/null || cat .edge-mdns.ip 2>/dev/null || true)
    res=$(resolve_ip "$host")
    if [ -n "$res" ] && [ "$res" = "$ip" ]; then ok "$host resolves to $res (this machine)"
    else
      no "$host resolves to ${res:-nothing}, this machine is $ip${ann:+ (announced: $ann)}"
      if [ $fix = 1 ]; then mdns_start "$(mdns_hosts)" || true; sleep 3; res=$(resolve_ip "$host")
        if [ "$res" = "$ip" ]; then ok "re-announced: $host -> $ip"; bad=0
        else hint "announced again; if the browser still fails, clear the Mac's DNS cache: sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder"
             hint "another device on the network may claim $host: dns-sd -G v4 $host   lists who answers"; fi; fi
    fi ;;
  esac
  [ $bad = 0 ] && say "all good: $(edge_url)" || say "problems remain (see above); run './watchover.sh doctor' again after the hints"
  return $bad
}
# the version the dashboard container is actually running (it can differ from this folder when an update did not finish)
running_version() { $COMPOSE exec -T dashboard curl -fsS --max-time 5 http://127.0.0.1:8501/api/health 2>/dev/null | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' || true; }
# after update: wait for the new container and say plainly which version runs; the browser keeps an open tab on the old build until reloaded
wait_health() {   # VERSION SECONDS: until the dashboard answers /api/health with VERSION (1 after SECONDS)
  local want="$1" secs="$2" v="" end; end=$(( $(date +%s) + secs ))
  while :; do v=$(running_version); [ "$v" = "$want" ] && { say "dashboard answers: v$v"; return 0; }; [ "$(date +%s)" -ge "$end" ] && break; sleep "${WATCHOVER_HEALTH_POLL:-3}"; done
  say "the dashboard reports '${v:-no answer}', expected v$want"; return 1
}

# ---------------------------------------------------------------- raw-log storage, given by hand (storage show | storage set)
# Nothing picks or proposes a disk: the operator names the hot directory and the archive directory. Data moves in a throwaway
# container of this release's image run as root (it reaches the data volume and any host path, from the host or the updater):
# copy + sha256 check while the stack runs, then the log writers stop for a final pass of what changed, .env switches, the stack
# starts, and only when /api/health answers do the old copies go. A new path that does not come up puts the old one back.
# The Python side: python -m watchover.logtiers relocate | relocate-clean | show | retention (logtiers.py, logaudit.py).
LOG_SYSTEM_DIRS="/ /bin /boot /dev /etc /home /lib /lib32 /lib64 /libx32 /media /mnt /opt /proc /root /run /sbin /srv /sys /tmp /usr /var /var/lib /var/log /var/tmp /Users /Volumes /private /System /Library /Applications"
log_path_ok() {   # KIND PATH: what logaudit.check_path allows: absolute, plain characters, not a system directory nor inside one
  local k="$1" p="$2" q d
  case "$p" in /*) ;; *) die "--$k must be an absolute directory such as /srv/watchover/$k (got '$p')" ;; esac
  case "$p" in *[!A-Za-z0-9._/-]*|*//*|*/./*|*/.|*/../*|*/..) die "--$k: use only letters, digits and . _ - / in the path (got '$p')" ;; esac
  q="${p%/}"; [ -n "$q" ] || q=/
  for d in $LOG_SYSTEM_DIRS; do [ "$q" = "$d" ] && die "--$k $p is a system directory: give a directory of its own, e.g. ${q%/}/watchover-$k"; done
  case "$q/" in /proc/*|/sys/*|/dev/*|/etc/*|/boot/*|/usr/*|/bin/*|/sbin/*|/lib/*|/lib64/*|/run/*|/var/lib/docker/*|/var/lib/containers/*|/System/*|/Library/*|/private/etc/*)
    die "--$k $p is inside a system directory" ;; esac
  case "$q/" in "$(pwd -P)/"*) die "--$k $p is inside the install folder: give a directory outside it" ;; esac
  return 0
}
log_img() { local t; t=$(env_get WATCHOVER_TAG); echo "watchover:${t:-$(version)}"; }
logpy() { docker run --rm --network none "$@"; }       # MOUNTS... IMAGE python -m watchover.logtiers ...
stg_step() { in_updater && echo "::step $1 $2"; say "$2"; }
tier_mount() {    # KIND PATH -> "-v SRC:/from[:z] /from/..." for the tier's current place: a host path, or the data volume's logs/KIND
  if [ -n "$2" ]; then echo "-v $2:/from:z /from"; else echo "-v $(vol_name watchover-data):/from /from/logs/$1"; fi
}
storage_show() {
  local hp cp img a h c
  hp=$(env_get WATCHOVER_LOG_HOT_PATH); cp=$(env_get WATCHOVER_LOG_COLD_PATH); img=$(log_img)
  if have_image "$img"; then
    a="-v $(vol_name watchover-data):/data:ro"; h=/data/logs/hot; c=/data/logs/cold
    [ -n "$hp" ] && { a="$a -v $hp:/hot:ro,z"; h=/hot; }
    [ -n "$cp" ] && { a="$a -v $cp:/cold:ro,z"; c=/cold; }
    # shellcheck disable=SC2086
    logpy --user 0 $a "$img" python -m watchover.logtiers show --home /data --hot "$h" --cold "$c" --hot-path "$hp" --cold-path "$cp" \
      || die "could not read the storage (image $img)"
  else
    for k in hot cold; do
      if [ "$k" = hot ]; then p="$hp"; else p="$cp"; fi
      if [ -n "$p" ]; then printf '  %-5s%s  (set by hand)   %s\n' "$k" "$p" "$(df -Ph "$p" 2>/dev/null | awk 'NR==2 {print $2" disk, "$4" free"}')"
      else printf '  %-5sNOT SET - inside the data volume (/data/logs/%s); set it: ./watchover.sh storage set --%s DIR\n' "$k" "$k" "$k"; fi
    done
  fi
  return 0
}
storage_set() {
  local hot="" cold="" hh="" cd="" keep=0 k K p cur moves="" img m src old_hot old_cold usage a b
  usage="usage: ./watchover.sh storage set [--hot DIR] [--cold DIR] [--hot-hours N] [--cold-days N] [--keep-old]"
  while [ $# -gt 0 ]; do case "$1" in
      --hot) hot="${2:-}"; shift ;; --cold) cold="${2:-}"; shift ;;
      --hot-hours) hh="${2:-}"; shift ;; --cold-days) cd="${2:-}"; shift ;;
      --keep-old) keep=1 ;;
      *) die "$usage" ;;
    esac; shift; done
  [ -n "$hot$cold$hh$cd" ] || die "$usage"
  case "$hh" in *[!0-9]*) die "--hot-hours must be a whole number of hours (1-8760)" ;; esac
  case "$cd" in *[!0-9]*) die "--cold-days must be a whole number of days (0-3650)" ;; esac
  [ -z "$hh" ] || { [ "$hh" -ge 1 ] && [ "$hh" -le 8760 ]; } || die "--hot-hours must be 1-8760"
  [ -z "$cd" ] || [ "$cd" -le 3650 ] || die "--cold-days must be 0-3650"
  [ -z "$hot" ] || { [ "$hot" = / ] || hot="${hot%/}"; log_path_ok hot "$hot"; }
  [ -z "$cold" ] || { [ "$cold" = / ] || cold="${cold%/}"; log_path_ok cold "$cold"; }
  old_hot=$(env_get WATCHOVER_LOG_HOT_PATH); old_cold=$(env_get WATCHOVER_LOG_COLD_PATH)
  a="${hot:-$old_hot}"; b="${cold:-$old_cold}"
  if [ -n "$a" ] && [ -n "$b" ]; then
    case "$a/" in "$b/"*) die "the hot directory and the archive directory must differ and not be inside each other" ;; esac
    case "$b/" in "$a/"*) die "the hot directory and the archive directory must differ and not be inside each other" ;; esac
  fi
  need_docker
  img=$(log_img); have_image "$img" || die "image $img is not here: ./watchover.sh install (or update) first"
  for k in hot cold; do
    if [ "$k" = hot ]; then p="$hot"; cur="$old_hot"; else p="$cold"; cur="$old_cold"; fi
    [ -n "$p" ] || continue
    if [ "${cur%/}" = "$p" ]; then say "$k logs already at $p"; continue; fi
    moves="$moves $k"
  done
  if [ -n "$moves" ]; then
    stg_step prepare "checking the new directories (created when missing, owner $LOG_UID:0, mode 2770, empty or Watchover's own, enough room)"
    for k in $moves; do
      if [ "$k" = hot ]; then p="$hot"; cur="$old_hot"; else p="$cold"; cur="$old_cold"; fi
      m=$(tier_mount "$k" "$cur"); src="${m##* }"
      # shellcheck disable=SC2086
      logpy --user 0 ${m% *} -v "$p:/to:z" "$img" python -m watchover.logtiers relocate "$src" /to --check --uid "$LOG_UID" --gid 0 >/dev/null \
        || die "the $k directory $p cannot be used (see above); nothing was changed"
      check_log_dir "$(echo "$k" | tr 'a-z' 'A-Z')" "$p"           # as every start will check it (host side)
    done
    stg_step copy "copying the existing $(echo $moves | sed 's/ / and /') files with a sha256 check while Watchover keeps running"
    for k in $moves; do
      if [ "$k" = hot ]; then p="$hot"; cur="$old_hot"; else p="$cold"; cur="$old_cold"; fi
      m=$(tier_mount "$k" "$cur"); src="${m##* }"
      # shellcheck disable=SC2086
      logpy --user 0 ${m% *} -v "$p:/to:z" "$img" python -m watchover.logtiers relocate "$src" /to --uid "$LOG_UID" --gid 0 \
        || die "copying the $k logs to $p failed (see above); Watchover still uses ${cur:-the data volume}, nothing was switched"
    done
    stg_step switch "stopping the log writers for the final pass (only what changed since the copy)"
    $COMPOSE $(profiles) stop dashboard mcp legacy >/dev/null 2>&1 || $COMPOSE stop dashboard >/dev/null 2>&1 || true
    for k in $moves; do
      if [ "$k" = hot ]; then p="$hot"; cur="$old_hot"; else p="$cold"; cur="$old_cold"; fi
      m=$(tier_mount "$k" "$cur"); src="${m##* }"
      # shellcheck disable=SC2086
      if ! logpy --user 0 ${m% *} -v "$p:/to:z" "$img" python -m watchover.logtiers relocate "$src" /to --final --uid "$LOG_UID" --gid 0; then
        up; die "the final copy of the $k logs failed; Watchover started again on ${cur:-the data volume}, nothing was switched"
      fi
      K=$(echo "$k" | tr 'a-z' 'A-Z'); env_set "WATCHOVER_LOG_${K}_PATH" "$p"
    done
  fi
  if [ -n "$hh$cd" ]; then
    # shellcheck disable=SC2086
    logpy -v "$(vol_name watchover-data):/data" "$img" python -m watchover.logtiers retention --home /data ${hh:+--hot-hours "$hh"} ${cd:+--cold-days "$cd"} >/dev/null \
      || die "could not write the retention into the settings"
    say "retention: ${hh:+hot $hh h }${cd:+archive $cd days}"
  fi
  [ -n "$moves" ] || return 0
  stg_step start "starting Watchover on the new paths"
  write_env
  stg_step health "waiting for the dashboard"
  if ! up || ! wait_health "$(version)" "${WATCHOVER_STORAGE_HEALTH_S:-300}"; then
    env_set WATCHOVER_LOG_HOT_PATH "$old_hot"; env_set WATCHOVER_LOG_COLD_PATH "$old_cold"; write_env; up
    die "Watchover did not come up on the new paths; the previous paths are back (their files were not touched; the copies stay in the new directories)"
  fi
  stg_step cleanup "$([ "$keep" = 1 ] && echo "keeping the old copies (--keep-old)" || echo "removing the old copies that were verified at the new place")"
  for k in $moves; do
    if [ "$k" = hot ]; then p="$hot"; cur="$old_hot"; else p="$cold"; cur="$old_cold"; fi
    m=$(tier_mount "$k" "$cur"); src="${m##* }"
    # shellcheck disable=SC2086
    logpy --user 0 ${m% *} -v "$p:/to:z" "$img" python -m watchover.logtiers relocate-clean "$src" /to $([ "$keep" = 1 ] && echo --keep) >/dev/null \
      || say "WARNING: the old $k copies in ${cur:-the data volume} could not all be removed; remove them by hand once you checked $p"
  done
  say "raw logs now: hot ${a:-the data volume} · archive ${b:-the data volume}   (./watchover.sh storage show)"
}

verify_running() {
  local v="" i; for i in $(seq 1 "${WATCHOVER_VERIFY_TRIES:-45}"); do v=$(running_version); [ "$v" = "$(version)" ] && break; sleep 2; done
  if [ "$v" = "$(version)" ]; then say "running v$v. In the browser reload the page once (Cmd+Shift+R / Ctrl+F5) if it still shows the old screens."
  else say "WARNING: this folder is v$(version) but the dashboard reports '${v:-no answer}'. See ./watchover.sh logs dashboard, then ./watchover.sh update"; fi
}
edge_url() { local h p; h=$(env_get WATCHOVER_EDGE_HOST); p=$(env_get WATCHOVER_EDGE_HTTPS_PORT || echo 443); [ "$p" = "443" ] && echo "https://$h" || echo "https://$h:$p"; }
# The edge's certificate from this installation's own CA (scripts/edge-cert.sh): issued for WATCHOVER_EDGE_HOSTS, re-issued when the
# names change or 30 days before it ends (every start / upgrade checks). Its public root ./certs/watchover-root.crt (0644) is for
# colleagues' browsers, and the receiver serves it as /agent/ca.crt, which the Agents panel's https install command pins by its
# sha256 (no manual CA copying on servers). A company certificate (./certs/watchover.crt + .key) is never touched.
edge_cert() {
  [ "$(env_get WATCHOVER_EDGE)" = "1" ] && [ "$(env_get WATCHOVER_EDGE_TLS)" = "internal" ] || return 0
  mkdir -p certs
  local before after; before=$(cksum certs/watchover.crt 2>/dev/null || true)
  sh scripts/edge-cert.sh certs "$(env_get WATCHOVER_EDGE_HOSTS | tr -d ' ')" >/dev/null || { say "WARNING: the edge certificate could not be issued (openssl missing?)"; return 1; }
  after=$(cksum certs/watchover.crt 2>/dev/null || true)
  [ "$before" = "$after" ] && return 0
  say "edge certificate issued for $(env_get WATCHOVER_EDGE_HOSTS) (CA root: ./certs/watchover-root.crt)"
  $ENGINE exec watchover-edge nginx -s reload >/dev/null 2>&1 || true
  return 2                                          # changed: the receiver re-reads the CA it serves at its next (re)start
}
# One address for the whole company network (./watchover.sh edge public NAME): the name lives on the load balancer, VPN and the
# corporate firewall decide who reaches it. What the network team sets up once, with this server's values:
lb_request() {
  local name="$1" ip p hp; ip=$(host_ip); p=$(env_get WATCHOVER_EDGE_HTTPS_PORT); p=${p:-443}; hp=$(env_get WATCHOVER_EDGE_HTTP_PORT); hp=${hp:-80}
  cat <<REQ
  For the network team, once (details: docs/KURULUM_DOCKER.md, "Şirket ağından tek adres"):
    DNS       $name  ->  the load balancer's virtual IP (VIP)
    LB        VIP:443/tcp  ->  pool member $ip:$p
              TLS passthrough, or TLS offload re-encrypting to $ip:$p with X-Forwarded-For and X-Forwarded-Proto: https
              (never plain http to $ip:$hp: that port only redirects to https)
              health monitor: HTTPS GET /healthz on $ip:$p (or HTTP GET /healthz on $ip:$hp), expect 200
              idle timeout 300 s or more (live log tail, long reports); no session persistence needed
    Firewall  VPN user subnets -> VIP:443;  the LB's self / SNAT addresses -> $ip:$p and $ip:$hp
              agents on monitored servers -> VIP:443 (https://$name) or $ip:8600 directly
    Config    ./watchover.sh edge lbconf f5|netscaler|haproxy [VIP]   (ready-to-review snippets with these values)
    Then      ./watchover.sh edge lb <the LB's SNAT addresses>   (if not given yet: real client addresses in logs)
              ./watchover.sh edge check                          (DNS, certificate, this server, through the LB)
REQ
}
valid_cidr() { printf '%s' "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$|^[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}(/[0-9]{1,3})?$'; }
pem_certs() {   # every certificate in FILE as PEM: PEM, DER (.cer) or PKCS#7 (.p7b, PEM or DER), as the company CA hands them out
  if grep -q 'BEGIN CERTIFICATE' "$1" 2>/dev/null; then sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' "$1"
  elif grep -q 'BEGIN PKCS7' "$1" 2>/dev/null; then openssl pkcs7 -in "$1" -print_certs 2>/dev/null | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p'
  elif openssl x509 -inform der -in "$1" 2>/dev/null; then :
  else openssl pkcs7 -inform der -in "$1" -print_certs 2>/dev/null | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p'; fi
}
public_name() { local n; n=$(env_get WATCHOVER_PUBLIC_URL | sed -E 's#^https?://##; s#[:/].*$##'); echo "${n:-$(env_get WATCHOVER_EDGE_HOST)}"; }
names_cert() { openssl x509 -in "$1" -noout -checkhost "$2" 2>/dev/null | grep -q 'does match'; }
edge_company() {   # the certificate the company CA issued for 'edge csr': the leaf is the one with our key, the rest is its chain
  local crt="$1" chain="$2" key=certs/.company/watchover.key tmp kp n leaf="" f name
  [ -f "$key" ] || die "no key from './watchover.sh edge csr NAME' in ./certs/.company: the certificate must be issued for that request"
  command -v openssl >/dev/null 2>&1 || die "openssl is required (RHEL: dnf install openssl)"
  tmp=$(mktemp -d); { pem_certs "$crt"; [ -z "$chain" ] || pem_certs "$chain"; } > "$tmp/all.pem"
  awk -v d="$tmp" '/-----BEGIN CERTIFICATE-----/{n++} n{print > (d "/c" sprintf("%03d", n) ".pem")}' "$tmp/all.pem"
  ls "$tmp"/c*.pem >/dev/null 2>&1 || { rm -rf "$tmp"; die "no certificate in $crt${chain:+ / $chain}"; }
  kp=$(openssl pkey -in "$key" -pubout 2>/dev/null | openssl sha256 | awk '{print $NF}')
  : > "$tmp/chain.pem"; : > "$tmp/seen"
  for f in "$tmp"/c*.pem; do
    n=$(openssl x509 -in "$f" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2); grep -qxF "$n" "$tmp/seen" && continue; echo "$n" >> "$tmp/seen"
    if [ -z "$leaf" ] && [ "$(openssl x509 -in "$f" -noout -pubkey 2>/dev/null | openssl sha256 | awk '{print $NF}')" = "$kp" ]; then leaf="$f"; else cat "$f" >> "$tmp/chain.pem"; fi
  done
  [ -n "$leaf" ] || { rm -rf "$tmp"; die "none of these certificates belongs to the key of the last 'edge csr' (issued for another request?)"; }
  openssl x509 -in "$leaf" -noout -checkend 0 >/dev/null 2>&1 || { rm -rf "$tmp"; die "the certificate has expired: $(openssl x509 -in "$leaf" -noout -enddate)"; }
  name=$(public_name)
  if [ -n "$name" ] && ! names_cert "$leaf" "$name"; then rm -rf "$tmp"; die "the certificate does not name $name (the address colleagues open); ask for it in the request: ./watchover.sh edge csr $name"; fi
  mkdir -p certs; cat "$leaf" "$tmp/chain.pem" > certs/watchover.crt.new && chmod 644 certs/watchover.crt.new
  ( umask 077; cp "$key" certs/watchover.key.new ) && chmod 600 certs/watchover.key.new
  mv -f certs/watchover.key.new certs/watchover.key; mv -f certs/watchover.crt.new certs/watchover.crt
  if [ -s "$tmp/chain.pem" ]; then cp "$tmp/chain.pem" certs/watchover-ca.crt; chmod 644 certs/watchover-ca.crt     # agents pin it (/agent/ca.crt)
  else say "WARNING: no CA chain given: a browser missing the company's intermediate CA warns; add it: ./watchover.sh edge company $crt <chain>"; fi
  rm -f certs/.ca/issued; rm -rf "$tmp"
  env_set WATCHOVER_EDGE_TLS company
  say "company certificate installed: $(openssl x509 -in certs/watchover.crt -noout -subject | sed 's/^subject= *//'), $(openssl x509 -in certs/watchover.crt -noout -enddate | sed 's/notAfter=/valid until /')"
}
# The chat model of the bundled Ollama by measured speed (src/watchover/llmbench.py): 7B measured, the 14B downloaded and measured
# only when this machine can carry it (60 GB+ RAM, predicted >= WATCHOVER_LLM_MIN_TPS tokens/s, 15 GB free), the 3B under the floor.
# Never for a connection or a model you configured yourself (LLM page settings win over .env in any case); WATCHOVER_LLM_AUTO=0: off.
llm_auto() {
  local cur mem_b mem_gb store free nopull="" out chat sum th mt ft
  [ "${WATCHOVER_LLM_AUTO:-$(env_get WATCHOVER_LLM_AUTO)}" != 0 ] || { say "llm auto off (WATCHOVER_LLM_AUTO=0): LLM_MODEL stays $(env_get LLM_MODEL)"; return 0; }
  case "$(env_get LLM_BASE_URL)" in ""|http://ollama:11434) ;; *) say "LLM connection $(env_get LLM_BASE_URL) is yours: not measured"; return 0 ;; esac
  cur=$(env_get LLM_MODEL)
  case "$cur" in ""|qwen2.5:14b-instruct|qwen2.5:7b-instruct|qwen2.5:3b-instruct) ;; *) say "LLM_MODEL=$cur was chosen by you: not changed (llm bench $cur measures it)"; return 0 ;; esac
  mem_b=$(docker info --format '{{.MemTotal}}' 2>/dev/null | tr -dc '0-9'); mem_gb=$(( (${mem_b:-0} + 536870912) / 1073741824 ))
  store=$(if [ "$ENGINE" = podman ]; then podman info --format '{{.Store.GraphRoot}}' 2>/dev/null; else docker info --format '{{.DockerRootDir}}' 2>/dev/null; fi)
  free=$(df -Pk "${store:-/}" 2>/dev/null | awk 'NR == 2 { print int($4 / 1048576) }'); [ "${free:-0}" -ge 15 ] || nopull="--no-pull"
  th=$(env_get WATCHOVER_LLM_THREADS); mt=$(env_get WATCHOVER_LLM_MIN_TPS); ft=$(env_get WATCHOVER_LLM_FLOOR_TPS)
  say "measuring the local LLM on this machine ($mem_gb GB RAM, ${th:-?} LLM threads): the 7B first, the 14B only if this machine can carry it"
  out=$($COMPOSE exec -T dashboard python -m watchover.llmbench auto --mem-gb "$mem_gb" --threads "${th:-0}" \
        --min-tps "${mt:-5}" --floor-tps "${ft:-2.5}" $nopull --shell 2>/dev/null || true)
  chat=$(printf '%s\n' "$out" | sed -n 's/^CHAT=//p' | head -1); sum=$(printf '%s\n' "$out" | sed -n 's/^SUMMARY=//p' | head -1)
  printf '%s\n' "$out" | sed -n 's/^NOTE=/      /p'
  [ -z "$nopull" ] || say "      14B not downloaded: ${free:-?} GB free on the container store (needs 15)"
  if [ -z "$chat" ]; then
    case "$out" in
      *"not downloaded"*) say "nothing to measure yet (the 7B is not downloaded): LLM_MODEL stays ${cur:-qwen2.5:7b-instruct}; later: ./watchover.sh llm pull qwen2.5:7b-instruct && ./watchover.sh llm auto" ;;
      *) say "the LLM speed could not be measured (Ollama not answering?): LLM_MODEL stays ${cur:-qwen2.5:7b-instruct}" ;;
    esac; return 0
  fi
  env_set WATCHOVER_LLM_BENCH "$sum ($(date +%Y-%m-%d))"
  [ "$chat" = "$cur" ] || env_set LLM_MODEL "$chat"
  if [ "$(env_get WATCHOVER_SIZING)" = auto ]; then size_env >/dev/null; fi        # the 14B needs a larger memory share
  [ "$chat" = "$cur" ] && [ "$chat" != qwen2.5:14b-instruct ] || $COMPOSE $(profiles) up -d --no-deps ollama dashboard >/dev/null 2>&1 || true
  say "local LLM: $chat (measured here: $sum; again: ./watchover.sh llm auto)"
}
# A rehearsal of the load balancer on this server (./watchover.sh edge lbtest [stop]): an HAProxy container built from
# 'edge lbconf haproxy' stands in front of the edge on 9443 / 9080, joins the edge's container network and is trusted for the
# test; then the checks the real LB (F5 ...) must pass: /healthz through it with both TLS legs verified, http -> https, the
# client's address reaching the edge instead of the LB's, a forged X-Forwarded-For ignored. 'stop' removes it and restores
# the trusted list. Nothing of the real setup changes.
LBTEST_IMG="${WATCHOVER_LBTEST_IMAGE:-docker.io/library/haproxy:3.2-alpine}"
edge_lbtest() {
  local d=.lbtest name net lbip ca port old code red tok line seen z fails=0
  okl() { printf '  \033[32m✓\033[0m %s\n' "$*"; }; badl() { printf '  \033[31m✗\033[0m %s\n' "$*"; fails=$((fails + 1)); }
  if [ "${1:-start}" = stop ]; then
    $ENGINE rm -f watchover-lbtest >/dev/null 2>&1 || true
    if [ -f "$d/trusted.before" ]; then
      env_set WATCHOVER_EDGE_TRUSTED "$(cat "$d/trusted.before")"; $COMPOSE $(profiles) up -d --no-deps --force-recreate edge dashboard >/dev/null 2>&1 || true
    fi
    if [ -f "$d/fw" ]; then firewall-cmd --zone="$(cat "$d/fw")" --remove-port=9443/tcp >/dev/null 2>&1 || true; fi
    rm -rf "${d:?}"; say "test load balancer removed; trusted load balancers: $(env_get WATCHOVER_EDGE_TRUSTED || true)"; return 0
  fi
  [ "$(env_get WATCHOVER_EDGE)" = 1 ] || die "the edge is off: ./watchover.sh edge public NAME first"
  name=$(public_name); [ -n "$name" ] || die "no public name yet: ./watchover.sh edge public NAME first"
  ca=certs/watchover-root.crt; [ "$(env_get WATCHOVER_EDGE_TLS)" != company ] || ca=certs/watchover-ca.crt
  [ -f "$ca" ] && [ -f certs/watchover.crt ] && [ -f certs/watchover.key ] || die "no certificate / CA in ./certs ($ca): ./watchover.sh edge public $name"
  port=$(env_get WATCHOVER_EDGE_HTTPS_PORT); port=${port:-443}
  net=$($ENGINE inspect watchover-edge --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | awk '{print $1}' || true)
  [ -n "$net" ] || die "the edge container is not running: ./watchover.sh doctor"
  $ENGINE rm -f watchover-lbtest >/dev/null 2>&1 || true
  mkdir -p "$d"; chmod 700 "$d"
  [ -f "$d/trusted.before" ] || env_get WATCHOVER_EDGE_TRUSTED > "$d/trusted.before"
  ( umask 077; cat certs/watchover.crt certs/watchover.key > "$d/front.pem" )
  { printf 'global\n    log stdout format raw local0\nresolvers podman\n    parse-resolv-conf\n    hold valid 5s\n'
    printf 'defaults\n    mode http\n    log global\n    option httplog\n    timeout connect 5s\n    timeout client 300s\n    timeout server 300s\n'
    WATCHOVER_LB_HTTPS_PORT=9443 WATCHOVER_LB_HTTP_PORT=9080 sh scripts/lb-config.sh haproxy "$name" edge "$port" 80 '*' \
      | sed 's/ check check-sni / resolvers podman init-addr last,libc,none check check-sni /'
  } > "$d/haproxy.cfg"
  say "test load balancer (HAProxy, the 'edge lbconf haproxy' configuration) in front of the edge: https://$name:9443"
  $ENGINE run -d --name watchover-lbtest --user 0 --network "$net" -p 9443:9443 -p 9080:9080 \
    -v "$PWD/$d/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro,z" -v "$PWD/$d/front.pem:/etc/haproxy/certs/$name.pem:ro,z" \
    -v "$PWD/$ca:/etc/haproxy/watchover-root.crt:ro,z" "$LBTEST_IMG" >/dev/null || die "the test load balancer did not start: $ENGINE logs watchover-lbtest"
  lbip=$($ENGINE inspect watchover-lbtest --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' 2>/dev/null | awk '{print $1}' || true)
  [ -n "$lbip" ] || die "no address for the test load balancer: $ENGINE logs watchover-lbtest"
  old=$(cat "$d/trusted.before")
  case " $old " in *" $lbip/32 "*) ;; *)
    say "trusting it for the test ($lbip/32, as 'edge lb' does for the real one): the edge and the dashboard restart"
    env_set WATCHOVER_EDGE_TRUSTED "$(echo $old "$lbip/32")"; $COMPOSE $(profiles) up -d --no-deps --force-recreate edge dashboard >/dev/null ;; esac
  for _ in $(seq 1 "${WATCHOVER_LBTEST_WAIT:-45}"); do
    code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 --cacert "$ca" --resolve "$name:9443:127.0.0.1" "https://$name:9443/healthz" 2>/dev/null || true)
    [ "$code" = 200 ] && break; sleep 2
  done
  say "checks (what the real load balancer must pass)"
  [ "$code" = 200 ] && okl "https://$name:9443/healthz through the LB -> 200 (TLS to the LB and from the LB to the edge both verified, SNI $name)" \
    || badl "https://$name:9443/healthz through the LB -> HTTP ${code:-000} ($ENGINE logs watchover-lbtest)"
  red=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 -H "Host: $name" http://127.0.0.1:9080/ 2>/dev/null || true)
  case "$red" in "301 https://"*) okl "http on the LB -> $red" ;; *) badl "http on the LB did not redirect to https: ${red:-no answer}" ;; esac
  tok="${WATCHOVER_LBTEST_TOKEN:-$(rand 6)}"
  curl -s --noproxy '*' -o /dev/null --max-time 10 --cacert "$ca" --resolve "$name:9443:127.0.0.1" -H "X-Forwarded-For: 203.0.113.66" \
    "https://$name:9443/api/health?lbtest=$tok" >/dev/null 2>&1 || true
  sleep 1
  line=$($ENGINE logs --tail 300 watchover-edge 2>&1 | grep "lbtest=$tok" | tail -1 || true); seen=$(printf '%s' "$line" | awk '{print $1}')
  if [ -z "$seen" ]; then badl "the edge did not log the test request ($ENGINE logs watchover-edge)"
  elif [ "$seen" = 203.0.113.66 ]; then badl "a forged X-Forwarded-For was believed ($seen)"
  elif [ "$seen" = "$lbip" ]; then badl "the edge logged the LB's own address ($lbip): its X-Forwarded-For was not used (edge lb)"
  else okl "the edge logs the client ($seen), not the LB ($lbip); the forged X-Forwarded-For 203.0.113.66 was ignored"; fi
  if [ "$(id -u)" = 0 ] && command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    z=$(env_get WATCHOVER_FW_ZONE); z=${z:-$(firewall-cmd --get-default-zone 2>/dev/null || echo public)}
    firewall-cmd --zone="$z" --add-port=9443/tcp >/dev/null 2>&1 && echo "$z" > "$d/fw"      # until 'lbtest stop' or a reboot
  fi
  say "from a laptop: https://$name:9443 (hosts entry: $name -> $(host_ip)); remove the rehearsal: ./watchover.sh edge lbtest stop"
  [ "$fails" = 0 ] && say "the load balancer path works: the F5 configuration (edge lbconf f5) does the same" || { say "$fails check(s) failed"; return 1; }
}
# ---------------------------------------------------------------- all: the containers plus what runs on the host
# The server's own agent (install-rhel.sh installs it) and its self-update timer are systemd units outside Compose: 'stop all' for a
# maintenance window stops them too (no self-update meanwhile), 'start all' brings back the ones that are enabled.
HOST_UNITS="watchover-agent.service watchover-actuator.service watchover-update.timer"
STACK_UNITS="watchover-stack.service watchover-backup.timer"
need_root() { [ "$(id -u)" = 0 ] || die "root gerekir: sudo watchover $*"; }
host_units() { local u; command -v systemctl >/dev/null 2>&1 || return 0; for u in $HOST_UNITS; do systemctl cat "$u" >/dev/null 2>&1 && echo "$u"; done; return 0; }
host_ctl() {   # start|stop|restart: the host agent's units (start / restart only the enabled ones)
  local u
  for u in $(host_units); do
    case "$1" in
      stop) systemctl stop "$u" >/dev/null 2>&1 && say "durduruldu: $u" || say "$u durdurulamadı" ;;
      *) systemctl is-enabled -q "$u" 2>/dev/null || continue; systemctl "$1" "$u" >/dev/null 2>&1 && say "$([ "$1" = start ] && echo başlatıldı || echo yeniden başlatıldı): $u" || say "$u: $1 başarısız (journalctl -u $u)" ;;
    esac
  done
  [ -n "$(host_units)" ] || say "sunucu ajanı kurulu değil (watchover-agent yok)"
}
unit_line() { printf '  %-28s %-9s %s\n' "$1" "$(systemctl is-enabled "$1" 2>/dev/null || echo -)" "$(systemctl is-active "$1" 2>/dev/null || true)"; }
status_all() {
  local u d hot cold b rv
  rv=$(running_version); say "Watchover v$(version)${rv:+ (çalışan: v$rv)}"
  $COMPOSE $(profiles) $(uprof) ps 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    printf '\n  %-28s %-9s %s\n' "servis" "açılışta" "durum"
    for u in $STACK_UNITS $(host_units); do systemctl cat "$u" >/dev/null 2>&1 && unit_line "$u"; done
  fi
  ! command -v rpm >/dev/null 2>&1 || printf '\n  Sunucu bileşenleri: %s\n' "$(rpm -q --qf '%{NAME} %{VERSION}\n' podman crun netavark 2>/dev/null | paste -sd, - | sed 's/,/, /g')"
  hot=$(env_get WATCHOVER_LOG_HOT_PATH); cold=$(env_get WATCHOVER_LOG_COLD_PATH)
  printf '\n  Disk\n'; for d in /var/lib/containers "$PWD" ${hot:+"$hot"} ${cold:+"$cold"}; do [ -e "$d" ] && df -hP "$d" 2>/dev/null | awk -v p="$d" 'NR == 2 {printf "    %-36s %s boş / %s (%s dolu)\n", p, $4, $2, $5}'; done
  b=$(ls -1t "$(backup_dir)"/watchover-*.tgz 2>/dev/null | head -1 || true)
  printf '  Son yedek: %s\n' "${b:+$b ($(date -r "$b" '+%Y-%m-%d %H:%M' 2>/dev/null))}${b:-yok}"
  printf '  Yerel LLM: %s\n' "$([ "$(env_get WATCHOVER_LLM)" = 1 ] && echo "açık ($(env_get LLM_MODEL))" || echo kapalı)"
  printf '  Güncellemeler: sudo watchover update all --check\n'
}
help_tr() {
  cat <<'HELPEOF'
Watchover komutları (sudo watchover …)

Günlük
  status [all]               konteynerler; all: + sunucu ajanı, servisler, disk, son yedek, LLM
  start | stop | restart all hepsi: konteynerler + sunucu ajanı + zamanlayıcılar (bakım penceresi için)
  start | stop | restart [servis]   yalnız konteynerler ya da tek servis (dashboard, receiver, edge, postgres, keyd, ollama)
  logs [servis]              servisin logları (canlı); sunucu ajanı: agent logs
  doctor [--check]           adres açılmıyor mu: kontrol eder, onarabildiğini onarır
  checklist                  devreye alma listesi

Güncelleme
  update all --check         neyin güncelleneceğini listeler, hiçbir şeyi değiştirmez
  update all [kurulum.sh]    sunucunun konteyner paketleri (Satellite) + Watchover yeni sürüm

Yedek
  backup [--no-logs]         elle yedek (gecelik yedek 02:15'te kendiliğinden)
  restore <dosya>            yedekten geri yükleme

Sunucunun kendi ajanı
  agent status | start | stop | restart | logs

Kullanıcılar
  admin-password             ilk yönetici parolası (değiştirilene kadar)
  user add <e-posta> --admin | user password <e-posta> | user unlock <e-posta> | user list

Erişim ve sertifika
  edge lb <LB IP/CIDR>       yük dengeleyiciyi güvenilir yap
  edge csr | edge company <sertifika> [zincir] | edge check | edge cert

Depolama ve kaynak
  storage show | storage set --hot <dizin> --cold <dizin> [--cold-days N]
  profile [auto|cpu8|cpu16]  CPU / bellek payları

Yerel LLM
  llm list | llm on | llm off | llm auto | llm bench <model>

HELPEOF
}

# ---------------------------------------------------------------- update all: the host's container packages, then Watchover
# Only the packages the stack runs on, from the server's own repositories (Satellite): never the kernel or anything else, never a
# reboot (a pending one is reported). The stack is stopped around the package update (a running container keeps the old runtime
# until it restarts) after a backup, then started and checked. Turkcell-style change control: --check first, in the window run it.
HOST_PKGS="podman crun conmon netavark aardvark-dns containers-common container-selinux podman-plugins slirp4netns fuse-overlayfs openssl chrony"
update_all() {
  local pkg="" check=0 have=() p out rc=0 ups
  while [ $# -gt 0 ]; do case "$1" in --check) check=1 ;; -*) die "usage: sudo watchover update all [watchover-kurulum-X.sh] [--check]" ;; *) pkg="$1" ;; esac; shift; done
  [ "$(id -u)" = 0 ] || die "root gerekir: sudo watchover update all"
  [ -z "$pkg" ] || [ -f "$pkg" ] || die "kurulum dosyası bulunamadı: $pkg"
  command -v dnf >/dev/null 2>&1 && command -v rpm >/dev/null 2>&1 || die "dnf / rpm yok: sunucu bileşenleri yalnız RHEL ailesinde güncellenir"
  busy_guard
  for p in $HOST_PKGS; do rpm -q "$p" >/dev/null 2>&1 && have+=("$p"); done
  [ "${#have[@]}" -gt 0 ] || die "Podman paketleri kurulu değil: önce sudo ./install-rhel.sh"   # an empty list would check every package
  say "Sunucu bileşenleri: $(rpm -q --qf '%{NAME} %{VERSION}-%{RELEASE}\n' podman crun netavark 2>/dev/null | paste -sd, - | sed 's/,/, /g')"
  out=$(dnf -q --refresh check-update "${have[@]}" 2>&1) || rc=$?          # 100: updates waiting, 0: none, else: no repository
  case "$rc" in
    0) ups=""; say "Sunucu bileşenleri güncel (paket deposuna göre)" ;;
    100) ups=$(printf '%s\n' "$out" | awk 'NF >= 3 && $1 ~ /\./ && $2 ~ /[0-9]/ {print "    " $1 "  ->  " $2}')
         say "Güncellenecek sunucu bileşenleri:"; printf '%s\n' "$ups" ;;
    *) die "paket deposuna ulaşılamadı (Satellite / dnf repolist): $(printf '%s' "$out" | tail -2 | tr '\n' ' ')" ;;
  esac
  if [ "$check" = 1 ]; then
    [ -z "$pkg" ] || say "Watchover: $(basename "$pkg") kurulacak (şu an v$(running_version || version))"
    say "--check: hiçbir şey değiştirilmedi"; return 0
  fi
  if [ -n "$ups" ]; then
    need_docker
    say "Yedek alınıyor (güncelleme öncesi)"; bash "$0" backup --no-logs
    say "Watchover durduruluyor (paketler güncellenirken)"; $COMPOSE $(profiles) $(uprof) stop >/dev/null 2>&1 || true
    if ! dnf -y upgrade "${have[@]}"; then
      say "paket güncellemesi başarısız: Watchover eski paketlerle yeniden başlatılıyor"; up; verify_running; die "sunucu bileşenleri güncellenemedi (dnf); ayrıntı yukarıda"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl try-restart podman.socket >/dev/null 2>&1 || true
    podman network reload --all >/dev/null 2>&1 || true
    say "Watchover başlatılıyor"; write_env; up; verify_running
    say "Sunucu bileşenleri şimdi: $(rpm -q --qf '%{NAME} %{VERSION}-%{RELEASE}\n' podman crun netavark 2>/dev/null | paste -sd, - | sed 's/,/, /g')"
  fi
  if command -v needs-restarting >/dev/null 2>&1 && ! needs-restarting -r >/dev/null 2>&1; then
    say "NOT: işletim sistemi bir yeniden başlatma bekliyor (needs-restarting -r); bakım penceresinde yapın. Watchover açılışta kendisi başlar"
  fi
  if [ -n "$pkg" ]; then say "Watchover güncelleniyor: $(basename "$pkg")"; exec bash "$pkg" --yes; fi
  if ! offline && command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then exec bash "$0" upgrade; fi
  offline && say "Watchover: yeni sürüm için kurulum dosyasını verin:  sudo watchover update all /var/tmp/watchover-kurulum-<sürüm>-x86_64-hepsi-dahil.sh"
  return 0
}

up() {   # inside the updater its own container is left alone (its profile is not active, so it is not an orphan either)
  $COMPOSE $(profiles) up -d --remove-orphans
  in_updater && return 0
  if updater_on; then $COMPOSE --profile updater up -d --no-deps updater || say "WARNING: the updater did not start (in-app updates unavailable): ./watchover.sh logs updater"; fi
  mdns_ensure
  edge_cert || [ $? -ne 2 ] || $COMPOSE $(profiles) up -d --no-deps --force-recreate receiver >/dev/null 2>&1 || true
}

# systemd (Linux servers): the stack at boot and the nightly backup; cron where there is no systemd (the backup only)
SYSTEMD_DIR="${WATCHOVER_SYSTEMD_DIR:-/etc/systemd/system}"
systemd_ok() { command -v systemctl >/dev/null 2>&1 && [ -d "${WATCHOVER_SYSTEMD_RUN:-/run/systemd/system}" ]; }
units_writable() { [ -w "$SYSTEMD_DIR" ] || die "systemd units are written to $SYSTEMD_DIR: run it with sudo (sudo ./watchover.sh $*)"; }
engine_unit() { [ "$ENGINE" = podman ] && echo podman.socket || echo docker.service; }
backup_dir() { local d; d=$(env_get WATCHOVER_BACKUP_DIR); echo "${d:-backups}"; }
autostart_state() { systemd_ok && [ -f "$SYSTEMD_DIR/watchover-stack.service" ] && systemctl is-enabled -q watchover-stack.service 2>/dev/null && echo on || echo off; }
autostart_on() {
  systemd_ok || die "no systemd on this machine: Docker Desktop starts the containers itself (restart: unless-stopped)"
  units_writable autostart on
  { printf '[Unit]\nDescription=Watchover containers (%s, %s)\nWants=network-online.target %s\nAfter=network-online.target %s\n\n' "$ENGINE" "$PWD" "$(engine_unit)" "$(engine_unit)"
    printf '[Service]\nType=oneshot\nRemainAfterExit=yes\nWorkingDirectory=%s\nEnvironment=WATCHOVER_ENGINE=%s\nExecStart="%s/watchover.sh" start\n' "$PWD" "$ENGINE" "$PWD"
    [ "$ENGINE" = podman ] && printf 'ExecStop="%s/watchover.sh" stop\n' "$PWD"     # Podman has no daemon that stops / restarts containers
    printf 'TimeoutStartSec=900\nTimeoutStopSec=180\n\n[Install]\nWantedBy=multi-user.target\n'; } > "$SYSTEMD_DIR/watchover-stack.service"
  systemctl daemon-reload; systemctl enable watchover-stack.service >/dev/null 2>&1 || systemctl enable watchover-stack.service
  say "start on boot: watchover-stack.service enabled ($ENGINE; runs ./watchover.sh start after $(engine_unit))"
}
backup_timer_state() {
  if systemd_ok && [ -f "$SYSTEMD_DIR/watchover-backup.timer" ] && systemctl is-enabled -q watchover-backup.timer 2>/dev/null; then
    echo "on (systemd, $(sed -n 's/^OnCalendar=\*-\*-\* \([0-9:]*\):00$/\1/p' "$SYSTEMD_DIR/watchover-backup.timer"))"
  elif command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q '# watchover-backup$'; then echo "on (cron)"
  else echo off; fi
}
checklist() {
  local bt as hot cold
  ck() { if [ "$1" = 1 ]; then printf '  [x] %s\n' "$2"; else printf '  [ ] %s\n' "$2"; fi; }
  bt=$(backup_timer_state); as=$(autostart_state)
  echo; say "production checklist ($ENGINE; ./watchover.sh checklist shows it again)"
  ck 1 "real data only: the simulator is retired (leftovers of an older version go at the first start, or ./watchover.sh demo-clean)"
  ck "$(case "$bt" in on*) echo 1 ;; esac)" "nightly backup: $bt  -> ./watchover.sh backup-timer on [HH:MM]; copy $(backup_dir)/ to another machine (data/.vault.key is in it)"
  if [ "$ENGINE" = podman ]; then ck "$([ "$as" = on ] && echo 1)" "start on boot: $as  -> sudo ./watchover.sh autostart on (Podman has no daemon that restarts containers)"
  else ck "$([ "$as" = on ] && echo 1 || { systemctl is-enabled -q docker.service 2>/dev/null && echo 1; })" "start on boot: Docker service enabled, containers restart: unless-stopped (optional unit: ./watchover.sh autostart on)"; fi
  ck "" "admin password: sign in as admin@watchover.local (a new password is required at once) or ./watchover.sh user password admin@watchover.local; personal admins: ./watchover.sh user add ops@firma.com --admin"
  ck "$([ -n "$(env_get WATCHOVER_LOG_HOT_PATH)" ] && [ -n "$(env_get WATCHOVER_LOG_COLD_PATH)" ] && echo 1)" "raw-log paths set by hand (hot: $(env_get WATCHOVER_LOG_HOT_PATH | grep . || echo 'not set'), archive: $(env_get WATCHOVER_LOG_COLD_PATH | grep . || echo 'not set')): ./watchover.sh storage set --hot DIR --cold DIR"
  ck "" "archive kept 365 days by default for audits (--cold-days N); size it: ./watchover.sh storage show (yearly need), alerts at 80 / 90 %, verify: Sistem › Depolama"
  ck "$([ "$(env_get WATCHOVER_EDGE)" = 1 ] && echo 1)" "TLS: ./watchover.sh edge on watchover.firma.com (company certificate: ./certs/watchover.crt + .key); then only 443 (+ 8600 for http agents) open"
  ck "$(updater_on || echo 1)" "in-app updates: $(updater_on && echo "on (the updater container holds the engine socket = root on this host; signing key: WATCHOVER_UPDATE_PUBKEY)" || echo off)  -> ./watchover.sh updater off|on"
  if [ -n "$(env_get WATCHOVER_FW_ZONE)" ]; then   # set by install-rhel.sh (RHEL 9): it opened exactly what is published
    ck 1 "firewall: firewalld zone $(env_get WATCHOVER_FW_ZONE) opened by install-rhel.sh: $([ "$(env_get WATCHOVER_EDGE)" = 1 ] && echo '443, 80' || echo 8501), 8600 (sudo ./watchover.sh mcp on adds 8765, otlp on 4317 / 4318); only some networks: a firewalld rich rule"
  else ck "" "firewall: 8501 / 8600 / 443 (and 8765 with MCP, 4317 / 4318 with OTLP) for the right networks only: sudo bash scripts/rhel-prepare.sh prints and opens them"; fi
}

banner() {
  local ip; ip=$(host_ip)
  cat <<TXT

  Watchover v$(version) is running.
    Dashboard      http://$ip:$(env_get WATCHOVER_UI_PORT || echo 8501)     (same machine: http://localhost:$(env_get WATCHOVER_UI_PORT || echo 8501); API docs at /api/docs)
    Agents post to http://$ip:$(env_get WATCHOVER_LIVE_PORT || echo 8600)/ingest
$( [ "$(env_get WATCHOVER_MCP)" = "1" ] && if [ "$(env_get WATCHOVER_EDGE)" = "1" ]; then printf '    MCP server     %s/mcp  (TLS; plain http://%s:%s/mcp)   key: Ayarlar › Bağlantılar › MCP, or MCP_API_KEY (read)\n' "$(edge_url)" "$ip" "$(env_get WATCHOVER_MCP_PORT || echo 8765)"; else printf '    MCP server     http://%s:%s/mcp   key: Ayarlar › Bağlantılar › MCP, or MCP_API_KEY in .env (read)\n' "$ip" "$(env_get WATCHOVER_MCP_PORT || echo 8765)"; fi )
$( [ "$(env_get WATCHOVER_LEGACY)" = "1" ] && printf '    Legacy (Streamlit) http://%s:%s\n' "$ip" "$(env_get WATCHOVER_LEGACY_PORT || echo 8502)" )
$( [ "$(env_get WATCHOVER_LLM)" = "1" ] && printf '    Local LLM        Ollama on the compose network (models: %s); manage with ./watchover.sh llm\n' "$(env_get WATCHOVER_LLM_MODELS || echo 'qwen2.5:7b-instruct qwen2.5:3b-instruct bge-m3')" )
$( [ "$(env_get WATCHOVER_EDGE)" = "1" ] && printf '    Company network  %s   (agents: %s/ingest; CA root for colleagues: ./watchover.sh edge cert)\n' "$(edge_url)" "$(edge_url)" )
    First sign-in: admin@watchover.local with the initial password you were given; a new password is required at once.
    Open the firewall for $(env_get WATCHOVER_UI_PORT || echo 8501) and $(env_get WATCHOVER_LIVE_PORT || echo 8600) if the server has one (RHEL firewalld: sudo bash scripts/rhel-prepare.sh; ufw allow 8501,8600/tcp).
    Status: ./watchover.sh status     Logs: ./watchover.sh logs     Backup: ./watchover.sh backup

TXT
}

case "${1:-}" in
  install)   # --production: production mode and the go-live checklist at the end
    prod=0; for a in "${@:2}"; do case "$a" in --production|--prod) prod=1 ;; *) die "usage: ./watchover.sh install [--production]" ;; esac; done
    need_docker; write_env
    if [ "$prod" = 1 ]; then env_set WATCHOVER_PRODUCTION 1; say "production install"; fi
    build; up
    say "waiting for the containers to become healthy"
    for _ in $(seq 1 60); do
      if $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^dashboard healthy'; then break; fi; sleep 2
    done
    if [ "$ENGINE" = podman ] && systemd_ok && [ -w "$SYSTEMD_DIR" ] && [ "$(autostart_state)" = off ]; then autostart_on; fi   # no daemon restarts Podman containers at boot
    $COMPOSE ps; banner
    if [ "$prod" = 1 ] || [ "$(env_get WATCHOVER_PRODUCTION)" = 1 ]; then checklist; fi ;;
  checklist) [ -f "$ENV_FILE" ] || die "not installed yet: ./watchover.sh install --production"; checklist ;;
  storage)
    [ -f "$ENV_FILE" ] || die "not installed yet: ./watchover.sh install"
    case "${2:-show}" in
      show) storage_show ;;
      set)  busy_guard; shift 2; storage_set "$@" ;;
      *) die "usage: ./watchover.sh storage show | storage set [--hot DIR] [--cold DIR] [--hot-hours N] [--cold-days N] [--keep-old]" ;;
    esac ;;
  backup-timer)   # nightly ./watchover.sh backup --no-logs (raw logs are large; their disks have their own snapshots)
    at="${3:-$(env_get WATCHOVER_BACKUP_AT)}"; at="${at:-02:15}"
    case "${2:-}" in
      on)
        echo "$at" | grep -Eq '^([01][0-9]|2[0-3]):[0-5][0-9]$' || die "usage: ./watchover.sh backup-timer on [HH:MM]   (24-hour time, e.g. 02:15)"
        [ -f "$ENV_FILE" ] || die "not installed yet: ./watchover.sh install"
        if systemd_ok; then
          units_writable backup-timer on "$at"
          printf '[Unit]\nDescription=Watchover nightly backup (PostgreSQL dump, data volume without raw logs, .env)\nAfter=%s\n\n[Service]\nType=oneshot\nWorkingDirectory=%s\nEnvironment=WATCHOVER_ENGINE=%s\nExecStart="%s/watchover.sh" backup --no-logs\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=7\n' \
            "$(engine_unit)" "$PWD" "$ENGINE" "$PWD" > "$SYSTEMD_DIR/watchover-backup.service"
          printf '[Unit]\nDescription=Watchover nightly backup at %s\n\n[Timer]\nOnCalendar=*-*-* %s:00\nPersistent=true\nRandomizedDelaySec=300\n\n[Install]\nWantedBy=timers.target\n' "$at" "$at" > "$SYSTEMD_DIR/watchover-backup.timer"
          systemctl daemon-reload; systemctl enable --now watchover-backup.timer
          say "nightly backup at $at: watchover-backup.timer (a night missed while the server was off runs at the next boot). Next run: systemctl list-timers watchover-backup.timer · log: journalctl -u watchover-backup"
        else
          command -v crontab >/dev/null 2>&1 || die "neither systemd nor cron on this machine: schedule  cd $PWD && ./watchover.sh backup --no-logs  yourself"
          cur=$(crontab -l 2>/dev/null | grep -v '# watchover-backup$' || true)        # read first: crontab - replaces the whole table
          { [ -z "$cur" ] || echo "$cur"; echo "$((10#${at##*:})) $((10#${at%%:*})) * * * cd '$PWD' && ./watchover.sh backup --no-logs >> '$(backup_dir)/backup.log' 2>&1 # watchover-backup"; } | crontab -
          say "nightly backup at $at (crontab of $(id -un))"
        fi
        env_set WATCHOVER_BACKUP_AT "$at"; k=$(env_get WATCHOVER_BACKUP_KEEP)
        say "the newest ${k:-14} backups stay in $(backup_dir)/ (.env WATCHOVER_BACKUP_KEEP; WATCHOVER_BACKUP_DIR puts them on another disk)" ;;
      off)
        if systemd_ok && [ -f "$SYSTEMD_DIR/watchover-backup.timer" ]; then
          units_writable backup-timer off; systemctl disable --now watchover-backup.timer >/dev/null 2>&1 || true
          rm -f "$SYSTEMD_DIR/watchover-backup.timer" "$SYSTEMD_DIR/watchover-backup.service"; systemctl daemon-reload
        fi
        if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q '# watchover-backup$'; then cur=$(crontab -l | grep -v '# watchover-backup$' || true); printf '%s\n' "$cur" | crontab -; fi
        say "nightly backup off (existing backups kept in $(backup_dir)/)" ;;
      "") echo "nightly backup: $(backup_timer_state)" ;;
      *) die "usage: ./watchover.sh backup-timer on [HH:MM] | off" ;; esac ;;
  autostart)
    case "${2:-}" in
      on)  autostart_on ;;
      off) if systemd_ok && [ -f "$SYSTEMD_DIR/watchover-stack.service" ]; then units_writable autostart off; systemctl disable watchover-stack.service >/dev/null 2>&1 || true; rm -f "$SYSTEMD_DIR/watchover-stack.service"; systemctl daemon-reload; fi
           say "start on boot off (running containers untouched)" ;;
      "") echo "start on boot: $(autostart_state)" ;;
      *) die "usage: ./watchover.sh autostart on|off" ;; esac ;;
  update)
    if [ "${2:-}" = all ]; then shift 2; update_all "$@"; exit 0; fi
    busy_guard; need_docker; before=$(running_version); write_env; build; up; docker image prune -f >/dev/null
    say "updated ${before:+from v$before }to v$(version); data volumes untouched"; $COMPOSE ps; verify_running ;;
  upgrade)   # fetch the new version into this folder, then update. Local edits to tracked files (the reason a plain 'git pull' refuses) are
             # stashed and re-applied; a folder with its own commits is left for a manual merge.
    busy_guard; need_docker
    command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout: copy the new source tree (or ./watchover.sh unbundle) over this folder and run ./watchover.sh update"
    offline && die "offline install: ./watchover.sh unbundle <new bundle>, then ./watchover.sh update"
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"; [ -n "$branch" ] && [ "$branch" != "HEAD" ] || branch=main
    say "fetching origin/$branch"; git fetch -q origin "$branch" || die "git fetch failed (network, proxy or credentials)"
    stashed=0
    if ! git diff --quiet || ! git diff --cached --quiet; then
      say "local edits to tracked files are set aside (git stash) and put back after the pull:"; git status --short | sed 's/^/      /'
      git -c user.name=watchover -c user.email=watchover@localhost stash push -q -m "watchover upgrade $(date +%Y%m%d-%H%M)" && stashed=1
    fi
    if ! git merge -q --ff-only "origin/$branch" 2>/dev/null; then
      [ "$stashed" = 1 ] && git stash pop -q 2>/dev/null || true
      die "this folder has commits of its own (or diverged); merge by hand:  git pull origin $branch   then  ./watchover.sh update"
    fi
    if [ "$stashed" = 1 ]; then git stash pop -q 2>/dev/null || say "WARNING: a local edit conflicts with the new version; it stays in 'git stash list' (git stash show -p)"; fi
    say "source tree now v$(version) ($(git_rev)); rebuilding"
    exec bash "$0" update ;;
  start)
    if [ "${2:-}" = all ]; then need_root start all; need_docker; write_env; up; host_ctl start; status_all; exit 0; fi
    need_docker; write_env; up; $COMPOSE ps ;;
  doctor)  need_docker; doctor "${2:-}" ;;
  stop)
    if [ "${2:-}" = all ]; then need_root stop all; host_ctl stop; need_docker; $COMPOSE $(profiles) $(uprof) stop; say "hepsi durduruldu (açılışta yine başlar; başlatmak: sudo watchover start all)"; exit 0; fi
    need_docker; $COMPOSE $(profiles) $(uprof) stop ;;
  restart)
    if [ "${2:-}" = all ]; then need_root restart all; bash "$0" stop all; exec bash "$0" start all; fi
    need_docker; $COMPOSE $(profiles) restart "${2:-}" ;;
  status)
    if [ "${2:-}" = all ]; then need_docker; status_all; exit 0; fi
    need_docker; $COMPOSE $(profiles) $(uprof) ps; docker stats --no-stream $($COMPOSE $(profiles) $(uprof) ps -q) 2>/dev/null || true ;;
  agent)   # the server's own wo-agent (systemd), installed by install-rhel.sh
    case "${2:-status}" in
      status) for u in $(host_units); do unit_line "$u"; done; [ -n "$(host_units)" ] || say "sunucu ajanı kurulu değil" ;;
      start|stop|restart) need_root agent "$2"; host_ctl "$2" ;;
      logs) exec journalctl -u watchover-agent -n 200 -f ;;
      *) die "usage: sudo watchover agent status|start|stop|restart|logs" ;;
    esac ;;
  help|yardim|yardım) help_tr; printf '\nTüm komutlar ve seçenekleri:\n\n'; awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0" ;;
  --help-all|-h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0" ;;
  logs)    need_docker; $COMPOSE $(profiles) $(uprof) logs -f --tail 200 "${2:-dashboard}" ;;
  build)   # the images of this tree's version, nothing started; the updater runs it in a new release's tree (WATCHOVER_ENV_FILE = the install's .env)
    need_docker; export WATCHOVER_IMAGE=watchover WATCHOVER_TAG="$(version)"; build; say "images of v$(version) ready (nothing restarted)" ;;
  images)  # exit 0 when the dashboard and receiver images of VERSION (default: this tree) are there
    need_docker; v="${2:-$(version)}"; have_image "watchover:$v" && have_image "watchover-receiver:$v" ;;
  images-rm)   # the images of a release the updater no longer keeps; the engine refuses an image a container still uses
    need_docker; [ -n "${2:-}" ] && [ "$2" != "$(version)" ] || die "usage: ./watchover.sh images-rm VERSION   (never the version of this folder)"
    for i in watchover watchover-receiver watchover-trainer watchover-postgres watchover-ollama; do docker image rm "$i:$2" >/dev/null 2>&1 && say "removed $i:$2" || true; done ;;
  health)  # after a start: wait until the dashboard answers /api/health with VERSION; exit 1 after SECONDS
    need_docker; wait_health "${2:-$(version)}" "${3:-180}" || exit 1 ;;
  updater)   # in-app updates: the updater container is the only one with the engine socket; off = no socket in any container
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_UPDATER 1; write_env
           have_image "watchover-updater:$UPDATER_TAG" || { offline && die "offline install: the updater image comes with a bundle made by a release that has the updater"; say "building the updater image"; build_img updater; }
           $COMPOSE --profile updater up -d --no-deps updater
           say "in-app updates on: Sistem › Güncelleme (upload a zip or a bundle). The updater holds the engine socket (= root on this host); a signing key makes it accept only signed packages: WATCHOVER_UPDATE_PUBKEY in .env" ;;
      off) $COMPOSE --profile updater stop updater >/dev/null 2>&1 || true; $COMPOSE --profile updater rm -f updater >/dev/null 2>&1 || true; env_set WATCHOVER_UPDATER 0
           say "in-app updates off: no container holds the engine socket now (kept releases stay in .releases/; ./watchover.sh updater on brings it back)" ;;
      status|"")
           echo "in-app updater: $(updater_on && echo on || echo off)   engine socket: $(env_get WATCHOVER_ENGINE_SOCK)"
           updater_on && { $COMPOSE --profile updater ps updater 2>/dev/null || true; }
           if [ -f .releases/index.json ]; then sed -n -e 's/^ *"current": *"\([^"]*\)".*/  current: \1/p' -e 's/^ *"previous": *"\([^"]*\)".*/  previous: \1/p' .releases/index.json | head -2; ls -1 .releases | grep -v -e '^index.json$' -e '^\.' | sed 's/^/  kept: /' || true; fi ;;
      *) die "usage: ./watchover.sh updater on|off|status" ;; esac ;;
  mcp)
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_MCP 1; up; mp=$(env_get WATCHOVER_MCP_PORT); fw_ports add "${mp:-8765}"; say "MCP server on: http://$(host_ip):$(env_get WATCHOVER_MCP_PORT || echo 8765)/mcp  key: $(env_get MCP_API_KEY)" ;;
      off) $COMPOSE --profile mcp stop mcp; $COMPOSE --profile mcp rm -f mcp; env_set WATCHOVER_MCP 0; mp=$(env_get WATCHOVER_MCP_PORT); fw_ports remove "${mp:-8765}"; say "MCP server off" ;;
      *) die "usage: ./watchover.sh mcp on|off" ;; esac ;;
  otlp)   # OpenTelemetry: the receiver always speaks OTLP; "on" publishes 4317 / 4318 (off by default: a host collector may own them)
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_OTLP 1; compose_files; up
           og=$(env_get WATCHOVER_OTLP_GRPC_PORT); oh=$(env_get WATCHOVER_OTLP_HTTP_PORT); fw_ports add "${og:-4317}" "${oh:-4318}"
           say "OTLP on: gRPC http://$(host_ip):$(env_get WATCHOVER_OTLP_GRPC_PORT || echo 4317)  HTTP http://$(host_ip):$(env_get WATCHOVER_OTLP_HTTP_PORT || echo 4318)/v1/{logs,traces,metrics}"
           say "exporter header: Authorization: Bearer <agent token> (Ajanlar panelinde bir ajan olarak kaydet; açık modda gerekmez)" ;;
      off) env_set WATCHOVER_OTLP 0; compose_files; up; og=$(env_get WATCHOVER_OTLP_GRPC_PORT); oh=$(env_get WATCHOVER_OTLP_HTTP_PORT); fw_ports remove "${og:-4317}" "${oh:-4318}"; say "OTLP ports closed (OTLP/HTTP stays on :$(env_get WATCHOVER_LIVE_PORT || echo 8600)/v1/*)" ;;
      *) die "usage: ./watchover.sh otlp on|off" ;; esac ;;
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
        if offline; then
          have_image "watchover-ollama:$(version)" || { env_set WATCHOVER_LLM 0; die "offline install and image watchover-ollama:$(version) is missing: run 'llm on' on the internet machine before 'bundle'"; }
          have_image "watchover-trainer:$(version)" && say "offline install: using the loaded trainer image" || { env_set WATCHOVER_LLM 0; die "offline install and image watchover-trainer:$(version) is missing: run 'llm on' on the internet machine before 'bundle'"; }
        else
          say "building the trainer image (torch CPU, transformers, peft; first time a few minutes)"; build_img trainer
          say "building the Ollama image (the release binary rebuilt with current Go and patched modules; a few minutes)"; build_img ollama
        fi
        up
        say "waiting for the Ollama service to become healthy"
        for _ in $(seq 1 60); do $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^ollama healthy' && break; sleep 3; done
        have="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1{print $1}')"
        for m in $models; do
          if echo "$have" | grep -qx "$m"; then say "$m already downloaded"; continue; fi
          if offline; then say "$m is not in the bundle (skipping the download: offline install); add it on the internet machine with 'llm pull $m' and bundle again"; continue; fi
          say "pulling $m (CPU inference; the 7B model is a few GB, first time is slow)"; $COMPOSE exec -T ollama ollama pull "$m" || say "could not pull $m (check internet / disk)"
        done
        $COMPOSE exec -T ollama ollama list || true
        llm_auto
        say "bundled Ollama on (chat: $(env_get LLM_MODEL), embeddings: bge-m3; speed here: $(s=$(env_get WATCHOVER_LLM_BENCH || true); echo "${s:-not measured}")). Models to download: .env WATCHOVER_LLM_MODELS" ;;
      auto)   # measure again and pick the chat model this machine carries (14B / 7B / 3B); a model you chose yourself stays
        WATCHOVER_LLM_AUTO=1 llm_auto ;;
      bench)  # tokens/s of one model on this machine (generation and prompt reading), with the LLM container's CPU share
        $COMPOSE exec -T dashboard python -m watchover.llmbench bench --model "${3:-$(env_get LLM_MODEL)}" --threads "$(env_get WATCHOVER_LLM_THREADS)" ;;
      use)   # switch the dashboard's chat model (any model your configured Ollama serves, e.g. watchover-ops); nothing else changes
        [ -n "${3:-}" ] || die "usage: ./watchover.sh llm use <model>"
        env_set LLM_MODEL "$3"; $COMPOSE $(profiles) up -d --no-deps dashboard; say "LLM_MODEL=$3 (switch back any time with llm use <model> or in LLM › Connection)" ;;
      pull)
        [ -n "${3:-}" ] || die "usage: ./watchover.sh llm pull <model>"
        $COMPOSE exec -T ollama ollama pull "$3" ;;
      list) $COMPOSE exec -T ollama ollama list ;;
      prepare)   # download the training base weights into the watchover-hf volume now (otherwise the first training does it); needed before 'bundle'
        base="${3:-$(env_get WATCHOVER_TRAIN_BASE)}"; base="${base:-qwen2.5:1.5b-instruct}"
        offline && die "offline install: the base weights come with the bundle (run 'llm prepare' on the internet machine, then 'bundle')"
        say "caching the base weights of $base for the trainer (Hugging Face, once; a few GB)"
        $COMPOSE --profile llm run --rm --no-deps -T trainer python -c "from watchover.training import HF_BASE; from huggingface_hub import snapshot_download; import sys; b=sys.argv[1]; p=snapshot_download(HF_BASE.get(b, b)); print('cached', HF_BASE.get(b, b), '->', p)" "$base" ;;
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
      *) die "usage: ./watchover.sh llm on|off|auto|bench [model]|use <model>|pull <model>|list|prepare [base]|export [file]|import <dir> [name]" ;; esac ;;
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
        env_set WATCHOVER_EDGE_SERVER_NAMES "$(echo "$hosts" | tr ',' ' ')"
        mdns_start "$hosts"
        [ -n "$(env_get WATCHOVER_EDGE_TLS)" ] || env_set WATCHOVER_EDGE_TLS internal
        [ -n "$(env_get WATCHOVER_EDGE_HTTPS_PORT)" ] || env_set WATCHOVER_EDGE_HTTPS_PORT 443
        [ -n "$(env_get WATCHOVER_EDGE_HTTP_PORT)" ] || env_set WATCHOVER_EDGE_HTTP_PORT 80
        if [ -f certs/watchover.crt ] && [ -f certs/watchover.key ] && [ ! -f certs/.ca/issued ]; then
          env_set WATCHOVER_EDGE_TLS company; say "company certificate found in ./certs"
        else
          env_set WATCHOVER_EDGE_TLS internal
        fi
        mkdir -p certs; edge_cert || [ $? -eq 2 ] || die "the edge needs a certificate: install openssl (RHEL: sudo dnf install openssl), or place watchover.crt + watchover.key in ./certs"
        compose_files                                                          # + docker-compose.edge.yml: plain http ports not published while the edge is on
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard receiver edge
        say "edge on: $(edge_url)   (http://$(env_get WATCHOVER_EDGE_HOST) redirects; plain :8501 is no longer published; health check: /healthz)"
        [ "$(env_get WATCHOVER_EDGE_TLS)" = "internal" ] && say "colleagues install the CA root once: ./certs/watchover-root.crt (agents: the Agents panel's https command pins it by itself)"
        [ -n "$(env_get WATCHOVER_EDGE_TRUSTED)" ] || say "behind a load balancer: ./watchover.sh edge lb <its address or CIDR> (then the real client address is logged)" ;;
      off)
        $COMPOSE --profile edge stop edge; $COMPOSE --profile edge rm -f edge; env_set WATCHOVER_EDGE 0; mdns_stop
        compose_files
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard; say "edge off: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)" ;;
      cert)
        edge_cert || true
        [ -f certs/watchover-root.crt ] || die "no CA root yet: ./watchover.sh edge on NAME first (a company certificate brings its own CA)"
        say "CA root: ./certs/watchover-root.crt  sha256 $(openssl x509 -in certs/watchover-root.crt -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
        say "macOS: Keychain Access › System › import, set to Always Trust; Windows: certutil -addstore -f Root watchover-root.crt; RHEL: cp to /etc/pki/ca-trust/source/anchors/ && update-ca-trust; Linux agents: --ca watchover-root.crt" ;;
      lb)   # the load balancer(s) in front: only from these addresses is X-Forwarded-For believed (the client address in logs / sign-in records)
        shift 2; [ $# -gt 0 ] || { say "trusted load balancers: $(env_get WATCHOVER_EDGE_TRUSTED || true)"; say "usage: ./watchover.sh edge lb 10.0.0.5/32 [10.0.1.0/24 ...] | off"; exit 0; }
        if [ "$1" = off ]; then env_set WATCHOVER_EDGE_TRUSTED ""; else
          for c in "$@"; do valid_cidr "$c" || die "not an address / CIDR: $c"; done
          env_set WATCHOVER_EDGE_TRUSTED "$*"; fi
        $COMPOSE $(profiles) up -d --no-deps --force-recreate edge dashboard      # the dashboard too: it never throttles sign-ins by an LB's address
        say "trusted load balancers: ${*}   (health check for the load balancer: GET https://<this server>/healthz, or http://…/healthz on port $(env_get WATCHOVER_EDGE_HTTP_PORT || echo 80))" ;;
      public)   # the one address everyone on the company network opens (VPN + firewall decide who reaches the load balancer)
        name="${3:-}"
        [ -n "$name" ] || die "usage: ./watchover.sh edge public NAME [LB address/CIDR ...]   e.g. edge public watchover.sirket.com.tr 10.20.0.10/32"
        case "$name" in *[!A-Za-z0-9.-]*|.*|*.|*..*|-*) die "not a host name: $name" ;; esac
        shift 3
        for c in "$@"; do valid_cidr "$c" || die "not an address / CIDR: $c"; done
        hosts="$name"; for h in $(env_get WATCHOVER_EDGE_HOSTS | tr ',' ' '); do [ "$h" = "$name" ] || hosts="$hosts,$h"; done
        env_set WATCHOVER_PUBLIC_URL "https://$name"            # sign-in callbacks and the web / mobile sensor snippet use it
        [ $# -eq 0 ] || env_set WATCHOVER_EDGE_TRUSTED "$*"
        bash "$PWD/$(basename "$0")" edge on "$hosts" || exit 1
        if [ "$(env_get WATCHOVER_EDGE_TLS)" = company ]; then
          names_cert certs/watchover.crt "$name" || say "WARNING: the company certificate in ./certs does not name $name: ./watchover.sh edge csr $name, then edge company <issued certificate> [chain]"
        else
          say "the certificate is from this install's own CA: a browser trusts it only after ./certs/watchover-root.crt is installed on it."
          say "for every colleague without that step, a company certificate: ./watchover.sh edge csr $name -> the PKI team -> ./watchover.sh edge company <issued> [chain]"
        fi
        lb_request "$name" ;;
      csr)   # a key and a certificate request for the company CA (AD CS / PKI team): company machines already trust what it issues
        name="${3:-$(public_name)}"; [ -n "$name" ] || die "usage: ./watchover.sh edge csr NAME"
        case "$name" in *[!A-Za-z0-9.-]*|.*|*.|*..*|-*) die "not a host name: $name" ;; esac
        command -v openssl >/dev/null 2>&1 || die "openssl is required (RHEL: dnf install openssl)"
        san="DNS.1 = $name"; i=1; j=0; list="$name"
        for h in $(env_get WATCHOVER_EDGE_HOSTS | tr ',' ' '); do
          case "$h" in "$name"|localhost|127.*|::1) continue ;; esac
          list="$list, $h"
          if printf '%s' "$h" | grep -Eq '^[0-9]+(\.[0-9]+){3}$|:'; then j=$((j + 1)); san="$san
IP.$j = $h"; else i=$((i + 1)); san="$san
DNS.$i = $h"; fi
        done
        mkdir -p certs/.company; chmod 700 certs/.company; cnf=$(mktemp)
        printf '[req]\ndistinguished_name = dn\nprompt = no\nreq_extensions = ext\n[dn]\nO = Watchover\nCN = %s\n[ext]\nkeyUsage = critical, digitalSignature, keyEncipherment\nextendedKeyUsage = serverAuth\nsubjectAltName = @alt\n[alt]\n%s\n' "$name" "$san" > "$cnf"
        if ! ( umask 077; openssl req -new -newkey rsa:3072 -nodes -sha256 -keyout certs/.company/watchover.key -config "$cnf" -out certs/watchover.csr ) >/dev/null 2>&1; then
          rm -f "$cnf"; die "the request could not be made (openssl)"; fi
        rm -f "$cnf"; chmod 600 certs/.company/watchover.key; chmod 644 certs/watchover.csr
        say "certificate request: ./certs/watchover.csr   (names: $list)"
        say "its key stays on this server: ./certs/.company/watchover.key (0600; never send it)"
        say "send the .csr to the PKI team (Microsoft AD CS: the 'Web Server' template); then: ./watchover.sh edge company <issued .cer/.crt> [CA chain .p7b/.cer]" ;;
      company)   # install what the company CA issued for 'edge csr' (PEM or DER, the chain as PEM, DER or PKCS#7)
        [ -f "${3:-}" ] || die "usage: ./watchover.sh edge company ISSUED.cer [CHAIN.p7b|CA.cer]   (after ./watchover.sh edge csr NAME)"
        [ -z "${4:-}" ] || [ -f "$4" ] || die "no such file: $4"
        edge_company "$3" "${4:-}"
        if [ "$(env_get WATCHOVER_EDGE)" = 1 ]; then $COMPOSE $(profiles) up -d --no-deps --force-recreate edge receiver; say "edge restarted with it: $(edge_url)"; fi ;;
      lbconf)   # the load balancer's configuration for the public name with this server's values (F5 BIG-IP, NetScaler, HAProxy)
        name=$(public_name); [ -n "$name" ] || die "no public name yet: ./watchover.sh edge public NAME first"
        p=$(env_get WATCHOVER_EDGE_HTTPS_PORT); hp=$(env_get WATCHOVER_EDGE_HTTP_PORT)
        sh scripts/lb-config.sh "${3:-all}" "$name" "$(host_ip)" "${p:-443}" "${hp:-80}" "${4:-<VIP>}" ;;
      tls)   # the TLS versions the edge speaks: 1.2 = TLS 1.2 + 1.3 (default; older load balancers and clients), 1.3 = TLS 1.3 only
        v="${3:-}"; [ -n "$v" ] || { say "TLS: $( [ "$(env_get WATCHOVER_EDGE_TLS_MIN)" = 1.3 ] && echo '1.3 only' || echo '1.2 + 1.3')"; say "usage: ./watchover.sh edge tls 1.2|1.3"; exit 0; }
        case "$v" in 1.2|1.3) ;; *) die "usage: ./watchover.sh edge tls 1.2|1.3   (1.2 = TLS 1.2 + 1.3, 1.3 = TLS 1.3 only)" ;; esac
        env_set WATCHOVER_EDGE_TLS_MIN "$v"
        [ "$(env_get WATCHOVER_EDGE)" = 1 ] && $COMPOSE $(profiles) up -d --no-deps --force-recreate edge >/dev/null
        [ "$v" = 1.3 ] && say "TLS 1.3 only. The load balancer's server-side profile must speak TLS 1.3 too (F5 BIG-IP: server-ssl without no-tlsv1.3), else its health check fails: ./watchover.sh edge lbtest" \
          || say "TLS 1.2 + 1.3 (TLS 1.2 with forward-secret AEAD suites only)" ;;
      lbtest)   # rehearse the load balancer here: a test HAProxy in front of the edge (9443 / 9080) and the checks the real LB must pass
        edge_lbtest "${3:-start}" ;;
      check)   # is https://NAME ready for colleagues: in DNS, on the certificate, answering here and through the load balancer
        name="${3:-$(public_name)}"; [ -n "$name" ] || die "usage: ./watchover.sh edge check [NAME]"
        p=$(env_get WATCHOVER_EDGE_HTTPS_PORT); p=${p:-443}; fails=0
        okl() { printf '  \033[32m✓\033[0m %s\n' "$*"; }; badl() { printf '  \033[31m✗\033[0m %s\n' "$*"; fails=$((fails + 1)); }; notel() { printf '  \033[33m!\033[0m %s\n' "$*"; }
        say "edge check: https://$name"
        if [ ! -f certs/watchover.crt ]; then badl "no certificate in ./certs (./watchover.sh edge public $name)"
        elif names_cert certs/watchover.crt "$name"; then okl "certificate ($(env_get WATCHOVER_EDGE_TLS)) names $name, $(openssl x509 -in certs/watchover.crt -noout -enddate | sed 's/notAfter=/valid until /')"
        else badl "the certificate does not name $name: ./watchover.sh edge public $name (own CA) or edge csr $name (company CA)"; fi
        code=$(curl -sk --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$name:$p:127.0.0.1" "https://$name:$p/healthz" 2>/dev/null || true); code=${code:-000}
        [ "$code" = 200 ] && okl "this server answers https://$name:$p/healthz" || badl "this server: https://$name:$p/healthz -> HTTP $code (./watchover.sh doctor)"
        if command -v openssl >/dev/null 2>&1; then   # the versions this server speaks: 1.3 always, 1.2 unless edge tls 1.3, never 1.0 / 1.1
          tls_ok() { echo | openssl s_client -connect "127.0.0.1:$p" -servername "$name" "$@" 2>/dev/null | grep -q '^ *Protocol *: *TLSv1\.[0-9]\|^New, TLSv1\.[0-9]'; }
          tls_ok -tls1_3 && okl "TLS 1.3 accepted" || badl "TLS 1.3 not accepted on :$p"
          if [ "$(env_get WATCHOVER_EDGE_TLS_MIN)" = 1.3 ]; then tls_ok -tls1_2 && badl "TLS 1.2 still accepted although edge tls 1.3 is set (recreate: ./watchover.sh edge tls 1.3)" || okl "TLS 1.2 refused (edge tls 1.3)"
          else tls_ok -tls1_2 && okl "TLS 1.2 accepted (strong suites only; TLS 1.3 only: ./watchover.sh edge tls 1.3)" || badl "TLS 1.2 not accepted on :$p"; fi
          for v in tls1 tls1_1; do tls_ok "-$v" && badl "an old protocol ($v) is accepted on :$p" || true; done
        fi
        addr=$({ getent hosts "$name" 2>/dev/null || true; } | awk '{print $1; exit}')
        if [ -z "$addr" ]; then badl "DNS: $name does not resolve (yet): an A record pointing to the load balancer's VIP"
        else
          okl "DNS: $name -> $addr"
          code=$(curl -sk --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 8 "https://$name/healthz" 2>/dev/null || true); code=${code:-000}
          [ "$code" = 200 ] && okl "through $addr (the load balancer): https://$name/healthz answers" \
            || notel "through $addr: HTTP $code. Some load balancers do not let a pool member reach its own VIP: try https://$name from a VPN laptop"
        fi
        [ -n "$(env_get WATCHOVER_EDGE_TRUSTED)" ] && okl "trusted load balancers: $(env_get WATCHOVER_EDGE_TRUSTED)" \
          || notel "no load balancer trusted yet: ./watchover.sh edge lb <its SNAT addresses> (real client addresses in logs)"
        [ "$fails" = 0 ] && say "ready: colleagues on the company network open https://$name" || { say "$fails problem(s) above"; exit 1; } ;;
      *) die "usage: ./watchover.sh edge on [name[,name2,ip]] | off | cert | lb CIDR... | public NAME [LB CIDR...] | csr [NAME] | company ISSUED [CHAIN] | lbconf [f5|netscaler|haproxy|all] [VIP] | lbtest [stop] | tls 1.2|1.3 | check [NAME]     e.g.  edge on watchover.local" ;; esac ;;
  api)     say "the API now runs inside the dashboard container: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)/api/docs" ;;
  backup)   # --no-logs: without the raw logs under /data/logs (the nightly timer's choice: they are large); on disks of their own they are never in it
    need_docker; keep="${WATCHOVER_BACKUP_KEEP:-$(env_get WATCHOVER_BACKUP_KEEP)}"; keep="${keep:-14}"; logs=1; out=""
    shift; while [ $# -gt 0 ]; do case "$1" in --keep) keep="${2:-14}"; shift ;; --no-logs) logs=0 ;; --out) out="${2:-}"; [ -n "$out" ] || die "--out needs a file"; shift ;; *) die "usage: ./watchover.sh backup [--keep N] [--no-logs] [--out FILE]" ;; esac; shift; done
    bdir=$(backup_dir); stamp=$(date +%Y%m%d-%H%M); fixed="$out"; [ -n "$out" ] || out="$bdir/watchover-$stamp.tgz"; mkdir -p "$(dirname "$out")"; tmp=$(mktemp -d)
    $COMPOSE exec -T postgres pg_dump -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)" | gzip > "$tmp/db.sql.gz"
    excl=""; [ "$logs" = 0 ] && excl="--exclude=./logs"
    docker run --rm -v "$(vol_name watchover-data)":/data -v "$tmp":/out:z "$ALPINE" tar czf /out/data.tgz $excl -C /data .
    printf 'version=%s\nengine=%s\nlogs=%s\n' "$(version)" "$ENGINE" "$logs" > "$tmp/MANIFEST"
    cp "$ENV_FILE" "$tmp/env"; [ -d certs ] && tar czf "$tmp/certs.tgz" certs 2>/dev/null || true   # the edge CA's key and certificates
    tar czf "$out" -C "$tmp" .; rm -rf "$tmp"; chmod 600 "$out"; say "backup written: $out ($(du -h "$out" | cut -f1))"
    [ -n "$fixed" ] || ls -1t "$bdir"/watchover-*.tgz 2>/dev/null | tail -n +"$((keep + 1))" | while read -r old; do rm -f "$old"; say "old backup removed: $old"; done ;;   # --out (the updater's pre-update backup) prunes nothing
  restore)   # a backup made with --no-logs leaves the raw logs in place
    busy_guard; need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh restore backups/watchover-….tgz"; write_env; tmp=$(mktemp -d); tar xzf "$2" -C "$tmp"
    wipe='rm -rf /data/*'; grep -q '^logs=0' "$tmp/MANIFEST" 2>/dev/null && wipe='find /data -mindepth 1 -maxdepth 1 ! -name logs -exec rm -rf {} +'
    $COMPOSE $(profiles) stop dashboard mcp legacy 2>/dev/null || $COMPOSE stop dashboard
    $COMPOSE up -d postgres; sleep 5
    say "restoring the database"; $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" "$(env_get POSTGRES_DB || echo watchover)"
    gunzip -c "$tmp/db.sql.gz" | $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)"
    say "restoring settings, secrets and snapshots"; docker run --rm -v "$(vol_name watchover-data)":/data -v "$tmp":/in:z "$ALPINE" sh -c "$wipe && tar xzf /in/data.tgz -C /data"
    if [ -f "$tmp/certs.tgz" ] && [ ! -f certs/.ca/watchover-ca.key ]; then tar xzf "$tmp/certs.tgz"; say "edge CA and certificates restored to ./certs"; fi
    rm -rf "$tmp"; up; say "restored from $2"; $COMPOSE ps ;;
  user)    need_docker; shift; $COMPOSE exec dashboard python -m watchover.useradmin "$@" ;;
  admin-password)   # the built-in administrator's first password (random per installation; only until it is changed at the first sign-in)
    need_docker; $COMPOSE exec -T dashboard python -m watchover.useradmin initial-password ;;
  profile)   # fixed caps for a known machine (WATCHOVER_SIZING=manual keeps them); the LLM gets a share, never every CPU, so a question
             # never slows the dashboard. An LLM connection you configured (LLM page / .env) is never changed.
    keys="WATCHOVER_SIZING WATCHOVER_CPUS WATCHOVER_MEMORY WATCHOVER_RECEIVER_CPUS WATCHOVER_RECEIVER_MEMORY WATCHOVER_PARSE_WORKERS WATCHOVER_SPOOL_MAX_MB WATCHOVER_PG_CPUS WATCHOVER_PG_MEMORY WATCHOVER_PG_SHARED_BUFFERS WATCHOVER_PG_CACHE WATCHOVER_LLM_CPUS WATCHOVER_LLM_THREADS WATCHOVER_LLM_MEMORY WATCHOVER_LLM_PARALLEL WATCHOVER_LLM_KEEPALIVE WATCHOVER_LLM_MODELS WATCHOVER_TRAIN_CPUS WATCHOVER_TRAIN_THREADS WATCHOVER_TRAIN_MEMORY"
    case "${2:-}" in
      cpu8|cpu16)
        write_env; pname="$2"
        if [ "$pname" = cpu8 ]; then
          set -- 6 16g 2 2g 3 4096 2 8g 2GB 32GB 4 4 10g 4 4 12g     # LLM: half the CPUs (v1.59)
        else
          set -- 10 24g 4 2g 6 6144 4 12g 4GB 48GB 8 8 16g 8 8 16g
        fi
        env_set WATCHOVER_SIZING manual
        env_set WATCHOVER_CPUS "$1"; env_set WATCHOVER_MEMORY "$2"; env_set WATCHOVER_RECEIVER_CPUS "$3"; env_set WATCHOVER_RECEIVER_MEMORY "$4"
        env_set WATCHOVER_PARSE_WORKERS "$5"; env_set WATCHOVER_SPOOL_MAX_MB "$6"; env_set WATCHOVER_PG_CPUS "$7"; env_set WATCHOVER_PG_MEMORY "$8"
        env_set WATCHOVER_PG_SHARED_BUFFERS "$9"; env_set WATCHOVER_PG_CACHE "${10}"; env_set WATCHOVER_LLM_CPUS "${11}"; env_set WATCHOVER_LLM_THREADS "${12}"
        env_set WATCHOVER_LLM_MEMORY "${13}"; env_set WATCHOVER_LLM_PARALLEL 1; env_set WATCHOVER_LLM_KEEPALIVE 24h
        env_set WATCHOVER_TRAIN_CPUS "${14}"; env_set WATCHOVER_TRAIN_THREADS "${15}"; env_set WATCHOVER_TRAIN_MEMORY "${16}"
        env_set WATCHOVER_LLM_MODELS "qwen2.5:3b-instruct qwen2.5:7b-instruct bge-m3"
        if [ -z "$(env_get LLM_BASE_URL)" ] && [ -z "$(env_get LLM_MODEL)" ]; then env_set LLM_MODEL "qwen2.5:3b-instruct"; fi
        say "profile $pname written to .env; apply it with: ./watchover.sh start (the LLM, if on, restarts with its share)"
        say "training runs only inside the window you set on the LLM page (Kalite > Eğitim takvimi)" ;;
      auto)
        write_env; env_set WATCHOVER_SIZING auto; size_env; say "sizing follows the machine again; apply with ./watchover.sh start" ;;
      "")
        for k in $keys; do printf '  %-28s %s\n' "$k" "$(env_get "$k")"; done ;;
      *) die "usage: ./watchover.sh profile [cpu8|cpu16|auto]" ;;
    esac ;;
  demo-clean)   # what the retired simulator left (watchover.demo.purge; also runs at the first start): lines, history, derived rows, LLM memory, demo-trained models; real data stays
    need_docker; shift; dry=0; yes=0
    for a in "$@"; do case "$a" in --dry-run) dry=1 ;; --yes|-y) yes=1 ;; *) die "usage: ./watchover.sh demo-clean [--dry-run] [--yes]" ;; esac; done
    if [ "$dry" = 1 ]; then $COMPOSE exec -T dashboard python -m watchover.demo clean --dry-run; exit 0; fi
    if [ "$yes" != 1 ] && [ -t 0 ]; then
      $COMPOSE exec -T dashboard python -m watchover.demo clean --dry-run
      printf 'Delete the demo data counted above? [y/N] '; read -r ans
      case "$ans" in y|Y|yes|e|E|evet) ;; *) say "nothing deleted"; exit 0 ;; esac
    fi
    say "restarting the dashboard: the simulator stops and the in-memory live window forgets the demo hosts"
    $COMPOSE restart dashboard
    for _ in $(seq 1 60); do
      if $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^dashboard healthy'; then break; fi; sleep 2
    done
    $COMPOSE exec -T dashboard python -m watchover.demo clean
    $COMPOSE restart dashboard                     # its memory (caches, learned templates) is rebuilt from the purged stores
    say "demo data removed (audited as 'cli' in the sign-in log)" ;;
  reset-data)   # the full reset (watchover.demo.reset_all): every analysis store, whoever wrote it, plus the demo clean-up; configuration stays
    need_docker; shift; dry=0; yes=0
    for a in "$@"; do case "$a" in --dry-run) dry=1 ;; --yes|-y) yes=1 ;; *) die "usage: ./watchover.sh reset-data [--dry-run] [--yes]" ;; esac; done
    $COMPOSE exec -T dashboard python -m watchover.demo reset --dry-run
    [ "$dry" = 1 ] && exit 0
    if [ "$yes" != 1 ]; then
      [ -t 0 ] || die "reset-data deletes every analysis store: run it in a terminal, or add --yes"
      say "kept: users, teams, roles, settings (LLM connection), agents, sources, policies, hand-entered inventory, audit logs, real log lines, base models"
      say "a backup first is wise: ./watchover.sh backup"
      printf 'Type SIFIRLA to delete the analysis data counted above: '; read -r ans
      case "$ans" in SIFIRLA|sifirla|sıfırla|Sıfırla|Sifirla) ;; *) say "nothing deleted"; exit 0 ;; esac
    fi
    $COMPOSE exec -T dashboard python -m watchover.demo reset --yes --at-start >/dev/null
    say "restarting the dashboard: it resets at start, before anything in memory can be written back"
    $COMPOSE restart dashboard
    for _ in $(seq 1 90); do
      if $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q '^dashboard healthy'; then break; fi; sleep 2
    done
    $COMPOSE exec -T dashboard python -m watchover.demo reset --result
    say "analysis data reset (audited as data-reset in the sign-in log); new findings come from real data only" ;;
  migrate) need_docker; shift; [ -d data ] && docker cp data/. watchover:/data/ 2>/dev/null || true; $COMPOSE exec dashboard python -m watchover.migrate --source /data "$@" ;;
  save)    need_docker; out="${2:-watchover-images-$(version).tgz}"; docker save "watchover:$(version)" "watchover-receiver:$(version)" "watchover-postgres:$(version)" | gzip > "$out"; say "images saved to $out (copy it with this folder to the air-gapped server, then: ./watchover.sh load $out && ./watchover.sh install)" ;;
  load)    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh load watchover-images-X.Y.tgz"; gunzip -c "$2" | docker load; say "images loaded; ./watchover.sh install skips the build when the image tag matches" ;;
  setup)   # the one-file installer: watchover-kurulum-<ver>.sh (online) or, with --offline, ...-<arch>-hepsi-dahil.sh (bundle + Podman RPMs)
    shift; exec bash "$(dirname "$(readlink -f "$0")")/scripts/make-setup.sh" "$@" ;;
  bundle)  # one file for the air-gapped server: images + model volumes + source tree; built where there is internet
    need_docker; out="${2:-watchover-bundle-$(version).tar}"; case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
    have_image "watchover:$(version)" || die "image watchover:$(version) is not built yet: ./watchover.sh install first (and 'llm on' if the server should get the local LLM)"
    tmp=$(mktemp -d); mkdir -p "$tmp/volumes" "$tmp/source"
    have_image "watchover-receiver:$(version)" || die "image watchover-receiver:$(version) is not built yet: ./watchover.sh update first"
    refresh_bases                                          # the bundle carries today's patches of nginx, PostgreSQL, Ollama
    imgs="watchover:$(version) watchover-receiver:$(version) watchover-postgres:$(version)"
    for i in "watchover-trainer:$(version)" "watchover-ollama:$(version)" "watchover-updater:$UPDATER_TAG" nginx:1.30-alpine alpine:latest; do have_image "$i" && imgs="$imgs $i" || true; done
    have_image alpine:latest || docker pull alpine:latest >/dev/null 2>&1 || true; have_image alpine:latest && case " $imgs " in *" alpine:latest "*) ;; *) imgs="$imgs alpine:latest" ;; esac
    if have_image "watchover-trainer:$(version)" && ! offline; then
      say "trainer image present: caching the training base weights so auto-learning works on the air-gapped server"
      bash "./$(basename "$0")" llm prepare || say "WARNING: base weights could not be cached; auto-learning on the offline server will fail until you run 'llm prepare' here and bundle again"
    fi
    say "saving images: $imgs"; docker save $imgs | gzip -1 > "$tmp/images.tar.gz"
    for v in watchover-ollama watchover-hf watchover-models; do
      n=$(vol_name "$v"); docker volume inspect "$n" >/dev/null 2>&1 || continue
      say "exporting volume $v ($n: Ollama models / training weights / adapters)"
      docker run --rm -v "$n":/v:ro -v "$tmp/volumes":/out:z "$ALPINE" tar cf "/out/$v.tar" -C /v . || say "could not export $v (skipped)"
    done
    say "adding the source tree"
    if git rev-parse HEAD >/dev/null 2>&1; then git archive --format=tar HEAD | tar xf - -C "$tmp/source"; else tar cf - --exclude=./.git --exclude=./.releases --exclude=./.updater --exclude=./data --exclude=./backups --exclude=./certs --exclude=./.env --exclude='./*.tar' --exclude='./*.tgz' --exclude=./web/node_modules . | tar xf - -C "$tmp/source"; fi
    [ ! -f scripts/prune-tree.sh ] || bash scripts/prune-tree.sh "$tmp/source"   # demo, tests, OpenShift chart: not for a server
    # the Compose v2 binary of this machine (Linux only; images and binary share its CPU architecture): an air-gapped RHEL server has
    # no download.docker.com, install-rhel.sh installs it from here (bin/docker-compose) before anything else
    mkdir -p "$tmp/bin"; arch=$(uname -m | sed 's/^arm64$/aarch64/; s/^amd64$/x86_64/'); cb=""
    if [ "$(uname -s)" = Linux ]; then cb=$(compose_bin 2>/dev/null || command docker info --format '{{range .ClientInfo.Plugins}}{{if eq .Name "compose"}}{{.Path}}{{end}}{{end}}' 2>/dev/null || true); fi
    if have_image "watchover-updater:$UPDATER_TAG" && c=$(docker create "watchover-updater:$UPDATER_TAG" 2>/dev/null); then   # the patched build
      docker cp "$c:/usr/local/libexec/docker/cli-plugins/docker-compose" "$tmp/bin/docker-compose" >/dev/null 2>&1 \
        && "$tmp/bin/docker-compose" version --short >/dev/null 2>&1 && cb="watchover-updater:$UPDATER_TAG" || rm -f "$tmp/bin/docker-compose"
      docker rm "$c" >/dev/null 2>&1 || true
    fi
    if [ -f "$tmp/bin/docker-compose" ]; then chmod 755 "$tmp/bin/docker-compose"; say "Compose binary added for linux/$arch (patched build from $cb)"
    elif [ -n "$cb" ] && [ -x "$cb" ]; then cp "$cb" "$tmp/bin/docker-compose"; chmod 755 "$tmp/bin/docker-compose"; say "Compose binary added for linux/$arch ($cb)"
    else say "note: no Linux Compose binary here; on an air-gapped RHEL server put docker-compose-linux-$arch next to install-rhel.sh"; fi
    printf 'version=%s\narch=%s\nimages=%s\nvolumes=%s\ncompose=%s\n' "$(version)" "$arch" "$imgs" "$(ls "$tmp/volumes" 2>/dev/null | tr '\n' ' ')" "$([ -f "$tmp/bin/docker-compose" ] && echo 1 || echo 0)" > "$tmp/MANIFEST"
    tar cf "$out" -C "$tmp" MANIFEST bin images.tar.gz volumes source; rm -rf "$tmp"
    say "bundle written: $out ($(du -h --apparent-size "$out" | cut -f1)). RHEL 9: put it next to install-rhel.sh and run sudo ./install-rhel.sh. Elsewhere:  tar xf $(basename "$out") --strip-components=1 source  &&  ./watchover.sh unbundle $(basename "$out")  &&  ./watchover.sh install" ;;
  unbundle)  # on the air-gapped server: images, model volumes, offline marker
    busy_guard; need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh unbundle watchover-bundle-X.Y.tar"
    # Disk: only the image archive is copied out (then loaded from the file, not through a pipe Podman would copy again) and it is
    # gone before the volumes, which stream from the bundle into their volume. The scratch folder goes on any exit: a failed load
    # used to leave a ~15 GB copy in /tmp, and the next attempt ran out of space.
    tmp=$(mktemp -d "${TMPDIR:-/var/tmp}/watchover-unbundle.XXXXXX"); trap 'rm -rf "$tmp"' EXIT
    p=""; tar tf "$2" 2>/dev/null | grep -x './MANIFEST' >/dev/null && p="./"   # not grep -q: tar killed by SIGPIPE fails the pipe (pipefail)
    tar xOf "$2" "${p}MANIFEST" > "$tmp/MANIFEST" 2>/dev/null && say "bundle $(sed -n 's/^version=//p' "$tmp/MANIFEST")" || true
    say "loading images"; tar xf "$2" -C "$tmp" "${p}images.tar.gz"; docker load -i "$tmp/images.tar.gz"; rm -f "$tmp/images.tar.gz"
    for v in $(tar tf "$2" | sed -n 's#^\(\./\)\{0,1\}volumes/\([^/]*\)\.tar$#\2#p'); do
      n=$(vol_name "$v"); say "restoring volume $v -> $n"
      docker volume inspect "$n" >/dev/null 2>&1 || docker volume create "$n" >/dev/null   # an upgrade: the volume is there (podman refuses a second create)
      tar xOf "$2" "${p}volumes/$v.tar" | docker run --rm -i -v "$n":/v "$ALPINE" tar xf - -C /v || say "could not restore $v"
    done
    rm -rf "$tmp"; trap - EXIT; touch "$ENV_FILE"; env_set WATCHOVER_OFFLINE 1
    say "offline install marked (.env WATCHOVER_OFFLINE=1: install/update never build, llm on never downloads). Next:  ./watchover.sh install   then  ./watchover.sh llm on  if the bundle carried Ollama models" ;;
  shell)   need_docker; $COMPOSE exec dashboard bash ;;
  version) echo "Watchover v$(version) ($(git_rev))" ;;
  *) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 1 ;;
esac
