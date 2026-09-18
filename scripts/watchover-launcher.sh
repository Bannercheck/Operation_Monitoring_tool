#!/usr/bin/env bash
# Watchover launcher (installed by scripts/install.sh). Usage: watchover start|stop|restart|status|run|open|update|logs|agent
DIR="__DIR__"; SRC="__SRC__"; PORT="__PORT__"
export WATCHOVER_HOME="$DIR/data"
[[ -f "$DIR/data/.env" ]] && set -a && . "$DIR/data/.env" && set +a
ADDR="${WATCHOVER_UI_ADDRESS:-127.0.0.1}"   # dashboard UI: loopback by default; WATCHOVER_UI_ADDRESS=0.0.0.0 in data/.env to expose it (put TLS/SSO in front)
PID="$DIR/data/watchover.pid"; LOG="$DIR/data/watchover.log"
run(){ cd "$SRC" && exec "$DIR/.venv/bin/python" -m streamlit run app.py --server.address "$ADDR" --server.port "$PORT" --server.headless true --browser.gatherUsageStats false; }
case "${1:-start}" in
  run) run;;
  start) if [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; then echo "already running (pid $(cat "$PID")) → http://localhost:$PORT"; exit 0; fi
         (cd "$SRC" && nohup "$DIR/.venv/bin/python" -m streamlit run app.py --server.address "$ADDR" --server.port "$PORT" --server.headless true --browser.gatherUsageStats false >>"$LOG" 2>&1 & echo $! > "$PID")
         echo "started → http://localhost:$PORT";;
  stop) [[ -f "$PID" ]] && kill "$(cat "$PID")" 2>/dev/null && rm -f "$PID" && echo "stopped" || echo "not running";;
  restart) "$0" stop; sleep 1; "$0" start;;
  status) if [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; then echo "running (pid $(cat "$PID")) → http://localhost:$PORT"; else curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && echo "running (service) → http://localhost:$PORT" || echo "stopped"; fi;;
  open) command -v open >/dev/null && open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT";;
  update)  # watchover update            → git pull (a cloned checkout)
           # watchover update FILE.zip   → unpack the zip over the code folder (settings, tokens and the knowledge base in data/ are kept)
    if [[ -n "${2:-}" && "$2" == *.zip ]]; then
      TMP="$(mktemp -d)"; unzip -q "$2" -d "$TMP" || { echo "cannot unzip $2"; exit 1; }
      NEW="$(find "$TMP" -maxdepth 2 -name app.py -print -quit)"; NEW="$(dirname "$NEW")"
      [[ -f "$NEW/app.py" && -d "$NEW/src/watchover" ]] || { echo "no Watchover code in $2"; exit 1; }
      (cd "$NEW" && tar cf - --exclude=.env --exclude='*.db' --exclude=data .) | (cd "$SRC" && tar xf -); rm -rf "$TMP"
      echo "code updated from $2 → $SRC"
    elif [[ -d "$SRC/.git" ]]; then git -C "$SRC" pull --ff-only
    else echo "$SRC is not a git checkout: run  watchover update /path/to/watchover.zip  with the new archive"; exit 1; fi
    "$DIR/.venv/bin/pip" install -q -e "$SRC[mcp]" && "$0" restart;;
  logs) tail -f "$LOG";;
  agent) shift; cd "$SRC" && exec "$DIR/.venv/bin/python" agent.py "$@";;
  *) echo "usage: watchover start|stop|restart|status|run|open|update [file.zip]|logs|agent [args]"; exit 1;;
esac
