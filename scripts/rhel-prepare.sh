#!/usr/bin/env bash
# Prepare a Red Hat Enterprise Linux 8.8 / 9 server (or Rocky / Alma) for Watchover. Two container engines, pick one:
#   docker (default)  Docker CE + Compose v2 from Docker's RHEL repository (podman / buildah / runc are removed first: they conflict),
#                     the service enabled, the operator in the docker group, /etc/docker/daemon.json (log caps, live-restore)
#   podman            Red Hat's own engine, rootful: podman + container-selinux from the RHEL repositories, podman.socket (the
#                     Docker-compatible API) and only the Compose v2 binary (docker-compose-plugin RPM from Docker's repository, no
#                     Docker engine); watchover.sh then drives Podman through that socket (WATCHOVER_ENGINE=podman, run it with sudo)
# Both: firewalld rules for the dashboard (8501), the agent receiver (8600), HTTPS (443) and with --mcp the MCP server (8765);
# SELinux stays enforcing; chrony for correct timestamps. Idempotent: run it again any time.
#
#   sudo bash scripts/rhel-prepare.sh [--engine docker|podman] [--user NAME] [--mcp] [--no-firewall] [--no-docker] [--check]
#
# --check prints what it would do and verifies the host (no changes, no root needed). --no-docker skips the engine packages (both
# engines; e.g. installed by hand). The packages need internet, Satellite or an internal mirror. Air-gapped, docker: download the
# RPMs on a machine with internet (dnf download --resolve docker-ce docker-ce-cli containerd.io docker-buildx-plugin
# docker-compose-plugin), dnf install ./*.rpm, then run this script with --no-docker. Air-gapped, podman: podman comes from the RHEL
# repositories (Satellite); copy the docker-compose-linux-x86_64 binary of github.com/docker/compose/releases to
# /usr/local/lib/docker/cli-plugins/docker-compose (chmod 755) and run with --engine podman --no-docker.
set -euo pipefail
USER_NAME="${SUDO_USER:-$(id -un)}"; FIREWALL=1; DOCKER=1; CHECK=0; ENGINE=docker; MCP=0
while [ $# -gt 0 ]; do case "$1" in
  --user) USER_NAME="$2"; shift ;; --engine) ENGINE="${2:-}"; shift ;; --mcp) MCP=1 ;;
  --no-firewall) FIREWALL=0 ;; --no-docker) DOCKER=0 ;; --check) CHECK=1 ;;
  *) echo "unknown option $1" >&2; exit 1 ;; esac; shift; done
case "$ENGINE" in docker|podman) ;; *) echo "--engine must be docker or podman" >&2; exit 1 ;; esac
say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
run() { if [ "$CHECK" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }
docker_repo() {   # Docker's RHEL repository (docker: the engine; podman: only the Compose plugin comes from it)
  if [ "$CHECK" = 1 ]; then echo "  would run: dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo"; else
    dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo 2>/dev/null || dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo; fi
}
compose_found() { for c in "$(command -v docker-compose 2>/dev/null || true)" /usr/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose; do [ -n "$c" ] && [ -x "$c" ] && return 0; done; return 1; }

# ---- host facts
. /etc/os-release 2>/dev/null || true
STORE=/var/lib/docker; [ "$ENGINE" = podman ] && STORE=/var/lib/containers
say "host: ${PRETTY_NAME:-unknown} · $(nproc 2>/dev/null || echo ?) CPU · $(awk '/MemTotal/ {printf "%d GB", $2/1024/1024}' /proc/meminfo 2>/dev/null) RAM · $(df -h /var/lib 2>/dev/null | awk 'NR==2 {print $4}') free under /var/lib · engine: $ENGINE"
case "${ID:-}" in rhel|rocky|almalinux|centos|ol) ;; *) warn "this is not a RHEL family system (ID=${ID:-?}); the install steps assume dnf and the RHEL repositories" ;; esac
[ "${VERSION_ID%%.*}" = "8" ] || [ "${VERSION_ID%%.*}" = "9" ] || warn "tested on RHEL 8 and 9, this is ${VERSION_ID:-?}"
if [ "$CHECK" = 0 ] && [ "$(id -u)" != 0 ]; then echo "run with sudo (or use --check)" >&2; exit 1; fi
avail_kb=$(df -k /var/lib 2>/dev/null | awk 'NR==2 {print $4}'); [ -n "$avail_kb" ] && [ "$avail_kb" -lt $((40 * 1024 * 1024)) ] && warn "/var/lib has less than 40 GB free: images, PostgreSQL and the live spool live in $STORE; mount a volume there (raw logs can go to disks of their own: WATCHOVER_LOG_HOT_PATH / WATCHOVER_LOG_COLD_PATH)"

