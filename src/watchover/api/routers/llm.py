"""Optional LLM: connection settings, model listing, Ollama management, quality figures. Never part of the deterministic core."""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ... import autolearn as wo_learn
from ... import llm as wo_llm
from ... import llm_eval as wo_eval
from ... import ollama as wo_ollama
from ... import settings as wo_settings
from ..security import access, current_user, require, require_install, services
from .datasets import dataset_for, dataset_visible

router = APIRouter(prefix="/llm", tags=["llm"])
KEYS = ("llm_provider", "llm_base", "llm_model", "llm_key", "llm_embed")
MASK = "•••"
_pulls: dict[str, dict] = {}


class LlmIn(BaseModel):
    llm_provider: str | None = None
    llm_base: str | None = None
    llm_model: str | None = None
    llm_key: str | None = None
    llm_embed: str | None = None


class PullIn(BaseModel):
    name: str


def _view(svc) -> dict:
    c = wo_settings.load(); cfg = svc.llm_cfg()
    return {"llm_provider": c.get("llm_provider", "auto"), "llm_base": cfg.base_url, "llm_model": cfg.model, "llm_key": MASK if cfg.api_key else "", "llm_embed": cfg.embed_model,
            "kind": cfg.kind, "enabled": cfg.enabled, "providers": list(wo_llm.PROVIDERS), "stats": svc.kb.llm_stats()}


@router.get("")
def get_llm(user=Depends(require("page.llm")), svc=Depends(services)):
    return _view(svc)


# The LLM endpoint, its models and training are installation-wide: every team's incidents go through it (a team member whose
# installation role holds page.llm could otherwise point it at their own server). Reads and tests stay on page.llm.
@router.put("")
def put_llm(body: LlmIn, user=Depends(require_install("page.llm")), svc=Depends(services)):
    vals = {k: v for k, v in body.model_dump().items() if v is not None and not (k == "llm_key" and v == MASK)}
    cur = wo_settings.load()
    moved = any(k in vals and str(vals[k] or "") != str(cur.get(k) or "") for k in ("llm_base", "llm_provider"))
    if moved and "llm_key" not in vals:                       # never send the stored key to a newly pointed endpoint (security review)
        vals["llm_key"] = ""
    wo_settings.save(vals)
    svc.refresh_llm()
    return _view(svc)


@router.post("/test")
def test(user=Depends(require("page.llm")), svc=Depends(services)):
    ok, info = wo_llm.test_connection(svc.llm_cfg())
    return {"ok": ok, "info": info}


@router.get("/models")
def models(user=Depends(require("page.llm")), svc=Depends(services)):
    return {"models": wo_llm.list_models(svc.llm_cfg())}


