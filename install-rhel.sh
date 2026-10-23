#!/usr/bin/env bash
# Watchover on Red Hat Enterprise Linux 8.6+, 9 or 10 (Rocky Linux / AlmaLinux / Oracle Linux / CentOS Stream alike) with rootful
# Podman (4.0 or newer; RHEL 9.5+ / 10 bring 5.x): one command, nothing to answer.
#
#   sudo ./install-rhel.sh          (from the unzipped release folder; without the exec bit: sudo bash install-rhel.sh)
#
# In order; every step is safe to repeat: a second run repairs (units, firewall, containers), a run from a newer release folder
# upgrades (a backup is taken first). Nothing is changed before the preflight has passed.
#   1. preflight: RHEL 8.6+, 9.x or 10.x (Rocky / Alma / Oracle Linux / CentOS Stream alike; 7 and 8.0-8.5: refused), CPU architecture (x86_64 / aarch64), CPU / RAM / disk
#      minimums, VM / AVX2 / cgroup v2 notes, SELinux, time sync, ports 80 / 443 / 8600 / 5433 free, package repositories (dnf
#      repolist), every internet host the install needs (the failing ones are named for the firewall team), where Podman keeps its
#      storage (local SSD expected, NFS / CIFS warned)
#   2. company proxy (HTTP(S)_PROXY / NO_PROXY in the environment or /etc/environment): dnf, subscription-manager, Podman pulls
#      (containers.conf.d + podman.service drop-in), image builds (build args via .env) and Ollama
#   3. packages via scripts/rhel-prepare.sh --engine podman: podman 5.x, container-selinux, crun, netavark, aardvark-dns (AppStream),
#      Compose v2 (Docker's docker-compose-plugin RPM, or from an offline bundle / a docker-compose-linux-<arch> file next to this
#      script), podman.socket, chrony
#   4. the release into /opt/watchover (root:root, SELinux default labels; .env, certs, backups and in-app releases there are kept)
#      and the 'watchover' command (/usr/local/bin/watchover runs /opt/watchover/watchover.sh as root)
#   5. watchover.sh install --production (secrets in .env 0600, sizing from this machine, PostgreSQL inside on 127.0.0.1:5433
#      only), HTTPS edge with Watchover's internal CA for this server's names and addresses (a company certificate in
#      certs/watchover.crt + .key wins), in-app updater, start on boot, nightly backup 02:15 (14 kept), firewalld 443 / 80 / 8600
#   6. local LLM (bundled CPU Ollama) in the background when the server has 16 GB RAM or more and reaches the model registry
#   7. this server's own agent (wo-agent, as on every monitored server: its journal, logs and metrics from the first minute),
#      installed with the Agents panel's verified command against 127.0.0.1; it replaces the built-in in-container agent
#   8. summary (Turkish) and the go-live checklist; the full log is /var/log/watchover/install-<time>.log
#
# Air-gapped: a watchover-bundle-<version>.tar (./watchover.sh bundle) next to this script, or in the folder above it, is used by
# itself (images, models and the Compose binary come from it; nothing is built or downloaded).
# Expert options, none needed: --check (preflight only, changes nothing)  --dir DIR (default /opt/watchover)  --host NAME (one more
# certificate name, e.g. a DNS alias; it becomes the main address)  --no-llm  --no-updater  --no-host-agent  --skip-netcheck
# Test hooks (never needed on a server): WATCHOVER_TEST_ROOT prefixes /etc, /proc, /sys, /opt, /usr/local and /var (and only its Compose
# path counts); WATCHOVER_TEST_ARCH.
set -Eeuo pipefail
umask 022
ARGS=("$@"); CLEAN_IMAGES=0; CHECK=0; DEST=""; XHOST=""; NO_LLM=0; NO_UPDATER=0; NETCHECK=1; NO_HOST_AGENT=0
while [ $# -gt 0 ]; do case "$1" in
  --check) CHECK=1 ;; --dir) DEST="${2:?--dir DIR}"; shift ;; --host) XHOST="${2:?--host NAME}"; shift ;;
  --no-llm) NO_LLM=1 ;; --no-updater) NO_UPDATER=1 ;; --no-host-agent) NO_HOST_AGENT=1 ;; --skip-netcheck) NETCHECK=0 ;;
  -h|--help) sed -n '2,/^set -E/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) echo "Bilinmeyen seçenek: $1   (seçenek gerekmez: sudo ./install-rhel.sh)" >&2; exit 2 ;;
esac; shift; done

if [ "$(id -u)" != 0 ]; then   # sudo drops the proxy variables: hand them over explicitly
  command -v sudo >/dev/null 2>&1 || { echo "Root yetkisi gerekiyor: su -c '$0'" >&2; exit 1; }
  keep=(); for k in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do [ -z "${!k:-}" ] || keep+=("$k=${!k}"); done
  echo "Root yetkisi gerekiyor: sudo ile yeniden başlatılıyor"
  exec sudo env ${keep[@]+"${keep[@]}"} bash "$0" ${ARGS[@]+"${ARGS[@]}"}
fi

R="${WATCHOVER_TEST_ROOT:-}"
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
DEST=$(realpath -m "${DEST:-$R/opt/watchover}")
LOGDIR="$R/var/log/watchover"; mkdir -p "$LOGDIR"; chmod 700 "$LOGDIR"
LOG="$LOGDIR/install-$(date +%Y%m%d-%H%M%S).log"; LLMLOG="$LOGDIR/llm-setup.log"
(umask 077; : > "$LOG")
exec > >(tee -a "$LOG") 2>&1

