"""System administration helpers: version facts, in-app update (zip or git), restart through the launcher or the
service manager, database size / vacuum."""
from __future__ import annotations

import io
import gzip
import json
import shutil
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


_git_cache: dict = {}


def version_info() -> dict:
    from . import stamp
    root = repo_root()
    git = _git_cache.get("line", "")
    if time.time() - _git_cache.get("at", 0) > 60:                 # one git subprocess per minute at most, not one per render
        try:
            git = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h %cs %s"], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:  # noqa: BLE001
            git = ""
        if not git:                                               # no git binary / no checkout (container): the revision baked at build time
            git = stamp.git_rev() if stamp.git_rev() != "-" else ""
        _git_cache.update({"line": git, "at": time.time()})
    import streamlit
    return {"engine": stamp.engine_id()[:12] if hasattr(stamp, "engine_id") else "-", "git": git or "-", "python": platform.python_version(),
            "streamlit": streamlit.__version__, "platform": f"{platform.system()} {platform.release()}", "root": str(root),
            "home": os.environ.get("WATCHOVER_HOME", "-"), "uptime_s": int(time.time() - STARTED), "launcher": os.environ.get("WATCHOVER_LAUNCHER", ""),
            "docker": in_docker(), "image_tag": os.environ.get("WATCHOVER_TAG", ""),
            "started": datetime.fromtimestamp(STARTED, tz=UTC).isoformat(timespec="seconds")}


def apply_zip(data: bytes, dest: Path | None = None) -> tuple[bool, str]:
    """Unpack a Watchover release zip over the code folder (data, .env, databases, venv and .git are kept)."""
    dest = dest or repo_root()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return False, "not a zip file"
    import stat
    from .updates import _zip_mode, safe_rel
    for info in zf.infolist():                                    # zip-slip, absolute paths and symlinks refuse the whole archive
        if safe_rel(info.filename) is None or stat.S_ISLNK(_zip_mode(info)):
            return False, f"unsafe entry in the archive: {info.filename[:80]}"
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
        r = subprocess.run(["git", "-C", str(src), "pull", "--ff-only", "origin", "--", branch], capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-400:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


CACHE_PROTECTED = (".venv", "node_modules", "data", "models", ".ollama", ".cache", "knowledge.db", "actions.db", "playbook.db")   # never touched by any cache cleanup


def clear_caches(root: Path | None = None) -> dict:
    """Remove what the previous version left behind: every __pycache__ under the code tree, .pytest_cache, Streamlit's on-disk
    cache and stale *.pyc files. Downloaded LLM models (Ollama's ~/.ollama, any models/ folder), the data folder, the databases
    and the virtual environment are never touched (CACHE_PROTECTED). Called after a deploy so the new code starts clean."""
    root = root or repo_root()
    dirs = files = 0

    def protected(p: Path) -> bool:
        return any(part in CACHE_PROTECTED for part in p.parts)

    for p in list(root.rglob("__pycache__")) + [root / ".pytest_cache"]:
        if not p.exists() or protected(p.relative_to(root)):
            continue
        shutil.rmtree(p, ignore_errors=True); dirs += 1
    for p in root.rglob("*.pyc"):
        if not protected(p.relative_to(root)):
            try:
                p.unlink(); files += 1
            except OSError:
                pass
    st_cache = Path.home() / ".streamlit" / "cache"
    if st_cache.exists():
        shutil.rmtree(st_cache, ignore_errors=True); dirs += 1
    return {"dirs": dirs, "files": files}


def deploy_finish(reason: str = "deploy") -> dict:
    """After an update or a rollback: purge caches, then hard-restart (launcher stop/start or process exit for the service manager)."""
    out = clear_caches()
    out["restart"] = restart_app(delay=2.0, hard=True)
    return out


def in_docker() -> bool:
    return os.environ.get("WATCHOVER_DOCKER") == "1" or Path("/.dockerenv").exists()


def restart_app(delay: float = 1.0, hard: bool = False) -> str:
    """Restart through the launcher when installed (service-aware), otherwise exit and let the service manager restart us
    (in Docker the container's restart policy brings it back)."""
    launcher = os.environ.get("WATCHOVER_LAUNCHER", "")

    def go():
        time.sleep(delay)
        if launcher and Path(launcher).exists():
            subprocess.Popen([launcher, "restart", "--hard"] if hard else [launcher, "restart"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        os._exit(3)
    threading.Thread(target=go, daemon=True).start()
    return "launcher" if launcher else ("docker" if in_docker() else "exit")


def db_info(kb) -> dict:
    """What the System page shows about the database: backend, connection without the password, size, rows per table."""
    out = {"url": kb.label, "backend": kb.backend, "size_mb": None, "tables": {}}
    try:
        size = kb.size_bytes()
        out["size_mb"] = round(size / 1e6, 2) if size else None
        for name in kb.tables():
            out["tables"][name] = kb.count(name)
    except Exception:  # noqa: BLE001
        pass
    return out


def vacuum(kb) -> str:
    kb._exec("VACUUM")             # PostgreSQL: a plain VACUUM (no lock) on the connection's database; SQLite: rewrites the file
    return "ok"


def tail_log(path: str, n: int = 80) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    with p.open("rb") as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 64_000))
        return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])


