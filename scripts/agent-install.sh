#!/usr/bin/env bash
# Watchover agent — install on a server (Linux systemd, macOS launchd, or plain background process).
# The dashboard serves this script and agent.py itself, so the server needs nothing but python3 and network access to
# the dashboard's receiver port:
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --token wo_... --logs "/var/log/syslog,/var/log/app/*.log"
#   … or let the server enrol itself with the fleet key (Agents panel › bulk install); its own token is issued and stored here:
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --enroll-key wk_... --env prod --site IST-DC1
# Options: --url U  --token T | --enroll-key K [--name N --site S --tags "a,b"]  --logs "a,b"  --interval 10  --env prod  --dir /opt/watchover-agent  --no-service  --no-flows  --no-test  --uninstall
# Action agent (Linux + systemd, amd64 / arm64; always installed, dormant until act mode is on in the dashboard):
#   --actions-token wa_…   (the fleet-key enrolment issues it by itself)   --actions-only (add the actuator to a host that already ships logs)
#   --agent-sha256 <sha>   pin the wo-agent binary (from the release notes; without it a plain-HTTP install is recorded "güvensiz kurulum")
#   --dispatch-pub / --anchor-pub "ed25519:…"   pin the dashboard's keys (else from the enrolment answer, else on first use)
#   --actions-packs linux-diag,linux-act   --actions-max-risk R0   --criticality normal|high|critical (high/critical: diagnose only)
#   --ca /etc/pki/firma-root.pem   CA (PEM) to trust for an https receiver (both agents)
set -euo pipefail
URL=""; TOKEN=""; LOGS=""; INTERVAL=10; ENV_=""; DIR=""; FLOWS=1; SERVICE=1; TEST=1; UNINSTALL=0; EKEY=""; NAME=""; SITE=""; TAGS=""
ATOKEN=""; ACT_ONLY=0; AGENT_SHA=""; DPUB=""; APUB=""; PACKS="linux-diag,linux-act"; MAXRISK="R0"; CRIT="normal"; CA=""
while [[ $# -gt 0 ]]; do case "$1" in
  --url) URL="$2"; shift;; --token) TOKEN="$2"; shift;; --logs) LOGS="$2"; shift;; --interval) INTERVAL="$2"; shift;; --env) ENV_="$2"; shift;;
  --enroll-key) EKEY="$2"; shift;; --name) NAME="$2"; shift;; --site) SITE="$2"; shift;; --tags) TAGS="$2"; shift;;
  --actions-token) ATOKEN="$2"; shift;; --actions-only) ACT_ONLY=1;; --agent-sha256) AGENT_SHA="$2"; shift;;
  --dispatch-pub) DPUB="$2"; shift;; --anchor-pub) APUB="$2"; shift;; --actions-packs) PACKS="$2"; shift;;
  --actions-max-risk) MAXRISK="$2"; shift;; --criticality) CRIT="$2"; shift;; --ca) CA="$2"; shift;;
  --dir) DIR="$2"; shift;; --no-service) SERVICE=0;; --no-flows) FLOWS=0;; --no-test) TEST=0;; --uninstall) UNINSTALL=1;; *) echo "unknown option: $1"; exit 1;; esac; shift; done