FATAL=(); NOTES=()
say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
note() { printf '  \033[33m!\033[0m %s\n' "$*"; NOTES+=("$*"); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FATAL+=("$*"); }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n  Ayrıntılı kayıt: %s\n' "$*" "$LOG"; exit 1; }
die_tail() { printf '  ── kayıttan son satırlar ──\n'; tail -n 25 "$LOG" | sed 's/^/  │ /'; die "$@"; }
mask() { printf '%s' "$1" | sed -E 's#(://)[^/@]*@#\1***@#'; }          # a proxy password is never printed
trap 'printf "\n\033[1;31m✗ Beklenmeyen hata (satır %s): %s\033[0m\n  Ayrıntılı kayıt: %s\n" "$LINENO" "$BASH_COMMAND" "$LOG"' ERR
# CMD's output goes to the log only; a line every two minutes shows that a long step (image builds) is alive
quiet() {
  local msg="$1" pid rc=0 t=0 d; shift
  say "$msg"; printf '\n### %s  %s\n' "$(date '+%F %T')" "$*" >> "$LOG"
  ( trap - ERR; "$@" ) >> "$LOG" 2>&1 < /dev/null & pid=$!
  for d in 0.1 0.2 0.5 1 2; do kill -0 "$pid" 2>/dev/null || break; sleep "$d"; done
  while kill -0 "$pid" 2>/dev/null; do sleep 5; t=$((t + 5)); [ $((t % 120)) -ne 0 ] || printf '    … %d dk sürüyor (ayrıntı: tail -f %s)\n' $((t / 60)) "$LOG"; done
  wait "$pid" || rc=$?
  return "$rc"
}
ver_ge() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$2" ]; }     # A >= B
ver_of() { { sed -n 's/^version = "\(.*\)"/\1/p' "$1/pyproject.toml" 2>/dev/null || true; } | head -1; }
near() { local p="$1"; while [ ! -e "$p" ] && [ "$p" != / ]; do p=$(dirname "$p"); done; echo "$p"; }
free_gb() { { df -Pk "$(near "$1")" 2>/dev/null || true; } | awk 'NR==2 {printf "%d", $4 / 1048576}'; }
ENVF="$DEST/.env"
eget() { { sed -n "s/^$1=//p" "${2:-$ENVF}" 2>/dev/null || true; } | head -1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//'; }
eset() {   # same file rewritten in place (owner and mode stay), like watchover.sh env_set
  local t; if grep -q "^$1=" "$ENVF" 2>/dev/null; then t=$(awk -v k="$1" -v v="$2" 'index($0, k "=") == 1 { print k "=" v; next } { print }' "$ENVF") && printf '%s\n' "$t" > "$ENVF"
  else printf '%s=%s\n' "$1" "$2" >> "$ENVF"; fi
}
W() { WATCHOVER_ENGINE=podman "$DEST/watchover.sh" "$@"; }
[ -z "$R" ] || export WATCHOVER_SYSTEMD_DIR="${WATCHOVER_SYSTEMD_DIR:-$R/etc/systemd/system}"
UNITS="${WATCHOVER_SYSTEMD_DIR:-/etc/systemd/system}"
have() { command -v "$1" >/dev/null 2>&1; }
running() { [ "$(podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }
rpm_ok() { rpm -q "$@" >/dev/null 2>&1; }
compose_path() {   # the places watchover.sh looks at (a test root: only its own)
  local c l=("${WATCHOVER_COMPOSE_BIN:-}" "$R/usr/local/lib/docker/cli-plugins/docker-compose")
  [ -n "$R" ] || l+=("$(command -v docker-compose 2>/dev/null || true)" /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose)
  for c in "${l[@]}"; do [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }; done; return 1
}
compose_ok() { local c v; c=$(compose_path) || return 1; v=$("$c" version --short 2>/dev/null | tr -d 'v'); [ -n "$v" ] && ver_ge "$v" 2.24.4; }   # !reset in the edge override

# ---------------------------------------------------------------------------------------------------------------- host facts
osv() { { sed -n "s/^$1=//p" "$R/etc/os-release" 2>/dev/null || true; } | head -1 | tr -d "\"'"; }
OS_ID=$(osv ID); OS_VER=$(osv VERSION_ID); OS_NAME=$(osv PRETTY_NAME)
ARCH="${WATCHOVER_TEST_ARCH:-$(uname -m)}"
NCPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)
MEM_KB=$(awk '/^MemTotal:/ {print $2}' "$R/proc/meminfo" 2>/dev/null || true); MEM_KB=${MEM_KB:-0}
# Installed RAM, not MemTotal: the kernel, kdump's crashkernel and the firmware keep part of it out of MemTotal (a 4096 MB VM reads
# about 3.2-3.6 GiB there and was refused as "3 GB"). /proc/iomem (root) lists all of it; without it MemTotal is rounded up.
PHYS_KB=0
if [ -r "$R/proc/iomem" ]; then
  while IFS=- read -r a rest; do b=${rest%% *}; PHYS_KB=$(( PHYS_KB + (16#$b - 16#$a + 1) / 1024 )); done \
    < <(grep -E '^[0-9a-fA-F]+-[0-9a-fA-F]+ : System RAM$' "$R/proc/iomem" 2>/dev/null || true)
fi
MEM_GB=$(( (MEM_KB + 1048575) / 1048576 )); _p=$(( (PHYS_KB + 524288) / 1048576 )); [ "$_p" -le "$MEM_GB" ] || MEM_GB=$_p
MEM_USE=$(awk -v k="$MEM_KB" 'BEGIN { printf "%.1f", k / 1048576 }')
VIRT=$(systemd-detect-virt 2>/dev/null || true); [ "$VIRT" != none ] || VIRT=""
SRC_VER=$(ver_of "$SRC"); [ -n "$SRC_VER" ] || die "Bu klasörde Watchover yok (pyproject.toml): betiği açılan sürüm klasörünün içinden çalıştırın"
OLD_VER=""; [ "$SRC" = "$DEST" ] || OLD_VER=$(ver_of "$DEST")
STORE="$R/var/lib/containers"
if [ -z "$R" ] && have podman; then gr=$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null || true); [ -z "$gr" ] || STORE="$gr"; fi
FQDN=$(hostname -f 2>/dev/null || true); SHORT=$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)
mapfile -t IPS < <(ip -4 -o addr show scope global 2>/dev/null | awk '$2 !~ /^(podman|cni|veth|docker|virbr|br-|vnet|flannel|cali|tun|wg)/ {split($4, a, "/"); print a[1]}')
[ "${#IPS[@]}" -gt 0 ] || mapfile -t IPS < <(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' | grep -v '^10\.8[89]\.' || true)
name_ok() { case "$1" in ""|localhost|localhost.*|*[!A-Za-z0-9.-]*) return 1 ;; esac; }
DEFIF=$(ip route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')

case "$OS_ID" in
  rhel) OS_LABEL="${OS_NAME:-RHEL $OS_VER}" ;;
  rocky|almalinux|ol|centos) OS_LABEL="${OS_NAME:-$OS_ID $OS_VER} · RHEL ${OS_VER%%.*} uyumlu" ;;
  *) OS_LABEL="${OS_NAME:-bilinmiyor}" ;;
esac
printf '\n\033[1mWatchover v%s · RHEL kurulumu (Podman)\033[0m   %s\n' "$SRC_VER" "$(date '+%F %T')"
say "Sunucu: $OS_LABEL · $ARCH · $NCPU CPU · $MEM_GB GB RAM (kullanılabilir $MEM_USE GB)${VIRT:+ · sanal makine ($VIRT)}"

# ---------------------------------------------------------------------------------------------------------------- proxy
envfile_get() { { sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1=//p" "$R/etc/environment" 2>/dev/null || true; } | tail -1 | sed "s/^[\"']//; s/[\"']\$//"; }
pick() { local k v; for k in "$@"; do v="${!k:-}"; [ -z "$v" ] || { echo "$v"; return 0; }; done
         for k in "$@"; do v=$(envfile_get "$k"); [ -z "$v" ] || { echo "$v"; return 0; }; done; }
PX_HTTPS=$(pick HTTPS_PROXY https_proxy); PX_HTTP=$(pick HTTP_PROXY http_proxy); PX_NO=$(pick NO_PROXY no_proxy); PX_FROM="ortam / /etc/environment"
if [ -z "$PX_HTTPS$PX_HTTP" ] && [ -f "$ENVF" ] && [ -n "$(eget WATCHOVER_HTTPS_PROXY)" ]; then
  PX_HTTPS=$(eget WATCHOVER_HTTPS_PROXY); PX_HTTP=$(eget WATCHOVER_HTTP_PROXY); PX_NO=$(eget WATCHOVER_NO_PROXY); PX_FROM=".env (önceki kurulum)"
fi
if [ -z "$PX_HTTPS$PX_HTTP" ]; then PX_HTTP=$({ sed -n 's/^[[:space:]]*proxy[[:space:]]*=[[:space:]]*//p' "$R/etc/dnf/dnf.conf" 2>/dev/null || true; } | head -1); PX_FROM="/etc/dnf/dnf.conf"; fi
[ -n "$PX_HTTPS" ] || PX_HTTPS="$PX_HTTP"; [ -n "$PX_HTTP" ] || PX_HTTP="$PX_HTTPS"
if [ -n "$PX_HTTPS" ]; then
  nl="$PX_NO,localhost,127.0.0.1,::1,0.0.0.0,postgres,dashboard,watchover,receiver,keyd,ollama,trainer,mcp,edge,updater,legacy"
  nl="$nl,watchover-postgres,watchover-receiver,watchover-keyd,watchover-ollama,watchover-mcp,watchover-edge,$FQDN,$SHORT,${IPS[*]+${IPS[*]}}"
  PX_NO=$(echo "$nl" | tr ' ,' '\n\n' | awk 'NF && !seen[$0]++' | paste -sd, -)
  export HTTP_PROXY="$PX_HTTP" HTTPS_PROXY="$PX_HTTPS" NO_PROXY="$PX_NO" http_proxy="$PX_HTTP" https_proxy="$PX_HTTPS" no_proxy="$PX_NO"
  say "Kurumsal proxy: $(mask "$PX_HTTPS") (kaynak: $PX_FROM)"
fi

# ---------------------------------------------------------------------------------------------------------------- offline bundle
BUNDLE=""; MF=""; OFFLINE=0
mf() { printf '%s\n' "$MF" | sed -n "s/^$1=//p" | head -1; }
for d in "$SRC" "$(dirname "$SRC")"; do
  for f in "$d"/watchover-bundle-*.tar; do
    [ -f "$f" ] || continue
    m=$(tar xOf "$f" --occurrence=1 MANIFEST 2>/dev/null || true)
    if [ "$(printf '%s\n' "$m" | sed -n 's/^version=//p')" = "$SRC_VER" ]; then BUNDLE="$f"; MF="$m"; break 2; fi
    [ -n "$BUNDLE" ] || BUNDLE_OTHER="${BUNDLE_OTHER:-} $(basename "$f")"
  done
done
[ -z "$BUNDLE" ] || OFFLINE=1
[ "$OFFLINE" = 1 ] || [ "$(eget WATCHOVER_OFFLINE)" != 1 ] || OFFLINE=2          # an offline install repaired without a bundle

# ---------------------------------------------------------------------------------------------------------------- preflight
say "Ön kontrol"
OS_MAJOR=${OS_VER%%.*}; case "$OS_MAJOR" in ""|*[!0-9]*) OS_MAJOR=0 ;; esac
case "$OS_ID" in
  rhel|rocky|almalinux|ol|centos)
    major=$OS_MAJOR; minor=${OS_VER#*.}; [ "$minor" != "$OS_VER" ] || minor=0; case "$minor" in ""|*[!0-9]*) minor=0 ;; esac
    if [ "$major" -le 7 ]; then bad "$OS_LABEL desteklenmez: RHEL 7 ve öncesinde Podman 4 ve cgroup v2 yok (RHEL 7'nin desteği bitti). RHEL 8.6+, 9 ya da 10 kullanın"
    elif [ "$major" = 8 ] && [ "$minor" -lt 6 ]; then bad "$OS_LABEL: RHEL 8'de en az 8.6 gerekir (Podman 4.x). Güncelleyin: sudo dnf -y update && sudo reboot"
    elif [ "$major" = 8 ]; then ok "İşletim sistemi: $OS_LABEL (Podman 4.x; desteklenen: 8.6+, 9, 10)"
    elif [ "$major" = 9 ] && [ "$minor" -lt 5 ]; then ok "İşletim sistemi: $OS_LABEL (Podman 4.x; desteklenen: 8.6+, 9, 10; 9.5+ Podman 5 getirir)"
    elif [ "$major" -le 10 ]; then ok "İşletim sistemi: $OS_LABEL (desteklenen: 8.6+, 9, 10)"
    else ok "İşletim sistemi: $OS_LABEL (desteklenen: 8.6+, 9, 10; daha yenisi de denenir)"; fi ;;
  *) bad "Bu kurulum Red Hat ailesi içindir (RHEL 8.6+, 9, 10; Rocky, AlmaLinux, Oracle Linux, CentOS Stream da olur); bu sunucu: $OS_LABEL" ;;
esac
case "$ARCH" in x86_64|aarch64) ok "İşlemci mimarisi: $ARCH" ;; *) bad "İşlemci mimarisi $ARCH desteklenmiyor (x86_64 ya da aarch64)" ;; esac
if [ "${#FATAL[@]}" -gt 0 ]; then printf '\n\033[1;31mKurulum yapılmadı (hiçbir şey değiştirilmedi).\033[0m\n  Ayrıntılı kayıt: %s\n' "$LOG"; exit 1; fi
if [ "$NCPU" -lt 2 ]; then bad "En az 2 CPU gerekir (bu sunucu: $NCPU)"; elif [ "$NCPU" -lt 4 ]; then note "$NCPU CPU: çalışır, 4+ CPU önerilir"; else ok "CPU: $NCPU"; fi
if [ "$MEM_GB" -lt 4 ]; then bad "En az 4 GB RAM gerekir (bu sunucu: $MEM_GB GB)"; elif [ "$MEM_GB" -lt 8 ]; then note "RAM $MEM_GB GB: çalışır, 8 GB+ önerilir (yerel LLM için 16 GB+)"; else ok "RAM: $MEM_GB GB"; fi
[ -z "$VIRT" ] || note "Sanal makinede çalışıyor ($VIRT): üretimde CPU / RAM / diski ayrılmış verin"
if [ "$ARCH" = x86_64 ] && ! grep -qw avx2 "$R/proc/cpuinfo" 2>/dev/null; then note "İşlemcide AVX2 yok (sanal makinede CPU tipi 'host' değilse olur): yerel LLM çok yavaş çalışır"; fi
if [ "$ARCH" = aarch64 ] && ! grep -qw asimd "$R/proc/cpuinfo" 2>/dev/null; then note "İşlemcide NEON (asimd) görünmüyor: yerel LLM çok yavaş çalışır"; fi
sf=$(free_gb "$STORE"); sf=${sf:-0}
# Watchover images of releases nobody keeps any more (an upgrade with this script tags the new release and left the old ones):
# not this folder's version, not the installed one (the way back), not the in-app updater's current / previous. Sizes overlap
# (shared layers), so the reclaimable figure is an upper bound and the space is measured again after the removal.
keep_vers() { printf '%s\n' "$SRC_VER" "$OLD_VER"; [ ! -f "$DEST/.releases/index.json" ] || grep -oE '"(current|previous)": *"[^"]*"' "$DEST/.releases/index.json" | sed -E 's/.*: *"v?([^"]*)"/\1/' || true; }
old_images() {   # "repo:tag" per line
  [ -z "$R" ] && have podman || return 0
  local keep; keep=" $(keep_vers | sed 's/^v//' | tr '\n' ' ') "
  podman images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E '/watchover(-receiver|-trainer|-postgres|-ollama)?:[0-9]+\.[0-9]+\.[0-9]+' | while IFS= read -r ref; do
    case "$keep" in *" ${ref##*:} "*) ;; *) echo "$ref" ;; esac
  done
}
OLD_IMGS=$(old_images || true); OLD_GB=0
if [ -n "$OLD_IMGS" ]; then
  OLD_GB=$(for i in $OLD_IMGS; do podman image inspect --format '{{.Size}}' "$i" 2>/dev/null || true; done | awk '{s += $1} END {printf "%d", s / 1073741824}')
fi
if [ "$sf" -lt 20 ] && [ -n "$OLD_IMGS" ] && [ $((sf + OLD_GB)) -ge 20 ]; then
  note "Konteyner deposunda $sf GB boş: eski sürümlerin imajları silinecek ($(echo "$OLD_IMGS" | sed 's#.*/##; s#:.*##' | sort | uniq -c | awk '{printf "%s%s ×%s", (NR > 1 ? ", " : ""), $2, $1}'); en çok ~$OLD_GB GB; kurulu sürüm ve geri dönüş sürümü kalır)"
  CLEAN_IMAGES=1
elif [ "$sf" -lt 20 ]; then CLEAN_IMAGES=0; bad "Konteyner deposunda ($(near "$STORE")) en az 20 GB boş alan gerekir (boş: $sf GB$([ -z "$OLD_IMGS" ] || echo ", eski sürüm imajları silinse ~$((sf + OLD_GB)) GB")): diski büyütün ya da 'podman images' ile Watchover dışı imajları silin"
elif [ "$sf" -lt 40 ]; then note "Konteyner deposunda $sf GB boş: 40 GB+ önerilir (imajlar, PostgreSQL, loglar; yerel LLM ~15 GB)"
else ok "Konteyner deposu: $(near "$STORE"), $sf GB boş"; fi
if [ -n "$OLD_IMGS" ] && [ "$CLEAN_IMAGES" = 0 ] && [ "$sf" -ge 20 ]; then
  CLEAN_IMAGES=1; ok "Eski sürüm imajları silinecek: $(echo "$OLD_IMGS" | wc -l) imaj, en çok ~$OLD_GB GB (kurulu sürüm ve geri dönüş sürümü kalır)"