# ---------------------------------------------------------------- version history: snapshots before updates / maintenance, rollback
SNAP_EXCLUDE = {"data", ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", "demo", "samples",
                "act_keys", "watchover-keys", "watchover-secrets", "secrets"}      # I17: no key material in a snapshot archive
SNAP_EXCLUDE_SUFFIX = (".db", ".pyc", ".key", ".pem", ".wokx")
DB_FILES = ("knowledge.db", "actions.db", "playbook.db")


def versions_dir() -> Path:
    from . import settings
    p = settings.home() / "versions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sqlite_copy(src: Path, dst: Path) -> None:
    """A consistent copy of a live SQLite file, including what still sits in its write-ahead log (db.py runs SQLite in WAL mode)."""
    import sqlite3
    try:
        a = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        b = sqlite3.connect(str(dst))
        try:
            a.backup(b)
        finally:
            b.close(); a.close()
    except sqlite3.Error:
        shutil.copy2(src, dst)


def _db_paths() -> list[Path]:
    out = []
    for env, name in (("KNOWLEDGE_DB", "knowledge.db"), ("ACTIONS_DB", "actions.db"), ("PLAYBOOK_DB", "playbook.db")):
        p = Path(os.environ.get(env) or name)
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.exists() and p.suffix == ".db":
            out.append(p)
    return out


