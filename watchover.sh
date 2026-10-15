#!/usr/bin/env bash
# Watchover on one server, entirely in Docker, built from this source tree. No GitHub, no registry, no Python on the host.
#
#   ./watchover.sh install          first start: writes .env (random passwords / keys), builds the image, starts PostgreSQL + dashboard
#   ./watchover.sh update           new source tree copied over this folder: rebuild the image, recreate the containers, keep the data
#   ./watchover.sh upgrade          git pull of this folder (local edits set aside and put back) + update: the one-line way to a new version
#   ./watchover.sh start|stop|restart|status|logs [service]
#   ./watchover.sh mcp on|off       our MCP server as a third container (MCP_API_KEY from .env)
#   ./watchover.sh legacy on|off    the former Streamlit interface as an extra container (:8502) while teams move over
#   ./watchover.sh edge on NAME     Caddy in front: https://NAME for the company network (TLS from Caddy's CA, or ./certs/watchover.crt+key);
#                                   NAME may be a DNS name, an IP or a Bonjour name like watchover.local (announced by the Mac itself)
#   ./watchover.sh edge off|cert    stop it | export the CA root certificate colleagues install once (./certs/watchover-root.crt)
#   ./watchover.sh doctor [--check] the address does not open? checks containers, the edge, the name (Bonjour) and repairs what it can
#   ./watchover.sh backup [--keep N] PostgreSQL dump + data volume -> ./backups/watchover-YYYYMMDD-HHMM.tgz (oldest beyond N=14 removed)
#   ./watchover.sh restore FILE     bring a backup back (containers are stopped meanwhile)
#   ./watchover.sh user ...         account management, e.g.  user list | user add ops@firma.com --admin | user password EMAIL | user unlock EMAIL
#   ./watchover.sh demo-clean [--dry-run] [--yes]   delete only what the simulator made (counts first; a real host with a demo name stays)
#   ./watchover.sh migrate          copy old SQLite files (knowledge.db / actions.db / playbook.db in ./data or the volume) into PostgreSQL
#   ./watchover.sh save|load FILE   export the built images to a .tgz (build on a machine with internet), import them on an air-gapped server
#   ./watchover.sh bundle [FILE]    everything an air-gapped server needs in one .tar: all images, downloaded Ollama models, training weights, source
#   ./watchover.sh unbundle FILE    on the air-gapped server: load the images and model volumes, mark the install offline (no builds, no pulls)
#   ./watchover.sh llm prepare      cache the training base weights (done by 'bundle' automatically) so the trainer never needs internet
#   ./watchover.sh profile [NAME]   sizing: auto (default: measured from this machine on every start), cpu8 / cpu16 (fixed numbers); no NAME shows it
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
  mem_b=$(docker info --format '{{.MemTotal}}' 2>/dev/null | tr -dc '0-9'); [ -n "$mem_b" ] && [ "$mem_b" -ge 1 ] || mem_b=$((8 * 1024 * 1024 * 1024))
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
  size_env
  chmod 600 "$ENV_FILE"
  mkdir -p datasets backups
}