fi
# An offline bundle needs room to be loaded, not only to run: the image archive copied out of it, Podman's unpacked copy while it
# loads (in /var/tmp), the loaded layers (about 2.5 x the gzip), the model volumes. 20 GB free passed while a 15 GB bundle needed
# far more, and the load stopped half way with "no space left on device". Sizes come from the bundle's own listing.
if [ -n "$BUNDLE" ]; then
  read -r BG BV < <(tar tvf "$BUNDLE" 2>/dev/null | awk '{ n = $NF; if (n ~ /(^|\/)images\.tar\.gz$/) g += $3; else if (n ~ /(^|\/)volumes\/[^\/]+\.tar$/) v += $3 } END { printf "%d %d\n", g / 1048576, v / 1048576 }') || true
  BG=${BG:-0}; BV=${BV:-0}; sfree=$((sf + (CLEAN_IMAGES == 1 ? OLD_GB : 0)))
  TMPD="${TMPDIR:-/var/tmp}"; need_store=$(( (BG * 5 / 2 + BV) / 1024 + 5 )); need_tmp=$(( BG * 7 / 2 / 1024 + 2 ))
  if [ "$(stat -c %d "$(near "$TMPD")" 2>/dev/null)" = "$(stat -c %d "$(near "$STORE")" 2>/dev/null)" ]; then
    need=$((need_store + need_tmp))
    if [ "$sfree" -lt "$need" ]; then bad "Kapalı ağ paketini yüklemek için $(near "$STORE") diskinde ~$need GB boş alan gerekir (boş: $sfree GB): imajlar yüklenirken geçici kopyaları da aynı diske yazılır. Diski büyütün (önerilen: 150 GB) ya da TMPDIR=<başka disk> ile çalıştırın"
    else ok "Paketi yüklemek için yer: ~$need GB gerekir, $sfree GB boş"; fi
  else
    tf=$(free_gb "$TMPD"); tf=${tf:-0}
    [ "$sfree" -ge "$need_store" ] || bad "Kapalı ağ paketi için konteyner deposunda ~$need_store GB boş alan gerekir (boş: $sfree GB): diski büyütün"
    [ "$tf" -ge "$need_tmp" ] || bad "Kapalı ağ paketi yüklenirken geçici klasörde ($(near "$TMPD")) ~$need_tmp GB boş alan gerekir (boş: $tf GB)"
    [ "$sfree" -lt "$need_store" ] || [ "$tf" -lt "$need_tmp" ] || ok "Paketi yüklemek için yer: depoda ~$need_store GB, geçici klasörde ~$need_tmp GB gerekir"
  fi
fi
df_=$(free_gb "$DEST"); [ "${df_:-0}" -ge 2 ] || bad "Kurulum klasörü için ($(near "$DEST")) en az 2 GB boş alan gerekir"
fst=$(findmnt -no FSTYPE -T "$(near "$STORE")" 2>/dev/null || stat -f -c %T "$(near "$STORE")" 2>/dev/null || true)
case "$fst" in
  nfs*|cifs|smb*|fuse*|glusterfs|ceph|9p|virtiofs) note "$(near "$STORE") ağ / paylaşımlı dosya sisteminde ($fst): PostgreSQL ve imajlar için yavaş ve riskli. /var/lib/containers yerel SSD'de olmalı" ;;
  *) dev=$(findmnt -no SOURCE -T "$(near "$STORE")" 2>/dev/null || true)
     [ "$(lsblk -ndo ROTA "$dev" 2>/dev/null | head -1 | tr -d ' ')" != 1 ] || note "$(near "$STORE") döner diskte (HDD) görünüyor: /var/lib/containers yerel SSD'de olmalı (sanal diskte bu bilgi yanıltıcı olabilir)" ;;
esac
se=$(getenforce 2>/dev/null || echo yok)
case "$se" in Enforcing) ok "SELinux: Enforcing (öyle kalır)" ;; Permissive) note "SELinux Permissive: üretimde Enforcing önerilir (Watchover Enforcing ile çalışır)" ;; *) note "SELinux: $se" ;; esac
cg=$(stat -fc %T "$R/sys/fs/cgroup" 2>/dev/null || true)
if [ -n "$cg" ] && [ "$cg" != cgroup2fs ] && [ "$OS_MAJOR" = 8 ]; then ok "cgroup v1 (RHEL 8 varsayılanı): root Podman 4.x ile çalışır"; cg=cgroup2fs; fi
[ -z "$cg" ] || [ "$cg" = cgroup2fs ] || note "cgroup v1 ($cg): Podman 5 cgroup v2 ister (RHEL 9 / 10 varsayılanı): sudo grubby --update-kernel=ALL --remove-args=systemd.unified_cgroup_hierarchy, sonra yeniden başlatın"
case "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" in yes) ok "Saat eşitlenmiş (NTP)" ;; *) note "Saat henüz eşitlenmemiş: chronyd açılacak (zaman damgaları ve TLS için gerekli)" ;; esac

# where the files go, what was there before
PREV=""
if [ ! -f "$ENVF" ]; then
  [ ! -f "$UNITS/watchover-stack.service" ] || PREV=$(sed -n 's/^WorkingDirectory=//p' "$UNITS/watchover-stack.service" | head -1)
  [ -n "$PREV" ] || ! have podman || PREV=$(podman inspect watchover --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
  [ "$PREV" != "$DEST" ] && [ -f "$PREV/.env" ] || PREV=""
fi
for e in "$ENVF" "${PREV:+$PREV/.env}"; do
  [ -n "$e" ] && [ -f "$e" ] || continue
  [ "$(eget WATCHOVER_ENGINE "$e")" != docker ] || bad "$(dirname "$e") Docker ile kurulu (WATCHOVER_ENGINE=docker): Podman'a geçiş otomatik değil. Yedek alın (./watchover.sh backup), Docker'ı kaldırın, bu betiği çalıştırın, sonra ./watchover.sh restore"
done
if [ ! -f "$ENVF" ] && [ -z "$PREV" ] && have podman && podman volume exists watchover_watchover-pg 2>/dev/null; then
  bad "Önceki bir kurulumun veritabanı birimi var ama .env dosyası (veritabanı parolası) bulunamadı: eski .env'yi $ENVF olarak kopyalayıp yeniden çalıştırın"
fi
if [ -n "$OLD_VER" ]; then
  if ! ver_ge "$SRC_VER" "$OLD_VER"; then bad "$DEST içinde daha yeni bir sürüm kurulu (v$OLD_VER > v$SRC_VER): eski sürüme dönmek için arayüzde Sistem › Güncelleme › Geri al"
  elif [ "$SRC_VER" != "$OLD_VER" ]; then ok "Yükseltme: v$OLD_VER → v$SRC_VER (önce yedek alınır)"
  else ok "Aynı sürüm kurulu (v$SRC_VER): onarım olarak çalışır"; fi
fi
[ -z "$(find "$DEST/.updater/busy" -mmin -180 2>/dev/null || true)" ] || bad "Uygulama içi bir güncelleme sürüyor ($DEST/.updater/busy): bitince yeniden çalıştırın"

# offline bundle and the edge's image (whatever docker-compose.yml's 'edge' service uses: this script names no proxy product)
EDGE=1; LLM_OFFLINE_OK=0
EDGE_IMG=$(awk '/^  edge:/ {e = 1; next} e && /^  [A-Za-z]/ {exit} e && /^    image:/ {print $2; exit}' "$SRC/docker-compose.yml" 2>/dev/null || true)
qual() { case "$1" in */*/*|*.*/*) echo "$1" ;; */*) echo "docker.io/$1" ;; *) echo "docker.io/library/$1" ;; esac; }   # Podman's full name
edge_img_ok() { [ -n "$EDGE_IMG" ] && podman image exists "$(qual "$EDGE_IMG")" 2>/dev/null; }
if [ "$OFFLINE" = 1 ]; then
  ok "Kapalı ağ paketi: $(basename "$BUNDLE") (imajlar, modeller ve Compose buradan; internet kullanılmaz)"
  a=$(mf arch); [ -z "$a" ] || [ "$a" = "$ARCH" ] || bad "Paket $a için hazırlanmış, bu sunucu $ARCH: paketi $ARCH bir makinede hazırlayın (./watchover.sh bundle)"
  case " $(mf images) " in *" ${EDGE_IMG:-edge-image} "*) ;; *) EDGE=0; note "Pakette HTTPS (edge) imajı ${EDGE_IMG:-?} yok: panel düz http://…:8501 ile açılır (internetli makinede 'edge on' sonra 'bundle')" ;; esac
  im=" $(mf images) "; case "$im" in *" watchover-ollama:"*|*" ollama/ollama:"*) case "$im $(mf volumes)" in *" watchover-trainer:"*watchover-ollama*) LLM_OFFLINE_OK=1 ;; esac ;; esac
elif [ -n "${BUNDLE_OTHER:-}" ]; then
  bad "Kapalı ağ paketi bulundu ama sürümü bu klasörle aynı değil (v$SRC_VER):${BUNDLE_OTHER}. Aynı sürümün paketini koyun"
elif [ "$OFFLINE" = 2 ]; then
  ok "Kapalı ağ kurulumu (WATCHOVER_OFFLINE=1): internet kullanılmaz"
  [ -z "$OLD_VER" ] || [ "$OLD_VER" = "$SRC_VER" ] || bad "Kapalı ağ kurulumu yükseltiliyor: watchover-bundle-$SRC_VER.tar dosyasını bu betiğin yanına koyun"
  edge_img_ok || EDGE=0
fi

# ports
port_of() { local v=""; [ ! -f "$ENVF" ] || v=$(eget "$1"); v="${v##*:}"; echo "${v:-$2}"; }
HTTPS_PORT=$(port_of WATCHOVER_EDGE_HTTPS_PORT 443); HTTP_PORT=$(port_of WATCHOVER_EDGE_HTTP_PORT 80); LIVE_PORT=$(port_of WATCHOVER_LIVE_PORT 8600); UI_PORT=$(port_of WATCHOVER_UI_PORT 8501)
if have ss; then
  specs="$LIVE_PORT:watchover-receiver:ajanlar 5433:watchover-postgres:PostgreSQL"
  if [ "$EDGE" = 1 ]; then specs="$HTTPS_PORT:watchover-edge:HTTPS $HTTP_PORT:watchover-edge:HTTP-yönlendirme $specs"; else specs="$UI_PORT:watchover:panel $specs"; fi
  busy=""
  for s in $specs; do
    p=${s%%:*}; c=${s#*:}; c=${c%%:*}; what=${s##*:}
    [ -n "$(ss -Hltn "sport = :$p" 2>/dev/null || true)" ] || continue
    have podman && running "$c" && continue                           # our own container holds it (a re-run)
    who=$(ss -Hltnp "sport = :$p" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1 || true); busy=1
    bad "Port $p ($what) kullanımda: ${who:-bilinmeyen süreç}. Durdurun (örn. sudo systemctl disable --now ${who:-<servis>}) ya da başka sunucuya kurun"
  done
  [ -n "$busy" ] || ok "Portlar boş: $([ "$EDGE" = 1 ] && echo "$HTTPS_PORT, $HTTP_PORT" || echo "$UI_PORT"), $LIVE_PORT, 5433"
else note "ss yok: port kontrolü atlandı"; fi

# packages and repositories
NEED_PKGS=0; rpm_ok podman container-selinux crun netavark aardvark-dns openssl || NEED_PKGS=1   # openssl: the edge certificate is issued on the host
LOCAL_COMPOSE=""; for f in "$SRC/docker-compose-linux-$ARCH" "$(dirname "$SRC")/docker-compose-linux-$ARCH"; do [ -f "$f" ] && { LOCAL_COMPOSE="$f"; break; }; done
if compose_ok; then COMPOSE_SRC="present"
elif [ "$OFFLINE" = 1 ] && [ "$(mf compose)" = 1 ]; then COMPOSE_SRC="bundle"
elif [ -n "$LOCAL_COMPOSE" ]; then COMPOSE_SRC="file"
elif [ "$OFFLINE" != 0 ]; then COMPOSE_SRC="none"; bad "Compose v2 yok ve internet kullanılmıyor: docker-compose-linux-$ARCH dosyasını (github.com/docker/compose/releases) bu betiğin yanına koyun"
else COMPOSE_SRC="repo"; fi
repo_urls() {   # enabled repositories of /etc/yum.repos.d: "id URL CA CERT KEY" (first baseurl / mirrorlist / metalink; the TLS files
                # dnf uses for it, "-" when none: the Red Hat CDN and Satellite answer with their own CA and want the entitlement certificate)
  cat "$R"/etc/yum.repos.d/*.repo 2>/dev/null | awk '
    function flush() { if (id != "" && en && url != "") print id, url, (ca != "" ? ca : "-"), (crt != "" ? crt : "-"), (key != "" ? key : "-"); id = ""; url = ""; ca = ""; crt = ""; key = ""; en = 1 }
    function val() { v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]]+$/, "", v); return v }
    /^[[:space:]]*\[/ { flush(); id = $0; gsub(/[][[:space:]]/, "", id); next }
    /^[[:space:]]*enabled[[:space:]]*=/ { en = (val() ~ /^(1|[Tt]rue|[Yy]es)/); next }
    /^[[:space:]]*(baseurl|mirrorlist|metalink)[[:space:]]*=/ { if (url == "") { split(val(), a, /[[:space:],]+/); url = a[1] } next }
    /^[[:space:]]*sslcacert[[:space:]]*=/ { ca = val(); next }
    /^[[:space:]]*sslclientcert[[:space:]]*=/ { crt = val(); next }
    /^[[:space:]]*sslclientkey[[:space:]]*=/ { key = val(); next }
    END { flush() }'
}
declare -A REPO_TLS=()   # https://host -> "CA CERT KEY" of its repository (the probe uses what dnf uses)
REPO_HOSTS=""
# Podman's RPMs carried by the all-in-one setup file (watchover setup --offline; rpms/ next to this folder): used when they were
# downloaded on the same RHEL major version and CPU architecture, so an air-gapped server without Satellite needs no repository
LOCAL_RPMS=""
for d in "$SRC/rpms" "$(dirname "$SRC")/rpms"; do
  [ -f "$d/MANIFEST" ] && ls "$d"/*.rpm >/dev/null 2>&1 || continue
  ro=$(sed -n 's/^os=//p' "$d/MANIFEST" | head -1); ra=$(sed -n 's/^arch=//p' "$d/MANIFEST" | head -1)
  if [ "$ro" = "el$OS_MAJOR" ] && [ "$ra" = "$ARCH" ]; then LOCAL_RPMS="$d"; break; fi
  note "Paketteki Podman RPM'leri $ro / $ra için, bu sunucu el$OS_MAJOR / $ARCH: paketler depodan kurulacak"
done
if [ "$NEED_PKGS" = 1 ] && [ -n "$LOCAL_RPMS" ] && [ "$COMPOSE_SRC" != repo ]; then
  ok "Podman paketleri kurulum dosyasından: $(ls "$LOCAL_RPMS"/*.rpm | wc -l) RPM (el$OS_MAJOR, $ARCH); paket deposu gerekmiyor"
elif [ "$NEED_PKGS" = 1 ] || [ "$COMPOSE_SRC" = repo ]; then
  repos=$(repo_urls || true)
  if [ -z "$repos" ]; then
    if [ "$OS_ID" = rhel ]; then bad "Etkin paket deposu yok: sunucu Red Hat aboneliğine kayıtlı değil. sudo subscription-manager register (Satellite: --org ORG --activationkey ANAHTAR)"
    else bad "Etkin paket deposu yok (/etc/yum.repos.d): podman paketleri kurulamaz"; fi
  else
    REPO_HOSTS=$(echo "$repos" | awk '{print $2}' | sed -nE 's#^((https?)://[^/]+).*#\1#p' | sort -u | tr '\n' ' ')
    while read -r _id u ca crt key; do
      b=$(echo "$u" | sed -nE 's#^((https?)://[^/]+).*#\1#p'); [ -n "$b" ] && [ -z "${REPO_TLS[$b]:-}" ] && REPO_TLS[$b]="$ca $crt $key"
    done <<< "$repos"
    if out=$(timeout 300 dnf -q repolist --enabled 2>&1); then ok "Paket depoları erişilebilir: $(echo "$repos" | awk '{print $1}' | head -4 | paste -sd' ' -)$([ "$(echo "$repos" | wc -l)" -gt 4 ] && echo ' …')"
    else bad "dnf repolist başarısız: $(echo "$out" | grep -m2 -iE 'error|fail|cannot' | tr '\n' ' ')"; fi
  fi
else ok "Podman paketleri kurulu: paket deposu gerekmiyor"; fi

# internet hosts (none for an offline bundle)
hub_mirror() { cat "$R"/etc/containers/registries.conf "$R"/etc/containers/registries.conf.d/*.conf 2>/dev/null | awk '
  /^[[:space:]]*\[\[registry\]\]/ { hub = 0; inm = 0; next }
  /^[[:space:]]*\[\[registry\.mirror\]\]/ { inm = 1; next }
  /^[[:space:]]*(location|prefix)[[:space:]]*=/ { v = $0; sub(/^[^=]*=[[:space:]]*"/, "", v); sub(/".*/, "", v)
    if (inm && hub) { print v; exit } else if (!inm && v ~ /^(docker\.io|registry-1\.docker\.io|index\.docker\.io)$/) hub = 1 }'; }