def _carry_append_only(current: Path, restored: Path) -> int:
    """Copy the append-only act_* rows of the current SQLite file that are newer than the restored file's into it."""
    import sqlite3
    if not current.exists():
        return 0
    from .act.store import APPEND_ONLY, SEQ_KEY
    n = 0
    src = sqlite3.connect(str(current))
    dst = sqlite3.connect(str(restored))
    try:
        have = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cur_have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in APPEND_ONLY:
            if t not in have or t not in cur_have:
                continue
            key = SEQ_KEY[t]
            top = dst.execute(f"SELECT COALESCE(MAX({key}), 0) FROM {t}").fetchone()[0]
            cols = [r[1] for r in dst.execute(f"PRAGMA table_info({t})")]
            scols = {r[1] for r in src.execute(f"PRAGMA table_info({t})")}
            cols = [c for c in cols if c in scols]
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {t} WHERE {key} > ? ORDER BY {key}", (top,)).fetchall()
            dst.executemany(f"INSERT OR IGNORE INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
            n += len(rows) if t == "act_audit" else 0
        if "act_audit_head" in have and "act_audit" in have:
            last = dst.execute("SELECT seq, mac FROM act_audit ORDER BY seq DESC LIMIT 1").fetchone()
            if last:
                dst.execute("UPDATE act_audit_head SET seq=?, mac=? WHERE id=1", last)
        dst.commit()
    finally:
        src.close()
        dst.close()
    return n


def snapshot(reason: str, code: bool = True, db: bool = True, root: Path | None = None, kb=None) -> dict:
    """Freeze the running version: code.zip (the code tree without data / .git / venv) plus the database, with meta.json.
    On PostgreSQL the database goes into db.json.gz (every table, JSON); on SQLite the files are copied.
    Called before a zip / git update and before database maintenance; the System page also offers it by hand."""
    from . import stamp
    root = root or repo_root()
    base = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    sid, n = base, 1
    while (versions_dir() / sid).exists():                # two snapshots within a second (rollback snapshots first) keep distinct ids
        n += 1; sid = f"{base}-{n}"
    d = versions_dir() / sid
    d.mkdir(parents=True, exist_ok=True)
    files = 0
    if code:
        with zipfile.ZipFile(d / "code.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(root.rglob("*")):
                rel = p.relative_to(root)
                if p.is_dir() or any(part in SNAP_EXCLUDE for part in rel.parts) or rel.name.endswith(SNAP_EXCLUDE_SUFFIX) or rel.name in (".env",):
                    continue
                zf.write(p, f"watchover/{rel.as_posix()}")
                files += 1
    dbs = []
    if db and kb is not None and kb.pg:
        with gzip.open(d / "db.json.gz", "wt", encoding="utf-8") as f:
            json.dump(kb.dump(), f, ensure_ascii=False)
        dbs.append("postgresql")
    elif db:
        for p in _db_paths():
            _sqlite_copy(p, d / p.name)
            dbs.append(p.name)
    st_ = stamp.stamp()
    meta = {"id": sid, "ts": datetime.now(UTC).isoformat(timespec="seconds"), "reason": reason, "version": st_["version"], "git": st_["git"], "engine": st_["engine"],
            "files": files, "dbs": dbs, "size": sum(f.stat().st_size for f in d.iterdir()), "db_url": os.environ.get("DATABASE_URL", "")[:12]}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def versions() -> list[dict]:
    out = []
    for d in sorted(versions_dir().iterdir(), reverse=True):
        m = d / "meta.json"
        if m.exists():
            try:
                out.append(json.loads(m.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return out


def rollback(sid: str, code: bool = True, db: bool = True, root: Path | None = None, kb=None) -> tuple[bool, str]:
    """Bring a snapshot back: the current state is snapshotted first (reason 'before rollback'), then code.zip is unpacked over the code
    folder and the database is restored (tables refilled on PostgreSQL, files copied back on SQLite). A restart follows."""
    d = versions_dir() / sid
    if not (d / "meta.json").exists():
        return False, "unknown version"
    snapshot(f"before rollback to {sid}", root=root, kb=kb)
    msgs = []
    if code and (d / "code.zip").exists():
        ok, msg = apply_zip((d / "code.zip").read_bytes(), dest=root)
        if not ok:
            return False, msg
        msgs.append(msg)
    if db and (d / "db.json.gz").exists():
        if kb is None or not kb.pg:
            return False, "this snapshot holds a PostgreSQL dump; the app is not connected to PostgreSQL"
        with gzip.open(d / "db.json.gz", "rt", encoding="utf-8") as f:
            done = kb.restore(json.load(f))
        msgs.append(f"postgresql: {sum(done.values())} rows in {len(done)} tables restored")
    elif db:
        targets = {p.name: p for p in _db_paths()}
        for f in d.glob("*.db"):
            target = targets.get(f.name) or (Path.cwd() / f.name)
            tmp = target.with_name(target.name + ".restore")
            shutil.copy2(f, tmp)
            carried = _carry_append_only(target, tmp)     # the audit chain is append-only: newer rows survive the rollback (C11)
            if carried:
                msgs.append(f"{f.name}: {carried} audit rows kept")
            os.replace(tmp, target)                      # a new file: the running connection keeps the old one until the restart
            for side in ("-wal", "-shm"):                # the old file's write-ahead log must never be replayed onto the restored one
                try:
                    target.with_name(target.name + side).unlink()
                except FileNotFoundError:
                    pass
            msgs.append(f"{f.name} restored")
    return True, "; ".join(msgs) or "nothing to restore"


def delete_version(sid: str) -> None:
    d = versions_dir() / sid
    if (d / "meta.json").exists():
        shutil.rmtree(d, ignore_errors=True)


def prune_versions(keep: int = 10) -> int:
    old = versions()[keep:]
    for m in old:
        delete_version(m["id"])
    return len(old)