log(){ printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
OS="$(uname -s)"; ROOT=0; [[ "$(id -u)" == "0" ]] && ROOT=1
if [[ -z "$DIR" ]]; then if [[ $ROOT -eq 1 ]]; then DIR=/opt/watchover-agent; else DIR="$HOME/.watchover-agent"; fi; fi
UNIT=watchover-agent
if [[ $ROOT -eq 1 ]]; then PLIST="/Library/LaunchDaemons/com.watchover.agent.plist"; else PLIST="$HOME/Library/LaunchAgents/com.watchover.agent.plist"; fi
[[ "$LOGS" == *"'"* ]] && die "--logs may not contain single quotes"
HAS_SYSTEMD=0; [[ "$OS" == "Linux" && -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1 && HAS_SYSTEMD=1

# the action agent (wo-agent act): fixed system locations, root-owned (docs/design/action-agent.md §5.2)
AUNIT=watchover-actuator; ABIN=/usr/local/lib/watchover; AETC=/etc/watchover; ASTATE=/var/lib/watchover; ASCRIPTS=/usr/lib/watchover; AUSER=wo-act

if [[ $UNINSTALL -eq 1 ]]; then
  log "Removing the agent"
  if [[ $HAS_SYSTEMD -eq 1 ]]; then
    systemctl disable --now "$AUNIT" 2>/dev/null || true; rm -f "/etc/systemd/system/$AUNIT.service"
  fi
  if [[ $ROOT -eq 1 ]]; then
    rm -rf "$ABIN" "$AETC" "$ASTATE" "$ASCRIPTS"
    if id "$AUSER" >/dev/null 2>&1; then userdel "$AUSER" 2>/dev/null || deluser "$AUSER" 2>/dev/null || true; fi
  fi
  if [[ $HAS_SYSTEMD -eq 1 ]]; then systemctl disable --now "$UNIT" 2>/dev/null || true; rm -f "/etc/systemd/system/$UNIT.service"; systemctl daemon-reload || true
  elif [[ "$OS" == "Darwin" ]]; then
    if [[ $ROOT -eq 1 ]]; then launchctl bootout system/com.watchover.agent 2>/dev/null || true; else launchctl unload "$PLIST" 2>/dev/null || true; fi; rm -f "$PLIST"; fi
  [[ -f "$DIR/agent.pid" ]] && kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true
  rm -rf "$DIR"; log "Done"; exit 0
fi
if [[ $ACT_ONLY -eq 1 ]]; then
  [[ -n "$URL" && -n "$ATOKEN" ]] || die "--actions-only needs --url and --actions-token wa_... (Bağlantı ayarları › Eylem ajanları)"
else
  [[ -n "$URL" && ( -n "$TOKEN" || -n "$EKEY" ) ]] || die "--url http://<dashboard>:<port>/ingest and --token wo_... (or --enroll-key wk_...) are required (both come from the Agents panel)"
fi
[[ -z "$LOGS" ]] && LOGS="auto"   # discover the applications on this server and follow their logs
[[ "$URL" == */ingest ]] || URL="${URL%/}/ingest"
BASE="${URL%/ingest}"
[[ -z "$ATOKEN" || "$ATOKEN" == wa_* ]] || die "--actions-token must start with wa_"
[[ -z "$AGENT_SHA" || "$AGENT_SHA" =~ ^[0-9a-f]{64}$ ]] || die "--agent-sha256 must be 64 lowercase hex characters"
case "$MAXRISK" in R0|R1|R2|R3|R4) ;; *) die "--actions-max-risk must be R0..R4";; esac
case "$CRIT" in low|normal|high|critical) ;; *) die "--criticality must be low, normal, high or critical";; esac
[[ -z "$CA" || -r "$CA" ]] || die "--ca $CA is not readable"