probe() {   # URL [CA CERT KEY] -> empty when anything answers (any HTTP status), else the reason
  local rc=0 tls=() ca="${2:--}" crt="${3:--}" key="${4:--}"
  [ "$ca" != - ] && [ -r "$R$ca" ] && tls+=(--cacert "$R$ca")          # a repository's own CA (Red Hat CDN: redhat-uep.pem)
  [ "$crt" != - ] && [ -r "$R$crt" ] && tls+=(--cert "$R$crt")        # its entitlement certificate, as dnf sends it
  [ "$key" != - ] && [ -r "$R$key" ] && tls+=(--key "$R$key")
  curl -sS -o /dev/null --connect-timeout 8 --max-time 20 "${tls[@]}" "$1" 2>/dev/null || rc=$?
  case $rc in 0|22) ;; 6) echo "DNS çözülemiyor" ;; 7) echo "bağlantı reddedildi" ;; 28) echo "zaman aşımı (güvenlik duvarı paketleri düşürüyor olabilir)" ;;
    5) echo "proxy adı çözülemiyor" ;; 56) echo "bağlantı kesildi / proxy izin vermiyor" ;; 97) echo "proxy kimlik doğrulaması" ;;
    35|51|58|59|60|77|83|90|91) echo "TLS doğrulanamadı (proxy SSL denetimi? bu adresler denetimden muaf olmalı)" ;; *) echo "curl hata $rc" ;; esac
}
H=()
if [ "$OFFLINE" = 0 ] && [ "$NETCHECK" = 1 ]; then
  m=$(hub_mirror || true)
  if [ -n "$m" ]; then H+=("https://${m%%/*}/v2/|Docker Hub aynası (registries.conf)|need")
  else H+=("https://registry-1.docker.io|Docker Hub (temel imajlar: python, node, golang, alpine, postgres, edge, ollama)|need" "https://auth.docker.io|Docker Hub oturum|need"
           "https://production.cloudflare.docker.com|Docker Hub katmanları (CDN)|need" "https://docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com|Docker Hub katmanları (R2)|need"); fi
  H+=("https://deb.debian.org|Debian paketleri (python imajında apt)|need" "https://dl-cdn.alpinelinux.org|Alpine paketleri (apk)|need"
      "https://pypi.org|Python paketleri (pip)|need" "https://files.pythonhosted.org|Python paket dosyaları|need" "https://registry.npmjs.org|arayüz paketleri (npm)|need")
  [ "$COMPOSE_SRC" != repo ] || H+=("https://download.docker.com|Compose v2 eklentisi (docker-compose-plugin)|need")
  if [ "$NO_LLM" = 0 ] && [ "$MEM_GB" -ge 16 ]; then
    H+=("https://registry.ollama.ai|yerel LLM modelleri|llm" "https://dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com|yerel LLM model dosyaları|llm"
        "https://download.pytorch.org|eğitim imajı (torch CPU)|llm" "https://huggingface.co|eğitim taban ağırlıkları (ilk eğitimde)|opt")
  fi
fi
for u in $REPO_HOSTS; do H+=("$u|paket deposu (RHEL CDN / Satellite / ayna)|need"); done
LLM_NET_FAIL=""; NET_FAIL=()
if [ "${#H[@]}" -gt 0 ]; then
  tmpd=$(mktemp -d); i=0; pids=()
  for h in "${H[@]}"; do u=${h%%|*}; ( probe "$u" ${REPO_TLS[$u]:-} > "$tmpd/$i" ) & pids+=($!); i=$((i + 1)); done; wait "${pids[@]}" || true
  i=0
  for h in "${H[@]}"; do
    url=${h%%|*}; rest=${h#*|}; why=${rest%|*}; lvl=${rest##*|}; host=${url#*://}; host=${host%%/*}; r=$(cat "$tmpd/$i"); i=$((i + 1))
    if [ -z "$r" ]; then ok "Erişim: $host"; continue; fi
    case "$lvl" in need) bad "Erişilemiyor: $host ($r) — gerekli: $why"; NET_FAIL+=("$host") ;;
                   llm) note "Erişilemiyor: $host ($r) — $why: yerel LLM kurulmayacak"; LLM_NET_FAIL="$LLM_NET_FAIL $host" ;;
                   *) note "Erişilemiyor: $host ($r) — $why" ;; esac
  done
  rm -rf "$tmpd"
  if [ "${#NET_FAIL[@]}" -gt 0 ]; then
    printf '\n  Güvenlik duvarı / proxy ekibine iletin (HTTPS 443/tcp çıkış):\n'; printf '    %s\n' "${NET_FAIL[@]}"
    [ -n "$PX_HTTPS" ] || printf '  Proxy kullanılıyorsa HTTPS_PROXY / NO_PROXY satırlarını /etc/environment dosyasına yazıp yeniden çalıştırın.\n'
  fi
elif [ "$NETCHECK" = 0 ]; then note "İnternet erişim kontrolü atlandı (--skip-netcheck)"; fi

if [ "${#FATAL[@]}" -gt 0 ]; then
  printf '\n\033[1;31mKurulum başlamadı: %d sorun var (hiçbir şey değiştirilmedi).\033[0m\n' "${#FATAL[@]}"
  printf '  - %s\n' "${FATAL[@]}"; printf '  Ayrıntılı kayıt: %s\n' "$LOG"; exit 1
