#!/usr/bin/env bash
# Watchover — one-line install for macOS and Linux, as the current user (no sudo, no Docker).
#   curl -fsSL https://raw.githubusercontent.com/Bannercheck/Operation_Monitoring_tool/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --with-ollama --models "qwen2.5:7b-instruct bge-m3"
#   Options: --dir <path>  --repo <git url>  --branch <name>  --port 8501  --with-ollama  --models "…"  --no-autostart  --no-open
set -euo pipefail
REPO="${WATCHOVER_REPO:-https://github.com/Bannercheck/Operation_Monitoring_tool.git}"
BRANCH="${WATCHOVER_BRANCH:-main}"
PORT=8501; WITH_OLLAMA=0
MODELS="qwen2.5:7b-instruct bge-m3"; AUTOSTART=1; OPEN=1
OS="$(uname -s)"
if [[ "$OS" == "Darwin" ]]; then DIR="$HOME/Library/Application Support/Watchover"; else DIR="${XDG_DATA_HOME:-$HOME/.local/share}/watchover"; fi
while [[ $# -gt 0 ]]; do case "$1" in
  --dir) DIR="$2"; shift;; --repo) REPO="$2"; shift;; --branch) BRANCH="$2"; shift;; --port) PORT="$2"; shift;;
  --with-ollama) WITH_OLLAMA=1;; --models) MODELS="$2"; shift;; --no-autostart) AUTOSTART=0;; --no-open) OPEN=0;;
  *) echo "unknown option: $1"; exit 1;; esac; shift; done
log(){ printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }

log "Watchover → $DIR"
PY=""
for c in python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$(command -v "$c")"; break; fi
done
if [[ -z "$PY" ]]; then
  if [[ "$OS" == "Darwin" ]]; then
    command -v brew >/dev/null 2>&1 || die "Python 3.9+ not found. Install Xcode Command Line Tools (xcode-select --install) or Homebrew, then rerun."
    log "Installing python via Homebrew"; brew install -q python@3.12; PY="$(brew --prefix)/bin/python3.12"
  else die "Python 3.9+ not found: install python3 + python3-venv with your package manager and rerun."; fi
fi
command -v git >/dev/null 2>&1 || { [[ "$OS" == "Darwin" ]] && xcode-select --install 2>/dev/null; die "git not found; install it and rerun."; }

mkdir -p "$DIR"
SRC_DIR="$DIR/app"
if [[ -f "$(pwd)/app.py" && -d "$(pwd)/src/watchover" ]]; then
  log "Using the repository in $(pwd)"; SRC_DIR="$(pwd)"
elif [[ -d "$SRC_DIR/.git" ]]; then
  log "Updating existing checkout"; git -C "$SRC_DIR" fetch -q origin "$BRANCH" && git -C "$SRC_DIR" checkout -q "$BRANCH" && git -C "$SRC_DIR" pull -q --ff-only origin "$BRANCH"
else
  log "Cloning $REPO ($BRANCH)"; git clone -q --depth 1 -b "$BRANCH" "$REPO" "$SRC_DIR"
fi
[[ -f "$SRC_DIR/app.py" && -d "$SRC_DIR/src/watchover" ]] || die "No Watchover code in $SRC_DIR (branch '$BRANCH'). Run the script from inside the repository folder or pass --branch."

log "Python environment"
if ! "$PY" -m venv "$DIR/.venv"; then
  if [[ "$OS" == "Darwin" ]]; then die "venv failed. Move out of Downloads/Desktop (privacy protection) or run: xattr -dr com.apple.quarantine \"$DIR\""; else die "venv failed. Install the venv module first: apt install python3-venv  (Debian/Ubuntu)  ·  dnf install python3  (RHEL/Rocky)"; fi
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$SRC_DIR[mcp]"
mkdir -p "$DIR/data/live"
[[ -f "$DIR/data/.env" ]] || cp "$SRC_DIR/.env.example" "$DIR/data/.env"

if [[ $WITH_OLLAMA -eq 1 ]]; then
  log "Ollama"
  if ! command -v ollama >/dev/null 2>&1; then
    if [[ "$OS" == "Darwin" ]]; then
      if command -v brew >/dev/null 2>&1; then brew install -q --cask ollama; open -a Ollama || true
      else die "Install Ollama from https://ollama.com/download (or Homebrew), open it once, then rerun with --with-ollama"; fi
    else curl -fsSL https://ollama.com/install.sh | sh; fi
  fi
  [[ "$OS" == "Darwin" ]] && (pgrep -x Ollama >/dev/null || open -a Ollama || (ollama serve >/dev/null 2>&1 &)) || true
  for i in $(seq 1 60); do curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || die "Ollama is not answering on :11434"
  for m in $MODELS; do log "ollama pull $m"; ollama pull "$m"; done
fi

log "Launcher: $DIR/bin/watchover"
mkdir -p "$DIR/bin"
sed "s#__DIR__#$DIR#g; s#__SRC__#$SRC_DIR#g; s#__PORT__#$PORT#g; s#__REPO__#$REPO#g; s#__BRANCH__#$BRANCH#g" "$SRC_DIR/scripts/watchover-launcher.sh" > "$DIR/bin/watchover"
chmod +x "$DIR/bin/watchover"
for b in "$HOME/.local/bin" /usr/local/bin /opt/homebrew/bin; do
  if [[ -d "$b" && -w "$b" ]]; then ln -sf "$DIR/bin/watchover" "$b/watchover"; log "Command 'watchover' linked into $b"; break; fi
done

if [[ $AUTOSTART -eq 1 ]]; then
  if [[ "$OS" == "Darwin" ]]; then
    PL="$HOME/Library/LaunchAgents/com.watchover.app.plist"; mkdir -p "$(dirname "$PL")"
    cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.watchover.app</string>
  <key>ProgramArguments</key><array><string>$DIR/bin/watchover</string><string>run</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/watchover.log</string><key>StandardErrorPath</key><string>$DIR/data/watchover.log</string>
</dict></plist>
PLIST
    launchctl unload "$PL" >/dev/null 2>&1 || true; launchctl load "$PL"; log "LaunchAgent installed (starts at login)"
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user is-system-running >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/watchover.service" <<UNIT
[Unit]
Description=Watchover operations signal desk
After=network-online.target
[Service]
ExecStart=$DIR/bin/watchover run
Restart=on-failure
[Install]
WantedBy=default.target
UNIT
    if systemctl --user daemon-reload && systemctl --user enable --now watchover; then
      loginctl enable-linger "$(id -un)" 2>/dev/null || true; log "user systemd service installed"
    else
      log "user systemd unavailable: starting as a background process"; "$DIR/bin/watchover" start
    fi
  else
    log "no launchd/systemd session: starting as a background process (use 'watchover start' after a reboot)"; "$DIR/bin/watchover" start
  fi
else
  "$DIR/bin/watchover" start
fi
UP=0; for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && { UP=1; break; }; sleep 1; done
[[ $UP -eq 1 ]] || die "the app did not answer on port $PORT within 60 s — see: watchover logs"
log "Done → http://localhost:$PORT   (watchover start|stop|status|open|update|logs|token)"
if [[ $OPEN -eq 1 ]]; then command -v open >/dev/null && open "http://localhost:$PORT" || { command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT"; } || true; fi
exit 0
