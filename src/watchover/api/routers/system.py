from __future__ import annotations

import os
import re
import shutil
import time

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from typing import Optional

from fastapi import Request

from ... import __version__
from ... import admin as wo_admin
from ... import logaudit
from ... import release as wo_release
from ... import settings as wo_settings
from ... import updates as wo_updates
from ..security import require, require_install, services
from .act_common import err, stepup

SETTING_KEYS = ("self_monitor", "learn_min", "alerts_on", "live_port", "live_key", "public_host", "lang", "workspace", "demo_on_start", "thr_cpu", "thr_memory", "thr_disk", "thr_gpu",
                "live_retention_h", "live_window_max_events", "live_ring", "live_spool_mb", "history_retention_days", "cold_retention_days", "cold_max_gb", "hot_max_gb",
                "trace_sample_pct", "trace_slow_ms", "trace_apdex_ms", "trace_retention_h", "trace_rollup_days", "rum_retention_days", "anomaly_learning_days", "anomaly_learning_mode", "anomaly_learning_min_days", "teams_strict", "teams_self_claim")
CHANNEL_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password", "smtp_from", "smtp_from_name",
                "sms_preset", "sms_url", "sms_method", "sms_auth", "sms_user", "sms_password", "sms_token", "sms_from", "sms_account", "sms_body", "sms_content_type", "webhook_url", "webhook_kind", "webhook_secret")
AUTH_KEYS = ("auth_google", "google_client_id", "google_client_secret", "auth_microsoft", "ms_tenant", "ms_client_id", "ms_client_secret",
             "auth_oidc", "oidc_issuer", "oidc_client_id", "oidc_client_secret",
             "auth_self_register", "auth_domains", "public_host", "mfa_email")
MASK = "•••"

router = APIRouter(prefix="/system", tags=["system"])


class SnapshotIn(BaseModel):
    reason: str = "api"
    code: bool = True
    db: bool = True


@router.get("/status")
def status(user=Depends(require("sys.status")), svc=Depends(services)):
    return svc.status()


@router.get("/storage")
def storage(user=Depends(require("sys.status")), svc=Depends(services)):
    """Log tiers (logtiers.py): hot day files and cold archive sizes, days, caps, bytes per line; the main database size; where the
    tiers live (set by hand or not), the alerts, the yearly estimate, the manifest check and the running audit job."""
    rep = svc.log_tiers.report(svc.kb, svc.live)
    m = wo_updates.mode()
    pinned = wo_settings.pinned()
    rep["setup"] = {"mode": m, "paths": logaudit.storage_paths(m), "pinned": sorted(k for k in ("live_retention_h", "cold_retention_days") if k in pinned),
                    "updater_online": bool(m == "updater" and _updater(_spool()).get("online"))}
    job = svc.audit_jobs.running()
    rep["job"] = job
    return rep


# ---------------------------------------------------------------- raw-log storage: paths by hand, a year's archive, audits (logaudit.py)
# Docker / Podman: the paths are host directories in .env; the dashboard never touches them. A change goes to the updater as a request
# (kind "storage"): it runs ./watchover.sh storage set (prepare, copy + verify, stop, final copy, switch, start, health, old copies
# removed). Without the updater the page shows that command. OpenShift: PVCs, set with Helm values; nothing is changed from the pod.
PATH_MSG = {"not_absolute": ("Mutlak bir dizin yazın (/srv/watchover/arsiv gibi)", "Give an absolute directory (such as /srv/watchover/archive)"),
            "bad_chars": ("Dizinde yalnız harf, rakam ve . _ - / kullanın", "Use only letters, digits and . _ - / in the directory"),
            "system_dir": ("Bu bir sistem dizini; kendine ait bir alt dizin verin (/srv/watchover/arsiv gibi)", "That is a system directory; give a directory of its own (such as /srv/watchover/archive)"),
            "same_dir": ("Sıcak ve arşiv dizini ayrı olmalı", "The hot and the archive directory must differ"),
            "nested": ("Sıcak ve arşiv dizini iç içe olamaz", "The hot and the archive directory cannot be inside each other"),
            "empty": ("Dizin boş", "No directory given")}


class StorageConfigIn(BaseModel):
    hot_path: str = ""
    cold_path: str = ""
    hot_hours: Optional[int] = None
    cold_days: Optional[int] = None
    keep_old: bool = False


def _stg_audit(svc, user, detail: str, ok: bool = True) -> None:
    try:
        svc.users._event(user.get("email", "") if isinstance(user, dict) else str(user), "sys.storage", bool(ok), detail[:200])
    except Exception:  # noqa: BLE001
        pass


def _storage_cmd(hot: str, cold: str, hh, cd, keep_old: bool) -> str:
    parts = ["./watchover.sh storage set"]
    if hot:
        parts.append(f"--hot {hot}")
    if cold:
        parts.append(f"--cold {cold}")
    if hh is not None:
        parts.append(f"--hot-hours {int(hh)}")
    if cd is not None:
        parts.append(f"--cold-days {int(cd)}")
    if keep_old:
        parts.append("--keep-old")
    return " ".join(parts)


