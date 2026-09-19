#!/usr/bin/env bash
# Watchover launcher (installed by scripts/install.sh). Usage: watchover start|stop|restart|status|run|open|update [file.zip]|logs|agent
DIR="__DIR__"; SRC="__SRC__"; PORT="__PORT__"; REPO="__REPO__"; BRANCH="__BRANCH__"
export WATCHOVER_HOME="$DIR/data"; export WATCHOVER_LAUNCHER="$DIR/bin/watchover"; export WATCHOVER_LOG="$DIR/data/watchover.log"
[[ -f "$DIR/data/.env" ]] && set -a && . "$DIR/data/.env" && set +a
ADDR="${WATCHOVER_UI_ADDRESS:-127.0.0.1}"   # dashboard UI: loopback by default; WATCHOVER_UI_ADDRESS=0.0.0.0 in data/.env to expose it (put TLS/SSO in front)
PID="$DIR/data/watchover.pid"; LOG="$DIR/data/watchover.log"
LABEL=com.watchover.app; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# production never hot-reloads modules (a half-updated process is worse than a restart): update → restart
ST_ARGS=(--server.address "$ADDR" --server.port "$PORT" --server.headless true --server.fileWatcherType none --browser.gatherUsageStats false)
svc(){  # who supervises the app: launchd (macOS login item), systemd (user unit) or nobody (pid file)
  if [[ "$(uname -s)" == "Darwin" ]] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then echo launchd
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled watchover >/dev/null 2>&1; then echo systemd; fi; }
run(){ cd "$SRC" && exec "$DIR/.venv/bin/python" -m streamlit run app.py "${ST_ARGS[@]}"; }
wait_up(){ for i in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && return 0; sleep 1; done; echo "not answering on port $PORT yet — see: watchover logs"; return 1; }
case "${1:-start}" in
  run) run;;
  start) case "$(svc)" in
           launchd) launchctl kickstart "gui/$(id -u)/$LABEL";;
           systemd) systemctl --user start watchover;;
           *) if [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; then echo "already running (pid $(cat "$PID")) → http://localhost:$PORT"; exit 0; fi
              if [[ "$(uname -s)" == "Darwin" && -f "$PLIST" ]]; then launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true; fi
              if [[ "$(svc)" == "" ]]; then (cd "$SRC" && nohup "$DIR/.venv/bin/python" -m streamlit run app.py "${ST_ARGS[@]}" >>"$LOG" 2>&1 & echo $! > "$PID"); fi;;
         esac; wait_up && echo "started → http://localhost:$PORT";;
  stop) case "$(svc)" in
          launchd) launchctl bootout "gui/$(id -u)/$LABEL" && echo "stopped (login item unloaded until 'watchover start' or next login)";;
          systemd) systemctl --user stop watchover && echo "stopped";;
          *) if [[ -f "$PID" ]] && kill "$(cat "$PID")" 2>/dev/null; then
               for i in $(seq 1 20); do kill -0 "$(cat "$PID")" 2>/dev/null || break; sleep 0.5; done; rm -f "$PID"; echo "stopped"
             else echo "not running"; fi;;
        esac;;
  restart) case "$(svc)" in
             launchd) launchctl kickstart -k "gui/$(id -u)/$LABEL";;
             systemd) systemctl --user restart watchover;;
             *) "$0" stop; sleep 1; "$0" start; exit $?;;
           esac; wait_up && echo "restarted → http://localhost:$PORT";;
  status) if curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then echo "running ($(svc)) → http://localhost:$PORT  ·  code: $SRC  ·  data: $DIR/data"; else echo "stopped"; fi;;
  open) command -v open >/dev/null && open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT";;
  update)  # watchover update            → git pull (a cloned checkout)
           # watchover update FILE.zip   → unpack the zip over the code folder (settings, tokens and the knowledge base in data/ are kept)
    if [[ -n "${2:-}" && "$2" == *.zip ]]; then
      TMP="$(mktemp -d)"; unzip -q "$2" -d "$TMP" || { echo "cannot unzip $2"; exit 1; }
      NEW="$(find "$TMP" -maxdepth 2 -name app.py -print -quit)"; NEW="$(dirname "$NEW")"
      [[ -f "$NEW/app.py" && -d "$NEW/src/watchover" ]] || { echo "no Watchover code in $2"; exit 1; }
      (cd "$NEW" && tar cf - --exclude=.env --exclude='*.db' --exclude=data .) | (cd "$SRC" && tar xf -); rm -rf "$TMP"
      echo "code updated from $2 → $SRC"
    elif [[ -d "$SRC/.git" ]]; then
      git -C "$SRC" remote get-url origin >/dev/null 2>&1 || git -C "$SRC" remote add origin "$REPO"
      git -C "$SRC" fetch -q origin "$BRANCH" || { echo "cannot reach $REPO — use: watchover update /path/to/watchover.zip"; exit 1; }
      if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then git -C "$SRC" stash push -q -u -m "watchover update $(date +%F)"; echo "local changes stashed (git stash list)"; fi
      git -C "$SRC" checkout -q -B "$BRANCH" "origin/$BRANCH" && echo "code updated → $(git -C "$SRC" log -1 --format='%h %s')"
    else echo "$SRC is not a git checkout: run  watchover update /path/to/watchover.zip  with the new archive"; exit 1; fi
    "$DIR/.venv/bin/pip" install -q -e "$SRC[mcp]"
    if [[ -f "$SRC/scripts/watchover-launcher.sh" ]]; then   # the launcher updates itself so new commands arrive with the code
      P="__"   # placeholders are spelled via $P so the install-time sed does not rewrite this very line
      sed "s#${P}DIR${P}#$DIR#g; s#${P}SRC${P}#$SRC#g; s#${P}PORT${P}#$PORT#g; s#${P}REPO${P}#$REPO#g; s#${P}BRANCH${P}#$BRANCH#g" "$SRC/scripts/watchover-launcher.sh" > "$DIR/bin/watchover.new" \
        && chmod +x "$DIR/bin/watchover.new" && mv "$DIR/bin/watchover.new" "$DIR/bin/watchover"
    fi
    exec "$DIR/bin/watchover" restart;;
  logs) tail -f "$LOG";;
  agent) shift; cd "$SRC" && exec "$DIR/.venv/bin/python" agent.py "$@";;
  *) echo "usage: watchover start|stop|restart|status|run|open|update [file.zip]|logs|agent [args]"; exit 1;;
esac
