"""Datasets: upload (background job with progress), demo set, funnel / profile, incidents, signals, noise audit, evidence, export."""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ... import compare as wo_compare
from ... import connectors as wo_conn
from ...analysis import incident_dict, llm_prompt, postmortem_md, signal_dict, suggested_owner
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
                 mapping: str = Form(""), user=Depends(require("page.data")), svc=Depends(services)):
    """Starts a background load per dataset; poll GET /datasets/jobs/{id} for pct / stage, then GET /datasets/{dataset}.
    `mapping` (JSON, optional) pins column roles for tabular files: {"timestamp": "ts", "message": "msg", "severity": "level", ...}."""
    import json
    try:
        mp = json.loads(mapping) if mapping.strip() else None
    except ValueError:
        raise HTTPException(400, "mapping must be JSON")
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
    return {"jobs": [svc.datasets.submit(n, d, mp) for n, d in blobs]}


@router.post("/demo")
def load_demo(user=Depends(require("page.data")), svc=Depends(services)):
    return svc.datasets.load_path(str(ROOT / "samples" / "demo_mixed.zip"))


@router.get("/samples")
def samples(user=Depends(require("page.data"))):
    """The sample sets shipped with the product (samples/*.zip)."""
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted((ROOT / "samples").glob("*.zip"))]


@router.post("/samples/{name}", status_code=202)
def load_sample(name: str, user=Depends(require("page.data")), svc=Depends(services)):
    p = ROOT / "samples" / os.path.basename(name)
    if not p.is_file() or p.suffix != ".zip":
        raise KeyError(name)
    return svc.datasets.submit(p.name, p.read_bytes())


class FetchIn(BaseModel):
    kind: str = "http"            # http | mcp
    url: str
    headers: str = ""             # "Key: Value" lines
    method: str = "GET"
    body: str = ""
    path: str = ""                # JSON path to the record list (http)
    tool: str = ""                # MCP tool name
    arguments: dict = {}


@router.post("/fetch", status_code=202)
def fetch_remote(body: FetchIn, user=Depends(require("page.data")), svc=Depends(services)):
    """Pull a dataset from an HTTP endpoint (JSON / text) or an MCP tool and load it as a background job."""
    headers = wo_conn.parse_headers(body.headers)
    try:
        if body.kind == "mcp":
            name, data = wo_conn.fetch_mcp(body.url, body.tool, body.arguments, headers)
        else:
            name, data = wo_conn.fetch_http(body.url, body.method, headers, body.body or None, body.path)
    except Exception as e:  # noqa: BLE001 - a remote that cannot be reached is the caller's problem, not a server error
        raise ValueError(f"fetch failed: {type(e).__name__}: {str(e)[:200]}")
    return svc.datasets.submit(name, data)


class FromSourceIn(BaseModel):
    id: int
    minutes: int = 60              # window before now
    limit: int = 20000


@router.post("/from-source", status_code=202)
def from_source(body: FromSourceIn, user=Depends(require("page.data")), svc=Depends(services)):
    """Pull a window from a saved pull source (Elasticsearch, Loki, Splunk, Graylog, HTTP) and load it as a dataset."""
    from datetime import datetime, timedelta, timezone
    from ... import sources as wo_src
    src = svc.sources.get(body.id)
    if not src:
        raise KeyError(body.id)
    since = datetime.now(timezone.utc) - timedelta(minutes=max(1, min(body.minutes, 7 * 1440)))
    try:
        rows, _cursor = wo_src.fetch(src, since=since, limit=max(1, min(body.limit, 200000)))
    except Exception as e:  # noqa: BLE001 - remote errors are reported to the caller, not logged as server faults
        raise ValueError(f"fetch failed: {type(e).__name__}: {str(e)[:200]}")
    if not rows:
        raise ValueError("the source returned no records for that window")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return svc.datasets.submit(f"{src.name}-{stamp}.jsonl", wo_src.to_jsonl(rows, src.env))


@router.post("/mcp-tools")
def list_mcp_tools(body: FetchIn, user=Depends(require("page.data"))):
    try:
        return [x["name"] for x in wo_conn.mcp_tools(body.url, wo_conn.parse_headers(body.headers))]
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"mcp failed: {type(e).__name__}: {str(e)[:200]}")


