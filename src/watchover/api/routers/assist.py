"""Ask Watchover: grounded answers over the loaded dataset and the knowledge base (LLM optional, deterministic fallback)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import assistant as wo_assistant
from ..security import require, services

router = APIRouter(prefix="/assist", tags=["assist"])


class AskIn(BaseModel):
    question: str
    history: list[dict] = []
    dataset: str | None = None
    lang: str = "tr"


class RateIn(BaseModel):
    question: str
    answer: str
    verdict: str


class SaveIn(BaseModel):
    question: str
    answer: str


def _analysis(svc, dataset: str | None):
    if dataset == "live":
        return svc.live_analysis()
    if dataset:
        return svc.datasets.get(dataset)["analysis"]
    items = list(svc.datasets.items.values())
    return items[-1]["analysis"] if items else None


@router.post("/ask")
def ask(body: AskIn, user=Depends(require("page.assist")), svc=Depends(services)):
    cfg = svc.llm_cfg("ops")
    res = wo_assistant.answer(cfg, body.question, body.history[-6:], _analysis(svc, body.dataset), svc.kb, body.lang)
    return {**res, "model": cfg.model if cfg.enabled else ""}


@router.post("/rate")
def rate(body: RateIn, user=Depends(require("page.assist")), svc=Depends(services)):
    svc.kb.rate_answer(svc.llm_cfg().model or "-", body.question, body.verdict, answer=body.answer)
    return {"ok": True}


@router.post("/save", status_code=201)
def save(body: SaveIn, user=Depends(require("page.assist")), svc=Depends(services)):
    return {"id": svc.kb.add("chat", body.question[:80] or "chat", f"Q: {body.question}\nA: {body.answer}", tags=["chat"])}


@router.post("/propose-rules")
def propose_rules(dataset: str | None = None, lang: str = "tr", user=Depends(require("page.assist")), svc=Depends(services)):
    cfg = svc.llm_cfg()
    if not cfg.enabled:
        raise ValueError("no LLM configured")
    return wo_assistant.propose_rules(cfg, _analysis(svc, dataset), svc.kb, lang)