fi
if [ "$CHECK" = 1 ]; then
  say "Ön kontrol tamam (--check: hiçbir şey değiştirilmedi). Kurulum: sudo ./install-rhel.sh"
  printf '  Plan: %s → %s · paketler: %s · Compose: %s · HTTPS: %s · %s\n' "v$SRC_VER" "$DEST" "$([ "$NEED_PKGS" = 1 ] && echo kurulacak || echo kurulu)" \
    "$COMPOSE_SRC" "$([ "$EDGE" = 1 ] && echo açık || echo kapalı)" "$([ "$OFFLINE" = 0 ] && echo internetli || echo 'kapalı ağ')"
  exit 0
fi

# ---------------------------------------------------------------------------------------------------------------- old images
if [ "$CLEAN_IMAGES" = 1 ]; then   # only images; volumes (database, LLM models, logs) are never touched
  n=0; for i in $OLD_IMGS; do podman image rm "$i" >/dev/null 2>&1 && n=$((n + 1)); done
  podman image prune -f >/dev/null 2>&1 || true                       # the untagged layers they leave
  sf=$(free_gb "$STORE"); sf=${sf:-0}
  [ "$sf" -ge 20 ] || die "Eski imajlar silindi ($n) ama konteyner deposunda hâlâ $sf GB boş: en az 20 GB gerekir (diski büyütün)"
  ok "Eski sürüm imajları silindi ($n imaj): konteyner deposunda $sf GB boş"
fi

# ---------------------------------------------------------------------------------------------------------------- proxy settings
proxy_configure() {
  local cc="$R/etc/containers/containers.conf.d/watchover-proxy.conf" dd="$R/etc/systemd/system/podman.service.d/watchover-proxy.conf"
  local dnf="$R/etc/dnf/dnf.conf" mode=644 f k hp t
  if [ -z "$PX_HTTPS" ]; then
    for f in "$cc" "$dd"; do if [ -f "$f" ] && grep -q 'install-rhel.sh' "$f"; then rm -f "$f"; say "eski proxy ayarı kaldırıldı: $f"; fi; done; return 0
  fi
  case "$PX_HTTPS $PX_HTTP" in *://*@*) mode=600 ;; esac
  mkdir -p "$(dirname "$cc")" "$(dirname "$dd")"
  { echo "# written by install-rhel.sh (Watchover): Podman pulls through the company proxy; containers do not inherit it"
    echo "[containers]"; echo "http_proxy = false"; echo; echo "[engine]"
    printf 'env = ["HTTP_PROXY=%s", "HTTPS_PROXY=%s", "NO_PROXY=%s", "http_proxy=%s", "https_proxy=%s", "no_proxy=%s"]\n' \
      "$PX_HTTP" "$PX_HTTPS" "$PX_NO" "$PX_HTTP" "$PX_HTTPS" "$PX_NO"; } > "$cc"
  { echo "# written by install-rhel.sh (Watchover): the Podman API service (compose) pulls through the company proxy"
    echo "[Service]"; for k in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do printf 'Environment="%s=%s"\n' "$k" "${!k}"; done; } > "$dd"
  chmod "$mode" "$cc" "$dd"
  if [ -f "$dnf" ] && grep -Eq '^[[:space:]]*proxy[[:space:]]*=' "$dnf"; then
    grep -Eq "^[[:space:]]*proxy[[:space:]]*=[[:space:]]*$(printf '%s' "$PX_HTTP" | sed 's/[][\.*^$/]/\\&/g')[[:space:]]*$" "$dnf" || note "dnf.conf'ta başka bir proxy tanımlı: dokunulmadı"
  else
    mkdir -p "$(dirname "$dnf")"; [ -f "$dnf" ] || printf '[main]\n' > "$dnf"
    t=$(awk -v p="$PX_HTTP" '{ print } /^\[main\]/ && !d { print "# company proxy (install-rhel.sh)"; print "proxy=" p; d = 1 }' "$dnf"); printf '%s\n' "$t" > "$dnf"
    [ "$mode" = 644 ] || note "Proxy parolası /etc/dnf/dnf.conf içinde (herkes okuyabilir): parolasız proxy ya da proxy_username / proxy_password tercih edin"
  fi
  if [ "$OS_ID" = rhel ] && have subscription-manager && [ -z "$(subscription-manager config --list 2>/dev/null | sed -n 's/^[[:space:]]*proxy_hostname[[:space:]]*=[[:space:]]*\[*\([^]]*\)\]*$/\1/p' | head -1)" ]; then
    hp=${PX_HTTPS#*://}; hp=${hp##*@}; hp=${hp%%/*}
    subscription-manager config --server.proxy_hostname="${hp%%:*}" --server.proxy_port="$([ "${hp##*:}" != "$hp" ] && echo "${hp##*:}" || echo 80)" >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl try-restart podman.service >/dev/null 2>&1 || true                     # running containers stay up
  ok "Proxy: dnf, subscription-manager, Podman (containers.conf.d + podman.service), imaj derlemeleri, Ollama"
}
proxy_configure

# ---------------------------------------------------------------------------------------------------------------- packages
install_compose_file() {   # FILE: the Compose v2 binary as Docker's CLI plugin path (watchover.sh finds it there)
  local dst="$R/usr/local/lib/docker/cli-plugins/docker-compose"
  mkdir -p "$(dirname "$dst")"; install -m 0755 "$1" "$dst"
  ! have restorecon || restorecon "$dst" >/dev/null 2>&1 || true
  "$dst" version >/dev/null 2>&1 || { rm -f "$dst"; return 1; }
}
compose_from_github() {   # last resort online: the release binary, checked against its published sha256
  local t base="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$ARCH"; t=$(mktemp -d)
  curl -fsSL --max-time 600 -o "$t/docker-compose" "$base" && curl -fsSL --max-time 60 -o "$t/sum" "$base.sha256" \
    && [ "$(sha256sum "$t/docker-compose" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "$t/sum")" ] && install_compose_file "$t/docker-compose"
  local rc=$?; rm -rf "$t"; return $rc
}
case "$COMPOSE_SRC" in
  bundle) t=$(mktemp -d); tar xf "$BUNDLE" --occurrence=1 -C "$t" bin/docker-compose 2>/dev/null && install_compose_file "$t/bin/docker-compose" \
            || die "Paketteki Compose çalışmıyor (mimari $ARCH?): docker-compose-linux-$ARCH dosyasını betiğin yanına koyun"; rm -rf "$t"; ok "Compose v2 paketten kuruldu" ;;
  file) install_compose_file "$LOCAL_COMPOSE" || die "$LOCAL_COMPOSE çalışmıyor (mimari $ARCH mi?)"; ok "Compose v2 kuruldu: $(basename "$LOCAL_COMPOSE")" ;;