@router.get("/ollama")
def ollama(user=Depends(require("page.llm")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    base = cfg.root if cfg.kind == "ollama" and cfg.root else wo_ollama.discover()
    if not base:
        return {"base": None, "installed": [], "running": [], "recommended": wo_ollama.RECOMMENDED, "pulls": list(_pulls.values())}
    installed = wo_ollama.installed(base)
    have = {m["name"].removesuffix(":latest") for m in installed}
    for name in [n for n, p in _pulls.items() if p["state"] == "done" and n.removesuffix(":latest") in have]:   # finished and visible in the list: drop the bar
        _pulls.pop(name, None)
    return {"base": base, "installed": installed, "running": wo_ollama.running(base), "recommended": wo_ollama.RECOMMENDED, "pulls": list(_pulls.values())}


@router.post("/ollama/pull", status_code=202)
def pull(body: PullIn, user=Depends(require_install("page.llm")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    base = cfg.root if cfg.kind == "ollama" and cfg.root else wo_ollama.discover()
    if not base:
        raise ValueError("no Ollama reachable")
    st = _pulls[body.name] = {"name": body.name, "state": "running", "pct": 0, "status": "", "error": ""}

    def run():
        try:
            for ev in wo_ollama.pull(base, body.name):
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                if ev.get("total"):
                    st["pct"] = int(100 * ev.get("completed", 0) / ev["total"])
                st["status"] = ev.get("status", "")
            st.update({"state": "done", "pct": 100})
        except Exception as e:  # noqa: BLE001
            st.update({"state": "error", "error": str(e)[:200]})
    threading.Thread(target=run, daemon=True, name=f"ollama-pull-{body.name}").start()
    return st


@router.delete("/ollama/{name:path}")
def remove(name: str, user=Depends(require_install("page.llm")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    base = cfg.root if cfg.kind == "ollama" and cfg.root else wo_ollama.discover()
    return {"ok": bool(base) and wo_ollama.remove(base, name)}


@router.get("/quality")
def quality(user=Depends(require_install("page.llm")), svc=Depends(services)):
    return {"stats": svc.kb.llm_stats(), "calls": svc.kb.llm_calls(200), "evals": svc.kb.eval_runs(20)}


@router.get("/tokens")
def tokens(user=Depends(current_user), svc=Depends(services)):
    """The token meter in the top bar: every signed-in user sees what the LLM calls cost (no secrets in here)."""
    return svc.kb.token_usage()


@router.post("/benchmark")
def benchmark(dataset: Optional[str] = None, lang: str = "tr", acc=Depends(access), user=Depends(require("page.llm")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    if not cfg.enabled:
        raise ValueError("no LLM configured")
    keys = [k for k in list(svc.datasets.items) if dataset_visible(svc, acc, k)]
    a = dataset_for(svc, acc, dataset)["analysis"] if dataset else (svc.datasets.items[keys[-1]]["analysis"] if keys else None)
    if a is None:
        raise ValueError("load a dataset first")
    res = wo_eval.run_benchmark(cfg, a, lang)
    svc.kb.save_eval(cfg.model, dataset or "-", res)
    return res


@router.get("/training-stats")
def training_stats(lang: str = "tr", user=Depends(require_install("page.llm")), svc=Depends(services)):
    """How many fine-tuning examples the memory yields today, by source (patterns, anomalies, resolutions, notes, rules …)."""
    return wo_learn.training_bundle(svc.kb, lang)["stats"]


@router.get("/training-export", response_class=PlainTextResponse)
def training_export(lang: str = "tr", part: str = "all", user=Depends(require_install("page.llm")), svc=Depends(services)):
    """Chat-format JSONL for scripts/train_lora.py: part=all (default), train or eval (held-out 10 %). Built from the whole memory,
    so installation-wide only (a team-scoped account would export other teams' lessons)."""
    if part in ("train", "eval"):
        body = wo_learn.training_bundle(svc.kb, lang)[part]
    else:
        body = wo_learn.training_export(svc.kb, lang).decode()
    name = f"watchover-training-{part}-{lang}.jsonl"
    return PlainTextResponse(body, media_type="application/jsonl", headers={"Content-Disposition": f'attachment; filename="{name}"'})


TRAIN_KEYS = ("train_auto", "train_every_h", "train_min_examples", "train_min_new", "train_base", "train_gate_min", "train_use_ops",
              "train_schedule_on", "train_days", "train_start", "train_end", "train_tz")


class TrainIn(BaseModel):
    train_auto: Optional[bool] = None
    train_every_h: Optional[int] = None
    train_min_examples: Optional[int] = None
    train_min_new: Optional[int] = None
    train_base: Optional[str] = None
    train_gate_min: Optional[float] = None
    train_use_ops: Optional[bool] = None
    train_schedule_on: Optional[bool] = None
    train_days: Optional[str] = None          # "1,2,3,4,5" (1 = Monday ... 7 = Sunday)
    train_start: Optional[str] = None         # "01:00"
    train_end: Optional[str] = None           # "06:00"; before the start = past midnight
    train_tz: Optional[str] = None            # IANA name, e.g. Europe/Istanbul


@router.get("/autotrain")
def autotrain_state(user=Depends(require("page.llm")), svc=Depends(services)):
    """Unattended learning: switches, thresholds, how far the memory is from the next training, run history, the active model."""
    from ... import autotrain as wo_at
    c = wo_settings.load()
    st = wo_at.AutoTrainer(svc.kb, lang=svc.lang).state()
    return {"settings": {k: c.get(k) for k in TRAIN_KEYS}, **st, "ops_model": svc.OPS_MODEL, "ops_active": bool(svc.ops_ready()) and bool(c.get("train_use_ops", True)),
            "trainer_alive": wo_at.trainer_alive()}


@router.put("/autotrain")
def autotrain_settings(body: TrainIn, user=Depends(require_install("page.llm")), svc=Depends(services)):
    vals = {k: v for k, v in body.model_dump().items() if v is not None and k in TRAIN_KEYS}
    import re
    for k in ("train_start", "train_end"):
        if k in vals and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d|24:00", vals[k]):
            raise HTTPException(400, f"{k}: HH:MM")
    if "train_days" in vals:
        days = sorted({int(d) for d in vals["train_days"].replace(" ", "").split(",") if d.isdigit() and 1 <= int(d) <= 7})
        if not days:
            raise HTTPException(400, "train_days: at least one day 1-7")
        vals["train_days"] = ",".join(map(str, days))
    if "train_tz" in vals:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(vals["train_tz"])
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "train_tz: unknown time zone")
    wo_settings.save(vals)
    svc._ops_at = 0
    return {k: wo_settings.load().get(k) for k in TRAIN_KEYS}


@router.post("/autotrain/run")
def autotrain_run(user=Depends(require_install("page.llm")), svc=Depends(services)):
    """Ask the trainer service to train at its next poll (even below the thresholds)."""
    from ... import autotrain as wo_at
    return {"ok": True, "flag": wo_at.request_run()}