# The action agent: the wo-agent binary from the dashboard's receiver, sha256-checked against /agent/manifest.json (and against
# --agent-sha256 when given), its allowlist and local policy written root-owned from the packs inside the binary, a systemd unit.
# It stays dormant (hello every 5 minutes, nothing else) until act mode is turned on and a person confirms its key.
install_actuator() {
  if [[ "$OS" != "Linux" || $ROOT -ne 1 || $HAS_SYSTEMD -ne 1 || $SERVICE -ne 1 ]]; then
    log "Action agent skipped: it needs Linux with systemd and root (this server: $OS, root=$ROOT, systemd=$HAS_SYSTEMD)"; return 0
  fi
  local arch; case "$(uname -m)" in x86_64|amd64) arch=amd64;; aarch64|arm64) arch=arm64;;
    *) log "Action agent skipped: no wo-agent build for $(uname -m) (linux amd64 / arm64 only)"; return 0;; esac
  ATMP="$(mktemp -d)"; trap 'rm -rf "${ATMP:-}"' EXIT; local tmp="$ATMP"
  if ! fetch "$BASE/agent/manifest.json" "$tmp/manifest.json" || ! fetch "$BASE/agent/wo-agent-linux-$arch" "$tmp/wo-agent"; then
    log "Action agent skipped: $BASE does not serve wo-agent-linux-$arch (receiver image without the Go agent)"; return 0
  fi
  local want got; got="$(sha256sum "$tmp/wo-agent" | cut -d' ' -f1)"
  want="$(sed -n "s/.*\"wo-agent-linux-$arch\": *{[^}]*\"sha256\": *\"\([0-9a-f]\{64\}\)\".*/\1/p" "$tmp/manifest.json")"
  [[ -n "$want" && "$got" == "$want" ]] || die "wo-agent checksum does not match the manifest (got $got): download corrupted or tampered with"
  local insecure=0
  if [[ -n "$AGENT_SHA" ]]; then
    [[ "$got" == "$AGENT_SHA" ]] || die "wo-agent checksum $got differs from --agent-sha256 $AGENT_SHA: not installed"
  elif [[ "$BASE" == http://* ]]; then
    insecure=1
    log "Warning: wo-agent came over plain HTTP and only the same server vouches for its checksum: recorded as 'güvensiz kurulum'"
    log "         (the dashboard refuses changes on this host until it is reinstalled over https or with --agent-sha256)"
  fi
  chmod 0755 "$tmp/wo-agent"
  mkdir -p "$ABIN" && install -o root -g root -m 0755 "$tmp/wo-agent" "$ABIN/wo-agent"
  if ! id "$AUSER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$AUSER" 2>/dev/null || adduser -S -H -s /sbin/nologin "$AUSER" 2>/dev/null || true
  fi
  umask 077; printf '%s\n' "$ATOKEN" > "$tmp/token"; umask 022
  "$ABIN/wo-agent" act install --actions-dir "$AETC/actions.d" --state-dir "$ASTATE/actions" --scripts-dir "$ASCRIPTS/actions" \
    --packs "$PACKS" --max-risk "$MAXRISK" --criticality "$CRIT" ${DPUB:+--dispatch-pub "$DPUB"} ${APUB:+--anchor-pub "$APUB"} \
    ${ATOKEN:+--token-file "$tmp/token"} || die "wo-agent act install failed"
  chmod 0755 "$AETC"
  umask 077
  cat > "$AETC/wo-agent.env" <<AEOF
WO_URL='$BASE'
WO_HOST='${NAME:-$(hostname)}'
WO_INSECURE_INSTALL=$insecure
WO_CA='$CA'
AEOF
  umask 022; chmod 0600 "$AETC/wo-agent.env"
  cat > "/etc/systemd/system/$AUNIT.service" <<UNITEOF
[Unit]
Description=Watchover action agent (wo-agent act: dormant until act mode is on and its key is confirmed)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$AETC/wo-agent.env
ExecStart=$ABIN/wo-agent act
Restart=always
RestartSec=10
Nice=10
CPUQuota=25%
MemoryMax=128M
TasksMax=64
UMask=0027
ProtectSystem=full
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  if [[ -s "$ASTATE/actions/actuator.token" ]]; then
    systemctl enable "$AUNIT" >/dev/null 2>&1 && systemctl restart "$AUNIT" || die "systemd could not start $AUNIT (journalctl -u $AUNIT)"
    log "Action agent installed (dormant). Key: $("$ABIN/wo-agent" act fingerprint --state-dir "$ASTATE/actions") — compare it in Bağlantı ayarları › Eylem ajanları"
  else
    log "Action agent installed but not started: no actuator token (add one in Bağlantı ayarları › Eylem ajanları, then re-run with --actions-only --actions-token wa_...)"
  fi
}

fetch() {  # fetch URL FILE: curl, wget or python, nothing else (the --ca file is trusted for https)
  if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 120 ${CA:+--cacert "$CA"} -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -T 120 ${CA:+--ca-certificate="$CA"} -O "$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then python3 -c 'import ssl,sys,urllib.request; c=ssl.create_default_context(cafile=sys.argv[3] or None); open(sys.argv[2],"wb").write(urllib.request.urlopen(sys.argv[1],timeout=120,context=c).read())' "$1" "$2" "$CA"
  else return 1; fi
}

if [[ $ACT_ONLY -eq 1 ]]; then
  install_actuator
  log "Done. Remove with: --uninstall"
  exit 0
fi

log "Python"
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  if [[ $ROOT -eq 1 && "$OS" == "Linux" ]]; then
    if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 >/dev/null; elif command -v dnf >/dev/null; then dnf install -y -q python3; elif command -v yum >/dev/null; then yum install -y -q python3; fi
    PY="$(command -v python3 || true)"
  fi
  [[ -n "$PY" ]] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "python3 >= 3.9 is required on this server"
fi

log "Agent files → $DIR"
mkdir -p "$DIR"
WO_CA="$CA" "$PY" - "$BASE/agent.py" "$DIR/agent.py" <<'PYEOF' || die "could not download agent.py from $BASE/agent.py — is the dashboard reachable from this server (address, port, firewall)?"
import os, ssl, sys, urllib.request
src, dst = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(cafile=os.environ.get("WO_CA") or None)
with urllib.request.urlopen(src, timeout=15, context=ctx) as r:
    data = r.read()
assert b"Watchover agent" in data, "unexpected content"
open(dst, "wb").write(data)
PYEOF
if [[ -z "$TOKEN" ]]; then
  log "Self-enrolment as ${NAME:-$(hostname)}"
  ENROLLED="$(WO_CA="$CA" "$PY" - "$BASE/enroll" "$EKEY" "${NAME:-$(hostname)}" "$ENV_" "$SITE" "$TAGS" <<'PYEOF'
import json, os, ssl, sys, urllib.request, urllib.error
url, key, name, env, site, tags = sys.argv[1:7]
ctx = ssl.create_default_context(cafile=os.environ.get("WO_CA") or None)
req = urllib.request.Request(url, data=json.dumps({"name": name, "env": env, "site": site, "tags": tags}).encode(),
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Agent": name}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        doc = json.loads(r.read())
        # the collector's token, then the action agent's token and the dashboard's public keys (empty when not issued yet)
        for k in ("token", "actuator_token", "dispatch_pub", "anchor_pub"):
            print(str(doc.get(k) or "").strip())
except urllib.error.HTTPError as e:
    sys.stderr.write("enrolment refused (HTTP %d): %s\n" % (e.code, (e.read() or b"").decode()[:200])); sys.exit(1)
PYEOF
)" || die "self-enrolment failed: is the enrolment key current (Agents panel › bulk install) and the name not already enrolled?"
  { read -r TOKEN; read -r _at; read -r _dp; read -r _ap; } <<<"$ENROLLED" || true
  [[ -z "$ATOKEN" ]] && ATOKEN="${_at:-}"
  [[ -z "$DPUB" ]] && DPUB="${_dp:-}"
  [[ -z "$APUB" ]] && APUB="${_ap:-}"
  [[ -n "$TOKEN" ]] || die "self-enrolment answered without a token"
fi
umask 077
cat > "$DIR/agent.env" <<ENVEOF
WATCHOVER_URL='$URL'
WATCHOVER_TOKEN='$TOKEN'
WATCHOVER_LOGS='$LOGS'
WATCHOVER_METRICS=1
WATCHOVER_FLOWS=$FLOWS
WATCHOVER_INTERVAL='$INTERVAL'
WATCHOVER_SPOOL='$DIR/spool'
AGENT_ENV='$ENV_'
WATCHOVER_CA='$CA'
ENVEOF
chmod 600 "$DIR/agent.env"; mkdir -p -m 700 "$DIR/spool"; chmod 700 "$DIR/spool"
umask 022
IFS=',' read -ra _paths <<<"$LOGS"
for _p in "${_paths[@]}"; do _p="$(echo "$_p" | xargs)"; [[ -z "$_p" || "$_p" == "auto" || "$_p" == "journal" || "$_p" == "windows-events" || "$_p" == *"*"* || -r "$_p" ]] || echo "  warning: $_p does not exist yet on this server (the agent keeps waiting for it)"; done

if [[ $TEST -eq 1 ]]; then
  log "Connectivity test"
  ( set -a; . "$DIR/agent.env"; set +a; "$PY" "$DIR/agent.py" --test ) || die "the receiver at $URL did not accept this agent: check the address/port (firewall on the dashboard machine) and that the token is active"
fi

[[ -f "$DIR/agent.pid" ]] && { kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true; rm -f "$DIR/agent.pid"; }   # replace a previous background run
if [[ $SERVICE -eq 1 && $ROOT -eq 1 && $HAS_SYSTEMD -eq 1 ]]; then
  log "systemd service $UNIT"
  cat > "/etc/systemd/system/$UNIT.service" <<UNITEOF
[Unit]
Description=Watchover agent (metrics + logs → dashboard)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$DIR/agent.env
ExecStart=$PY $DIR/agent.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload && systemctl enable "$UNIT" >/dev/null 2>&1 && systemctl restart "$UNIT" || die "systemd could not start $UNIT (journalctl -u $UNIT)"
  sleep 1; systemctl --no-pager --lines=3 status "$UNIT" || true
elif [[ $SERVICE -eq 1 && "$OS" == "Darwin" ]]; then
  log "launchd agent com.watchover.agent"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.watchover.agent</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string><string>set -a; . "$DIR/agent.env"; set +a; exec "$PY" "$DIR/agent.py"</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/agent.log</string><key>StandardErrorPath</key><string>$DIR/agent.log</string>
</dict></plist>
PLISTEOF
  if [[ $ROOT -eq 1 ]]; then
    chown root:wheel "$PLIST"; chmod 644 "$PLIST"
    launchctl bootout system/com.watchover.agent 2>/dev/null || true; launchctl bootstrap system "$PLIST"   # LaunchDaemon: runs at boot, as root
  else
    launchctl unload "$PLIST" 2>/dev/null || true; launchctl load "$PLIST"
  fi
elif [[ $SERVICE -eq 1 ]]; then
  log "Background process (no systemd/launchd): $DIR/agent.log"
  ( set -a; . "$DIR/agent.env"; set +a; nohup "$PY" "$DIR/agent.py" >>"$DIR/agent.log" 2>&1 & echo $! > "$DIR/agent.pid" )
fi
install_actuator
log "Installed. Files: $DIR · settings: $DIR/agent.env · re-run with --uninstall to remove"