@router.get("/compare")
def compare(a: str, b: str, user=Depends(require("page.data")), svc=Depends(services)):
    """Two datasets side by side: the same KPIs and what changed (same engine, so a difference is a difference in the data)."""
    da, db = svc.datasets.get(a), svc.datasets.get(b)
    return wo_compare.compare(da["analysis"], da["profile"], db["analysis"], db["profile"])


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
    root = a.signal_by_id.get(inc.root_cause_signal)
    d["root_cause"] = signal_dict(root) if root else {"id": inc.root_cause_signal, "template": inc.title, "severity": inc.severity, "count": 0, "services": "", "hosts": "", "onset": ""}
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
    try:
        d["actions"] = svc.actions.list(inc.id)
    except Exception as e:  # noqa: BLE001 - the card is still useful without its side panels
        d["actions"], d["actions_error"] = [], str(e)[:200]
    try:
        root = a.signal_by_id.get(inc.root_cause_signal)
        d["playbook"] = svc.playbook.lookup(root.template) if root else None
    except Exception as e:  # noqa: BLE001
        d["playbook"], d["playbook_error"] = None, str(e)[:200]
    return _json_safe(d)


def _json_safe(x):
    """NaN / inf floats break the JSON encoder (a 500 the browser shows as an endless spinner): they become null."""
    import math
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    return x


@router.get("/{key}/actions")
def dataset_actions(key: str, user=Depends(require("page.data")), svc=Depends(services)):
    ids = {i.id for i in svc.datasets.get(key)["analysis"].incidents}
    return [a for a in svc.actions.list() if a["incident_id"] in ids]


class FeedbackIn(BaseModel):
    verdict: str                  # up | down
    correct: str = "__keep__"     # __keep__ | __noise__ | __other__ | alt<i>
    comment: str = ""


@router.post("/{key}/incidents/{iid}/feedback")
def feedback(key: str, iid: str, body: FeedbackIn, user=Depends(require("page.data")), svc=Depends(services)):
    """Thumbs up / down on the card with an optional corrected root cause; wrong verdicts become rule proposals."""
    d = svc.datasets.get(key); a = d["analysis"]
    inc = a.incident_by_id.get(iid)
    if not inc:
        raise KeyError(iid)
    root = a.signal_by_id[inc.root_cause_signal]
    root_type = str(root.observations[0].attributes.get("alarm_type", "")).lower() if root.observations else ""
    alt_type, correct_txt = "", ""
    if body.correct.startswith("alt"):
        x = inc.root_cause_alternatives[int(body.correct[3:])]
        sig = a.signal_by_id.get(x.get("signal", "")) if isinstance(x, dict) else None
        alt_type = str(sig.observations[0].attributes.get("alarm_type", "")).lower() if sig and sig.observations else ""
        correct_txt = f"{x.get('template', '')[:80]} ({', '.join(x.get('services', [])[:2])})"
    else:
        correct_txt = {"__keep__": "", "__noise__": "noise", "__other__": body.comment}.get(body.correct, body.comment)
    out = svc.kb.add_feedback(d["name"], inc, root, body.verdict, correct=correct_txt, comment=body.comment, alt_type=alt_type, root_type=root_type, mark_noise=(body.correct == "__noise__"))
    return {"ok": True, "proposals": len(out.get("proposals", []))}


