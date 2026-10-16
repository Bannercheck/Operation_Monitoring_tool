#!/usr/bin/env bash
# Watchover on one server, entirely in Docker, built from this source tree. No GitHub, no registry, no Python on the host.
#
#   ./watchover.sh install          first start: writes .env (random passwords / keys), builds the image, starts PostgreSQL + dashboard
#   ./watchover.sh install --production   the same on a production server: demo off (WATCHOVER_DEMO=0) and the go-live checklist
#   ./watchover.sh checklist        the production checklist again: backup timer, start on boot, disk caps, admin password, TLS
#   ./watchover.sh backup-timer on [HH:MM]|off   nightly backup (systemd timer; cron where there is no systemd), default 02:15
#   ./watchover.sh autostart on|off the stack starts with the server (systemd unit; switched on by 'install' with Podman)
#   ./watchover.sh storage          where raw logs live (data volume, or the WATCHOVER_LOG_HOT_PATH / _COLD_PATH disks) and free space
#                                   engine: Docker, or rootful Podman (RHEL): WATCHOVER_ENGINE=docker|podman in .env, detected when unset
#   ./watchover.sh update           new source tree copied over this folder: rebuild the image, recreate the containers, keep the data
#   ./watchover.sh upgrade          git pull of this folder (local edits set aside and put back) + update: the one-line way to a new version
#   ./watchover.sh start|stop|restart|status|logs [service]
#   ./watchover.sh mcp on|off       our MCP server as a third container (MCP_API_KEY from .env)
#   ./watchover.sh otlp on|off      publish the OpenTelemetry ports 4317 (gRPC) / 4318 (HTTP) on the receiver (OTLP/HTTP also on 8600)
#   ./watchover.sh legacy on|off    the former Streamlit interface as an extra container (:8502) while teams move over
#   ./watchover.sh edge on NAME     Caddy in front: https://NAME for the company network (TLS from Caddy's CA, or ./certs/watchover.crt+key);
#                                   NAME may be a DNS name, an IP or a Bonjour name like watchover.local (announced by the Mac itself)
#   ./watchover.sh edge off|cert    stop it | export the CA root certificate colleagues install once (./certs/watchover-root.crt)
#   ./watchover.sh doctor [--check] the address does not open? checks containers, the edge, the name (Bonjour) and repairs what it can
#   ./watchover.sh backup [--keep N] [--no-logs]  PostgreSQL dump + data volume (--no-logs: without /data/logs) + .env
#                                   -> ./backups/watchover-YYYYMMDD-HHMM.tgz (WATCHOVER_BACKUP_DIR; oldest beyond N=14 removed)
#   ./watchover.sh restore FILE     bring a backup back (containers are stopped meanwhile)
#   ./watchover.sh user ...         account management, e.g.  user list | user add ops@firma.com --admin | user password EMAIL | user unlock EMAIL
#   ./watchover.sh demo-clean [--dry-run] [--yes]   delete everything the simulator made, in every store (counts first; real data stays)
#   ./watchover.sh migrate          copy old SQLite files (knowledge.db / actions.db / playbook.db in ./data or the volume) into PostgreSQL
#   ./watchover.sh save|load FILE   export the built images to a .tgz (build on a machine with internet), import them on an air-gapped server
#   ./watchover.sh bundle [FILE]    everything an air-gapped server needs in one .tar: all images, downloaded Ollama models, training weights, source
#   ./watchover.sh unbundle FILE    on the air-gapped server: load the images and model volumes, mark the install offline (no builds, no pulls)
#   ./watchover.sh llm prepare      cache the training base weights (done by 'bundle' automatically) so the trainer never needs internet
#   ./watchover.sh profile [NAME]   sizing: auto (default: measured from this machine on every start), cpu8 / cpu16 (fixed numbers); no NAME shows it
#   ./watchover.sh shell            a shell inside the dashboard container
#   ./watchover.sh updater on|off|status   in-app updates (Sistem › Güncelleme): upload a zip / bundle, apply, roll back. A separate
#                                   'updater' container is the only one with the engine socket (= root on this host); on by default
#   ./watchover.sh build            only the images of this tree's version (the updater builds a new release with it before switching)
#   ./watchover.sh health [VER] [S] wait up to S seconds (180) until the dashboard answers /api/health with VER (default: this tree)
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE="${WATCHOVER_ENV_FILE:-.env}"      # the updater builds a release tree with the install folder's .env
UPDATER_TAG=1                               # watchover-updater:1 carries tools only (its logic runs from this folder): bump when Dockerfile.updater changes
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
  local c; for c in "${WATCHOVER_COMPOSE_BIN:-}" "$(command -v docker-compose 2>/dev/null || true)" /usr/libexec/docker/cli-plugins/docker-compose \
    /usr/local/lib/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose; do
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
    compose_bin >/dev/null || die "Compose v2 is missing for Podman: sudo bash scripts/rhel-prepare.sh --engine podman installs docker-compose-plugin (Compose only, no Docker engine); air-gapped: copy the docker-compose-linux-x86_64 binary to /usr/local/lib/docker/cli-plugins/docker-compose (chmod +x)"
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
  lcpu=$(clamp $((ncpu * 3 / 8)) 1 8);       lmem=$(clamp $((mem_gb * 16 / 100)) 4 16)
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
    have_image "watchover:$(version)" && have_image "watchover-receiver:$(version)" || die "offline install and image watchover:$(version) or watchover-receiver:$(version) is missing: unbundle the bundle of this version first (./watchover.sh unbundle watchover-bundle-$(version).tar)"
    say "offline install: using the loaded image watchover:$(version) (no build)"
    if updater_on && ! have_image "watchover-updater:$UPDATER_TAG"; then env_set WATCHOVER_UPDATER 0; say "offline install: the bundle has no watchover-updater image, in-app updates stay off (bundle a newer version)"; fi
    return 0
  fi
  say "building image watchover:$(version) from this source tree (first time: a few minutes, needs internet for PyPI)"
  build_img dashboard --build-arg GIT_REV="$(git_rev)" --build-arg VERSION="$(version)"
  say "building the receiver (Go, a few seconds)"; build_img receiver
  [ "$(env_get WATCHOVER_LLM)" = "1" ] && build_img trainer || true
  if updater_on && ! have_image "watchover-updater:$UPDATER_TAG"; then
    say "building the updater image (once: Docker CLI + Compose, bash, Python)"
    build_img updater || { env_set WATCHOVER_UPDATER 0; say "WARNING: the updater image could not be built; in-app updates stay off (./watchover.sh updater on to retry)"; }
  fi
  return 0
}
# one image: Docker through compose; Podman with podman build (Docker image format keeps HEALTHCHECK) under the name Docker gives
# 'watchover:<tag>', which is the name compose asks the Podman socket for
build_img() {
  local svc="$1" f=Dockerfile n=watchover tag; tag="$(version)"; shift
  if [ "$ENGINE" = podman ]; then
    case "$svc" in receiver) f=Dockerfile.receiver; n=watchover-receiver ;; trainer) f=Dockerfile.trainer; n=watchover-trainer ;;
      updater) f=Dockerfile.updater; n=watchover-updater; tag=$UPDATER_TAG; set -- "$@" --build-arg PODMAN=1 ;; esac
    podman build --format docker -f "$f" -t "docker.io/library/$n:$tag" "$@" .
  elif [ "$svc" = trainer ]; then $COMPOSE --profile llm build "$@" trainer
  elif [ "$svc" = updater ]; then $COMPOSE --profile updater build "$@" updater
  else $COMPOSE build "$@" "$svc"; fi
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
verify_running() {
  local v="" i; for i in $(seq 1 "${WATCHOVER_VERIFY_TRIES:-45}"); do v=$(running_version); [ "$v" = "$(version)" ] && break; sleep 2; done
  if [ "$v" = "$(version)" ]; then say "running v$v. In the browser reload the page once (Cmd+Shift+R / Ctrl+F5) if it still shows the old screens."
  else say "WARNING: this folder is v$(version) but the dashboard reports '${v:-no answer}'. See ./watchover.sh logs dashboard, then ./watchover.sh update"; fi
}
edge_url() { local h p; h=$(env_get WATCHOVER_EDGE_HOST); p=$(env_get WATCHOVER_EDGE_HTTPS_PORT || echo 443); [ "$p" = "443" ] && echo "https://$h" || echo "https://$h:$p"; }
# Caddy's own CA root -> ./certs/watchover-root.crt (0644, public): for colleagues' browsers, and served by the receiver as
# /agent/ca.crt, which the Agents panel's https install command pins by its sha256 (no manual CA copying on servers)
edge_ca_export() {
  [ "$(env_get WATCHOVER_EDGE)" = "1" ] && [ "$(env_get WATCHOVER_EDGE_TLS)" = "internal" ] || return 0
  mkdir -p certs; local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if docker cp watchover-edge:/data/caddy/pki/authorities/local/root.crt certs/watchover-root.crt.tmp 2>/dev/null; then
      chmod 644 certs/watchover-root.crt.tmp && mv -f certs/watchover-root.crt.tmp certs/watchover-root.crt && return 0
    fi
    sleep 2
  done
  say "WARNING: the edge CA was not exported yet (./watchover.sh edge cert later): https agent installs need it"; return 1
}
up() {   # inside the updater its own container is left alone (its profile is not active, so it is not an orphan either)
  $COMPOSE $(profiles) up -d --remove-orphans
  in_updater && return 0
  if updater_on; then $COMPOSE --profile updater up -d --no-deps updater || say "WARNING: the updater did not start (in-app updates unavailable): ./watchover.sh logs updater"; fi
  mdns_ensure
  [ -f certs/watchover-root.crt ] || edge_ca_export || true
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
cap_gb() {   # TIER OTHER -> suggested hot_max_gb / cold_max_gb: 80% of a disk of its own, 40% of one shared by both tiers, 30% of the engine's
  local p o pct=30 base kb
  p=$(env_get "WATCHOVER_LOG_$1_PATH"); o=$(env_get "WATCHOVER_LOG_$2_PATH")
  if [ -n "$p" ]; then base="$p"; pct=80; [ -n "$o" ] && [ "$(fs_of "$p")" = "$(fs_of "$o")" ] && pct=40
  else base=/var/lib/docker; [ "$ENGINE" = podman ] && base=/var/lib/containers; [ -d "$base" ] || base=/var/lib; [ -d "$base" ] || base=/; fi
  kb=$(df -Pk "$base" 2>/dev/null | awk 'NR==2 {print $2}'); [ -n "$kb" ] && echo $((kb / 1048576 * pct / 100)) || echo "?"
}
checklist() {
  local bt as hot cold
  ck() { if [ "$1" = 1 ]; then printf '  [x] %s\n' "$2"; else printf '  [ ] %s\n' "$2"; fi; }
  bt=$(backup_timer_state); as=$(autostart_state); hot=$(cap_gb HOT COLD); cold=$(cap_gb COLD HOT)
  echo; say "production checklist ($ENGINE; ./watchover.sh checklist shows it again)"
  ck "$([ "$(env_get WATCHOVER_DEMO)" = 0 ] && echo 1)" "demo off (WATCHOVER_DEMO=0): no simulator, no demo data. Leftovers from a demo: ./watchover.sh demo-clean"
  ck "$(case "$bt" in on*) echo 1 ;; esac)" "nightly backup: $bt  -> ./watchover.sh backup-timer on [HH:MM]; copy $(backup_dir)/ to another machine (data/.vault.key is in it)"
  if [ "$ENGINE" = podman ]; then ck "$([ "$as" = on ] && echo 1)" "start on boot: $as  -> sudo ./watchover.sh autostart on (Podman has no daemon that restarts containers)"
  else ck "$([ "$as" = on ] && echo 1 || { systemctl is-enabled -q docker.service 2>/dev/null && echo 1; })" "start on boot: Docker service enabled, containers restart: unless-stopped (optional unit: ./watchover.sh autostart on)"; fi
  ck "" "admin password: sign in as admin@watchover.local (a new password is required at once) or ./watchover.sh user password admin@watchover.local; personal admins: ./watchover.sh user add ops@firma.com --admin"
  ck "" "disk caps: Sistem › Ayarlar  hot_max_gb ~ $hot, cold_max_gb ~ $cold  (0 = none; Sistem › Depolama warns at 85%)"
  ck "$([ -n "$(env_get WATCHOVER_LOG_HOT_PATH)$(env_get WATCHOVER_LOG_COLD_PATH)" ] && echo 1)" "raw logs on disks of their own: WATCHOVER_LOG_HOT_PATH / WATCHOVER_LOG_COLD_PATH in .env (./watchover.sh storage)"
  ck "$([ "$(env_get WATCHOVER_EDGE)" = 1 ] && echo 1)" "TLS: ./watchover.sh edge on watchover.firma.com (company certificate: ./certs/watchover.crt + .key); then only 443 (+ 8600 for http agents) open"
  ck "$(updater_on || echo 1)" "in-app updates: $(updater_on && echo "on (the updater container holds the engine socket = root on this host; signing key: WATCHOVER_UPDATE_PUBKEY)" || echo off)  -> ./watchover.sh updater off|on"
  ck "" "firewall: 8501 / 8600 / 443 (and 8765 with MCP, 4317 / 4318 with OTLP) for the right networks only: sudo bash scripts/rhel-prepare.sh prints and opens them"
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
  install)   # --production: demo off for good (WATCHOVER_DEMO=0) and the go-live checklist at the end
    prod=0; for a in "${@:2}"; do case "$a" in --production|--prod) prod=1 ;; *) die "usage: ./watchover.sh install [--production]" ;; esac; done
    need_docker; write_env
    if [ "$prod" = 1 ]; then env_set WATCHOVER_DEMO 0; env_set WATCHOVER_PRODUCTION 1; say "production install: WATCHOVER_DEMO=0 (no simulator, no demo data)"; fi
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
    for k in HOT COLD; do
      p=$(env_get "WATCHOVER_LOG_${k}_PATH")
      if [ -n "$p" ]; then check_log_dir "$k" "$p"; printf '  %-4s %s -> /logs/%s   %s\n' "$(lower "$k")" "$p" "$(lower "$k")" "$(df -Ph "$p" | awk 'NR==2 {print $2" disk, "$4" free, mounted at "$6}')"
      else printf '  %-4s data volume (/data/logs/%s)   for a disk of its own: WATCHOVER_LOG_%s_PATH=/srv/watchover/%s in .env, then ./watchover.sh start\n' "$(lower "$k")" "$(lower "$k")" "$k" "$(lower "$k")"; fi
    done
    say "suggested caps (Sistem › Ayarlar): hot_max_gb ~ $(cap_gb HOT COLD), cold_max_gb ~ $(cap_gb COLD HOT)" ;;
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
  start)   need_docker; write_env; up; $COMPOSE ps ;;
  doctor)  need_docker; doctor "${2:-}" ;;
  stop)    need_docker; $COMPOSE $(profiles) $(uprof) stop ;;
  restart) need_docker; $COMPOSE $(profiles) restart "${2:-}" ;;
  status)  need_docker; $COMPOSE $(profiles) $(uprof) ps; docker stats --no-stream $($COMPOSE $(profiles) $(uprof) ps -q) 2>/dev/null || true ;;
  logs)    need_docker; $COMPOSE $(profiles) $(uprof) logs -f --tail 200 "${2:-dashboard}" ;;
  build)   # the images of this tree's version, nothing started; the updater runs it in a new release's tree (WATCHOVER_ENV_FILE = the install's .env)
    need_docker; export WATCHOVER_IMAGE=watchover WATCHOVER_TAG="$(version)"; build; say "images of v$(version) ready (nothing restarted)" ;;
  images)  # exit 0 when the dashboard and receiver images of VERSION (default: this tree) are there
    need_docker; v="${2:-$(version)}"; have_image "watchover:$v" && have_image "watchover-receiver:$v" ;;
  images-rm)   # the images of a release the updater no longer keeps; the engine refuses an image a container still uses
    need_docker; [ -n "${2:-}" ] && [ "$2" != "$(version)" ] || die "usage: ./watchover.sh images-rm VERSION   (never the version of this folder)"
    for i in watchover watchover-receiver watchover-trainer; do docker image rm "$i:$2" >/dev/null 2>&1 && say "removed $i:$2" || true; done ;;
  health)  # after a start: wait until the dashboard answers /api/health with VERSION; exit 1 after SECONDS
    need_docker; want="${2:-$(version)}"; secs="${3:-180}"; v=""; end=$(( $(date +%s) + secs ))
    while :; do v=$(running_version); [ "$v" = "$want" ] && { say "dashboard answers: v$v"; exit 0; }; [ "$(date +%s)" -ge "$end" ] && break; sleep "${WATCHOVER_HEALTH_POLL:-3}"; done
    say "the dashboard reports '${v:-no answer}', expected v$want"; exit 1 ;;
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
      on)  env_set WATCHOVER_MCP 1; up; say "MCP server on: http://$(host_ip):$(env_get WATCHOVER_MCP_PORT || echo 8765)/mcp  key: $(env_get MCP_API_KEY)" ;;
      off) $COMPOSE --profile mcp stop mcp; $COMPOSE --profile mcp rm -f mcp; env_set WATCHOVER_MCP 0; say "MCP server off" ;;
      *) die "usage: ./watchover.sh mcp on|off" ;; esac ;;
  otlp)   # OpenTelemetry: the receiver always speaks OTLP; "on" publishes 4317 / 4318 (off by default: a host collector may own them)
    need_docker; case "${2:-}" in
      on)  env_set WATCHOVER_OTLP 1; compose_files; up
           say "OTLP on: gRPC http://$(host_ip):$(env_get WATCHOVER_OTLP_GRPC_PORT || echo 4317)  HTTP http://$(host_ip):$(env_get WATCHOVER_OTLP_HTTP_PORT || echo 4318)/v1/{logs,traces,metrics}"
           say "exporter header: Authorization: Bearer <agent token> (Ajanlar panelinde bir ajan olarak kaydet; açık modda gerekmez)" ;;
      off) env_set WATCHOVER_OTLP 0; compose_files; up; say "OTLP ports closed (OTLP/HTTP stays on :$(env_get WATCHOVER_LIVE_PORT || echo 8600)/v1/*)" ;;
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
          have_image "watchover-trainer:$(version)" && say "offline install: using the loaded trainer image" || { env_set WATCHOVER_LLM 0; die "offline install and image watchover-trainer:$(version) is missing: run 'llm on' on the internet machine before 'bundle'"; }
        else
          say "building the trainer image (torch CPU, transformers, peft; first time a few minutes)"; build_img trainer
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
        say "bundled Ollama on (chat/review: qwen2.5:7b, fast: 3b, embeddings: bge-m3). Models to download: .env WATCHOVER_LLM_MODELS" ;;
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
      *) die "usage: ./watchover.sh llm on|off|use <model>|pull <model>|list|prepare [base]|export [file]|import <dir> [name]" ;; esac ;;
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
        compose_files                                                          # + docker-compose.edge.yml: plain http ports not published while the edge is on
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard edge
        say "edge on: $(edge_url)   (http://$(env_get WATCHOVER_EDGE_HOST) redirects; plain :8501 is no longer published)"
        if [ "$(env_get WATCHOVER_EDGE_TLS)" = "internal" ]; then
          edge_ca_export && $COMPOSE $(profiles) up -d --no-deps --force-recreate receiver >/dev/null 2>&1 || true   # the receiver serves it as /agent/ca.crt
          say "colleagues install the CA root once: ./certs/watchover-root.crt (agents: the Agents panel's https command pins it by itself)"
        fi ;;
      off)
        $COMPOSE --profile edge stop edge; $COMPOSE --profile edge rm -f edge; env_set WATCHOVER_EDGE 0; mdns_stop
        compose_files
        $COMPOSE $(profiles) up -d --remove-orphans --force-recreate dashboard; say "edge off: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)" ;;
      cert)
        mkdir -p certs; docker cp watchover-edge:/data/caddy/pki/authorities/local/root.crt certs/watchover-root.crt && chmod 644 certs/watchover-root.crt
        say "CA root: ./certs/watchover-root.crt  — macOS: Keychain Access › System › import, set to Always Trust; Windows: certutil -addstore -f Root watchover-root.crt; Linux agents: --ca watchover-root.crt" ;;
      *) die "usage: ./watchover.sh edge on [name[,name2,ip]] | off | cert     e.g.  edge on watchover.local" ;; esac ;;
  api)     say "the API now runs inside the dashboard container: http://$(host_ip):$(env_get WATCHOVER_UI_PORT || echo 8501)/api/docs" ;;
  backup)   # --no-logs: without the raw logs under /data/logs (the nightly timer's choice: they are large); on disks of their own they are never in it
    need_docker; keep="${WATCHOVER_BACKUP_KEEP:-$(env_get WATCHOVER_BACKUP_KEEP)}"; keep="${keep:-14}"; logs=1; out=""
    shift; while [ $# -gt 0 ]; do case "$1" in --keep) keep="${2:-14}"; shift ;; --no-logs) logs=0 ;; --out) out="${2:-}"; [ -n "$out" ] || die "--out needs a file"; shift ;; *) die "usage: ./watchover.sh backup [--keep N] [--no-logs] [--out FILE]" ;; esac; shift; done
    bdir=$(backup_dir); stamp=$(date +%Y%m%d-%H%M); fixed="$out"; [ -n "$out" ] || out="$bdir/watchover-$stamp.tgz"; mkdir -p "$(dirname "$out")"; tmp=$(mktemp -d)
    $COMPOSE exec -T postgres pg_dump -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)" | gzip > "$tmp/db.sql.gz"
    excl=""; [ "$logs" = 0 ] && excl="--exclude=./logs"
    docker run --rm -v "$(vol_name watchover-data)":/data -v "$tmp":/out:z "$ALPINE" tar czf /out/data.tgz $excl -C /data .
    printf 'version=%s\nengine=%s\nlogs=%s\n' "$(version)" "$ENGINE" "$logs" > "$tmp/MANIFEST"
    cp "$ENV_FILE" "$tmp/env"; tar czf "$out" -C "$tmp" .; rm -rf "$tmp"; chmod 600 "$out"; say "backup written: $out ($(du -h "$out" | cut -f1))"
    [ -n "$fixed" ] || ls -1t "$bdir"/watchover-*.tgz 2>/dev/null | tail -n +"$((keep + 1))" | while read -r old; do rm -f "$old"; say "old backup removed: $old"; done ;;   # --out (the updater's pre-update backup) prunes nothing
  restore)   # a backup made with --no-logs leaves the raw logs in place
    busy_guard; need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh restore backups/watchover-….tgz"; write_env; tmp=$(mktemp -d); tar xzf "$2" -C "$tmp"
    wipe='rm -rf /data/*'; grep -q '^logs=0' "$tmp/MANIFEST" 2>/dev/null && wipe='find /data -mindepth 1 -maxdepth 1 ! -name logs -exec rm -rf {} +'
    $COMPOSE $(profiles) stop dashboard mcp legacy 2>/dev/null || $COMPOSE stop dashboard
    $COMPOSE up -d postgres; sleep 5
    say "restoring the database"; $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" "$(env_get POSTGRES_DB || echo watchover)"
    gunzip -c "$tmp/db.sql.gz" | $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)"
    say "restoring settings, secrets and snapshots"; docker run --rm -v "$(vol_name watchover-data)":/data -v "$tmp":/in:z "$ALPINE" sh -c "$wipe && tar xzf /in/data.tgz -C /data"
    rm -rf "$tmp"; up; say "restored from $2"; $COMPOSE ps ;;
  user)    need_docker; shift; $COMPOSE exec dashboard python -m watchover.useradmin "$@" ;;
  profile)   # fixed caps for a known machine (WATCHOVER_SIZING=manual keeps them); the LLM gets a share, never every CPU, so a question
             # never slows the dashboard. An LLM connection you configured (LLM page / .env) is never changed.
    keys="WATCHOVER_SIZING WATCHOVER_CPUS WATCHOVER_MEMORY WATCHOVER_RECEIVER_CPUS WATCHOVER_RECEIVER_MEMORY WATCHOVER_PARSE_WORKERS WATCHOVER_SPOOL_MAX_MB WATCHOVER_PG_CPUS WATCHOVER_PG_MEMORY WATCHOVER_PG_SHARED_BUFFERS WATCHOVER_PG_CACHE WATCHOVER_LLM_CPUS WATCHOVER_LLM_THREADS WATCHOVER_LLM_MEMORY WATCHOVER_LLM_PARALLEL WATCHOVER_LLM_KEEPALIVE WATCHOVER_LLM_MODELS WATCHOVER_TRAIN_CPUS WATCHOVER_TRAIN_THREADS WATCHOVER_TRAIN_MEMORY"
    case "${2:-}" in
      cpu8|cpu16)
        write_env; pname="$2"
        if [ "$pname" = cpu8 ]; then
          set -- 6 16g 2 2g 3 4096 2 8g 2GB 32GB 3 3 10g 4 4 12g
        else
          set -- 10 24g 4 2g 6 6144 4 12g 4GB 48GB 6 6 12g 8 8 16g
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
  demo-clean)   # the same full purge as switching the simulator off (watchover.demo.purge): lines, history, derived rows, LLM memory, demo-trained models; real data stays
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
  migrate) need_docker; shift; [ -d data ] && docker cp data/. watchover:/data/ 2>/dev/null || true; $COMPOSE exec dashboard python -m watchover.migrate --source /data "$@" ;;
  save)    need_docker; out="${2:-watchover-images-$(version).tgz}"; docker save "watchover:$(version)" "watchover-receiver:$(version)" postgres:16-alpine | gzip > "$out"; say "images saved to $out (copy it with this folder to the air-gapped server, then: ./watchover.sh load $out && ./watchover.sh install)" ;;
  load)    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh load watchover-images-X.Y.tgz"; gunzip -c "$2" | docker load; say "images loaded; ./watchover.sh install skips the build when the image tag matches" ;;
  bundle)  # one file for the air-gapped server: images + model volumes + source tree; built where there is internet
    need_docker; out="${2:-watchover-bundle-$(version).tar}"; case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
    have_image "watchover:$(version)" || die "image watchover:$(version) is not built yet: ./watchover.sh install first (and 'llm on' if the server should get the local LLM)"
    tmp=$(mktemp -d); mkdir -p "$tmp/volumes" "$tmp/source"
    have_image "watchover-receiver:$(version)" || die "image watchover-receiver:$(version) is not built yet: ./watchover.sh update first"
    imgs="watchover:$(version) watchover-receiver:$(version) postgres:16-alpine"
    for i in "watchover-trainer:$(version)" "watchover-updater:$UPDATER_TAG" caddy:2-alpine ollama/ollama:latest alpine:latest; do have_image "$i" && imgs="$imgs $i" || true; done
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
    printf 'version=%s\nimages=%s\nvolumes=%s\n' "$(version)" "$imgs" "$(ls "$tmp/volumes" 2>/dev/null | tr '\n' ' ')" > "$tmp/MANIFEST"
    tar cf "$out" -C "$tmp" MANIFEST images.tar.gz volumes source; rm -rf "$tmp"
    say "bundle written: $out ($(du -h "$out" | cut -f1)). On the air-gapped server:  tar xf $(basename "$out") --strip-components=1 source  &&  ./watchover.sh unbundle $(basename "$out")  &&  ./watchover.sh install" ;;
  unbundle)  # on the air-gapped server: images, model volumes, offline marker
    busy_guard; need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh unbundle watchover-bundle-X.Y.tar"
    tmp=$(mktemp -d); tar xf "$2" -C "$tmp" MANIFEST images.tar.gz volumes 2>/dev/null || tar xf "$2" -C "$tmp"
    [ -f "$tmp/MANIFEST" ] && say "bundle $(sed -n 's/^version=//p' "$tmp/MANIFEST")" || true
    say "loading images"; gunzip -c "$tmp/images.tar.gz" | docker load
    for f in "$tmp"/volumes/*.tar; do
      [ -f "$f" ] || continue; v=$(basename "$f" .tar); n=$(vol_name "$v")
      say "restoring volume $v -> $n"; docker volume create "$n" >/dev/null
      docker run --rm -v "$n":/v -v "$tmp/volumes":/in:z "$ALPINE" tar xf "/in/$v.tar" -C /v || say "could not restore $v"
    done
    rm -rf "$tmp"; touch "$ENV_FILE"; env_set WATCHOVER_OFFLINE 1
    say "offline install marked (.env WATCHOVER_OFFLINE=1: install/update never build, llm on never downloads). Next:  ./watchover.sh install   then  ./watchover.sh llm on  if the bundle carried Ollama models" ;;
  shell)   need_docker; $COMPOSE exec dashboard bash ;;
  version) echo "Watchover v$(version) ($(git_rev))" ;;
  *) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 1 ;;
esac
