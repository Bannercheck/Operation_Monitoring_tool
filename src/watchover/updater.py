"""The in-app updater of the Docker / Podman install (System › Güncelleme): applies an uploaded release, rolls back to a kept one.

It runs in the `updater` compose service, the only container that holds the engine socket (Docker's /var/run/docker.sock or the
rootful Podman socket) and the install folder, bind-mounted at the same absolute path so `docker compose` resolves relative paths as
on the host. It has no network and no port: the dashboard writes requests into the spool volume (/updates) and reads the state the
updater writes there. Every engine step is ./watchover.sh (backup, build, unbundle, start, health, restore, images), so the logic is
the one the owner runs by hand; this file only orders the steps, keeps the releases and writes the state.

    python3 src/watchover/updater.py daemon     the service's command (re-reads its own code from the install folder after a job)
    python3 src/watchover/updater.py once       process the waiting requests once (tests, manual runs)
    python3 src/watchover/updater.py status     the release index and the last state as JSON

Install folder (it stays the running release, so ./watchover.sh works as before):
    .releases/index.json             {"current", "previous", "releases": {version: meta}}
    .releases/<ver>/tree/            source tree of that release (its images: watchover:<ver>, watchover-receiver:<ver>)
    .releases/<ver>/files.txt        its file list: what a switch removes when the next release no longer has a file
    .releases/<ver>/pre-update.tgz   the backup taken when this release was last left (./watchover.sh backup --no-logs)
    .updater/tmp, .updater/busy      work space of watchover.sh (paths the engine can mount), the marker of a running job

Apply:    received → validated → backup → build (bundle: load images) → switch → start → health → done
Rollback: received → validated → backup → build (skipped when the images are there) → switch → [restore_db] → start → health → done
Storage:  received → validated → prepare → copy → switch → start → health → cleanup → done: ./watchover.sh storage set with the raw-log
          paths and retention an admin gave on System › Storage (logaudit.py); its "::step NAME" lines move the steps
A failure after the switch brings the previous release back when auto-rollback is on (the default). Python 3.9, standard library."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

if __package__ in (None, ""):                                   # run as a script inside the updater container
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from watchover import updates as U                          # noqa: E402
else:
    from . import updates as U
try:                                                             # raw-log path checks (storage requests); a release without it refuses them
    if __package__ in (None, ""):
        from watchover import logaudit as LA                    # noqa: E402
    else:
        from . import logaudit as LA
except ImportError:                                              # pragma: no cover
    LA = None

DASH_UID = 10001                                                 # the dashboard container's user (writes inbox/ requests/ dash/)
LOCAL_TOP = {".env", ".git", ".releases", ".updater", "backups", "datasets", "certs", "data", ".venv", "venv", "node_modules",
             "update-pubkey.pem", ".edge-mdns.pid", ".DS_Store"}
LOCAL_PATHS = {".streamlit/secrets.toml"}
SKIP_ANY = {"__pycache__", "node_modules", ".pytest_cache", ".DS_Store", ".mypy_cache"}
BIG_SUFFIX = (".tar", ".tgz", ".tar.gz", ".zip", ".db", ".bak", ".log")
APPLY_STEPS = ("received", "validated", "backup", "build", "switch", "start", "health", "done")
ROLLBACK_STEPS = ("received", "validated", "backup", "build", "switch", "start", "health", "done")
STORAGE_STEPS = ("received", "validated", "prepare", "copy", "switch", "start", "health", "cleanup", "done")
KINDS = {"apply": APPLY_STEPS, "rollback": ROLLBACK_STEPS, "storage": STORAGE_STEPS}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_local(rel: str) -> bool:
    """Files of this installation, never part of a release: .env, backups, certificates, data, bundles and caches."""
    parts = rel.split("/")
    if parts[0] in LOCAL_TOP or rel in LOCAL_PATHS or rel.endswith(".pyc") or any(p in SKIP_ANY for p in parts):
        return True
    if len(parts) == 1 and (rel.endswith(BIG_SUFFIX) or (rel.startswith(".env") and rel not in (".env.example", ".env.docker.example"))):
        return True
    return False


class StepError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class Job:
    """The state of one request, written to <spool>/state.json after every change (the page polls it and survives restarts)."""

    def __init__(self, up: "Updater", req: dict, steps):
        self.up = up
        self.s = {"id": req["id"], "kind": req["kind"], "user": req.get("user", ""), "from": "", "to": req.get("version", ""),
                  "status": "running", "step": "", "steps": [{"name": n, "status": "pending"} for n in steps],
                  "started": now(), "ended": "", "auto_rollback": bool(req.get("auto_rollback", True)),
                  "restore_db": bool(req.get("restore_db", False)), "error": None, "result": None}
        self.save()

    def _step(self, name: str) -> dict:
        for st in self.s["steps"]:
            if st["name"] == name:
                return st
        st = {"name": name, "status": "pending"}
        done = [x for x in self.s["steps"] if x["name"] == "done"]
        self.s["steps"].insert(self.s["steps"].index(done[0]) if done else len(self.s["steps"]), st)
        return st

    def set(self, name: str, status: str, msg: str = "") -> None:
        st = self._step(name)
        if status == "running":
            st["at"] = now()
            self.s["step"] = name
        else:
            st["end"] = now()
        st["status"] = status
        if msg:
            st["msg"] = msg[:400]
        self.save()

    def fail_running(self, e: Exception) -> None:
        for st in self.s["steps"]:
            if st["status"] == "running":
                st.update(status="failed", end=now(), msg=str(getattr(e, "detail", "") or e)[:400])
        self.save()

    def finish(self, status: str, err: Optional[Exception] = None) -> None:
        self.s["status"], self.s["ended"] = status, now()
        if err is not None and self.s["error"] is None:
            self.s["error"] = {"code": getattr(err, "code", "internal"), "detail": str(getattr(err, "detail", "") or err)[:400]}
        if status in ("done", "rolled_back"):
            for st in self.s["steps"]:
                if st["status"] == "pending":
                    st["status"] = "skipped"
        if status == "done":
            self._step("done").update(status="ok", end=now())
        self.save()

    def save(self) -> None:
        U.write_json(self.up.spool / "state.json", self.s)


class Updater:
    def __init__(self, install: Path, spool: Path):
        self.install = Path(install).resolve()
        self.spool = Path(spool)
        self.rels = self.install / ".releases"
        self.work = self.install / ".updater"
        self.started = now()
        self.log_path: Optional[Path] = None
        self.job: Optional[Job] = None
        self._images: Dict[str, tuple] = {}
        st = self.install.stat()
        self.owner = (st.st_uid, st.st_gid)

    # ---------------------------------------------------------------- environment and settings
    def cfg(self) -> dict:
        env = U.dotenv(self.install / ".env")
        dash = U.read_json(self.spool / "dash" / "config.json", {}) or {}

        def num(key: str, envkey: str, default: int, lo: int, hi: int) -> int:
            try:
                v = int(dash.get(key) or env.get(envkey) or default)
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))
        return {"keep": num("keep", "WATCHOVER_UPDATE_KEEP", 5, 2, 50), "health_s": num("health_s", "WATCHOVER_UPDATE_HEALTH_S", 240, 5, 1800),
                "auto_rollback": dash.get("auto_rollback", True) is not False,
                "max_bytes": num("-", "WATCHOVER_UPDATE_MAX_MB", 8192, 16, 1 << 20) * 1048576}

    def pubkey(self) -> Optional[bytes]:
        try:
            return U.configured_pubkey(self.install, env={})
        except ValueError as e:                                  # a configured but unreadable key refuses everything (fail closed)
            raise StepError("pubkey_invalid", str(e))

    def own(self, path: Path) -> None:
        """Files the updater (root in its container) writes into the install folder belong to the folder's owner, so the owner's own
        ./watchover.sh keeps working on Linux; on Docker Desktop the file sharing maps them to the user anyway."""
        if os.geteuid() != 0 or self.owner[0] == 0:
            return
        try:
            paths = [path] + (list(path.rglob("*")) if path.is_dir() and not path.is_symlink() else [])
            for p in paths:
                os.lchown(p, *self.owner)
        except OSError:
            pass

    def wenv(self, **extra) -> dict:
        e = dict(os.environ)
        e.update(WATCHOVER_IN_UPDATER="1", TMPDIR=str(self.work / "tmp"))
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def sh(self, args: List[str], cwd: Optional[Path] = None, timeout: int = 7200, quiet: bool = False, on_line=None, **env) -> int:
        """./watchover.sh ARGS in the install folder (or a release tree), output appended to the job's log with secrets masked."""
        cwd = Path(cwd or self.install)
        (self.work / "tmp").mkdir(parents=True, exist_ok=True)
        log = None if quiet or self.log_path is None else open(self.log_path, "a", encoding="utf-8")
        if log:
            log.write(f"\n$ ./watchover.sh {' '.join(map(str, args))}\n")
            log.flush()
        p = subprocess.Popen(["bash", str(cwd / "watchover.sh"), *map(str, args)], cwd=str(cwd), env=self.wenv(**env), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)

        def kill():
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            for line in p.stdout:
                if log:
                    log.write(U.redact(line))
                    log.flush()
                if on_line is not None:
                    on_line(line)
            rc = p.wait()
        finally:
            timer.cancel()
            if log:
                log.write(f"[exit {p.returncode}]\n")
                log.close()
        return rc

    # ---------------------------------------------------------------- release index
    def index(self) -> dict:
        idx = U.read_json(self.rels / "index.json", None)
        if not isinstance(idx, dict) or not isinstance(idx.get("releases"), dict):
            idx = {"current": "", "previous": "", "releases": {}}
        return idx

    def save_index(self, idx: dict) -> None:
        self.rels.mkdir(parents=True, exist_ok=True)
        U.write_json(self.rels / "index.json", idx)
        self.own(self.rels)

    def folder_version(self) -> str:
        return U.read_version(self.install)

    def manifest(self, v: str) -> Set[str]:
        try:
            return {ln for ln in (self.rels / v / "files.txt").read_text(encoding="utf-8").splitlines() if ln}
        except OSError:
            return set()

    def set_current(self, new: str, prev: str) -> None:
        idx = self.index()
        idx["current"], idx["previous"] = new, prev
        if prev in idx["releases"]:
            idx["releases"][prev]["left_at"] = now()
        idx["releases"].setdefault(new, {"version": new, "installed_at": now(), "source": "manual"})["last_active"] = now()
        self.save_index(idx)

    def capture(self, source: str = "initial") -> str:
        """The folder's own release into .releases (first start after the upgrade, or after a manual ./watchover.sh update)."""
        v = self.folder_version()
        if not U.VERSION_RE.match(v or ""):
            return ""
        idx = self.index()
        d = self.rels / v
        if not ((d / "tree").is_dir() and (d / "files.txt").is_file()):
            stage = self.rels / f".capture-{v}"
            shutil.rmtree(stage, ignore_errors=True)
            files: List[str] = []
            for dirpath, dirnames, filenames in os.walk(self.install):
                rel_dir = os.path.relpath(dirpath, self.install)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                dirnames[:] = [n for n in dirnames if not is_local(f"{rel_dir}/{n}".lstrip("/")) and not os.path.islink(os.path.join(dirpath, n))]
                for n in filenames:
                    rel = f"{rel_dir}/{n}".lstrip("/")
                    src = Path(dirpath) / n
                    if is_local(rel) or src.is_symlink() or not src.is_file():
                        continue
                    dst = stage / "tree" / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    files.append(rel)
            (stage / "files.txt").write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")
            keep_backup = d / "pre-update.tgz"
            if keep_backup.exists():
                os.replace(keep_backup, stage / "pre-update.tgz")
            shutil.rmtree(d, ignore_errors=True)
            os.replace(stage, d)
            self.own(d)
            rev = ""
            try:
                rev = (d / "tree" / ".release-rev").read_text(encoding="utf-8").strip()[:12]
            except OSError:
                pass
            meta = idx["releases"].get(v, {})
            meta.update(version=v, source=meta.get("source") or source, git=meta.get("git") or rev)
            meta.setdefault("installed_at", now())
            idx["releases"][v] = meta
        if idx["current"] != v:
            if idx["current"]:
                idx["previous"] = idx["current"]
                if idx["current"] in idx["releases"]:
                    idx["releases"][idx["current"]]["left_at"] = now()
            idx["current"] = v
            idx["releases"][v]["last_active"] = now()
        self.save_index(idx)
        return v

    # ---------------------------------------------------------------- the steps
    def switch(self, frm: str, to: str) -> dict:
        """Make the install folder release `to`: files of `frm` that `to` no longer has are removed, changed files are replaced
        atomically (a running bash keeps reading the old watchover.sh), local files (.env, backups, certs, data) are never touched."""
        new, old = self.manifest(to), self.manifest(frm) if frm else set()
        if not new:
            raise StepError("switch_failed", f"release {to} has no file list")
        tree = self.rels / to / "tree"
        removed = changed = 0
        for rel in sorted(old - new, reverse=True):
            if is_local(rel):
                continue
            p = self.install / rel
            if p.is_symlink() or p.is_file():
                p.unlink()
                removed += 1
            q = p.parent
            while q != self.install and q.is_dir() and not any(q.iterdir()):
                q.rmdir()
                q = q.parent
        for rel in sorted(new):
            if is_local(rel):
                continue
            src, dst = tree / rel, self.install / rel
            if dst.is_file() and not dst.is_symlink() and _same(src, dst):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.parent / f".wo-new-{dst.name}"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            changed += 1
            self.own(dst)
        if self.folder_version() != to:
            raise StepError("switch_failed", f"the folder reports {self.folder_version()} after the switch, expected {to}")
        return {"changed": changed, "removed": removed}

    def backup(self, v: str, before: str) -> dict:
        d = self.rels / v
        d.mkdir(parents=True, exist_ok=True)
        part = d / "pre-update.tgz.part"
        rc = self.sh(["backup", "--no-logs", "--out", str(part)], timeout=6 * 3600)
        if rc != 0 or not part.is_file():
            if part.exists():
                part.unlink()
            raise StepError("backup_failed", f"./watchover.sh backup exit {rc}")
        os.replace(part, d / "pre-update.tgz")
        self.own(d / "pre-update.tgz")
        meta = {"at": now(), "before": before, "size": (d / "pre-update.tgz").stat().st_size}
        idx = self.index()
        idx["releases"].setdefault(v, {"version": v, "installed_at": now(), "source": "manual"})["backup"] = meta
        self.save_index(idx)
        return meta

    def images_ok(self, v: str, fresh: bool = False) -> bool:
        hit = self._images.get(v)
        if hit and not fresh and time.time() - hit[1] < 600:
            return hit[0]
        ok = self.sh(["images", v], quiet=True, timeout=120) == 0
        self._images[v] = (ok, time.time())
        return ok

    def build(self, v: str, bundle: Optional[Path]) -> str:
        tree = self.rels / v / "tree"
        env = {"WATCHOVER_ENV_FILE": str(self.install / ".env")}
        rc = self.sh(["unbundle", str(bundle)] if bundle else ["build"], cwd=tree, **env)
        if rc != 0:
            raise StepError("build_failed", f"./watchover.sh {'unbundle' if bundle else 'build'} exit {rc}")
        if not self.images_ok(v, fresh=True):
            raise StepError("images_missing", f"watchover:{v} / watchover-receiver:{v}")
        return "images loaded from the bundle" if bundle else f"watchover:{v} built"

    def start_and_check(self, v: str, health_s: int, job: Job) -> None:
        job.set("start", "running")
        rc = self.sh(["start"])
        if rc != 0:
            raise StepError("start_failed", f"./watchover.sh start exit {rc}")
        job.set("start", "ok")
        job.set("health", "running", f"/api/health → v{v} ≤ {health_s} s")
        if self.sh(["health", v, str(health_s)], timeout=health_s + 180) != 0:
            raise StepError("health_failed", f"v{v} did not answer /api/health within {health_s} s")
        job.set("health", "ok", f"v{v} answers /api/health")

    def revert(self, job: Job, to: str, frm: str, health_s: int, err: StepError, db_backup: Optional[Path] = None) -> None:
        """Auto-rollback: the release that ran before comes back (code, its kept images; the data only when the job had restored it)."""
        job.set("rollback", "running", f"{U.display(frm)} → {U.display(to)}")
        try:
            self.switch(frm, to)
            self.set_current(to, frm)
            if db_backup is not None:
                if self.sh(["restore", str(db_backup)], timeout=6 * 3600) != 0:
                    raise StepError("restore_failed", "./watchover.sh restore of the backup taken before the job")
            if self.sh(["start"]) != 0:
                raise StepError("start_failed", "./watchover.sh start")
            if self.sh(["health", to, str(health_s)], timeout=health_s + 180) != 0:
                raise StepError("health_failed", f"v{to} did not answer either")
            idx = self.index()
            if frm in idx["releases"]:
                idx["releases"][frm]["failed_at"] = now()
                self.save_index(idx)
            job.set("rollback", "ok", f"{U.display(to)} runs again")
            job.finish("rolled_back", err)
        except Exception as e2:  # noqa: BLE001
            e2 = _step_error(e2)
            job.set("rollback", "failed", e2.detail)
            job.s["rollback_error"] = {"code": e2.code, "detail": e2.detail}
            job.finish("rollback_failed", err)

    def apply(self, req: dict) -> None:
        job = self.job = Job(self, req, APPLY_STEPS)
        cfg = self.cfg()
        cur = self.capture() or self.folder_version()
        job.s["from"] = cur
        if not cur:
            raise StepError("no_current", "the install folder has no readable version (src/watchover/__init__.py)")
        job.set("received", "ok", req.get("name", ""))
        job.set("validated", "running")
        pdir = self.spool / "inbox" / req["id"]
        pkg = next((pdir / n for n in ("package.zip", "package.tar") if (pdir / n).is_file() and not (pdir / n).is_symlink()), None)
        if pkg is None or pdir.is_symlink():
            raise StepError("package_missing", f"inbox/{req['id']}")
        info = U.inspect(pkg, sig=req.get("sig", ""), pubkey=self.pubkey(), current=cur, max_bytes=cfg["max_bytes"])
        new = info["version"]
        job.s["to"], job.s["package"] = new, {k: info[k] for k in ("kind", "sha256", "size", "git") if k in info}
        if new == cur:
            raise StepError("same_version", f"v{new} is already running")
        if req.get("version") and req["version"] != new:
            raise StepError("version_mismatch", f"checked {req['version']}, the package says {new}")
        stage = self.rels / f".staging-{req['id']}"
        shutil.rmtree(stage, ignore_errors=True)
        files = U.extract(pkg, info, stage / "tree")
        (stage / "files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
        sig = info["signature"]["status"]
        job.set("validated", "ok", f"{info['kind']} {U.display(new)} · sha256 {info['sha256'][:12]} · {len(files)} files · signature {sig}"
                + (" · DOWNGRADE" if info.get("direction") == "downgrade" else ""))
        job.set("backup", "running")
        b = self.backup(cur, before=new)
        job.set("backup", "ok", f"{b['size'] // 1048576} MB")
        job.set("build", "running")
        target = self.rels / new
        created = not target.exists()
        if not created:
            if (target / "pre-update.tgz").exists():
                os.replace(target / "pre-update.tgz", stage / "pre-update.tgz")
            shutil.rmtree(target)
        os.replace(stage, target)
        self.own(target)
        idx = self.index()
        meta = idx["releases"].get(new, {})
        meta.update(version=new, installed_at=now(), source=info["kind"], sha256=info["sha256"], signed=sig == "ok",
                    git=info.get("git", ""), package=req.get("name", "")[:120])
        meta.pop("failed_at", None)
        idx["releases"][new] = meta
        self.save_index(idx)
        try:
            msg = self.build(new, pkg if info["kind"] == "bundle" else None)
        except StepError:
            if created:
                shutil.rmtree(target, ignore_errors=True)
                idx = self.index()
                idx["releases"].pop(new, None)
                self.save_index(idx)
            raise
        job.set("build", "ok", msg)
        try:
            job.set("switch", "running")
            r = self.switch(cur, new)
            self.set_current(new, cur)
            job.set("switch", "ok", f"{r['changed']} files changed, {r['removed']} removed; previous release kept")
            self.start_and_check(new, cfg["health_s"], job)
        except Exception as e:  # noqa: BLE001 - whatever broke after the switch, the previous release must come back
            e = _step_error(e)
            job.fail_running(e)
            if job.s["auto_rollback"]:
                self.revert(job, cur, new, cfg["health_s"], e)
            else:
                job.finish("failed", e)
            return
        job.finish("done")
        job.s["result"] = {"pruned": self.prune(cfg["keep"])}
        job.save()

    def rollback(self, req: dict) -> None:
        steps = list(ROLLBACK_STEPS)
        if req.get("restore_db"):
            steps.insert(steps.index("start"), "restore_db")
        job = self.job = Job(self, req, steps)
        cfg = self.cfg()
        cur = self.capture() or self.folder_version()
        to = req["version"]
        job.s["from"] = cur
        job.set("received", "ok")
        job.set("validated", "running")
        if to == cur:
            raise StepError("same_version", f"v{to} is already running")
        if not (self.rels / to / "tree").is_dir() or not self.manifest(to):
            raise StepError("unknown_release", f"v{to} is not kept")
        db_src = self.rels / to / "pre-update.tgz"
        if job.s["restore_db"]:
            if not db_src.is_file():
                raise StepError("no_backup", f"no backup was taken when v{to} was left")
        job.set("validated", "ok", f"{U.display(cur)} → {U.display(to)}" + (" · database too" if job.s["restore_db"] else ""))
        job.set("backup", "running")
        b = self.backup(cur, before=to)
        job.set("backup", "ok", f"{b['size'] // 1048576} MB")
        if self.images_ok(to, fresh=True):
            job.set("build", "skipped", "images present, nothing rebuilt")
        else:
            job.set("build", "running")
            job.set("build", "ok", self.build(to, None))
        restored = False
        try:
            job.set("switch", "running")
            r = self.switch(cur, to)
            self.set_current(to, cur)
            job.set("switch", "ok", f"{r['changed']} files changed, {r['removed']} removed")
            if job.s["restore_db"]:
                job.set("restore_db", "running")
                restored = True
                if self.sh(["restore", str(db_src)], timeout=6 * 3600) != 0:
                    raise StepError("restore_failed", "./watchover.sh restore")
                job.set("restore_db", "ok", f"backup of {(self.index()['releases'].get(to, {}).get('backup') or {}).get('at', '?')}")
            self.start_and_check(to, cfg["health_s"], job)
        except Exception as e:  # noqa: BLE001
            e = _step_error(e)
            job.fail_running(e)
            if job.s["auto_rollback"]:
                self.revert(job, cur, to, cfg["health_s"], e, db_backup=(self.rels / cur / "pre-update.tgz") if restored else None)
            else:
                job.finish("failed", e)
            return
        job.finish("done")
        job.s["result"] = {"pruned": self.prune(cfg["keep"])}
        job.save()

    # ---------------------------------------------------------------- retention
    def storage(self, req: dict) -> None:
        """Raw-log paths / retention (System › Storage): ./watchover.sh storage set does the work (prepare the directories, copy
        and verify while the stack runs, stop the log writers, final copy, switch .env, start, health, old copies removed; it puts
        the old paths back by itself when the new ones do not come up). Its '::step NAME' lines move this job's steps."""
        job = self.job = Job(self, req, STORAGE_STEPS)
        job.s["from"] = job.s["to"] = self.folder_version()
        job.set("received", "ok", ", ".join(f"{k} {req[k]}" for k in ("hot", "cold", "hot_hours", "cold_days") if req.get(k) not in (None, "")))
        job.set("validated", "running")
        for k in ("hot", "cold"):
            why = LA.check_path(req[k]) if req.get(k) else None
            if why:
                raise StepError("bad_path", f"{k}: {req[k]} ({why})")
        why = LA.check_pair(req.get("hot", ""), req.get("cold", ""))
        if why:
            raise StepError("bad_path", why)
        job.set("validated", "ok")
        args = ["storage", "set"]
        for k, flag in (("hot", "--hot"), ("cold", "--cold"), ("hot_hours", "--hot-hours"), ("cold_days", "--cold-days")):
            if req.get(k) not in (None, ""):
                args += [flag, str(req[k])]
        if req.get("keep_old"):
            args.append("--keep-old")
        cur = {"name": ""}
        last = {"line": ""}

        def on_line(line: str) -> None:
            t = U.redact(line).strip()
            if t.startswith("::step "):
                name = t.split()[1] if len(t.split()) > 1 else ""
                if name in STORAGE_STEPS:
                    if cur["name"]:
                        job.set(cur["name"], "ok")
                    cur["name"] = name
                    job.set(name, "running", t[len("::step ") + len(name):].strip())
            elif t:
                last["line"] = t
        rc = self.sh(args, on_line=on_line, timeout=48 * 3600)
        if rc != 0:
            raise StepError("storage_failed", f"./watchover.sh {' '.join(args)}: exit {rc}: {last['line'][-300:]}")
        if cur["name"]:
            job.set(cur["name"], "ok")
        job.finish("done")

    def prune(self, keep: int) -> List[str]:
        """Keep the `keep` most recently used releases (source tree + images); the current and the previous one always stay."""
        idx = self.index()
        recent = sorted(idx["releases"], key=lambda v: _recency(idx["releases"][v]), reverse=True)
        stay = set(recent[:max(2, keep)]) | {idx["current"], idx["previous"]}
        gone = []
        for v in recent:
            if v in stay:
                continue
            shutil.rmtree(self.rels / v, ignore_errors=True)
            self.sh(["images-rm", v], timeout=300)              # the engine refuses an image a container still uses
            idx["releases"].pop(v, None)
            self._images.pop(v, None)
            gone.append(v)
        for p in self.rels.glob(".staging-*") if self.rels.is_dir() else []:
            if time.time() - p.stat().st_mtime > 86400:
                shutil.rmtree(p, ignore_errors=True)
        if gone:
            self.save_index(idx)
        return gone

    # ---------------------------------------------------------------- what the dashboard reads
    def releases_view(self, images: bool = True) -> List[dict]:
        idx = self.index()
        out = []
        for v, m in idx["releases"].items():
            try:
                cl = (self.rels / v / "tree" / "CHANGELOG.md").read_text(encoding="utf-8")[:4 << 20]
            except OSError:
                cl = ""
            bk = m.get("backup") if (self.rels / v / "pre-update.tgz").is_file() else None
            out.append({"version": v, "display": U.display(v), "installed_at": m.get("installed_at", ""), "last_active": m.get("last_active", ""),
                        "left_at": m.get("left_at", ""), "source": m.get("source", ""), "sha256": m.get("sha256", ""), "signed": bool(m.get("signed")),
                        "git": m.get("git", ""), "package": m.get("package", ""), "failed_at": m.get("failed_at", ""),
                        "current": v == idx["current"], "previous": v == idx["previous"], "backup": bk,
                        "images": self.images_ok(v) if images else None, "notes": U.notes_for(cl, v)})
        return sorted(out, key=_recency, reverse=True)

    def publish(self, images: bool = True) -> None:
        idx = self.index()
        U.write_json(self.spool / "releases.json", {"at": now(), "current": idx["current"], "previous": idx["previous"],
                                                    "releases": self.releases_view(images)})

    def heartbeat(self, error: str = "") -> None:
        env = U.dotenv(self.install / ".env")
        try:
            key = self.pubkey()
            signing = {"required": bool(key), "fingerprint": U.fingerprint(key) if key else ""}
        except StepError as e:
            signing = {"required": True, "fingerprint": "", "error": e.detail}
        cfg = self.cfg()
        U.write_json(self.spool / "updater.json", {
            "pid": os.getpid(), "started": self.started, "heartbeat": now(), "ts": time.time(), "install_dir": str(self.install),
            "folder_version": self.folder_version(), "engine": env.get("WATCHOVER_ENGINE", "docker"), "offline": env.get("WATCHOVER_OFFLINE") == "1",
            "keep": cfg["keep"], "health_s": cfg["health_s"], "auto_rollback": cfg["auto_rollback"], "signing": signing,
            "busy": self.job.s["id"] if self.job is not None and self.job.s["status"] == "running" else "", "error": error})

    # ---------------------------------------------------------------- requests
    def prepare(self) -> None:
        """The spool: updater-owned outputs at the top, dashboard-writable inbox/ requests/ dash/ (UID 10001, group 0)."""
        self.spool.mkdir(parents=True, exist_ok=True)
        for name in ("inbox", "requests", "dash", "jobs"):
            p = self.spool / name
            if p.is_symlink():
                p.unlink()
            p.mkdir(exist_ok=True)
        if os.geteuid() == 0:
            os.chown(self.spool, 0, 0)
            os.chmod(self.spool, 0o755)
            for name in ("inbox", "requests", "dash"):
                os.chown(self.spool / name, DASH_UID, 0)
                os.chmod(self.spool / name, 0o2770)
            os.chown(self.spool / "jobs", 0, 0)
            os.chmod(self.spool / "jobs", 0o755)
        self.work.mkdir(parents=True, exist_ok=True)
        self.own(self.work)

    def clean(self, raw, stem: str) -> Optional[dict]:
        """A request as the dashboard wrote it, reduced to what the updater accepts (the dashboard is not trusted for more)."""
        if not isinstance(raw, dict):
            return None
        rid = str(raw.get("id", ""))
        if rid != stem or not U.ID_RE.match(rid) or raw.get("kind") not in KINDS:
            return None
        if raw["kind"] == "storage":                              # only what storage set takes: checked paths, numbers in range
            if LA is None:
                return None
            req = {"id": rid, "kind": "storage", "user": str(raw.get("user", ""))[:200], "keep_old": raw.get("keep_old") is True}
            for k in ("hot", "cold"):
                v = raw.get(k) or ""
                if v and (not isinstance(v, str) or LA.check_path(v)):
                    return None
                req[k] = v
            for k, lo, hi in (("hot_hours", 1, 8760), ("cold_days", 0, 3650)):
                v = raw.get(k)
                if v is None:
                    req[k] = None
                elif isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                    req[k] = v
                else:
                    return None
            if not (req["hot"] or req["cold"]) or LA.check_pair(req["hot"], req["cold"]):
                return None
            return req
        req = {"id": rid, "kind": raw["kind"], "user": str(raw.get("user", ""))[:200], "auto_rollback": raw.get("auto_rollback", True) is not False,
               "restore_db": raw.get("restore_db") is True, "sig": str(raw.get("sig", ""))[:512], "name": str(raw.get("name", ""))[:200],
               "version": str(raw.get("version", ""))[:40]}
        if raw["kind"] == "rollback" and not U.VERSION_RE.match(req["version"]):
            return None
        if req["version"] and not U.VERSION_RE.match(req["version"]):
            return None
        return req

    def next_request(self) -> Optional[dict]:
        rq = self.spool / "requests"
        try:
            files = sorted((p for p in rq.glob("*.json") if p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
        except OSError:
            return None
        for p in files:
            raw = U.read_json(p, None)
            try:
                p.unlink()
            except OSError:
                pass
            req = self.clean(raw, p.stem)
            if req:
                return req
        return None

    def handle(self, req: dict) -> dict:
        (self.spool / "jobs").mkdir(exist_ok=True)
        self.log_path = self.spool / "jobs" / f"{req['id']}.log"
        self.job = None
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "busy").write_text(json.dumps({"id": req["id"], "kind": req["kind"], "at": now()}) + "\n", encoding="utf-8")
        try:
            {"apply": self.apply, "rollback": self.rollback, "storage": self.storage}[req["kind"]](req)
        except (StepError, U.PackageError) as e:
            if self.job is None:
                self.job = Job(self, req, KINDS[req["kind"]])
            self.job.fail_running(e)
            self.job.finish("failed", e)
        except Exception as e:  # noqa: BLE001 - the state must say what happened, the daemon must live on
            if self.job is None:
                self.job = Job(self, req, KINDS.get(req["kind"], APPLY_STEPS))
            self.job.fail_running(e)
            self.job.finish("failed", StepError("internal", repr(e)[:300]))
        finally:
            for p in (self.work / "busy",):
                try:
                    p.unlink()
                except OSError:
                    pass
            shutil.rmtree(self.spool / "inbox" / req["id"], ignore_errors=True)
            shutil.rmtree(self.rels / f".staging-{req['id']}", ignore_errors=True)
            for p in (self.work / "tmp").glob("*") if (self.work / "tmp").is_dir() else []:
                shutil.rmtree(p, ignore_errors=True)
            self.own(self.work)
            s = self.job.s if self.job is not None else {"id": req["id"], "kind": req["kind"], "status": "failed"}
            with open(self.spool / "history.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({k: s.get(k) for k in ("id", "kind", "user", "from", "to", "status", "started", "ended", "error", "restore_db")},
                                   ensure_ascii=False) + "\n")
            self.log_path = None
        return s

    def recover(self) -> None:
        """A job that was running when the updater stopped (engine restart, host reboot) is marked interrupted."""
        st = U.read_json(self.spool / "state.json", None)
        if isinstance(st, dict) and st.get("status") == "running":
            st.update(status="failed", ended=now(), error={"code": "interrupted", "detail": f"the updater restarted during step '{st.get('step', '')}'"})
            U.write_json(self.spool / "state.json", st)
        try:
            (self.work / "busy").unlink()
        except OSError:
            pass
        for p in self.rels.glob(".staging-*") if self.rels.is_dir() else []:
            shutil.rmtree(p, ignore_errors=True)

    def once(self) -> List[dict]:
        self.prepare()
        out = []
        while True:
            req = self.next_request()
            if not req:
                break
            out.append(self.handle(req))
            self.publish()
        return out

    def daemon(self, interval: float = 2.0) -> None:
        import fcntl
        self.prepare()
        lock = open(self.spool / ".lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)                      # one updater per spool
        error = ""
        if not (self.install / "watchover.sh").is_file() or str(self.install) == "/install":
            error = f"the install folder is not mounted at its own path ({self.install}): start the updater with ./watchover.sh updater on"
        self.recover()
        if not error:
            self.capture()
            self.publish()
        print(f"watchover updater: install {self.install}, spool {self.spool}, release {self.folder_version() or '?'}" + (f", ERROR {error}" if error else ""), flush=True)
        last = time.time()
        while True:
            self.heartbeat(error)
            req = None if error else self.next_request()
            if req:
                print(f"{now()} {req['kind']} {req['id']} by {req['user'] or '?'}", flush=True)
                st = self.handle(req)
                print(f"{now()} {req['id']}: {st.get('status')} {(st.get('error') or {}).get('code', '')}", flush=True)
                self.publish()
                if _code_ok(self.install):                    # the next job runs the update logic of the release now installed
                    lock.close()
                    os.execv(sys.executable, [sys.executable, str(self.install / "src" / "watchover" / "updater.py"), "daemon"])
            elif not error and (self.folder_version() != self.index()["current"] or time.time() - last > 600):
                self.capture("manual")                         # a manual ./watchover.sh update since the last look
                self.publish()
                last = time.time()
            time.sleep(interval)


def _step_error(e: Exception) -> StepError:
    return e if isinstance(e, StepError) else StepError("switch_failed" if isinstance(e, OSError) else "internal", repr(e)[:300])


def _same(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size or (a.stat().st_mode & 0o111) != (b.stat().st_mode & 0o111):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(1 << 20), fb.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


def _recency(m: dict) -> str:
    return max(m.get("last_active", "") or "", m.get("left_at", "") or "", m.get("installed_at", "") or "")


def _code_ok(install: Path) -> bool:
    for name in ("updater.py", "updates.py", "logaudit.py"):
        p = install / "src" / "watchover" / name
        if name == "logaudit.py" and not p.exists():
            continue
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except (OSError, SyntaxError, ValueError):
            return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    install = Path(os.environ.get("WATCHOVER_INSTALL_DIR") or Path(__file__).resolve().parents[2])
    up = Updater(install, Path(os.environ.get("WATCHOVER_UPDATES_DIR") or "/updates"))
    if cmd == "daemon":
        up.daemon()
    elif cmd == "once":
        print(json.dumps(up.once(), ensure_ascii=False, indent=1))
    elif cmd == "status":
        print(json.dumps({"index": up.index(), "state": U.read_json(up.spool / "state.json", None)}, ensure_ascii=False, indent=1))
    else:
        print("usage: updater.py daemon | once | status", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