esac
if c=$(compose_path); then export WATCHOVER_COMPOSE_BIN="$c"; fi          # rhel-prepare.sh then leaves Docker's repository alone
local_rpms() {   # the carried RPMs whose package is not installed yet (installed ones, newer or older, are left alone), from no repository
  local f n list=()
  for f in "$LOCAL_RPMS"/*.rpm; do
    n=$(rpm -qp --qf '%{NAME}' "$f" 2>/dev/null || true); [ -n "$n" ] || n=$(basename "$f" | sed -E 's/-[^-]+-[^-]+\.[^.]+\.rpm$//')
    rpm -q "$n" >/dev/null 2>&1 || list+=("$f")
  done
  [ "${#list[@]}" -eq 0 ] || dnf -y --disablerepo='*' --setopt=install_weak_deps=False --skip-broken install "${list[@]}"
}
if [ "$NEED_PKGS" = 1 ] && [ -n "$LOCAL_RPMS" ] && [ "$COMPOSE_SRC" != repo ]; then
  quiet "Podman paketleri kurulum dosyasından kuruluyor (depo kullanılmaz)" local_rpms || die_tail "Paketteki RPM'ler kurulamadı"
  rpm_ok podman container-selinux || die "Paketteki RPM'lerle Podman kurulamadı: sunucuya paket deposu (Satellite / ayna) tanımlayın ya da paketi bu sürümde hazırlayın"
  NEED_PKGS=0
fi
prep=(--engine podman --no-firewall); [ "$NEED_PKGS" = 1 ] || [ "$COMPOSE_SRC" = repo ] || prep+=(--no-docker)
if ! quiet "Podman, container-selinux, netavark, Compose v2, podman.socket, chrony (scripts/rhel-prepare.sh)" bash "$SRC/scripts/rhel-prepare.sh" "${prep[@]}"; then
  rpm_ok podman && ! compose_ok || die_tail "Paket kurulumu başarısız (dnf). Depo / proxy / abonelik: yukarıdaki satırlar"
  note "docker-compose-plugin depodan gelmedi: Compose v2 GitHub sürümünden alınıyor"
fi
if ! compose_ok; then
  { rpm_ok docker-compose-plugin && quiet "Compose v2 güncelleniyor" dnf -y upgrade docker-compose-plugin && compose_ok; } \
    || { [ "$OFFLINE" = 0 ] && quiet "Compose v2 indiriliyor (github.com, sha256 doğrulamalı)" compose_from_github && compose_ok; } \
    || die "Compose v2 (2.24.4+) kurulamadı: download.docker.com ya da github.com erişimi açın, veya docker-compose-linux-$ARCH dosyasını betiğin yanına koyun"
  quiet "podman.socket" bash "$SRC/scripts/rhel-prepare.sh" --engine podman --no-firewall --no-docker || die_tail "podman.socket açılamadı"
fi
export WATCHOVER_COMPOSE_BIN; WATCHOVER_COMPOSE_BIN=$(compose_path)
have openssl || quiet "openssl (edge sertifikası sunucuda üretilir)" dnf -y install openssl || die_tail "openssl kurulamadı: sudo dnf -y install openssl"
pv=$(podman version --format '{{.Client.Version}}' 2>/dev/null || podman --version 2>/dev/null | awk '{print $NF}' || true)
ver_ge "${pv:-0}" 4.0 || die "Podman $pv: en az 4.0 gerekir (RHEL 8.6+ / 9 depolarında var: sudo dnf -y update podman)"
ver_ge "${pv:-0}" 5.0 || note "Podman $pv: çalışır; Podman 5 daha hızlıdır (RHEL 9.5+ / 10 getirir)"
nb=$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null || true)
if [ "$nb" = cni ]; then   # RHEL 8's Podman 4 starts on CNI: netavark when it could be installed, else CNI with the dnsname plugin
  if running watchover; then note "Podman CNI ağında çalışıyor (netavark önerilir); mevcut kuruluma dokunulmadı"
  elif ! rpm_ok netavark aardvark-dns && { rpm_ok podman-plugins || [ -x "$R/usr/libexec/cni/dnsname" ]; }; then
    note "Podman CNI ağında: konteyner adları dnsname eklentisiyle çözülür (RHEL 8; netavark depoda yok)"
  elif [ -z "$(podman ps -aq 2>/dev/null)" ] && [ -z "$(podman images -q 2>/dev/null)" ] && rpm_ok netavark aardvark-dns; then
    mkdir -p "$R/etc/containers/containers.conf.d"
    printf '# written by install-rhel.sh (Watchover): container names (postgres, receiver) resolve with netavark + aardvark-dns\n[network]\nnetwork_backend = "netavark"\n' > "$R/etc/containers/containers.conf.d/watchover-network.conf"
    podman system reset --force >/dev/null 2>&1 || true; systemctl restart podman.socket >/dev/null 2>&1 || true; nb=netavark
  elif ! rpm_ok netavark aardvark-dns; then die "Podman CNI ağında ve konteyner ad çözümü yok: sudo dnf -y install netavark aardvark-dns (RHEL 8: ya da podman-plugins), sonra bu betiği yeniden çalıştırın"
  else die "Podman CNI ağ altyapısında ve sunucuda başka konteyner / imaj var: Watchover netavark ister (RHEL 8: podman-plugins da olur). /etc/containers/containers.conf [network] network_backend=\"netavark\" + podman system reset (tüm Podman konteynerlerini siler)"; fi
fi
ok "Podman ${pv:-?} (ağ: ${nb:-?}) · Compose $("$WATCHOVER_COMPOSE_BIN" version --short 2>/dev/null || echo ?) · podman.socket"

# ---------------------------------------------------------------------------------------------------------------- release folder
if [ -n "$OLD_VER" ] && [ "$OLD_VER" != "$SRC_VER" ] && [ -f "$ENVF" ] && running watchover-postgres; then
  quiet "Yükseltme öncesi yedek (v$OLD_VER)" W backup --no-logs || die_tail "Yükseltme öncesi yedek alınamadı: hiçbir şey değiştirilmedi"
fi
mkdir -p "$DEST"; chmod 755 "$DEST"
if [ "$SRC" != "$DEST" ]; then
  say "Dosyalar: $SRC → $DEST"
  list=$(mktemp)
  ( cd "$SRC" && find . \( -path ./.git -o -path ./.releases -o -path ./.updater -o -path ./backups -o -path ./certs -o -path ./datasets -o -path ./data \
      -o -path ./web/node_modules -o -path ./node_modules -o -name __pycache__ -o -path ./.pytest_cache \
      -o -path ./demo -o -path ./deploy -o -path ./prompts -o \( -path './tests/*' ! -path './tests/fixtures' ! -path './tests/fixtures/*' \) \) -prune \
      -o \( -type f -o -type l \) ! -path ./AI_JURI.md ! -path ./submission.json ! -path ./CLAUDE.md \
      ! -name .env ! -name .env.bak ! -name 'watchover-bundle-*.tar' ! -name '*.zip' ! -name 'docker-compose-linux-*' ! -name .install-files \
      ! -name '.edge-mdns.*' -print ) | LC_ALL=C sort > "$list"
  tar -C "$SRC" -cf - -T "$list" | tar -C "$DEST" -xf - --no-same-owner
  if [ -f "$DEST/.install-files" ]; then   # files of the previous release that this one no longer has (only what this script put there)
    LC_ALL=C comm -23 "$DEST/.install-files" "$list" | while IFS= read -r f; do case "$f" in ./*..*|"") ;; ./*) rm -f "$DEST/${f#./}" ;; esac; done
  fi
  for d in demo deploy prompts tests; do [ ! -d "$DEST/$d" ] || find "$DEST/$d" -depth -type d -empty -delete 2>/dev/null || true; done   # what an older release copied
  cp "$list" "$DEST/.install-files"; rm -f "$list"
  if [ "$(git -C "$SRC" rev-parse --show-toplevel 2>/dev/null || true)" = "$SRC" ]; then git -C "$SRC" rev-parse --short=12 HEAD > "$DEST/.release-rev" 2>/dev/null || true
  elif [ -f "$(dirname "$SRC")/watchover-$SRC_VER.zip" ] && have unzip; then
    r=$(unzip -zq "$(dirname "$SRC")/watchover-$SRC_VER.zip" 2>/dev/null | grep -oE '[0-9a-f]{40}' | head -1 || true); [ -z "$r" ] || echo "${r:0:12}" > "$DEST/.release-rev"
  fi
fi
chmod 755 "$DEST/watchover.sh" "$DEST/install-rhel.sh" "$DEST"/scripts/*.sh 2>/dev/null || true
chown root:root "$DEST" 2>/dev/null || true
ctx=$(stat -c %C "$DEST" 2>/dev/null | cut -d: -f3 || true)          # a folder moved from a home directory keeps its label
case "$ctx" in *home_t|*tmp_t|user_*) ! have restorecon || restorecon -R -e "$DEST/certs" -e "$DEST/datasets" "$DEST" >/dev/null 2>&1 || true; ok "SELinux etiketleri düzeltildi ($ctx → varsayılan)" ;; esac
mkdir -p "$R/usr/local/bin"
printf '#!/bin/sh\n# Watchover command (written by install-rhel.sh): the stack runs on rootful Podman, so every call runs as root\n[ "$(id -u)" = 0 ] || exec sudo -- "%s/watchover.sh" "$@"\nexec "%s/watchover.sh" "$@"\n' "$DEST" "$DEST" > "$R/usr/local/bin/watchover"
chmod 755 "$R/usr/local/bin/watchover"; ! have restorecon || restorecon "$R/usr/local/bin/watchover" >/dev/null 2>&1 || true
# sudo's secure_path on RHEL (/sbin:/bin:/usr/sbin:/usr/bin) leaves /usr/local/bin out, so 'sudo watchover ...' answered "command
# not found" (seen on the first real RHEL 9.8 install): a link in /usr/bin lets 'watchover' and 'sudo watchover' both work
mkdir -p "$R/usr/bin"
if [ ! -e "$R/usr/bin/watchover" ] || [ "$(readlink "$R/usr/bin/watchover" 2>/dev/null)" = /usr/local/bin/watchover ]; then
  ln -sfn /usr/local/bin/watchover "$R/usr/bin/watchover"
else note "$R/usr/bin/watchover başka bir programa ait: komutu 'watchover ...' (sudo'suz; yetkiyi kendisi ister) ya da 'sudo $DEST/watchover.sh ...' ile çalıştırın"; fi
ok "Kurulum klasörü $DEST (root:root) · komut: watchover (= sudo $DEST/watchover.sh)"

# ---------------------------------------------------------------------------------------------------------------- .env
FRESH=0
if [ ! -f "$ENVF" ]; then
  if [ -n "$PREV" ]; then
    cp "$PREV/.env" "$ENVF"; mkdir -p "$DEST/certs"; [ -n "$(ls -A "$DEST/certs" 2>/dev/null)" ] || cp -a "$PREV/certs/." "$DEST/certs/" 2>/dev/null || true
    ok "Önceki kurulumun ayarları ve parolaları alındı: $PREV (o klasör artık kullanılmıyor)"
  else cp "$DEST/.env.docker.example" "$ENVF"; FRESH=1; fi
fi
chown root:root "$ENVF" 2>/dev/null || true; chmod 600 "$ENVF"
eset WATCHOVER_ENGINE podman; eset WATCHOVER_PRODUCTION 1
[ -n "$(eget WATCHOVER_BACKUP_KEEP)" ] || eset WATCHOVER_BACKUP_KEEP 14
[ -n "$(eget WATCHOVER_BACKUP_AT)" ] || eset WATCHOVER_BACKUP_AT 02:15
UPD_TAG=$(sed -n 's/^UPDATER_TAG=\([0-9][0-9]*\).*/\1/p' "$DEST/watchover.sh" | head -1)
if [ "$NO_UPDATER" = 1 ]; then eset WATCHOVER_UPDATER 0
elif [ -z "$(eget WATCHOVER_UPDATER)" ]; then eset WATCHOVER_UPDATER 1
elif [ "$(eget WATCHOVER_UPDATER)" = 0 ] && ! podman image exists "docker.io/library/watchover-updater:${UPD_TAG:-1}" 2>/dev/null; then eset WATCHOVER_UPDATER 1; fi   # its build failed before: retry
if [ -n "$PX_HTTPS" ]; then eset WATCHOVER_HTTPS_PROXY "$PX_HTTPS"; eset WATCHOVER_HTTP_PROXY "$PX_HTTP"; eset WATCHOVER_NO_PROXY "$PX_NO"; fi
# the certificate's names: --host, the name used so far, this server's FQDN and short name, its addresses (the IP always)
EH=""; for n in "$XHOST" "$(eget WATCHOVER_EDGE_HOST)" "$FQDN" "$SHORT" ${IPS[@]+"${IPS[@]}"}; do name_ok "$n" && { EH="$n"; break; }; done
# shellcheck disable=SC2046   # the names kept so far, split on purpose
HOSTS_CSV=$(printf '%s\n' "$EH" "$XHOST" $(eget WATCHOVER_EDGE_HOSTS | tr ',' ' ') "$FQDN" "$SHORT" ${IPS[@]+"${IPS[@]}"} | while IFS= read -r n; do if name_ok "$n"; then echo "$n"; fi; done | awk '!s[$0]++' | paste -sd, -),localhost   # localhost: the SSH-tunnel path
if [ "$EDGE" = 1 ]; then eset WATCHOVER_EDGE 1; eset WATCHOVER_EDGE_HOST "$EH"; eset WATCHOVER_EDGE_HOSTS "${HOSTS_CSV//,/, }"; fi   # the first start already has HTTPS only

# rootless Podman cannot publish 80 / 443: let unprivileged processes bind from 80 up, persisted (only for a rootless engine socket;
# this installer itself sets up rootful Podman, where nothing is needed)
low_ports_for_rootless() {
  local f="$R/etc/sysctl.d/90-watchover.conf" line="net.ipv4.ip_unprivileged_port_start=80"
  case "$(eget WATCHOVER_ENGINE_SOCK)" in /run/user/*) ;; *) return 0 ;; esac
  if ! grep -qx "$line" "$f" 2>/dev/null; then
    mkdir -p "$(dirname "$f")"; printf '# written by install-rhel.sh (Watchover): rootless Podman publishes the edge on 80 / 443\n%s\n' "$line" > "$f"
  fi
  sysctl --system >/dev/null 2>&1 || note "sysctl --system başarısız: $line elle uygulayın"
  ok "Rootless Podman: $line ($f)"
}
low_ports_for_rootless

# ---------------------------------------------------------------------------------------------------------------- the stack
company_cert() { [ -f "$DEST/certs/watchover.crt" ] && [ -f "$DEST/certs/watchover.key" ] && [ ! -f "$DEST/certs/.ca/issued" ]; }   # not one the edge issued itself
if [ "$OFFLINE" = 1 ]; then
  if podman image exists "docker.io/library/watchover:$SRC_VER" 2>/dev/null && podman image exists "docker.io/library/watchover-receiver:$SRC_VER" 2>/dev/null && [ "$(eget WATCHOVER_OFFLINE)" = 1 ]; then
    ok "Paket imajları zaten yüklü (v$SRC_VER)"
  else quiet "Kapalı ağ paketi yükleniyor: imajlar ve model birimleri ($(du -h "$BUNDLE" | cut -f1))" W unbundle "$BUNDLE" || die_tail "Paket yüklenemedi"; fi
  if [ "$EDGE" = 1 ] && ! edge_img_ok; then EDGE=0; eset WATCHOVER_EDGE 0; note "edge imajı ${EDGE_IMG:-?} yüklenmedi: HTTPS kapalı"; fi
fi
quiet "Watchover v$SRC_VER kuruluyor: imajlar, PostgreSQL, panel, alıcı (ilk kurulum 5-20 dk)" W install --production || die_tail "Kurulum başarısız"
if [ "$EDGE" = 1 ]; then
  quiet "HTTPS: $HOSTS_CSV ($(company_cert && echo 'şirket sertifikası' || echo 'Watchover iç CA'))" W edge on "$HOSTS_CSV" || die_tail "HTTPS (edge) açılamadı"
else
  [ "$(eget WATCHOVER_EDGE)" != 1 ] || quiet "HTTPS kapatılıyor" W edge off || true
fi
[ "$NO_UPDATER" = 0 ] || [ "$(eget WATCHOVER_UPDATER)" != 0 ] || quiet "Uygulama içi güncelleme kapatılıyor (--no-updater)" W updater off || true
quiet "Açılışta başlatma (watchover-stack.service)" W autostart on || die_tail "Açılış birimi yazılamadı"
# the host's Compose: the one rebuilt in the updater image (current Go, patched modules; Dockerfile.updater) replaces Docker's
# docker-compose-plugin build in Watchover's own plugin path, which watchover.sh looks at first; the RPM is left installed
patched_compose() {
  local img="docker.io/library/watchover-updater:$(sed -n 's/^UPDATER_TAG=\([0-9][0-9]*\).*/\1/p' "$DEST/watchover.sh" | head -1)" t c v
  [ -z "$R" ] && podman image exists "$img" 2>/dev/null || return 0
  t=$(mktemp -d); c=$(podman create "$img" 2>/dev/null) || { rm -rf "$t"; return 0; }
  if podman cp "$c:/usr/local/libexec/docker/cli-plugins/docker-compose" "$t/docker-compose" >/dev/null 2>&1 && v=$("$t/docker-compose" version --short 2>/dev/null) \
     && ver_ge "${v#v}" 2.24.4 && install_compose_file "$t/docker-compose"; then ok "Compose $v: yamalı derleme (/usr/local/lib/docker/cli-plugins)"; fi
  podman rm "$c" >/dev/null 2>&1 || true; rm -rf "$t"
}
patched_compose
quiet "Gecelik yedek ($(eget WATCHOVER_BACKUP_AT), son $(eget WATCHOVER_BACKUP_KEEP) kopya)" W backup-timer on || die_tail "Yedek zamanlayıcısı kurulamadı"

# ---------------------------------------------------------------------------------------------------------------- firewalld
# One place for the ports: what the stack publishes and the network needs. Echoes "PORT|why" lines.
fw_wanted() {
  if [ "$EDGE" = 1 ]; then
    echo "$HTTPS_PORT|arayüz ve ajanlar (HTTPS, edge)"
    echo "$HTTP_PORT|https'e yönlendirme (isteğe bağlı)"
  else echo "$UI_PORT|arayüz (http, edge kapalı)"; fi
  case "$(eget WATCHOVER_LIVE_PORT)" in 127.*|localhost:*) ;; *) echo "$LIVE_PORT|ajanların düz http gönderimi" ;; esac   # plain ingest: off = WATCHOVER_LIVE_PORT=127.0.0.1:8600
  [ "$(eget WATCHOVER_OTLP)" != 1 ] || { echo "$(port_of WATCHOVER_OTLP_GRPC_PORT 4317)|OTLP gRPC"; echo "$(port_of WATCHOVER_OTLP_HTTP_PORT 4318)|OTLP HTTP"; }
  [ "$(eget WATCHOVER_MCP)" != 1 ] || case "$(eget WATCHOVER_MCP_PORT)" in 127.*) ;; *) echo "$(port_of WATCHOVER_MCP_PORT 8765)|MCP" ;; esac
}
FW="kapalı"
firewall_setup() {   # firewalld: open what fw_wanted lists, close Watchover's ports it no longer lists; never starts firewalld itself
  local want=() p z; mapfile -t want < <(fw_wanted | cut -d'|' -f1)
  if ! have firewall-cmd || ! firewall-cmd --state >/dev/null 2>&1; then
    note "firewalld $(have firewall-cmd && echo çalışmıyor || echo yok) (açılmadı: diğer servisleri etkiler). Sunucuyu süzen güvenlik duvarında açın: $(printf '%s/tcp ' "${want[@]}")"; return 0
  fi
  z=""; [ -z "$DEFIF" ] || z=$(firewall-cmd --get-zone-of-interface="$DEFIF" 2>/dev/null || true)
  [ -n "$z" ] || z=$(firewall-cmd --get-default-zone 2>/dev/null || echo public)
  for p in "${want[@]}"; do
    firewall-cmd --permanent --zone="$z" --query-port="$p/tcp" >/dev/null 2>&1 || firewall-cmd --permanent --zone="$z" --add-port="$p/tcp" >/dev/null 2>&1 || note "firewalld: $p/tcp açılamadı"
    firewall-cmd --zone="$z" --query-port="$p/tcp" >/dev/null 2>&1 || firewall-cmd --zone="$z" --add-port="$p/tcp" >/dev/null 2>&1 || true   # now too, without a reload
  done
  for p in "$UI_PORT" "$LIVE_PORT"; do   # published no more (edge on / plain ingest off): close what an earlier setup opened
    case " ${want[*]} " in *" $p "*) continue ;; esac
    if firewall-cmd --permanent --zone="$z" --query-port="$p/tcp" >/dev/null 2>&1; then firewall-cmd --permanent --zone="$z" --remove-port="$p/tcp" >/dev/null 2>&1 && ok "Güvenlik duvarı: $p/tcp kapatıldı"; fi
    firewall-cmd --zone="$z" --remove-port="$p/tcp" >/dev/null 2>&1 || true
  done
  # No 'firewall-cmd --reload': it drops what netavark added at runtime (the containers' subnet in the trusted zone), and the
  # containers could no longer resolve each other's names (aardvark-dns on the bridge gateway; RHEL 9.8: edge unhealthy, "failed
  # to resolve host 'postgres'"). A reload by an administrator later: netavark-firewalld-reload.service puts the rules back.
  if systemctl cat netavark-firewalld-reload.service >/dev/null 2>&1; then systemctl enable --now netavark-firewalld-reload.service >/dev/null 2>&1 || true; fi
  podman network reload --all >/dev/null 2>&1 || true          # an install that an earlier version reloaded: repaired now
  eset WATCHOVER_FW_ZONE "$z"
  FW="$z bölgesi: $(printf '%s/tcp\n' "${want[@]}" | paste -sd, - | sed 's/,/, /g')"; ok "Güvenlik duvarı ($FW)"
}
firewall_setup