offline() { [ "$(env_get WATCHOVER_OFFLINE)" = "1" ]; }
have_image() { docker image inspect "$1" >/dev/null 2>&1; }
build() {
  if offline; then   # air-gapped: the images came in with ./watchover.sh unbundle, never build or pull here
    have_image "watchover:$(version)" && have_image "watchover-receiver:$(version)" || die "offline install and image watchover:$(version) or watchover-receiver:$(version) is missing: unbundle the bundle of this version first (./watchover.sh unbundle watchover-bundle-$(version).tar)"
    say "offline install: using the loaded image watchover:$(version) (no build)"; return 0
  fi
  say "building image watchover:$(version) from this source tree (first time: a few minutes, needs internet for PyPI)"
  $COMPOSE build --build-arg GIT_REV="$(git_rev)" --build-arg VERSION="$(version)" dashboard
  say "building the receiver (Go, a few seconds)"; $COMPOSE build receiver
  [ "$(env_get WATCHOVER_LLM)" = "1" ] && $COMPOSE --profile llm build trainer || true
}
# compose volume name (project prefix) for a volume declared in docker-compose.yml
vol_name() { $COMPOSE config --format json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['volumes']['$1']['name'])" 2>/dev/null || echo "watchover_$1"; }

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
up() { $COMPOSE $(profiles) up -d --remove-orphans; mdns_ensure; }

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
    need_docker; before=$(running_version); write_env; build; up; docker image prune -f >/dev/null
    say "updated ${before:+from v$before }to v$(version); data volumes untouched"; $COMPOSE ps; verify_running ;;
  upgrade)   # fetch the new version into this folder, then update. Local edits to tracked files (the reason a plain 'git pull' refuses) are
             # stashed and re-applied; a folder with its own commits is left for a manual merge.
    need_docker
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
        if offline; then
          have_image "watchover-trainer:$(version)" && say "offline install: using the loaded trainer image" || { env_set WATCHOVER_LLM 0; die "offline install and image watchover-trainer:$(version) is missing: run 'llm on' on the internet machine before 'bundle'"; }
        else
          say "building the trainer image (torch CPU, transformers, peft; first time a few minutes)"; $COMPOSE --profile llm build trainer
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
    cp "$ENV_FILE" "$tmp/env"; tar czf "$out" -C "$tmp" .; rm -rf "$tmp"; say "backup written: $out ($(du -h "$out" | cut -f1))"
    keep="${WATCHOVER_BACKUP_KEEP:-$(env_get WATCHOVER_BACKUP_KEEP)}"; keep="${keep:-14}"
    case "${2:-}" in --keep) keep="${3:-14}" ;; esac
    ls -1t backups/watchover-*.tgz 2>/dev/null | tail -n +"$((keep + 1))" | while read -r old; do rm -f "$old"; say "old backup removed: $old"; done ;;
  restore)
    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh restore backups/watchover-….tgz"; tmp=$(mktemp -d); tar xzf "$2" -C "$tmp"
    $COMPOSE $(profiles) stop dashboard mcp legacy 2>/dev/null || $COMPOSE stop dashboard
    $COMPOSE up -d postgres; sleep 5
    say "restoring the database"; $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" "$(env_get POSTGRES_DB || echo watchover)"
    gunzip -c "$tmp/db.sql.gz" | $COMPOSE exec -T postgres psql -q -U "$(env_get POSTGRES_USER || echo watchover)" "$(env_get POSTGRES_DB || echo watchover)"
    say "restoring settings, secrets and snapshots"; docker run --rm -v "watchover_watchover-data":/data -v "$tmp":/in alpine sh -c 'rm -rf /data/* && tar xzf /in/data.tgz -C /data'
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
  demo-clean)   # only simulator-made data goes (its lines, "demo (simulator)" inventory / RUM apps, the demo flow); a real host with a demo name stays
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
    for i in "watchover-trainer:$(version)" caddy:2-alpine ollama/ollama:latest alpine:latest; do have_image "$i" && imgs="$imgs $i" || true; done
    have_image alpine:latest || docker pull alpine:latest >/dev/null 2>&1 || true; have_image alpine:latest && case " $imgs " in *" alpine:latest "*) ;; *) imgs="$imgs alpine:latest" ;; esac
    if have_image "watchover-trainer:$(version)" && ! offline; then
      say "trainer image present: caching the training base weights so auto-learning works on the air-gapped server"
      bash "./$(basename "$0")" llm prepare || say "WARNING: base weights could not be cached; auto-learning on the offline server will fail until you run 'llm prepare' here and bundle again"
    fi
    say "saving images: $imgs"; docker save $imgs | gzip -1 > "$tmp/images.tar.gz"
    for v in watchover-ollama watchover-hf watchover-models; do
      n=$(vol_name "$v"); docker volume inspect "$n" >/dev/null 2>&1 || continue
      say "exporting volume $v ($n: Ollama models / training weights / adapters)"
      docker run --rm -v "$n":/v:ro -v "$tmp/volumes":/out alpine tar cf "/out/$v.tar" -C /v . || say "could not export $v (skipped)"
    done
    say "adding the source tree"
    if git rev-parse HEAD >/dev/null 2>&1; then git archive --format=tar HEAD | tar xf - -C "$tmp/source"; else tar cf - --exclude=./.git --exclude=./data --exclude=./backups --exclude=./certs --exclude=./.env --exclude='./*.tar' --exclude='./*.tgz' --exclude=./web/node_modules . | tar xf - -C "$tmp/source"; fi
    printf 'version=%s\nimages=%s\nvolumes=%s\n' "$(version)" "$imgs" "$(ls "$tmp/volumes" 2>/dev/null | tr '\n' ' ')" > "$tmp/MANIFEST"
    tar cf "$out" -C "$tmp" MANIFEST images.tar.gz volumes source; rm -rf "$tmp"
    say "bundle written: $out ($(du -h "$out" | cut -f1)). On the air-gapped server:  tar xf $(basename "$out") --strip-components=1 source  &&  ./watchover.sh unbundle $(basename "$out")  &&  ./watchover.sh install" ;;
  unbundle)  # on the air-gapped server: images, model volumes, offline marker
    need_docker; [ -f "${2:-}" ] || die "usage: ./watchover.sh unbundle watchover-bundle-X.Y.tar"
    tmp=$(mktemp -d); tar xf "$2" -C "$tmp" MANIFEST images.tar.gz volumes 2>/dev/null || tar xf "$2" -C "$tmp"
    [ -f "$tmp/MANIFEST" ] && say "bundle $(sed -n 's/^version=//p' "$tmp/MANIFEST")" || true
    say "loading images"; gunzip -c "$tmp/images.tar.gz" | docker load
    for f in "$tmp"/volumes/*.tar; do
      [ -f "$f" ] || continue; v=$(basename "$f" .tar); n=$(vol_name "$v")
      say "restoring volume $v -> $n"; docker volume create "$n" >/dev/null
      docker run --rm -v "$n":/v -v "$tmp/volumes":/in alpine tar xf "/in/$v.tar" -C /v || say "could not restore $v"
    done
    rm -rf "$tmp"; touch "$ENV_FILE"; env_set WATCHOVER_OFFLINE 1
    say "offline install marked (.env WATCHOVER_OFFLINE=1: install/update never build, llm on never downloads). Next:  ./watchover.sh install   then  ./watchover.sh llm on  if the bundle carried Ollama models" ;;
  shell)   need_docker; $COMPOSE exec dashboard bash ;;
  version) echo "Watchover v$(version) ($(git_rev))" ;;
  *) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 1 ;;
esac
