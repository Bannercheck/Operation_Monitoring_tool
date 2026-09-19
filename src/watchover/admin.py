"""System administration helpers: version facts, in-app update (zip or git), restart through the launcher or the
service manager, database size / vacuum."""
from __future__ import annotations

import io
import os
import platform
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
STARTED = time.time()
KEEP = ("data", ".env", "knowledge.db", "actions.db", "playbook.db", ".venv", ".git")   # never overwritten by an update


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def version_info() -> dict:
    from . import stamp
    root = repo_root()
    git = ""
    try:
        git = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h %cs %s"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    import streamlit
    return {"engine": stamp.engine_id()[:12] if hasattr(stamp, "engine_id") else "-", "git": git or "-", "python": platform.python_version(),
            "streamlit": streamlit.__version__, "platform": f"{platform.system()} {platform.release()}", "root": str(root),
            "home": os.environ.get("WATCHOVER_HOME", "-"), "uptime_s": int(time.time() - STARTED), "launcher": os.environ.get("WATCHOVER_LAUNCHER", ""),
            "started": datetime.fromtimestamp(STARTED, tz=UTC).isoformat(timespec="seconds")}


def apply_zip(data: bytes, dest: Path | None = None) -> tuple[bool, str]:
    """Unpack a Watchover release zip over the code folder (data, .env, databases, venv and .git are kept)."""
    dest = dest or repo_root()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return False, "not a zip file"
    names = zf.namelist()
    app = next((n for n in names if n.endswith("app.py") and n.count("/") <= 1), None)
    if not app or not any(n.endswith("src/watchover/analysis.py") for n in names):
        return False, "no Watchover code in the archive"
    prefix = app[: -len("app.py")]
    n = 0
    for info in zf.infolist():
        if not info.filename.startswith(prefix) or info.is_dir():
            continue
        rel = info.filename[len(prefix):]
        if not rel or rel.split("/")[0] in KEEP or ".." in rel:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(info))
        n += 1
    return True, f"{n} files updated"


def git_update(src: Path | None = None, branch: str = "") -> tuple[bool, str]:
    src = src or repo_root()
    if not (src / ".git").exists():
        return False, "not a git checkout"
    try:
        cur = subprocess.run(["git", "-C", str(src), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        branch = branch or cur
        r = subprocess.run(["git", "-C", str(src), "pull", "--ff-only", "origin", branch], capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-400:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def restart_app(delay: float = 1.0) -> str:
    """Restart through the launcher when installed (service-aware), otherwise exit and let the service manager restart us."""
    launcher = os.environ.get("WATCHOVER_LAUNCHER", "")

    def go():
        time.sleep(delay)
        if launcher and Path(launcher).exists():
            subprocess.Popen([launcher, "restart"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        os._exit(3)
    threading.Thread(target=go, daemon=True).start()
    return "launcher" if launcher else "exit"


def db_info(kb) -> dict:
    url = getattr(kb, "url", "") or ""
    out = {"url": url or "-", "size_mb": None, "tables": {}}
    if not kb.pg:
        p = Path(url) if url else None
        if p and p.exists():
            out["size_mb"] = round(p.stat().st_size / 1e6, 2)
        try:
            for r in kb._exec("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                out["tables"][r["name"]] = int(kb._exec(f"SELECT COUNT(*) AS n FROM {r['name']}")[0]["n"])
        except Exception:  # noqa: BLE001
            pass
    return out


def vacuum(kb) -> str:
    if kb.pg:
        return "VACUUM is left to the PostgreSQL maintenance window"
    kb._exec("VACUUM")
    return "ok"


def tail_log(path: str, n: int = 80) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    with p.open("rb") as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 64_000))
        return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])
