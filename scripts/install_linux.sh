#!/usr/bin/env bash
# Watchover — Linux server install (Debian/Ubuntu, RHEL/Rocky, Fedora). No Docker.
#   sudo bash scripts/install_linux.sh                 # app only (python venv + systemd service on :8501)
#   sudo bash scripts/install_linux.sh --with-ollama   # + Ollama runtime + recommended models (chat + embeddings)
#   Options: --dir /opt/watchover  --user watchover  --port 8501  --models "qwen2.5:7b-instruct bge-m3"  --no-service
set -euo pipefail
DIR=/opt/watchover; USER_=watchover; PORT=8501; WITH_OLLAMA=0; MODELS="qwen2.5:7b-instruct bge-m3"; SERVICE=1
while [[ $# -gt 0 ]]; do case "$1" in
  --with-ollama) WITH_OLLAMA=1;; --dir) DIR="$2"; shift;; --user) USER_="$2"; shift;; --port) PORT="$2"; shift;;
  --models) MODELS="$2"; shift;; --no-service) SERVICE=0;; *) echo "unknown option $1"; exit 1;; esac; shift; done
SRC="$(cd "$(dirname "$0")/.." && pwd)"
log(){ printf '\033[1;36m▶ %s\033[0m\n' "$*"; }

log "System packages"
if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip curl git >/dev/null
elif command -v dnf >/dev/null; then dnf install -y -q python3 python3-pip curl git
else echo "unsupported package manager; install python3 (>=3.9), pip, curl, git and rerun"; exit 1; fi

log "Service user and directory ($DIR)"
id -u "$USER_" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$DIR" "$USER_"
mkdir -p "$DIR"
if [[ "$SRC" != "$DIR" ]]; then rsync -a --delete --exclude .git --exclude .venv --exclude '*.db' --exclude data "$SRC/" "$DIR/" 2>/dev/null || cp -r "$SRC/." "$DIR/"; fi
mkdir -p "$DIR/data/live"
[[ -f "$DIR/.env" ]] || cp "$DIR/.env.example" "$DIR/.env"

log "Python environment"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR[mcp]"
chown -R "$USER_":"$USER_" "$DIR"

if [[ $WITH_OLLAMA -eq 1 ]]; then
  log "Ollama runtime"
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  systemctl enable --now ollama 2>/dev/null || true
  for i in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
  for m in $MODELS; do log "ollama pull $m"; ollama pull "$m"; done
  sed -i 's#^LLM_BASE_URL=.*#LLM_BASE_URL=http://127.0.0.1:11434#; s#^LLM_MODEL=.*#LLM_MODEL='"${MODELS%% *}"'#; s#^LLM_EMBED_MODEL=.*#LLM_EMBED_MODEL='"$(echo "$MODELS" | tr ' ' '\n' | grep -E 'embed|bge' | head -1)"'#' "$DIR/.env"
  grep -q '^LLM_PROVIDER=' "$DIR/.env" || echo "LLM_PROVIDER=ollama" >> "$DIR/.env"
fi

if [[ $SERVICE -eq 1 ]]; then
  log "systemd service watchover.service (:$PORT)"
  sed "s#__DIR__#$DIR#g; s#__USER__#$USER_#g; s#__PORT__#$PORT#g" "$DIR/scripts/watchover.service" > /etc/systemd/system/watchover.service
  systemctl daemon-reload && systemctl enable --now watchover
  sleep 2; systemctl --no-pager --lines=5 status watchover || true
fi
log "Done. Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT  · env: $DIR/.env  · logs: journalctl -u watchover -f"