if [ "$ENGINE" = docker ]; then
  # ---- Docker CE + Compose v2 (Red Hat ships podman; this engine is Docker's own)
  if [ "$DOCKER" = 1 ]; then
    if command -v docker >/dev/null 2>&1 && ! docker --version 2>/dev/null | grep -qi podman && docker compose version >/dev/null 2>&1; then
      say "docker already installed: $(docker --version 2>/dev/null) · $(docker compose version 2>/dev/null)"
    else
      say "installing Docker CE + Compose v2 from download.docker.com (podman / buildah / runc are removed first: they conflict)"
      run dnf -y remove podman podman-docker buildah runc docker docker-client docker-common docker-engine 2>/dev/null || true
      run dnf -y install dnf-plugins-core
      docker_repo
      run dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
    run systemctl enable --now docker
    if [ ! -f /etc/docker/daemon.json ]; then
      say "writing /etc/docker/daemon.json (json-file logs capped at 50 MB x 5 per container, live-restore so a daemon restart keeps containers up)"
      [ "$CHECK" = 1 ] || { mkdir -p /etc/docker; printf '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "50m", "max-file": "5"},\n  "live-restore": true\n}\n' > /etc/docker/daemon.json; systemctl restart docker; }
    fi
    if id "$USER_NAME" >/dev/null 2>&1 && ! id -nG "$USER_NAME" | grep -qw docker; then say "adding $USER_NAME to the docker group (sign out and in again afterwards)"; run usermod -aG docker "$USER_NAME"; fi
  fi
else
  # ---- Podman (rootful) + the Compose v2 binary on its Docker-compatible socket
  if [ "$DOCKER" = 1 ]; then
    command -v dockerd >/dev/null 2>&1 && warn "Docker CE is installed too: watchover.sh picks Docker unless .env says WATCHOVER_ENGINE=podman"
    say "installing Podman from the RHEL repositories (podman, container-selinux; netavark + aardvark-dns: containers find each other by name)"
    run dnf -y install podman container-selinux
    run dnf -y install netavark aardvark-dns || warn "netavark / aardvark-dns not in the repositories: container name resolution needs them (RHEL 8: podman-plugins with CNI)"
    if compose_found; then say "Compose v2 binary already present"; else
      say "installing only the Compose v2 plugin from download.docker.com (docker-compose-plugin: one binary, no Docker engine, no conflict with Podman)"
      run dnf -y install dnf-plugins-core
      docker_repo
      run dnf -y install docker-compose-plugin
    fi
  fi
  run systemctl enable --now podman.socket
  say "watchover.sh drives Podman through /run/podman/podman.sock (root only: run ./watchover.sh with sudo; WATCHOVER_ENGINE=podman is detected when no Docker is installed)"
  if [ "$CHECK" = 0 ] && command -v podman >/dev/null 2>&1; then
    nb=$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null || true)
    [ "$nb" = cni ] && warn "Podman uses the CNI network backend: container names (postgres, receiver) resolve only with netavark. On this fresh server set network_backend = \"netavark\" under [network] in /etc/containers/containers.conf and run: podman system reset --force (deletes all Podman containers and images)"
    say "podman: $(podman --version 2>/dev/null) · network backend ${nb:-?}"
  fi
fi

# ---- firewalld: who talks to Watchover
PORTS="8501/tcp 8600/tcp 443/tcp"; [ "$MCP" = 1 ] && PORTS="$PORTS 8765/tcp"
if [ "$FIREWALL" = 1 ] && command -v firewall-cmd >/dev/null 2>&1 && (systemctl is-active firewalld >/dev/null 2>&1 || [ "$CHECK" = 1 ]); then
  say "firewalld: $PORTS (8501 dashboard, 8600 agents, 443 edge TLS$([ "$MCP" = 1 ] && echo ', 8765 MCP'))"
  for p in $PORTS; do run firewall-cmd --permanent --add-port="$p" >/dev/null; done
  run firewall-cmd --reload >/dev/null
  say "note: $ENGINE publishes container ports through its own NAT rules; the firewalld rules keep the intent documented and cover the host. Only some networks: firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=10.0.0.0/8 port port=8501 protocol=tcp accept' (instead of --add-port), or bind a port to one address in .env (WATCHOVER_UI_PORT=10.0.0.5:8501)"
else
  [ "$FIREWALL" = 1 ] && warn "firewalld not running: open $PORTS on whatever filters this host"
fi

# ---- SELinux stays enforcing
if command -v getenforce >/dev/null 2>&1; then
  if [ "$ENGINE" = podman ]; then say "SELinux: $(getenforce 2>/dev/null). Podman confines containers (container_t); the compose bind mounts carry :z and the log disks are labelled on start: nothing to change"
  else say "SELinux: $(getenforce 2>/dev/null) (Docker CE default keeps containers unconfined; nothing to change. With selinux-enabled in daemon.json the :z on the bind mounts labels them)"; fi
fi

# ---- time: anomaly timestamps and TLS depend on it
if ! systemctl is-active chronyd >/dev/null 2>&1; then say "enabling chronyd (time sync)"; run dnf -y install chrony >/dev/null 2>&1 || true; run systemctl enable --now chronyd || true; fi

if [ "$ENGINE" = podman ]; then
  say "done. Next: git clone (or unpack the bundle) into /opt/watchover-docker, then  sudo ./watchover.sh install --production  (Podman: the stack starts on boot by itself, watchover-stack.service)"
else
  say "done. Next: git clone (or unpack the bundle) into /opt/watchover-docker and run ./watchover.sh install --production  (air-gapped: ./watchover.sh unbundle FILE first)"
fi
