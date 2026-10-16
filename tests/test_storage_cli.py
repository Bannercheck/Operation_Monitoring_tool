"""./watchover.sh storage show | set in a sandbox: Docker is a fake on PATH that runs the image's `python -m watchover.logtiers ...`
commands here, with the container's mount points mapped to the host paths and named volumes (so the data really moves). Checked:
paths are given by hand and validated, the existing hot days and sealed archives move without loss (sha256 per file), .env and
the retention switch, the old copies go only after the health check, a new path that does not come up puts the old one back, and
show prints what is set (or "NOT SET") without proposing any size."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
FAKE = r'''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_DIR/calls.log"
case "$1" in
  run)
    shift; mounts=()
    while [ $# -gt 0 ]; do case "$1" in --rm) shift ;; --network|--user|--entrypoint) shift 2 ;; -v) mounts+=("$2"); shift 2 ;; *) break ;; esac; done
    shift                                                     # the image
    args=()
    for a in "$@"; do
      for m in "${mounts[@]}"; do
        src="${m%%:*}"; rest="${m#*:}"; dst="${rest%%:*}"
        case "$src" in /*) ;; *) src="$FAKE_DIR/volumes/$src"; mkdir -p "$src" ;; esac
        case "$a" in "$dst"|"$dst"/*) a="$src${a#"$dst"}"; break ;; esac
      done
      args+=("$a")
    done
    [ "${args[0]}" = python ] && args[0]="$FAKE_PY"
    PYTHONPATH="$FAKE_SRC" "${args[@]}"; exit $? ;;
  image) exit 0 ;;
  info) case "${3:-}" in *NCPU*) echo 8 ;; *MemTotal*) echo 17179869184 ;; esac; exit 0 ;;
  compose)
    case "$*" in
      *"compose version"*) echo "Docker Compose version v2.29" ;;
      *" ps "*--format*|*" ps --format"*) echo "dashboard healthy" ;;
      *" config"*) echo '{"volumes":{"watchover-data":{"name":"watchover_watchover-data"}}}' ;;
      *" up "*) cp .env "$FAKE_DIR/env-at-up" ;;
      *api/health*) [ -f "$FAKE_DIR/broken" ] && grep -q "$(cat "$FAKE_DIR/broken")" .env && exit 7
                    echo "{\"ok\":true,\"version\":\"$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)\"}" ;;
    esac ;;
esac
exit 0
'''


@pytest.fixture
def box(tmp_path):
    d = tmp_path / "install"
    d.mkdir()
    for f in ("watchover.sh", "docker-compose.yml", "docker-compose.edge.yml", "docker-compose.loghot.yml", "docker-compose.logcold.yml",
              "docker-compose.otlp.yml", ".env.docker.example", "pyproject.toml"):
        shutil.copy(ROOT / f, d / f)
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin" / "docker").write_text(FAKE)
    (fake / "bin" / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake / 'bin'}:{os.environ['PATH']}", FAKE_DIR=str(fake), FAKE_SRC=str(ROOT / "src"), FAKE_PY=sys.executable,
               WATCHOVER_HEALTH_POLL="0.1", WATCHOVER_STORAGE_HEALTH_S="2", WATCHOVER_ENGINE="docker")
    for k in ("WATCHOVER_IN_UPDATER", "WATCHOVER_LOG_HOT_DIR", "WATCHOVER_LOG_COLD_DIR", "WATCHOVER_HOME"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(d / "watchover.sh"), "install"], cwd=d, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    vol = fake / "volumes" / "watchover_watchover-data"
    return d, env, fake, vol


def run(box, *args):
    d, env, _f, _v = box
    return subprocess.run(["bash", str(d / "watchover.sh"), *args], cwd=d, env=env, capture_output=True, text=True, timeout=120)


def envval(d, key):
    return next((ln.split("=", 1)[1] for ln in (d / ".env").read_text().splitlines() if ln.startswith(key + "=")), None)


def seed(vol: Path):
    """Raw logs in the data volume as a running install leaves them: hot day files, sealed archives, the manifest."""
    from watchover import logtiers as lt
    from watchover.live import LiveStore
    hot, cold = lt.HotStore(vol / "logs" / "hot"), lt.ColdStore(vol / "logs" / "cold")
    st = LiveStore(maxlen=5)
    st.retention_h = 24 * 30
    st.attach_db(hot)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in (5, 4, 3, 1):
        recs = [{"ts": (today - timedelta(days=d) + timedelta(hours=8, minutes=i)).isoformat(), "level": "info", "host": "ea-app-01",
                 "msg": f"line {d}-{i}"} for i in range(30)]
        st.ingest("app.jsonl", ("\n".join(json.dumps(x) for x in recs) + "\n").encode(), agent="ea-app-01")
    tiers = lt.LogTiers(hot, cold, lambda: {"live_retention_h": 48, "cold_retention_days": 365}, subprocess_ok=False)
    tiers.tick(now=today + timedelta(hours=12))
    hot.close()
    assert len(cold.days()) == 3 and hot.days()
    return {k: digest(vol / "logs" / k) for k in ("hot", "cold")}


def digest(d: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()
            if p.is_file() and (p.name.startswith("logs-") or p.name in ("meta.db", "MANIFEST.jsonl", "MANIFEST.head"))}


def test_storage_set_moves_both_tiers_without_loss_then_show(box, tmp_path):
    d, env, fake, vol = box
    before = seed(vol)
    r = run(box, "storage", "show")
    assert r.returncode == 0, r.stderr
    assert "NOT SET - inside the data volume (/data/logs/hot)" in r.stdout and "NOT SET - inside the data volume (/data/logs/cold)" in r.stdout
    assert "keeps 365 days · 3 archives" in r.stdout and "manifest: 3 entries, head seq 3" in r.stdout and "suggest" not in r.stdout.lower()
    hot, cold = tmp_path / "nvme" / "wo-hot", tmp_path / "sata" / "wo-arsiv"
    r = run(box, "storage", "set", "--hot", str(hot), "--cold", str(cold), "--cold-days", "400", "--hot-hours", "72")
    assert r.returncode == 0, r.stdout + r.stderr
    assert envval(d, "WATCHOVER_LOG_HOT_PATH") == str(hot) and envval(d, "WATCHOVER_LOG_COLD_PATH") == str(cold)
    assert envval(d, "COMPOSE_FILE") == "docker-compose.yml:docker-compose.loghot.yml:docker-compose.logcold.yml"
    assert f"WATCHOVER_LOG_COLD_PATH={cold}" in (fake / "env-at-up").read_text()           # the stack was started on the new paths
    assert digest(hot) == before["hot"] and digest(cold) == before["cold"]                  # every file, byte for byte
    assert not digest(vol / "logs" / "hot") and not digest(vol / "logs" / "cold")          # old copies gone after the health check
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o440 for p in cold.glob("logs-*.wcl"))    # still sealed
    assert oct(cold.stat().st_mode)[-4:] == "2770" and (os.geteuid() != 0 or cold.stat().st_uid == 10001)
    cfg = json.loads((vol / "config.json").read_text())
    assert cfg["cold_retention_days"] == 400 and cfg["live_retention_h"] == 72
    from watchover import logaudit as la
    assert la.verify(cold)["ok"]
    calls = (fake / "calls.log").read_text()
    assert calls.index("--check") < calls.index("compose stop") < calls.index("--final") < calls.index("relocate-clean")
    r = run(box, "storage", "show")
    assert f"hot  {hot}  (set by hand)" in r.stdout and f"cold {cold}  (set by hand)" in r.stdout and "keeps 400 days" in r.stdout and "keeps 72 h" in r.stdout


def test_storage_set_refuses_bad_paths_and_a_foreign_directory(box, tmp_path):
    d, env, fake, vol = box
    seed(vol)
    for args, msg in ((["--hot", "relative/hot"], "must be an absolute directory"), (["--cold", "/etc"], "is a system directory"),
                      (["--cold", "/etc/wo"], "inside a system directory"), (["--cold", "/srv/a b"], "use only letters"),
                      (["--hot", "/srv/wo", "--cold", "/srv/wo/arsiv"], "must differ and not be inside each other"),
                      (["--cold", str(d / "logs")], "inside the install folder"), (["--cold-days", "9999"], "must be 0-3650"), ([], "usage")):
        r = run(box, "storage", "set", *args)
        assert r.returncode == 1 and msg in r.stderr, (args, r.stderr)
    home = tmp_path / "someone"
    home.mkdir()
    (home / "notes.txt").write_text("keep")
    r = run(box, "storage", "set", "--cold", str(home))
    assert r.returncode == 1 and "holds other files" in r.stderr and "nothing was changed" in r.stderr
    assert envval(d, "WATCHOVER_LOG_COLD_PATH") == "" and (home / "notes.txt").read_text() == "keep" and digest(vol / "logs" / "cold")


def test_a_new_path_that_does_not_come_up_puts_the_old_one_back(box, tmp_path):
    d, env, fake, vol = box
    before = seed(vol)
    cold = tmp_path / "bad-disk" / "arsiv"
    (fake / "broken").write_text(str(cold))                                                 # the dashboard never answers on it
    r = run(box, "storage", "set", "--cold", str(cold))
    assert r.returncode == 1 and "the previous paths are back" in r.stderr
    assert envval(d, "WATCHOVER_LOG_COLD_PATH") == "" and digest(vol / "logs" / "cold") == before["cold"]   # old place untouched
    assert digest(cold) == before["cold"]                                                   # the verified copy stays for a retry
