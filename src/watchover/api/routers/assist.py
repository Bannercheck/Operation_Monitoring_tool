"""Ask Watchover: grounded answers over the loaded dataset and the knowledge base (LLM optional, deterministic fallback). A
team-scoped account asks over its own teams' datasets and live hosts, and its answers cite only the knowledge it may read."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import assistant as wo_assistant
from ... import ownership
from ..security import access, require, services, user_scope
from .datasets import dataset_for, dataset_visible

router = APIRouter(prefix="/assist", tags=["assist"])


class AskIn(BaseModel):
    question: str
    history: List[dict] = []
    dataset: Optional[str] = None
    lang: str = "tr"
    mcp: bool = True                   # also ask the external MCP tools allowed for the assistant (read-only, cited [M1])


class RateIn(BaseModel):
    question: str
    answer: str
    verdict: str


class SaveIn(BaseModel):
    question: str
    answer: str
    team_id: Optional[int] = None


def _analysis(svc, acc, dataset: Optional[str], sc=None):
    """The analysis to answer over: the live buffer (scoped), a dataset the caller may open, or the newest such dataset."""
    if dataset == "live":
        return svc.live_analysis(sc)
    if dataset:
        return dataset_for(svc, acc, dataset)["analysis"]
    keys = [k for k in list(svc.datasets.items) if dataset_visible(svc, acc, k)]
    return svc.datasets.items[keys[-1]]["analysis"] if keys else None


def _team(acc, team_id: Optional[int]) -> Optional[int]:
    """Notes and saved answers of a team-scoped account belong to its team (the given one, else its first)."""
    if acc.wide:
        return team_id if team_id is not None and acc.can_in(team_id, "page.assist") else None
    ids = acc.team_ids("page.assist")
    return team_id if team_id in ids else (ids[0] if ids else None)


@router.post("/ask")
def ask(body: AskIn, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    cfg = svc.llm_cfg("ops")
    from ...mcpclient import assist_context
    extra = assist_context(svc, acc, body.question) if body.mcp else []
    res = wo_assistant.answer(cfg, body.question, body.history[-6:], _analysis(svc, acc, body.dataset, sc), ownership.kb_for(svc, acc, sc), body.lang,
                              extra=extra)
    return {**res, "model": cfg.model if cfg.enabled else ""}


@router.post("/rate")
def rate(body: RateIn, user=Depends(require("page.assist")), svc=Depends(services)):
    svc.kb.rate_answer(svc.llm_cfg().model or "-", body.question, body.verdict, answer=body.answer)
    return {"ok": True}


@router.post("/save", status_code=201)
def save(body: SaveIn, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    lid = svc.kb.add("chat", body.question[:80] or "chat", f"Q: {body.question}\nA: {body.answer}", tags=["chat"])
    tid = _team(acc, body.team_id)
    if tid is not None:
        svc.kb._exec("UPDATE lessons SET team_id=? WHERE id=? AND team_id IS NULL", (tid, lid))
    return {"id": lid}


@router.post("/propose-rules")
def propose_rules(dataset: Optional[str] = None, lang: str = "tr", acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services),
                  sc=Depends(user_scope)):
    cfg = svc.llm_cfg()
    if not cfg.enabled:
        raise ValueError("no LLM configured")
    return wo_assistant.propose_rules(cfg, _analysis(svc, acc, dataset, sc), ownership.kb_for(svc, acc, sc), lang)
