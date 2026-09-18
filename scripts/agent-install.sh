#!/usr/bin/env bash
# Watchover agent — install on a server (Linux systemd, macOS launchd, or plain background process).
# The dashboard serves this script and agent.py itself, so the server needs nothing but python3 and network access to
# the dashboard's receiver port:
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --token wo_... --logs "/var/log/syslog,/var/log/app/*.log"
# Options: --url U  --token T  --logs "a,b"  --interval 10  --env prod  --dir /opt/watchover-agent  --no-service  --no-test  --uninstall
set -euo pipefail
URL=""; TOKEN=""; LOGS=""; INTERVAL=10; ENV_=""; DIR=""; SERVICE=1; TEST=1; UNINSTALL=0
while [[ $# -gt 0 ]]; do case "$1" in
  --url) URL="$2"; shift;; --token) TOKEN="$2"; shift;; --logs) LOGS="$2"; shift;; --interval) INTERVAL="$2"; shift;; --env) ENV_="$2"; shift;;
  --dir) DIR="$2"; shift;; --no-service) SERVICE=0;; --no-test) TEST=0;; --uninstall) UNINSTALL=1;; *) echo "unknown option: $1"; exit 1;; esac; shift; done
log(){ printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
OS="$(uname -s)"; ROOT=0; [[ "$(id -u)" == "0" ]] && ROOT=1
if [[ -z "$DIR" ]]; then if [[ $ROOT -eq 1 ]]; then DIR=/opt/watchover-agent; else DIR="$HOME/.watchover-agent"; fi; fi
UNIT=watchover-agent; PLIST="$HOME/Library/LaunchAgents/com.watchover.agent.plist"

if [[ $UNINSTALL -eq 1 ]]; then
  log "Removing the agent"
  if [[ "$OS" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then systemctl disable --now "$UNIT" 2>/dev/null || true; rm -f "/etc/systemd/system/$UNIT.service"; systemctl daemon-reload || true
  elif [[ "$OS" == "Darwin" ]]; then launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; fi
  [[ -f "$DIR/agent.pid" ]] && kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true
  rm -rf "$DIR"; log "Done"; exit 0
fi
[[ -n "$URL" && -n "$TOKEN" ]] || die "--url http://<dashboard>:<port>/ingest and --token wo_... are required (both come from the Agents panel)"
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
cat > "$DIR/agent.env" <<ENVEOF
WATCHOVER_URL=$URL
WATCHOVER_TOKEN=$TOKEN
WATCHOVER_LOGS=$LOGS
WATCHOVER_METRICS=1
WATCHOVER_INTERVAL=$INTERVAL
WATCHOVER_SPOOL=$DIR/spool
AGENT_ENV=$ENV_
ENVEOF
chmod 600 "$DIR/agent.env"; mkdir -p "$DIR/spool"

if [[ $TEST -eq 1 ]]; then
  log "Connectivity test"
  ( set -a; . "$DIR/agent.env"; set +a; "$PY" "$DIR/agent.py" --test ) || die "the receiver did not accept this agent: check the address/port (firewall on the dashboard machine) and that the token is active"
fi

if [[ $SERVICE -eq 1 && "$OS" == "Linux" && $ROOT -eq 1 ]] && command -v systemctl >/dev/null 2>&1; then
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
  systemctl daemon-reload && systemctl enable --now "$UNIT" && sleep 1 && systemctl --no-pager --lines=3 status "$UNIT" || true
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
  launchctl unload "$PLIST" 2>/dev/null || true; launchctl load "$PLIST"
elif [[ $SERVICE -eq 1 ]]; then
  log "Background process (no systemd/launchd rights): $DIR/agent.log"
  ( set -a; . "$DIR/agent.env"; set +a; nohup "$PY" "$DIR/agent.py" >>"$DIR/agent.log" 2>&1 & echo $! > "$DIR/agent.pid" )
fi
log "Installed. Files: $DIR · settings: $DIR/agent.env · re-run with --uninstall to remove"