def _helm_storage(hh, cd) -> list:
    sets = [f"--set retention.liveRetentionH={int(hh)}" if hh is not None else "", f"--set retention.coldRetentionDays={int(cd)}" if cd is not None else "",
            "--set persistence.logsCold.size=<BOYUT>Gi"]
    return ["helm upgrade watchover deploy/openshift/watchover -n watchover --reuse-values " + " ".join(x for x in sets if x),
            "oc get pvc watchover-logs-cold -n watchover   # büyütme: StorageClass allowVolumeExpansion: true olmalı",
            "oc exec deploy/watchover -n watchover -- python -m watchover.logtiers verify"]


@router.put("/storage/config")
def storage_config(body: StorageConfigIn, user=Depends(require_install("sys.update")), svc=Depends(services)):
    """The raw-log places and retention, given by hand. Paths: Docker through the updater (request kind "storage") or the command;
    OpenShift: the Helm values. Hours / days alone: saved at once (the tier job reads them every hour). sys.update + a fresh step-up."""
    stepup(svc, user)
    m = wo_updates.mode()
    paths = logaudit.storage_paths(m)
    hot, cold = (body.hot_path or "").strip(), (body.cold_path or "").strip()
    for v in (hot, cold):
        why = logaudit.check_path(v) if v else None
        if why:
            raise err(422, f"stg_path_{why}", *PATH_MSG.get(why, (why, why)), path=v)
    why = logaudit.check_pair(hot or paths["hot"]["host_path"], cold or paths["cold"]["host_path"])
    if why:
        raise err(422, f"stg_path_{why}", *PATH_MSG[why])
    if body.hot_hours is not None and not 1 <= body.hot_hours <= 8760:
        raise err(422, "stg_bad_hours", "Sıcak süre 1-8760 saat olmalı", "Hot hours must be 1-8760")
    if body.cold_days is not None and not 0 <= body.cold_days <= 3650:
        raise err(422, "stg_bad_days", "Arşiv süresi 0-3650 gün olmalı", "Archive days must be 0-3650")
    move = {k: v for k, v in (("hot", hot), ("cold", cold)) if v and v.rstrip("/") != (paths[k]["host_path"] or "").rstrip("/")}
    if m == "openshift":
        return {"applied": False, "mode": m, "helm": _helm_storage(body.hot_hours, body.cold_days)}
    nums = {k: v for k, v in (("live_retention_h", body.hot_hours), ("cold_retention_days", body.cold_days)) if v is not None}
    if not move:
        pinned = [k for k in nums if k in wo_settings.pinned()]
        if pinned:
            raise err(409, "stg_pinned", "Bu değerler kurulumda sabitlenmiş (WATCHOVER_SETTINGS)", "These values are pinned by the deployment (WATCHOVER_SETTINGS)", keys=pinned)
        if nums:
            before = wo_settings.load()
            cfg = wo_settings.save(nums)
            svc.apply_capacity_settings(cfg)
            _stg_audit(svc, user, "storage retention " + ", ".join(f"{k} {before.get(k)} -> {v}" for k, v in nums.items()))
        return {"applied": True, "mode": m, "saved": nums}
    cmd = _storage_cmd(move.get("hot", ""), move.get("cold", ""), body.hot_hours, body.cold_days, body.keep_old)
    if m == "updater":
        sp = _spool()
        _idle(sp)
        if not _updater(sp).get("online"):
            raise err(409, "upd_offline", "Güncelleyici çalışmıyor: sunucuda ./watchover.sh updater on (ya da komutu sunucuda çalıştırın)",
                      "The updater is not running: ./watchover.sh updater on on the server (or run the command there)", command=cmd)
        rid = wo_updates.new_id()
        wo_updates.write_json(sp / "requests" / f"{rid}.json", {"id": rid, "kind": "storage", "hot": move.get("hot", ""), "cold": move.get("cold", ""),
                                                               "hot_hours": body.hot_hours, "cold_days": body.cold_days, "keep_old": body.keep_old,
                                                               "user": user.get("email", ""), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, mode=0o640)
        _stg_audit(svc, user, f"storage move requested: {cmd[len('./watchover.sh '):]}")
        return {"applied": False, "mode": m, "queued": rid, "command": cmd}
    if m == "manual":
        return {"applied": False, "mode": m, "command": cmd}
    return {"applied": False, "mode": m, "commands": [
        *(f"python -m watchover.logtiers relocate {paths[k]['dir']} {v} && python -m watchover.logtiers relocate {paths[k]['dir']} {v} --final" for k, v in move.items()),
        *(f"WATCHOVER_LOG_{k.upper()}_DIR={v}" for k, v in move.items()), "# uygulamayı yeniden başlatın; sonra: python -m watchover.logtiers relocate-clean ESKİ YENİ"]}


class AuditJobIn(BaseModel):
    kind: str = "verify"            # verify | export | search
    start: str = ""                 # YYYY-MM-DD (inclusive)
    end: str = ""                   # YYYY-MM-DD (inclusive)
    format: str = "archive"         # export: archive | ndjson
    query: str = ""                 # search: the log explorer's query language


_TICKETS: dict = {}


@router.post("/storage/jobs", status_code=202)
def storage_job(body: AuditJobIn, user=Depends(require_install("sys.maint")), svc=Depends(services)):
    """A background audit job: verify (every archive re-read against the manifest), export (archives + manifest for auditors, or
    the stored lines as NDJSON) or search (a log query over months, hot and archive, no time budget). One at a time; exports and
    searches need a fresh step-up."""
    if svc.log_cold is None:
        raise err(409, "stg_no_archive", "Bu kurulumda arşiv yok (WATCHOVER_LOG_STORE=db)", "No archive in this installation (WATCHOVER_LOG_STORE=db)")
    if body.kind not in ("verify", "export", "search") or body.format not in ("archive", "ndjson"):
        raise err(422, "stg_bad_job", "Bilinmeyen iş", "Unknown job")
    try:
        frm, to = logaudit.day_arg(body.start), logaudit.day_arg(body.end)
    except ValueError:
        raise err(422, "stg_bad_date", "Tarih YYYY-AA-GG olmalı", "Dates are YYYY-MM-DD")
    if frm and to and frm > to:
        raise err(422, "stg_bad_range", "Başlangıç bitişten sonra", "The start is after the end")
    if body.kind != "verify":
        stepup(svc, user)
    email = user.get("email", "")
    if body.kind == "verify":
        def fn(prog, cancel, out):
            return svc.log_tiers.verify_deep(frm, to, prog, cancel, email)
    elif body.kind == "export" and body.format == "archive":
        def fn(prog, cancel, out):
            return logaudit.export_archive(svc.log_cold.dir, frm, to, out, prog, cancel)
    elif body.kind == "export":
        def fn(prog, cancel, out):
            return logaudit.export_ndjson(svc.log_cold, frm, to, out, svc.log_hot, prog, cancel)
    else:
        if not frm or not to:
            raise err(422, "stg_range_needed", "Arama için başlangıç ve bitiş tarihi gerekli", "A search needs a start and an end date")
        try:
            svc.logs.query(body.query, None)
        except Exception as e:  # noqa: BLE001 - the query language says what is wrong
            raise err(400, "stg_bad_query", f"Sorgu çalıştırılamadı: {e}", f"The query does not run: {e}")
        s_iso = f"{frm[:4]}-{frm[4:6]}-{frm[6:]}T00:00:00+00:00"
        from datetime import datetime as _dt, timedelta as _td
        e_iso = (_dt.fromisoformat(f"{to[:4]}-{to[4:6]}-{to[6:]}T00:00:00+00:00") + _td(days=1)).isoformat()

        def fn(prog, cancel, out):
            return logaudit.ndjson_file(out, f"watchover-search-{frm}-{to}", lambda w: svc.logs.export_matches(body.query, s_iso, e_iso, None, w, prog, cancel))
    try:
        job = svc.audit_jobs.start(body.kind, {"start": frm, "end": to, "format": body.format, "query": body.query[:500]}, email, fn)
    except logaudit.Busy:
        raise err(409, "stg_job_busy", "Bir denetim işi zaten sürüyor", "An audit job is already running")
    _stg_audit(svc, user, f"audit {body.kind} {frm or '-'}..{to or '-'}" + (f" {body.format}" if body.kind == "export" else "") + (f" q={body.query[:80]}" if body.query else ""))
    return job


@router.post("/storage/stepup")
def storage_stepup(user=Depends(require_install("sys.maint")), svc=Depends(services)):
    """428 act_stepup_required until the account has verified itself again (the page asks before an export or a search)."""
    return {"ok": True, **stepup(svc, user)}


@router.get("/storage/jobs")
def storage_jobs(user=Depends(require_install("sys.maint")), svc=Depends(services)):
    return svc.audit_jobs.list()


@router.get("/storage/jobs/{jid}")
def storage_job_state(jid: str, user=Depends(require_install("sys.maint")), svc=Depends(services)):
    j = svc.audit_jobs.get(jid)
    if j is None:
        raise HTTPException(404, "not found")
    return j


@router.post("/storage/jobs/{jid}/cancel")
def storage_job_cancel(jid: str, user=Depends(require_install("sys.maint")), svc=Depends(services)):
    return {"ok": svc.audit_jobs.cancel(jid)}


@router.post("/storage/exports/{name}/ticket")
def storage_export_ticket(name: str, user=Depends(require_install("sys.maint")), svc=Depends(services)):
    """A one-file download link valid for 10 minutes (an export can be gigabytes: the browser saves it straight to disk)."""
    if svc.audit_jobs.path(name) is None:
        raise HTTPException(404, "not found")
    now = time.time()
    for k in [k for k, v in _TICKETS.items() if v[1] < now]:
        _TICKETS.pop(k, None)
    import secrets
    tk = secrets.token_urlsafe(24)
    _TICKETS[tk] = (name, now + 600, user.get("email", ""))
    _stg_audit(svc, user, f"audit export download {name}")
    return {"url": f"/api/system/storage/exports/{name}?ticket={tk}", "expires_s": 600}


@router.get("/storage/exports/{name}")
def storage_export_file(name: str, ticket: str = "", svc=Depends(services)):
    t = _TICKETS.get(ticket or "")
    if not t or t[0] != name or t[1] < time.time():
        raise HTTPException(403, "a download ticket is required (POST …/ticket)")
    p = svc.audit_jobs.path(name)
    if p is None:
        raise HTTPException(404, "not found")
    sha = ""
    try:
        sha = (p.parent / (p.name + ".sha256")).read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        pass
    return FileResponse(str(p), filename=name, media_type="application/octet-stream", headers={"X-Content-SHA256": sha} if sha else None)


_coverage_cache: dict = {}


@router.get("/coverage")
def parser_coverage(refresh: bool = False, user=Depends(require("sys.status"))):
    """Parser coverage over samples/corpus (one file per log family) with the accepted baseline; cached per process."""
    from ... import coverage as wo_cov
    if refresh or "res" not in _coverage_cache:
        res = wo_cov.run()
        base = wo_cov.load_baseline()
        for r in res["files"]:
            b = base.get(r["file"], {})
            r["baseline"] = b.get("score")
            r["delta"] = round(r["score"] - b["score"], 1) if b.get("score") is not None else None
        res["regressions"] = wo_cov.regressions(res, base)
        _coverage_cache["res"] = res
    return _coverage_cache["res"]


@router.get("/releases")
def releases(user=Depends(require("sys.status"))):
    return wo_release.entries() if hasattr(wo_release, "entries") else []


@router.get("/log", response_class=PlainTextResponse)
def log(n: int = 200, user=Depends(require("sys.status"))):
    return wo_admin.tail_log(os.environ.get("WATCHOVER_LOG", ""), n)


@router.post("/vacuum")
def vacuum(user=Depends(require("sys.maint")), svc=Depends(services)):
    wo_admin.snapshot("api maintenance", code=False, kb=svc.kb)
    return {"result": wo_admin.vacuum(svc.kb)}


@router.get("/snapshots")
def snapshots(user=Depends(require("sys.update"))):
    return wo_admin.versions()


@router.post("/snapshots", status_code=201)
def snapshot(body: SnapshotIn, user=Depends(require("sys.update")), svc=Depends(services)):
    return wo_admin.snapshot(body.reason, body.code, body.db, kb=svc.kb)


@router.delete("/snapshots/{sid}")
def delete_snapshot(sid: str, user=Depends(require("sys.update"))):
    wo_admin.delete_version(sid); return {"ok": True}


@router.get("/auth")
def auth_settings(request: Request, user=Depends(require("sys.auth"))):
    """Sign-in provider settings; secrets come back masked and stay untouched when the mask is sent back."""
    from .auth import PROVIDERS, _redirect_uri
    cfg = wo_settings.load()
    out = {k: (MASK if k in wo_settings.SECRET_KEYS and cfg.get(k) else cfg.get(k, "")) for k in AUTH_KEYS}
    out["callbacks"] = {n: _redirect_uri(request, n) for n in PROVIDERS}
    return out


@router.put("/auth")
def save_auth_settings(body: dict, user=Depends(require("sys.auth"))):
    cur = wo_settings.load()
    vals = {}
    for k in AUTH_KEYS:
        if k not in body:
            continue
        v = body[k]
        if k in wo_settings.SECRET_KEYS and v == MASK:
            continue
        vals[k] = bool(v) if k.startswith("auth_") and k != "auth_domains" or k == "mfa_email" else str(v or "").strip()
    wo_settings.save(vals)
    return {"ok": True, "changed": sorted(vals)}


def _masked(keys: tuple) -> dict:
    cfg = wo_settings.load()
    return {k: (MASK if k in wo_settings.SECRET_KEYS and cfg.get(k) else cfg.get(k, "")) for k in keys}


def _save(keys: tuple, body: dict) -> list:
    vals = {}
    for k in keys:
        if k in body and not (k in wo_settings.SECRET_KEYS and body[k] == MASK):
            vals[k] = body[k]
    wo_settings.save(vals)
    return sorted(vals)


@router.get("/settings")
def get_settings(user=Depends(require("sys.status"))):
    return _masked(SETTING_KEYS)


@router.put("/settings")
def put_settings(body: dict, user=Depends(require("sys.maint")), svc=Depends(services)):
    changed = _save(SETTING_KEYS, body)
    if "alerts_on" in body:
        (svc.alerts.start() if body["alerts_on"] else svc.alerts.stop.set())
    if "self_monitor" in body and svc.selfmon is not None:
        (svc.selfmon.start() if body["self_monitor"] else svc.selfmon.stop())
    if any(k in body for k in ("live_retention_h", "live_window_max_events", "live_spool_mb", "history_retention_days")):
        svc.apply_capacity_settings()                       # retention takes effect without a restart
    return {"ok": True, "changed": changed}


@router.get("/channels")
def get_channels(user=Depends(require_install("sys.notify"))):
    return _masked(CHANNEL_KEYS)


@router.put("/channels")
def put_channels(body: dict, user=Depends(require_install("sys.notify"))):
    return {"ok": True, "changed": _save(CHANNEL_KEYS, body)}


@router.get("/update")
def update_info(user=Depends(require("sys.update"))):
    """How this installation updates: in Docker the commands, elsewhere the launcher; plus the release notes."""
    return {"docker": wo_admin.in_docker(), "commands": ["./watchover.sh update", "./watchover.sh backup", "./watchover.sh restore backups/<file>.tgz"] if wo_admin.in_docker() else ["scripts/watchover-launcher.sh update"],
            "releases": wo_release.entries()[:10]}


@router.post("/snapshots/{sid}/rollback")
def rollback(sid: str, code: bool = True, db: bool = True, user=Depends(require("sys.update")), svc=Depends(services)):
    """Bring a snapshot back and restart (Docker's restart policy or the launcher brings the process up again). Needs a fresh step-up."""
    stepup(svc, user)
    ok, msg = wo_admin.rollback(sid, code, db, kb=svc.kb)
    _audit(svc, user, f"snapshot rollback {sid} (code={code}, db={db}): {msg}", ok)
    if not ok:
        raise ValueError(msg)
    out = wo_admin.deploy_finish("rollback")
    return {"ok": True, "message": msg, "restart": out.get("restart")}


@router.post("/restart")
def restart(user=Depends(require("sys.update"))):
    return {"restart": wo_admin.restart_app(delay=1.0, hard=True)}


@router.post("/snapshots/prune")
def prune(keep: int = 10, user=Depends(require("sys.update"))):
    return {"removed": wo_admin.prune_versions(keep)}


@router.post("/deploy")
async def deploy(file: UploadFile = File(...), user=Depends(require("sys.update")), svc=Depends(services)):
    """Classic installs: unpack a release zip over the code folder (a snapshot is taken first), then restart. Docker updates the image instead."""
    if wo_admin.in_docker():
        raise ValueError("in Docker the update is the image: ./watchover.sh update")
    stepup(svc, user)
    wo_admin.snapshot("before zip deploy", kb=svc.kb); wo_admin.prune_versions(12)
    ok, msg = wo_admin.apply_zip(await file.read())
    _audit(svc, user, f"zip deploy {file.filename or ''}: {msg}", ok)
    if not ok:
        raise ValueError(msg)
    out = wo_admin.deploy_finish("zip")
    return {"ok": True, "message": msg, "restart": out.get("restart")}


@router.post("/git-update")
def git_update(branch: str = "", user=Depends(require("sys.update")), svc=Depends(services)):
    if wo_admin.in_docker():
        raise ValueError("in Docker the update is the image: ./watchover.sh update")
    stepup(svc, user)
    wo_admin.snapshot("before git update", kb=svc.kb); wo_admin.prune_versions(12)
    ok, msg = wo_admin.git_update(branch=branch)
    _audit(svc, user, f"git update {branch}: {msg[:120]}", ok)
    if not ok:
        raise ValueError(msg)
    out = wo_admin.deploy_finish("git")
    return {"ok": True, "message": msg, "restart": out.get("restart")}


@router.get("/readme", response_class=PlainTextResponse)
def readme(user=Depends(require("page.readme"))):
    p = Path(__file__).resolve().parents[4] / "README.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------- in-app updates (updates.py checks packages, updater.py executes)
# Docker / Podman: the dashboard never touches the engine. It stores the package in the spool volume shared with the 'updater'
# container (the only one with the engine socket), writes a request and shows the state the updater writes back. Classic installs
# unpack a checked zip in place (snapshot first, then restart). OpenShift: Helm decides (the page shows the commands).
OPENSHIFT_COMMANDS = ["helm upgrade watchover deploy/openshift/watchover -n watchover --reuse-values --set image.tag=<sürüm>",
                      "helm history watchover -n watchover", "helm rollback watchover <REVİZYON> -n watchover"]
MANUAL_COMMANDS = ["./watchover.sh updater on", "./watchover.sh update", "./watchover.sh backup", "./watchover.sh restore backups/<dosya>.tgz"]
CLASSIC_REQUIRED = wo_updates.CLASSIC_FILES + ("src/watchover/__init__.py",)


class UpdApplyIn(BaseModel):
    auto_rollback: bool = True


class UpdRollbackIn(BaseModel):
    version: str
    restore_db: bool = False
    auto_rollback: bool = True


class UpdConfigIn(BaseModel):
    keep: int = 5
    health_s: int = 240
    auto_rollback: bool = True


def _audit(svc, user: dict, detail: str, ok: bool = True) -> None:
    """Every update, rollback, upload and refusal goes into the security log (auth_events, event 'sys.update')."""
    try:
        svc.users._event(user.get("email", "") if isinstance(user, dict) else str(user), "sys.update", bool(ok), detail[:200])
    except Exception:  # noqa: BLE001 - the audit never blocks the action
        pass


def _spool() -> Path:
    sp = wo_updates.spool_dir()
    for d in ("inbox", "requests", "dash"):
        try:
            (sp / d).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return sp


def _pkg_error(e: wo_updates.PackageError, status: int = 422) -> HTTPException:
    return err(status, f"upd_{e.code}", e.detail or e.code, e.detail or e.code)      # the page translates the code


def _pubkey():
    try:
        return wo_updates.configured_pubkey(None if wo_admin.in_docker() else wo_admin.repo_root())
    except ValueError as e:
        raise err(500, "upd_pubkey_invalid", "İmza anahtarı okunamadı (WATCHOVER_UPDATE_PUBKEY)", f"The signing key is unreadable: {e}")


def _updater(sp: Path) -> dict:
    u = wo_updates.read_json(sp / "updater.json", {}) or {}
    try:
        age = time.time() - float(u.get("ts") or 0)
    except (TypeError, ValueError):
        age = 1e9
    u["online"] = bool(u) and age < 30
    u["age_s"] = int(age) if u else None
    return u


def _pending(sp: Path) -> list:
    out = []
    for p in sorted((sp / "requests").glob("*.json")):
        r = wo_updates.read_json(p, {}) or {}
        out.append({k: r.get(k) for k in ("id", "kind", "version", "user", "at", "restore_db")})
    return out


def _packages(sp: Path) -> list:
    out = []
    for d in sorted((sp / "inbox").iterdir() if (sp / "inbox").is_dir() else [], reverse=True):
        m = wo_updates.read_json(d / "meta.json", None)
        if isinstance(m, dict):
            out.append({k: v for k, v in m.items() if k != "sig"})
    return out


def _prune_inbox(sp: Path, keep: int = 3) -> None:
    """Uploaded packages not applied: the newest few stay for a day; one waiting in a request is never removed."""
    waiting = {r.get("id") for r in _pending(sp)}
    dirs = sorted((d for d in (sp / "inbox").iterdir() if d.is_dir() and d.name not in waiting), key=lambda d: d.stat().st_mtime, reverse=True)
    for i, d in enumerate(dirs):
        if i >= keep or time.time() - d.stat().st_mtime > 86400:
            shutil.rmtree(d, ignore_errors=True)


def _audit_finished(svc, sp: Path) -> None:
    """The updater has no database: the dashboard records each finished job once (dash/audited.json)."""
    st = wo_updates.read_json(sp / "state.json", None)
    if not isinstance(st, dict) or st.get("status") in (None, "running") or not st.get("id"):
        return
    marks = wo_updates.read_json(sp / "dash" / "audited.json", []) or []
    if st["id"] in marks:
        return
    e = st.get("error") or {}
    _audit(svc, st.get("user") or "updater", f"{st.get('kind')} {st.get('from')} -> {st.get('to')}: {st.get('status')}"
           + (f" ({e.get('code')}: {e.get('detail', '')[:80]})" if e else ""), st.get("status") == "done")
    try:
        wo_updates.write_json(sp / "dash" / "audited.json", (marks + [st["id"]])[-50:])
    except OSError:
        pass


def _idle(sp: Path) -> None:
    st = wo_updates.read_json(sp / "state.json", None)
    if (isinstance(st, dict) and st.get("status") == "running") or _pending(sp):
        raise err(409, "upd_busy", "Bir güncelleme zaten sürüyor; bitmesini bekleyin", "An update is already running; wait for it to finish")


def _check_id(pid: str) -> None:
    if not wo_updates.ID_RE.match(pid or ""):
        raise HTTPException(404, "not found")


@router.get("/updates")
def updates_overview(user=Depends(require("sys.update")), svc=Depends(services)):
    """Everything the update card shows: the mode, the running version, uploaded packages, kept releases and the last job."""
    m = wo_updates.mode()
    out = {"mode": m, "current": __version__, "display": wo_updates.display(__version__), "max_mb": wo_updates.max_upload_bytes() // 1048576,
           "packages": [], "releases": [], "state": None, "pending": [], "updater": None, "config": {}, "commands": []}
    try:
        key = _pubkey()
        out["signing"] = {"required": bool(key), "fingerprint": wo_updates.fingerprint(key) if key else ""}
    except HTTPException:
        out["signing"] = {"required": True, "fingerprint": "", "error": "pubkey_invalid"}
    if m == "openshift":
        out["commands"] = OPENSHIFT_COMMANDS
        return out
    if m == "manual":
        out["commands"] = MANUAL_COMMANDS
        return out
    sp = _spool()
    out["packages"] = _packages(sp)
    if m == "updater":
        u = _updater(sp)
        rel = wo_updates.read_json(sp / "releases.json", {}) or {}
        out.update(updater=u, releases=rel.get("releases", []), state=wo_updates.read_json(sp / "state.json", None), pending=_pending(sp),
                   config=wo_updates.read_json(sp / "dash" / "config.json", {}) or {"keep": u.get("keep", 5), "health_s": u.get("health_s", 240), "auto_rollback": True})
        if (u.get("signing") or {}).get("required") and not out["signing"]["required"]:
            out["signing"] = dict(u["signing"], checked_by="updater")
        if not u.get("online"):
            out["commands"] = ["./watchover.sh updater on", "./watchover.sh logs updater"]
        _audit_finished(svc, sp)
    else:
        out["commands"] = ["scripts/watchover-launcher.sh update"]
    return out


@router.post("/updates/stepup")
def updates_stepup(user=Depends(require("sys.update")), svc=Depends(services)):
    """428 act_stepup_required until the account has verified itself again (the page asks before a large upload)."""
    return {"ok": True, **stepup(svc, user)}


@router.post("/updates/upload", status_code=201)
async def updates_upload(request: Request, name: str = "", user=Depends(require("sys.update")), svc=Depends(services)):
    """The package as the raw request body (streamed to the spool, size-capped), its .sig in X-Watchover-Signature; checked at once."""
    stepup(svc, user)
    m = wo_updates.mode()
    if m not in ("updater", "inprocess"):
        raise err(409, "upd_unavailable", "Bu kurulumda uygulama içinden güncellenmez", "This installation is not updated from the interface")
    sp = _spool()
    pid = wo_updates.new_id()
    d = sp / "inbox" / pid
    d.mkdir(parents=True)
    limit, n = wo_updates.max_upload_bytes(), 0
    part = d / "upload.part"
    with open(part, "wb") as f:
        async for chunk in request.stream():
            n += len(chunk)
            if n > limit:
                break
            f.write(chunk)
    name = re.sub(r"[^\w.@+-]", "_", os.path.basename(name or "package"))[:120]
    if n > limit:
        shutil.rmtree(d, ignore_errors=True)
        _audit(svc, user, f"update upload refused {name}: larger than {limit // 1048576} MB", False)
        raise err(413, "upd_too_large", f"Paket {limit // 1048576} MB sınırını aşıyor", f"The package exceeds {limit // 1048576} MB")
    sig = (request.headers.get("x-watchover-signature") or "").strip()[:512]
    try:
        kind = wo_updates.kind_of(part)
        final = d / ("package.zip" if kind == "zip" else "package.tar")
        os.replace(part, final)
        info = wo_updates.inspect(final, sig=sig, pubkey=_pubkey(), current=__version__, require_updater=(m == "updater"),
                                  require=wo_updates.REQUIRED if m == "updater" else CLASSIC_REQUIRED)
    except wo_updates.PackageError as e:
        shutil.rmtree(d, ignore_errors=True)
        _audit(svc, user, f"update upload refused {name}: {e.code} {e.detail[:80]}", False)
        raise _pkg_error(e)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    meta = {"id": pid, "name": name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "user": user.get("email", ""), "sig": sig, **info}
    wo_updates.write_json(d / "meta.json", meta, mode=0o640)
    _audit(svc, user, f"update uploaded {name}: v{info['version']} ({info['kind']}, sha256 {info['sha256'][:12]}, signature {info['signature']['status']})")
    _prune_inbox(sp)
    return {k: v for k, v in meta.items() if k != "sig"}


@router.delete("/updates/packages/{pid}")
def updates_discard(pid: str, user=Depends(require("sys.update")), svc=Depends(services)):
    _check_id(pid)
    sp = _spool()
    if any(r.get("id") == pid for r in _pending(sp)):
        raise err(409, "upd_busy", "Bu paket uygulanmak üzere sırada", "This package is queued for the update")
    shutil.rmtree(sp / "inbox" / pid, ignore_errors=True)
    _audit(svc, user, f"update package discarded {pid}")
    return {"ok": True}


@router.post("/updates/packages/{pid}/apply")
def updates_apply(pid: str, body: UpdApplyIn, user=Depends(require("sys.update")), svc=Depends(services)):
    """Docker / Podman: a request for the updater (backup, build, switch, start, health check, auto-rollback). Classic: unpack and restart."""
    stepup(svc, user)
    _check_id(pid)
    sp = _spool()
    d = sp / "inbox" / pid
    meta = wo_updates.read_json(d / "meta.json", None)
    if not isinstance(meta, dict):
        raise HTTPException(404, "not found")
    m = wo_updates.mode()
    if m == "updater":
        _idle(sp)
        if not _updater(sp).get("online"):
            raise err(409, "upd_offline", "Güncelleyici çalışmıyor: sunucuda ./watchover.sh updater on", "The updater is not running: ./watchover.sh updater on on the server")
        req = {"id": pid, "kind": "apply", "user": user.get("email", ""), "auto_rollback": body.auto_rollback, "sig": meta.get("sig", ""),
               "name": meta.get("name", ""), "version": meta.get("version", ""), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        wo_updates.write_json(sp / "requests" / f"{pid}.json", req, mode=0o640)
        _audit(svc, user, f"update requested v{__version__} -> v{meta.get('version')} ({meta.get('kind')}, auto-rollback {'on' if body.auto_rollback else 'off'})")
        return {"ok": True, "queued": pid}
    if m == "inprocess":
        pkg = d / "package.zip"
        if not pkg.is_file():
            raise HTTPException(404, "not found")
        wo_admin.snapshot(f"before update to v{meta.get('version')}", kb=svc.kb)
        wo_admin.prune_versions(12)
        ok, msg = wo_admin.apply_zip(pkg.read_bytes())
        _audit(svc, user, f"update v{__version__} -> v{meta.get('version')} (in place): {msg}", ok)
        if not ok:
            raise ValueError(msg)
        shutil.rmtree(d, ignore_errors=True)
        out = wo_admin.deploy_finish("zip")
        return {"ok": True, "message": msg, "restart": out.get("restart"), "inprocess": True}
    raise err(409, "upd_unavailable", "Bu kurulumda uygulama içinden güncellenmez", "This installation is not updated from the interface")


@router.post("/updates/rollback")
def updates_rollback(body: UpdRollbackIn, user=Depends(require("sys.update")), svc=Depends(services)):
    """Back to a kept release: its images are reused (built only when missing); restore_db also brings back the backup taken when that
    release was left, so everything written since is lost. Default: the data stays (schema changes are additive)."""
    stepup(svc, user)
    if wo_updates.mode() != "updater":
        raise err(409, "upd_unavailable", "Sürüm geçmişi bu kurulumda güncelleyiciyle yönetilmiyor", "Releases are not managed by the updater here")
    if not wo_updates.VERSION_RE.match(body.version or ""):
        raise err(422, "upd_bad_version", "Geçersiz sürüm", "Invalid version")
    sp = _spool()
    rel = {r.get("version"): r for r in (wo_updates.read_json(sp / "releases.json", {}) or {}).get("releases", [])}
    r = rel.get(body.version)
    if r is None:
        raise err(404, "upd_unknown_release", "Bu sürüm saklanmıyor", "This release is not kept")
    if r.get("current") or body.version == __version__:
        raise err(409, "upd_same_version", "Bu sürüm zaten çalışıyor", "This release is already running")
    if body.restore_db and not r.get("backup"):
        raise err(409, "upd_no_backup", "Bu sürümden çıkılırken alınmış yedek yok", "No backup was taken when this release was left")
    _idle(sp)
    if not _updater(sp).get("online"):
        raise err(409, "upd_offline", "Güncelleyici çalışmıyor: sunucuda ./watchover.sh updater on", "The updater is not running: ./watchover.sh updater on on the server")
    rid = wo_updates.new_id()
    wo_updates.write_json(sp / "requests" / f"{rid}.json", {"id": rid, "kind": "rollback", "version": body.version, "restore_db": body.restore_db,
                                                           "auto_rollback": body.auto_rollback, "user": user.get("email", ""),
                                                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, mode=0o640)
    _audit(svc, user, f"rollback requested v{__version__} -> v{body.version}" + (" WITH database restore" if body.restore_db else " (data kept)"))
    return {"ok": True, "queued": rid}


@router.get("/updates/state")
def updates_state(user=Depends(require("sys.update")), svc=Depends(services)):
    """What the page polls during a job (it keeps polling while the dashboard restarts)."""
    if wo_updates.mode() != "updater":
        return {"state": None, "updater": None, "pending": [], "version": __version__}
    sp = _spool()
    _audit_finished(svc, sp)
    u = _updater(sp)
    return {"state": wo_updates.read_json(sp / "state.json", None), "pending": _pending(sp), "version": __version__,
            "updater": {k: u.get(k) for k in ("online", "busy", "heartbeat", "age_s", "error")}}


@router.get("/updates/log/{jid}", response_class=PlainTextResponse)
def updates_log(jid: str, n: int = 200, user=Depends(require("sys.update"))):
    _check_id(jid)
    p = _spool() / "jobs" / f"{jid}.log"
    if p.is_symlink():
        raise HTTPException(404, "not found")
    return "\n".join(wo_updates.redact(ln) for ln in wo_admin.tail_log(str(p), max(10, min(n, 2000))).splitlines())


@router.put("/updates/config")
def updates_config(body: UpdConfigIn, user=Depends(require("sys.update")), svc=Depends(services)):
    """Releases kept (2..50; the current and the previous one always stay), the health wait (30..1800 s), the auto-rollback default."""
    cfg = {"keep": max(2, min(50, body.keep)), "health_s": max(30, min(1800, body.health_s)), "auto_rollback": body.auto_rollback}
    wo_updates.write_json(_spool() / "dash" / "config.json", cfg)
    _audit(svc, user, f"update settings: keep {cfg['keep']}, health {cfg['health_s']} s, auto-rollback {cfg['auto_rollback']}")
    return cfg
