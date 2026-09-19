#!/usr/bin/env bash
# Watchover agent — install on a server (Linux systemd, macOS launchd, or plain background process).
# The dashboard serves this script and agent.py itself, so the server needs nothing but python3 and network access to
# the dashboard's receiver port:
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --token wo_... --logs "/var/log/syslog,/var/log/app/*.log"
#   … or let the server enrol itself with the fleet key (Agents panel › bulk install); its own token is issued and stored here:
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --enroll-key wk_... --env prod --site IST-DC1
# Options: --url U  --token T | --enroll-key K [--name N --site S --tags "a,b"]  --logs "a,b"  --interval 10  --env prod  --dir /opt/watchover-agent  --no-service  --no-test  --uninstall
set -euo pipefail
URL=""; TOKEN=""; LOGS=""; INTERVAL=10; ENV_=""; DIR=""; SERVICE=1; TEST=1; UNINSTALL=0; EKEY=""; NAME=""; SITE=""; TAGS=""
while [[ $# -gt 0 ]]; do case "$1" in
  --url) URL="$2"; shift;; --token) TOKEN="$2"; shift;; --logs) LOGS="$2"; shift;; --interval) INTERVAL="$2"; shift;; --env) ENV_="$2"; shift;;
  --enroll-key) EKEY="$2"; shift;; --name) NAME="$2"; shift;; --site) SITE="$2"; shift;; --tags) TAGS="$2"; shift;;
  --dir) DIR="$2"; shift;; --no-service) SERVICE=0;; --no-test) TEST=0;; --uninstall) UNINSTALL=1;; *) echo "unknown option: $1"; exit 1;; esac; shift; done
log(){ printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
OS="$(uname -s)"; ROOT=0; [[ "$(id -u)" == "0" ]] && ROOT=1
if [[ -z "$DIR" ]]; then if [[ $ROOT -eq 1 ]]; then DIR=/opt/watchover-agent; else DIR="$HOME/.watchover-agent"; fi; fi
UNIT=watchover-agent
if [[ $ROOT -eq 1 ]]; then PLIST="/Library/LaunchDaemons/com.watchover.agent.plist"; else PLIST="$HOME/Library/LaunchAgents/com.watchover.agent.plist"; fi
[[ "$LOGS" == *"'"* ]] && die "--logs may not contain single quotes"
HAS_SYSTEMD=0; [[ "$OS" == "Linux" && -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1 && HAS_SYSTEMD=1

if [[ $UNINSTALL -eq 1 ]]; then
  log "Removing the agent"
  if [[ $HAS_SYSTEMD -eq 1 ]]; then systemctl disable --now "$UNIT" 2>/dev/null || true; rm -f "/etc/systemd/system/$UNIT.service"; systemctl daemon-reload || true
  elif [[ "$OS" == "Darwin" ]]; then
    if [[ $ROOT -eq 1 ]]; then launchctl bootout system/com.watchover.agent 2>/dev/null || true; else launchctl unload "$PLIST" 2>/dev/null || true; fi; rm -f "$PLIST"; fi
  [[ -f "$DIR/agent.pid" ]] && kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true
  rm -rf "$DIR"; log "Done"; exit 0
fi
[[ -n "$URL" && ( -n "$TOKEN" || -n "$EKEY" ) ]] || die "--url http://<dashboard>:<port>/ingest and --token wo_... (or --enroll-key wk_...) are required (both come from the Agents panel)"
[[ -z "$LOGS" ]] && LOGS="auto"   # discover the applications on this server and follow their logs
[[ "$URL" == */ingest ]] || URL="${URL%/}/ingest"
BASE="${URL%/ingest}"

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
"$PY" - "$BASE/agent.py" "$DIR/agent.py" <<'PYEOF' || die "could not download agent.py from $BASE/agent.py — is the dashboard reachable from this server (address, port, firewall)?"
import sys, urllib.request
src, dst = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(src, timeout=15) as r:
    data = r.read()
assert b"Watchover agent" in data, "unexpected content"
open(dst, "wb").write(data)
PYEOF
if [[ -z "$TOKEN" ]]; then
  log "Self-enrolment as ${NAME:-$(hostname)}"
  TOKEN="$("$PY" - "$BASE/enroll" "$EKEY" "${NAME:-$(hostname)}" "$ENV_" "$SITE" "$TAGS" <<'PYEOF'
import json, sys, urllib.request, urllib.error
url, key, name, env, site, tags = sys.argv[1:7]
req = urllib.request.Request(url, data=json.dumps({"name": name, "env": env, "site": site, "tags": tags}).encode(),
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Agent": name}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(json.loads(r.read())["token"])
except urllib.error.HTTPError as e:
    sys.stderr.write("enrolment refused (HTTP %d): %s\n" % (e.code, (e.read() or b"").decode()[:200])); sys.exit(1)
PYEOF
)" || die "self-enrolment failed: is the enrolment key current (Agents panel › bulk install) and the name not already enrolled?"
fi
umask 077
cat > "$DIR/agent.env" <<ENVEOF
WATCHOVER_URL='$URL'
WATCHOVER_TOKEN='$TOKEN'
WATCHOVER_LOGS='$LOGS'
WATCHOVER_METRICS=1
WATCHOVER_INTERVAL='$INTERVAL'
WATCHOVER_SPOOL='$DIR/spool'
AGENT_ENV='$ENV_'
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
log "Installed. Files: $DIR · settings: $DIR/agent.env · re-run with --uninstall to remove"
