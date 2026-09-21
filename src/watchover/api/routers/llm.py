"""Optional LLM: connection settings, model listing, Ollama management, quality figures. Never part of the deterministic core."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import llm as wo_llm
from ... import llm_eval as wo_eval
from ... import ollama as wo_ollama
from ... import settings as wo_settings
from ..security import require, services

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


@router.put("")
def put_llm(body: LlmIn, user=Depends(require("page.llm")), svc=Depends(services)):
    vals = {k: v for k, v in body.model_dump().items() if v is not None and not (k == "llm_key" and v == MASK)}
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
    return {"base": base, "installed": wo_ollama.installed(base), "running": wo_ollama.running(base), "recommended": wo_ollama.RECOMMENDED, "pulls": list(_pulls.values())}


@router.post("/ollama/pull", status_code=202)
def pull(body: PullIn, user=Depends(require("page.llm")), svc=Depends(services)):
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
def remove(name: str, user=Depends(require("page.llm")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    base = cfg.root if cfg.kind == "ollama" and cfg.root else wo_ollama.discover()
    return {"ok": bool(base) and wo_ollama.remove(base, name)}


@router.get("/quality")
def quality(user=Depends(require("page.llm")), svc=Depends(services)):
    return {"stats": svc.kb.llm_stats(), "calls": svc.kb.llm_calls(200), "evals": svc.kb.eval_runs(20)}


@router.post("/benchmark")
def benchmark(dataset: str | None = None, lang: str = "tr", user=Depends(require("page.llm")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    if not cfg.enabled:
        raise ValueError("no LLM configured")
    a = svc.datasets.get(dataset)["analysis"] if dataset else (list(svc.datasets.items.values())[-1]["analysis"] if svc.datasets.items else None)
    if a is None:
        raise ValueError("load a dataset first")
    res = wo_eval.run_benchmark(cfg, a, lang)
    svc.kb.save_eval(cfg.model, dataset or "-", res)
    return res
