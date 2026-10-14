#!/usr/bin/env bash
# Prepare a Red Hat Enterprise Linux 8.8 / 9 server (or Rocky / Alma) for Watchover in Docker: Docker CE + Compose v2 from
# Docker's own RHEL repository, the service enabled, the operator in the docker group, firewalld rules for the dashboard
# (8501), the agent receiver (8600) and HTTPS (443), chrony for correct timestamps. Idempotent: run it again any time.
#
#   sudo bash scripts/rhel-prepare.sh [--user NAME] [--no-firewall] [--check]
#
# --check prints what it would do and verifies the host (no changes, no root needed). The Docker packages need internet
# or an internal mirror of download.docker.com; for an air-gapped server download the RPMs on a machine with internet
# (dnf download --resolve docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin) and install them with
# dnf install ./*.rpm before running this script with --no-docker.
set -euo pipefail
USER_NAME="${SUDO_USER:-$(id -un)}"; FIREWALL=1; DOCKER=1; CHECK=0
while [ $# -gt 0 ]; do case "$1" in
  --user) USER_NAME="$2"; shift ;; --no-firewall) FIREWALL=0 ;; --no-docker) DOCKER=0 ;; --check) CHECK=1 ;;
  *) echo "unknown option $1" >&2; exit 1 ;; esac; shift; done
say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
run() { if [ "$CHECK" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }

# ---- host facts
. /etc/os-release 2>/dev/null || true
say "host: ${PRETTY_NAME:-unknown} · $(nproc 2>/dev/null || echo ?) CPU · $(awk '/MemTotal/ {printf "%d GB", $2/1024/1024}' /proc/meminfo 2>/dev/null) RAM · $(df -h /var/lib 2>/dev/null | awk 'NR==2 {print $4}') free under /var/lib"
case "${ID:-}" in rhel|rocky|almalinux|centos|ol) ;; *) warn "this is not a RHEL family system (ID=${ID:-?}); Docker install steps assume dnf and Docker's RHEL repository" ;; esac
[ "${VERSION_ID%%.*}" = "8" ] || [ "${VERSION_ID%%.*}" = "9" ] || warn "tested on RHEL 8 and 9, this is ${VERSION_ID:-?}"
if [ "$CHECK" = 0 ] && [ "$(id -u)" != 0 ]; then echo "run with sudo (or use --check)" >&2; exit 1; fi
avail_kb=$(df -k /var/lib 2>/dev/null | awk 'NR==2 {print $4}'); [ -n "$avail_kb" ] && [ "$avail_kb" -lt $((40 * 1024 * 1024)) ] && warn "/var/lib has less than 40 GB free: images, PostgreSQL and the live spool live in /var/lib/docker; add a volume or set data-root in /etc/docker/daemon.json"

# ---- Docker CE + Compose v2 (Red Hat ships podman; the Watchover scripts use 'docker compose')
if [ "$DOCKER" = 1 ]; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    say "docker already installed: $(docker --version 2>/dev/null) · $(docker compose version 2>/dev/null)"
  else
    say "installing Docker CE + Compose v2 from download.docker.com (podman / buildah / runc are removed first: they conflict)"
    run dnf -y remove podman buildah runc docker docker-client docker-common docker-engine 2>/dev/null || true
    run dnf -y install dnf-plugins-core
    if [ "$CHECK" = 1 ]; then echo "  would run: dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo"; else
      dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo 2>/dev/null || dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo; fi
    run dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  run systemctl enable --now docker
  if [ ! -f /etc/docker/daemon.json ]; then
    say "writing /etc/docker/daemon.json (json-file logs capped at 50 MB x 5 per container, live-restore so a daemon restart keeps containers up)"
    [ "$CHECK" = 1 ] || { mkdir -p /etc/docker; printf '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "50m", "max-file": "5"},\n  "live-restore": true\n}\n' > /etc/docker/daemon.json; systemctl restart docker; }
  fi
  if id "$USER_NAME" >/dev/null 2>&1 && ! id -nG "$USER_NAME" | grep -qw docker; then say "adding $USER_NAME to the docker group (sign out and in again afterwards)"; run usermod -aG docker "$USER_NAME"; fi
fi

# ---- firewalld: who talks to Watchover
if [ "$FIREWALL" = 1 ] && command -v firewall-cmd >/dev/null 2>&1 && (systemctl is-active firewalld >/dev/null 2>&1 || [ "$CHECK" = 1 ]); then
  say "firewalld: 8501/tcp (dashboard), 8600/tcp (agents), 443/tcp (edge TLS)"
  for p in 8501/tcp 8600/tcp 443/tcp; do run firewall-cmd --permanent --add-port="$p" >/dev/null; done
  run firewall-cmd --reload >/dev/null
  say "note: docker also publishes ports through its own iptables chains; firewalld rules above keep the intent documented and survive a docker reinstall"
else
  [ "$FIREWALL" = 1 ] && warn "firewalld not running: open 8501, 8600 and 443/tcp on whatever filters this host"
fi

# ---- SELinux: Docker CE runs with SELinux disabled for containers by default (selinux-enabled=false), bind mounts work as is
if command -v getenforce >/dev/null 2>&1; then say "SELinux: $(getenforce 2>/dev/null) (Docker CE default keeps containers unconfined; nothing to change. If you enable selinux-enabled in daemon.json, mount ./certs and ./docker with :z)"; fi

# ---- time: anomaly timestamps and TLS depend on it
if ! systemctl is-active chronyd >/dev/null 2>&1; then say "enabling chronyd (time sync)"; run dnf -y install chrony >/dev/null 2>&1 || true; run systemctl enable --now chronyd || true; fi

say "done. Next: git clone (or unpack the bundle) into /opt/watchover-docker and run ./watchover.sh install  (air-gapped: ./watchover.sh unbundle FILE first)"