@router.get("/{key}/incidents/{iid}/related")
def related(key: str, iid: str, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    inc = a.incident_by_id.get(iid)
    if not inc:
        raise KeyError(iid)
    return svc.kb.related(inc, a.signal_by_id[inc.root_cause_signal])


@router.get("/{key}/incidents/{iid}/prompt", response_class=PlainTextResponse)
def prompt(key: str, iid: str, user=Depends(require("page.data")), svc=Depends(services)):
    a = svc.datasets.get(key)["analysis"]
    inc = a.incident_by_id.get(iid)
    if not inc:
        raise KeyError(iid)
    return llm_prompt(inc, a.signal_by_id)


@router.post("/{key}/incidents/{iid}/explain")
def explain(key: str, iid: str, user=Depends(require("page.data")), svc=Depends(services)):
    """Ask the configured LLM for a narrative of the card (the deterministic verdict is never changed by it)."""
    from ...llm import chat
    a = svc.datasets.get(key)["analysis"]
    inc = a.incident_by_id.get(iid)
    if not inc:
        raise KeyError(iid)
    cfg = svc.llm_cfg()
    if not cfg.enabled:
        raise ValueError("no LLM configured")
    return {"text": chat(cfg, llm_prompt(inc, a.signal_by_id)), "model": cfg.model}


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
    """Noise audit: eliminated signals with reasons, demoted groups, and for alarm storms the service x time heat map."""
    from collections import Counter
    from datetime import timedelta
    a = svc.datasets.get(key)["analysis"]
    na = a.noise_audit()
    for r in na["rows"]:
        for k in ("first", "last"):
            if hasattr(r.get(k), "isoformat"):
                r[k] = r[k].isoformat()
    na["demoted"] = [{"id": i.id, "severity": i.severity, "count": sum(a.signal_by_id[x].count for x in i.signal_ids), "services": ", ".join(i.affected_services[:4]),
                      "first": i.started_at.isoformat(), "last": i.ended_at.isoformat(), "title": i.title[:80]} for i in getattr(a, "demoted", [])]
    na["heat"] = None
    if getattr(a, "mode", "") == "density" and a.storm.get("cells") is not None and a.storm.get("t0"):
        t0, bm, hot = a.storm["t0"], a.storm["bucket_min"], a.storm["cells"]
        counts = Counter((o.service or "-", int((o.timestamp - t0).total_seconds() // (bm * 60))) for o in a.observations)
        svc_tot = Counter()
        for (svc_, b), n in counts.items():
            svc_tot[svc_] += n
        buckets = sorted({b for _, b in counts})
        na["heat"] = {"services": [x for x, _ in svc_tot.most_common()], "buckets": [(t0 + timedelta(minutes=b * bm)).strftime("%H:%M") for b in buckets],
                      "cells": [{"service": svc_, "bucket": (t0 + timedelta(minutes=b * bm)).strftime("%H:%M"), "n": n, "hot": (svc_, b) in hot} for (svc_, b), n in counts.items()],
                      "bucket_min": bm}
    return na


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


SRCH_TOK = re.compile(r'(-)?(?:(\w+):)?(?:"([^"]*)"|(\S+))')
SRCH_FIELDS = {"service": "service", "servis": "service", "host": "host", "sunucu": "host", "severity": "severity", "önem": "severity", "sev": "severity", "env": "environment", "ortam": "environment", "file": "source", "dosya": "source"}


def _parse_query(q: str) -> list[tuple[bool, str | None, str]]:
    """'timeout db-01 service:payment-api -debug "exact phrase"' -> [(neg, field|None, term)]"""
    out = []
    for m in SRCH_TOK.finditer(q or ""):
        term = m[3] if m[3] is not None else m[4]
        if term:
            out.append((m[1] == "-", SRCH_FIELDS.get((m[2] or "").lower()), term.lower()))
    return out


@router.get("/{key}/search")
def search(key: str, q: str = "", regex: bool = False, severity: str | None = None, limit: int = Query(200, le=2000), user=Depends(require("page.data")), svc=Depends(services)):
    """Log search over the dataset: AND of terms, field:term narrows one field, -term excludes, quotes keep phrases; optional regex."""
    a = svc.datasets.get(key)["analysis"]
    terms = _parse_query(q)
    sev = {x.strip().upper() for x in (severity or "").split(",") if x.strip()}
    out, total = [], 0
    for o in a.observations:
        if sev and o.severity not in sev:
            continue
        hay = None
        ok = True
        for neg, field, term in terms:
            if field:
                val = str(getattr(o, field, "") or "").lower()
                hit = bool(re.search(term, val)) if regex else term in val
            else:
                hay = hay if hay is not None else f"{o.message} {o.service} {o.host} {o.severity} {o.source} {o.environment}".lower()
                try:
                    hit = bool(re.search(term, hay)) if regex else term in hay
                except re.error:
                    hit = term in hay
            if hit == neg:
                ok = False; break
        if not ok:
            continue
        total += 1
        if len(out) < limit:
            out.append({"ref": o.ref, "timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host, "environment": o.environment,
                        "source": o.source, "line_no": o.line_no, "message": o.message[:400]})
    return {"total": total, "rows": out}
