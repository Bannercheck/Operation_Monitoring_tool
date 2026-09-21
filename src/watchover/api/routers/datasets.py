"""Datasets: upload (background job with progress), demo set, funnel / profile, incidents, signals, noise audit, evidence, export."""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse

from ...analysis import incident_dict, postmortem_md, signal_dict
from ...enrich import enrich, summary, to_csv, to_jsonl
from ...i18n import narrative_text, reason_text
from ..security import require, services

router = APIRouter(prefix="/datasets", tags=["datasets"])
ROOT = Path(__file__).resolve().parents[4]


@router.get("")
def list_datasets(user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.list()


@router.post("", status_code=202)
async def upload(files: list[UploadFile] = File(...), combine: bool = Query(True, description="several files -> one dataset (ZIP in memory)"),
                 user=Depends(require("page.data")), svc=Depends(services)):
    """Starts a background load per dataset; poll GET /datasets/jobs/{id} for pct / stage, then GET /datasets/{dataset}."""
    blobs = [(f.filename or "upload", await f.read()) for f in files]
    blobs = [(n, d) for n, d in blobs if d]
    if not blobs:
        raise HTTPException(400, "no data")
    if combine and len(blobs) > 1:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for n, d in blobs:
                zf.writestr(os.path.basename(n), d)
        blobs = [(f"{len(blobs)} files.zip", buf.getvalue())]
    return {"jobs": [svc.datasets.submit(n, d) for n, d in blobs]}


@router.post("/demo")
def load_demo(user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.load_path(str(ROOT / "samples" / "demo_mixed.zip"))


@router.get("/jobs")
def jobs(user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.list_jobs()


@router.get("/jobs/{jid}")
def job(jid: str, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.job(jid)


@router.get("/{key}")
def dataset(key: str, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.summary(key)


@router.delete("/{key}")
def delete(key: str, user=Depends(require("page.data")), svc=Depends(services)):
    svc.datasets.delete(key)
    return {"ok": True}


@router.get("/{key}/incidents")
def incidents(key: str, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    return [_incident(a, i, svc.lang, brief=True) for i in a.incidents]


def _incident(a, inc, lang: str, brief: bool = False) -> dict:
    d = incident_dict(inc)
    d["narrative_text"] = narrative_text(inc, a.signal_by_id, lang)
    d["reason_text"] = reason_text(inc.root_cause_codes, lang)
    d["root_cause"] = signal_dict(a.signal_by_id[inc.root_cause_signal])
    if brief:
        for k in ("timeline", "links", "factors", "evidence"):
            d[k] = len(d.get(k) or [])
    return d


@router.get("/{key}/incidents/{iid}")
def incident(key: str, iid: str, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    inc = a.incident_by_id.get(iid)
    if not inc:
        raise KeyError(iid)
    d = _incident(a, inc, svc.lang)
    d["signals"] = [signal_dict(a.signal_by_id[s]) for s in inc.signal_ids if s in a.signal_by_id]
    d["actions"] = svc.actions.list(inc.id)
    d["playbook"] = svc.playbook.lookup(a.signal_by_id[inc.root_cause_signal].template)
    return d


@router.get("/{key}/incidents/{iid}/postmortem", response_class=PlainTextResponse)
def postmortem(key: str, iid: str, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    inc = a.incident_by_id.get(iid)
    if not inc:
        raise KeyError(iid)
    return postmortem_md(inc, a.signal_by_id)


@router.get("/{key}/signals")
def signals(key: str, limit: int = 100, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    return [signal_dict(s) | {"why": s.why(), "evidence": s.evidence} for s in a.signals[:limit]]


@router.get("/{key}/noise")
def noise(key: str, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.get(key)["analysis"].noise_audit()


@router.get("/{key}/evidence")
def evidence(key: str, ref: str, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    o = a.obs_by_ref.get(ref)
    if not o:
        raise KeyError(ref)
    return {"ref": o.ref, "timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host,
            "environment": o.environment, "message": o.message, "attributes": o.attributes, "template": o.template}


@router.get("/{key}/export")
def export(key: str, fmt: str = Query("csv", pattern="^(csv|jsonl|summary)$"), user=Depends(require("act.export")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    rows = enrich(a)
    if fmt == "summary":
        return summary(a, rows)
    text = to_csv(rows) if fmt == "csv" else to_jsonl(rows)
    return PlainTextResponse(text, media_type="text/csv" if fmt == "csv" else "application/x-ndjson")