# ---------------------------------------------------------------------------------------------------------------- checks
say "Doğrulama"
CONT=$(podman ps -a --filter label=com.docker.compose.project=watchover --format '{{.Names}}|{{.Status}}' 2>/dev/null || true)
conts="watchover watchover-postgres watchover-receiver watchover-keyd"; [ "$EDGE" != 1 ] || conts="$conts watchover-edge"; [ "$(eget WATCHOVER_UPDATER)" != 1 ] || conts="$conts watchover-updater"
for c in $conts; do
  st=$(printf '%s\n' "$CONT" | awk -F'|' -v c="$c" '$1 == c {print $2; exit}')
  case "$st" in Up*unhealthy*) note "$c: $st (sudo watchover logs ${c#watchover-})" ;; Up*) ok "$c: $st" ;; *) note "$c çalışmıyor (${st:-yok}): sudo watchover logs ${c#watchover-}" ;; esac
done
pp=$(eget WATCHOVER_PG_PORT); case "${pp:-127.0.0.1:5433}" in 127.0.0.1:*) ok "PostgreSQL yalnız ${pp:-127.0.0.1:5433} (dışarı kapalı), parola .env içinde" ;; *) note "WATCHOVER_PG_PORT=$pp: PostgreSQL dışarı açık; 127.0.0.1:5433 yapın" ;; esac
[ "$(stat -c %a "$ENVF" 2>/dev/null)" = 600 ] && ok ".env 0600 (root)" || note ".env izinleri 600 değil: sudo chmod 600 $ENVF"
URLS=()
if [ "$EDGE" = 1 ]; then
  sfx=""; [ "$HTTPS_PORT" = 443 ] || sfx=":$HTTPS_PORT"; code=""
  for _ in $(seq 1 "${WATCHOVER_HTTPS_TRIES:-30}"); do
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 --resolve "$EH:$HTTPS_PORT:127.0.0.1" "https://$EH$sfx/api/health" 2>/dev/null || true)
    [ "$code" != 200 ] || break; sleep 3
  done
  [ "$code" = 200 ] && ok "https://$EH$sfx yanıt veriyor" || note "https://$EH$sfx henüz yanıt vermiyor (HTTP ${code:-yok}): sudo watchover doctor"
  for n in $(echo "$HOSTS_CSV" | tr ',' ' '); do case "$n" in "$SHORT") [ "$n" = "$EH" ] || continue ;; esac; [ "$n" != localhost ] || continue; URLS+=("https://$n$sfx/"); done
else URLS+=("http://${IPS[0]:-$EH}:$UI_PORT/"); fi
[ "${#URLS[@]}" -gt 0 ] || URLS+=("https://$(hostname -I 2>/dev/null | awk '{print $1}')")

# ---------------------------------------------------------------------------------------------------------------- local LLM
LLM_STATE=""; LLM_DEFAULT_MODELS="qwen2.5:7b-instruct qwen2.5:3b-instruct bge-m3"
llm_decide() {
  local cur free models chat; cur=$(eget WATCHOVER_LLM)
  if [ "$NO_LLM" = 1 ]; then LLM_STATE="kapalı (--no-llm)"; return; fi
  if [ "$cur" = 1 ] && { running watchover-ollama || systemctl is-active --quiet watchover-llm-setup 2>/dev/null; }; then LLM_STATE="açık (modeller: $(eget WATCHOVER_LLM_MODELS); durum: sudo watchover llm list)"; return; fi
  if [ "$cur" = 1 ]; then llm_start "$(eget WATCHOVER_LLM_MODELS)"; LLM_STATE="$LLM_STATE (önceki kurulum yarım kalmıştı)"; return; fi
  if [ "$cur" = 0 ]; then LLM_STATE="kapalı (yönetici kapatmış; açmak için: sudo watchover llm on)"; return; fi
  if [ "$MEM_GB" -lt 16 ]; then LLM_STATE="kurulmadı: RAM $MEM_GB GB, yerel LLM için en az 16 GB gerekir"; return; fi
  free=$(free_gb "$STORE"); if [ "${free:-0}" -lt 30 ]; then LLM_STATE="kurulmadı: konteyner deposunda ${free:-?} GB boş (en az 30 GB)"; return; fi
  if [ "$OFFLINE" != 0 ] && [ "$LLM_OFFLINE_OK" != 1 ]; then LLM_STATE="kurulmadı: kapalı ağ paketinde Ollama / eğitim imajı ya da model yok (internetli makinede 'llm on', sonra 'bundle')"; return; fi
  if [ -n "$LLM_NET_FAIL" ]; then LLM_STATE="kurulmadı: erişilemeyen adres(ler):$LLM_NET_FAIL"; return; fi
  if [ "$MEM_GB" -ge 48 ]; then models="$LLM_DEFAULT_MODELS"; chat=qwen2.5:7b-instruct; else models="qwen2.5:3b-instruct bge-m3"; chat=qwen2.5:3b-instruct; fi
  case "$(eget WATCHOVER_LLM_MODELS)" in ""|"$LLM_DEFAULT_MODELS") eset WATCHOVER_LLM_MODELS "$models" ;; *) models=$(eget WATCHOVER_LLM_MODELS) ;; esac
  if [ -z "$(eget LLM_BASE_URL)" ]; then case "$(eget LLM_MODEL)" in ""|qwen2.5:7b-instruct) eset LLM_MODEL "$chat" ;; esac; fi   # a configured connection stays as it is
  llm_start "$models"
}
llm_start() {   # MODELS: ./watchover.sh llm on as a transient systemd unit (survives this shell and the SSH session; never blocks the install)
  local models="$1" cmd; cmd="exec \"$DEST/watchover.sh\" llm on >> \"$LLMLOG\" 2>&1"
  : >> "$LLMLOG"; chmod 600 "$LLMLOG"
  if systemctl is-active --quiet watchover-llm-setup 2>/dev/null; then LLM_STATE="kurulumu zaten sürüyor (tail -f $LLMLOG)"; return; fi
  if have systemd-run; then
    systemctl reset-failed watchover-llm-setup >/dev/null 2>&1 || true
    systemd-run --unit=watchover-llm-setup --description="Watchover local LLM setup" --collect -p Nice=10 -p WorkingDirectory="$DEST" \
      --setenv=WATCHOVER_ENGINE=podman /bin/bash -c "$cmd" >/dev/null 2>&1 || { setsid nohup bash -c "export WATCHOVER_ENGINE=podman; $cmd" >/dev/null 2>&1 & }
  else setsid nohup bash -c "export WATCHOVER_ENGINE=podman; $cmd" >/dev/null 2>&1 & fi
  LLM_STATE="arka planda kuruluyor: $models (izlemek: tail -f $LLMLOG); sonra hız ölçülüp sohbet modeli seçilir (14B / 7B / 3B), panel bir kez yeniden başlar"
}
llm_decide

# ---------------------------------------------------------------------------------------------------------------- this server's agent
# The built-in agent runs inside the dashboard container: it sees the container (its CPU / disk view, no host journal), not the
# server. The real wo-agent goes onto the host like on every monitored server, with the Agents panel's command (boot.sh checked
# by its sha256, the signed manifest by keyd's release key, so its self-updates verify too) against the local receiver; its own
# token (python -m watchover.selfagent), never the fleet key. A re-run (repair, upgrade) leaves a running agent alone: it updates itself.
HOST_AGENT=""; AGENT_LOG="$LOGDIR/agent-install.log"
builtin_off() {   # the in-container agent is redundant once the host has its own: stopped, rows removed, off after restarts
  [ "$(eget WATCHOVER_SELFMON)" = 0 ] && return 0
  podman exec watchover python -m watchover.selfagent builtin-off >/dev/null 2>&1 || true
  eset WATCHOVER_SELFMON 0
}
host_agent() {
  local name arch base tok sha fp boot rc
  if [ "$NO_HOST_AGENT" = 1 ]; then HOST_AGENT="kurulmadı (--no-host-agent)"; return; fi
  if [ -n "$R" ]; then HOST_AGENT="(test kökü: atlandı)"; return; fi   # needs the real containers: rehearsed on a VM
  if systemctl is-active --quiet watchover-agent 2>/dev/null; then builtin_off; HOST_AGENT="çalışıyor (watchover-agent; kendini günceller)"; return; fi
  case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) HOST_AGENT="kurulmadı: $(uname -m) için wo-agent yok"; return ;; esac
  running watchover && running watchover-receiver || { note "Sunucu ajanı kurulmadı: panel / alıcı çalışmıyor (sonra: sudo $DEST/install-rhel.sh)"; HOST_AGENT="kurulmadı"; return; }
  name=$(hostname -s 2>/dev/null || hostname); name=${name:-watchover}
  base="http://127.0.0.1:$LIVE_PORT"
  tok=$(mktemp); chmod 600 "$tok"
  if ! podman exec watchover python -m watchover.selfagent token --name "$name" --env prod > "$tok" 2>>"$AGENT_LOG" || [ ! -s "$tok" ]; then
    rm -f "$tok"; note "Sunucu ajanı kurulmadı: token alınamadı ($AGENT_LOG)"; HOST_AGENT="kurulmadı"; return
  fi
  # keyd signs the release a few seconds after it starts; until then the command falls back to the receiver's pinned binary, and an
  # agent installed without the release key cannot verify its own signed self-updates: wait for the signature (up to 90 s)
  fp=""; for _ in $(seq 1 "${WATCHOVER_RELEASE_TRIES:-18}"); do
    fp=$(podman exec watchover python -m watchover.selfagent release-key 2>/dev/null | tail -1 || true)
    [ -z "$fp" ] || break; sleep 5
  done
  boot=$(mktemp); rc=1
  if [ -n "$fp" ] && curl -fsS --max-time 30 -o "$boot" "$base/agent/boot.sh" 2>>"$AGENT_LOG" \
     && [ "$(sha256sum "$boot" | cut -d' ' -f1)" = "$(sha256sum "$DEST/scripts/agent-boot.sh" | cut -d' ' -f1)" ]; then
    bash "$boot" --url "$base/ingest" --release-key "$fp" --token-file "$tok" --name "$name" --env prod --tags watchover-server >>"$AGENT_LOG" 2>&1 && rc=0
  else   # still no signed release (keyd down): the installer itself, the binary pinned by the receiver's own copy
    echo "no signed release from keyd after $((${WATCHOVER_RELEASE_TRIES:-18} * 5)) s: installing with --agent-sha256" >> "$AGENT_LOG"
    sha=$(podman exec watchover-receiver sha256sum "/app/agent/wo-agent-linux-$arch" 2>/dev/null | cut -d' ' -f1 || true)
    if [ -n "$sha" ] && curl -fsS --max-time 30 -o "$boot" "$base/agent/install.sh" 2>>"$AGENT_LOG"; then
      bash "$boot" --url "$base/ingest" --token-file "$tok" --name "$name" --env prod --tags watchover-server --agent-sha256 "$sha" >>"$AGENT_LOG" 2>&1 && rc=0
    fi
  fi
  rm -f "$boot" "$tok"; chmod 600 "$AGENT_LOG" 2>/dev/null || true
  if [ "$rc" = 0 ] && systemctl is-active --quiet watchover-agent 2>/dev/null; then
    builtin_off; HOST_AGENT="kuruldu: $name (journal, loglar, metrikler; Ajanlar panelinde 'watchover-server' etiketiyle)"; ok "Sunucu ajanı ($name)"
  else note "Sunucu ajanı kurulamadı: $AGENT_LOG (elle: Ajanlar paneli › kurulum komutu)"; HOST_AGENT="kurulamadı ($AGENT_LOG)"; fi
}
host_agent

# ---------------------------------------------------------------------------------------------------------------- summary
st_unit() { systemctl is-enabled -q "$1" 2>/dev/null && [ -f "$UNITS/$1" ] && echo açık || echo kapalı; }
CA="$DEST/certs/watchover-root.crt"
if [ "$EDGE" != 1 ]; then CA_LINE="HTTPS kapalı"
elif [ "$(eget WATCHOVER_EDGE_TLS)" = company ] || company_cert; then CA_LINE="şirket sertifikası kullanılıyor ($DEST/certs/watchover.crt)"
elif [ -f "$CA" ]; then CA_LINE="$CA  (Watchover iç CA; tarayıcılara bir kez 'güvenilir kök' olarak yükleyin; yeniden almak: sudo watchover edge cert)"
else CA_LINE="Watchover iç CA: sudo watchover edge cert (dosyayı tarayıcılara bir kez 'güvenilir kök' olarak yükleyin)"; fi
UPD=$([ "$(eget WATCHOVER_UPDATER)" = 1 ] && echo açık || echo "kapalı$([ "$NO_UPDATER" = 1 ] && echo ' (--no-updater)' || echo ' (imaj derlenemedi; yeniden: sudo watchover updater on)')")
BDIR=$(eget WATCHOVER_BACKUP_DIR); BDIR=${BDIR:-$DEST/backups}
{
printf '\n\033[1m════════════════════════════════════════════════════════════════════════\033[0m\n'
printf '\033[1m Watchover v%s %s · %s · Podman\033[0m\n' "$SRC_VER" "$([ "$FRESH" = 1 ] && echo kuruldu || { [ -n "$OLD_VER" ] && [ "$OLD_VER" != "$SRC_VER" ] && echo "yükseltildi (v$OLD_VER'dan)" || echo 'onarıldı / güncel'; })" "$OS_LABEL"
printf '\033[1m════════════════════════════════════════════════════════════════════════\033[0m\n'
printf ' Açılacak adres %s\n' "${URLS[0]}"; for u in "${URLS[@]:1}"; do printf '                %s\n' "$u"; done
printf ' Sertifika      %s\n' "$CA_LINE"
[ "$EDGE" != 1 ] || printf '                Şirket sertifikası: %s/certs/watchover.crt + watchover.key koyun, sonra: sudo watchover edge on (ya da bu betiği yeniden çalıştırın)\n' "$DEST"
printf ' İlk giriş      sudo watchover admin-password  (kuruluma özel rastgele ilk parola; ilk girişte yeni parola zorunludur)\n'
printf '                Parolayı şimdi konsoldan belirlemek: sudo watchover user password <e-posta> · kişisel yönetici: sudo watchover user add ad.soyad@firma.com --admin\n'
if [ "$EDGE" = 1 ]; then
printf ' LB arkasında   Sağlık kontrolü: GET /healthz → 200 (HTTPS %s ya da HTTP %s). LB X-Forwarded-For / X-Forwarded-Proto iletsin ya da TCP geçişi\n' "$HTTPS_PORT" "$HTTP_PORT"
printf '                (TLS passthrough) kullansın; LB adresini güvenilir yapın: sudo watchover edge lb <LB IP / CIDR> (loglarda gerçek istemci adresi)\n'
printf '                Sertifikadaki adlar: %s (LB adı farklıysa: sudo %s/install-rhel.sh --host <LB adı>)\n' "${HOSTS_CSV//,/, }" "$DEST"
printf ' Şirket ağı     Tek adres (VPN + LB): sudo watchover edge public <ad.sirket.com.tr> <LB SNAT IP> → ağ ekibine talep listesi; şirket\n'
printf '                sertifikası: sudo watchover edge csr → PKI → sudo watchover edge company <sertifika> [zincir]; kontrol: sudo watchover edge check\n'
printf ' Masaüstüsüz    Dizüstündeki tarayıcıdan LB VIP / sunucu adresiyle açın ya da SSH tüneli: ssh -L 8443:localhost:%s kullanici@%s → https://localhost:8443/\n' "$HTTPS_PORT" "${IPS[0]:-$EH}"
fi
printf ' Sunucu ajanı   %s\n' "$HOST_AGENT"
printf ' Ajanlar        Arayüzde Ajanlar paneli › kurulum komutu (Linux amd64 / arm64; https adreste CA otomatik doğrulanır)\n'
printf ' Açık olanlar   Uygulama içi güncelleme: %s · Açılışta başlama: %s · Gecelik yedek: %s %s, son %s kopya (%s)\n' "$UPD" "$(st_unit watchover-stack.service)" \
  "$(st_unit watchover-backup.timer)" "$(eget WATCHOVER_BACKUP_AT)" "$(eget WATCHOVER_BACKUP_KEEP)" "$BDIR"
printf '                Güvenlik duvarı: %s · PostgreSQL: yalnız 127.0.0.1:%s\n' "$FW" "$(port_of WATCHOVER_PG_PORT 5433)"
printf ' Yerel LLM      %s\n' "$LLM_STATE"
[ -z "$PX_HTTPS" ] || printf ' Proxy          %s (dnf, Podman, derlemeler, Ollama)\n' "$(mask "$PX_HTTPS")"
[ "$OFFLINE" = 0 ] || printf ' Kapalı ağ      imajlar paketten; güncellemeler: yeni paket + bu betik ya da Sistem › Güncelleme\n'
printf ' Depolama       /var/lib/containers yerel SSD üzerinde olmalı · ham log diskleri sonradan arayüzden: Sistem › Depolama\n'
printf ' Komut          sudo watchover status | logs | doctor | backup | checklist   (= %s/watchover.sh)\n' "$DEST"
printf ' Güncelleme     sudo watchover update all [yeni kurulum dosyası] [--check]: sunucunun konteyner paketleri (Satellite) + Watchover\n'
printf ' Kayıt          %s\n' "$LOG"
}
if [ "${#NOTES[@]}" -gt 0 ]; then printf '\n Dikkat:\n'; printf '   ! %s\n' "${NOTES[@]}"; fi
W checklist 2>&1 || true
printf '\nKurulum tamamlandı. Yeniden çalıştırmak güvenlidir (onarım / yükseltme): sudo %s/install-rhel.sh\n' "$DEST"
