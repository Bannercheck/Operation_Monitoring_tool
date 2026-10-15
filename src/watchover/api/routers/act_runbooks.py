"""/api/act/runbooks, /api/act/templates (docs/design/action-agent.md §5, §6, §12.1).

Team checks: writing needs act.runbook in the owning team, publishing act.publish (never the author, I6, administrators
included; R3 needs two reviewers, one an installation act.policy holder, and a step-up). Shared runbooks (team NULL) need the
key installation-wide."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ...act.runbooks import RunbookError, diagnose_only
from ...act.types import RISKS
from ..security import access, check_team, current_user, pick_team, require
from .act_common import act_on, err, lift, stepup

router = APIRouter(prefix="/act", tags=["act"])


class RunbookIn(BaseModel):
    key: str
    title: str = ""
    team_id: Optional[int] = None
    body: Dict[str, Any] = {}
    adopt: str = ""                       # copy a starter / shared runbook into this team


class DraftIn(BaseModel):
    body: Dict[str, Any]
    change_note: str = ""


class SubmitIn(BaseModel):
    tabletop: bool = False


class ReviewIn(BaseModel):
    decision: str
    reason: str = ""


class DraftFromIn(BaseModel):
    playbook_key: str = ""
    case_id: Optional[int] = None
    text: str = ""
    key: str = ""
    team_id: Optional[int] = None


def _rb(svc, acc, key: str) -> dict:
    rb = svc.act.runbooks.get(key)
    if not acc.visible_team(rb["team_id"], "page.auto"):
        raise KeyError(key)
    return rb


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except RunbookError as e:
        raise lift(e)
    except Exception as e:  # noqa: BLE001 - ApprovalError and friends carry .detail()
        if hasattr(e, "detail") and callable(getattr(e, "detail")) and hasattr(e, "status"):
            raise lift(e)
        raise


@router.get("/runbooks")
def runbooks(svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    svc.act.ensure_starter()
    return svc.act.runbooks.list(None if acc.wide else acc.team_ids("page.auto"))


@router.get("/runbooks/{key}")
def runbook(key: str, svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    rb = dict(_rb(svc, acc, key))
    rb["latest"] = svc.act.runbooks.version(key)
    rb["playbook_links"] = svc.kb._exec("SELECT playbook_key FROM act_playbook_links WHERE runbook_key=?", (key,))
    return rb


@router.get("/runbooks/{key}/versions/{v}")
def version(key: str, v: int, svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    _rb(svc, acc, key)
    return svc.act.runbooks.version(key, v)


@router.get("/runbooks/{key}/diff")
def diff(key: str, from_: int = Query(0, alias="from"), to: int = 0, svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    _rb(svc, acc, key)
    return svc.act.runbooks.diff(key, from_, to)


@router.post("/runbooks", status_code=201)
def create(body: RunbookIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.runbook"))):
    tid = pick_team(acc, "act.runbook", body.team_id)
    check_team(acc, tid, "act.runbook", "page.auto")
    src_body, origin, ref = dict(body.body or {}), "human", ""
    if body.adopt:
        src = svc.act.runbooks.version(body.adopt)
        src_body, origin, ref = dict(src["body"]), "starter", body.adopt
    return _call(svc.act.runbooks.create, body.key, body.title or src_body.get("title", body.key), tid, src_body, user["email"], origin=origin,
                 origin_ref=ref)


@router.put("/runbooks/{key}/draft")
def put_draft(key: str, body: DraftIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.runbook"))):
    rb = _rb(svc, acc, key)
    check_team(acc, rb["team_id"], "act.runbook", "page.auto")
    return _call(svc.act.runbooks.put_draft, key, body.body, user["email"], body.change_note)


@router.post("/runbooks/{key}/versions/{v}/lint")
def lint(key: str, v: int, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.runbook"))):
    rb = _rb(svc, acc, key)
    check_team(acc, rb["team_id"], "act.runbook", "page.auto")
    return {"findings": svc.act.runbooks.relint(key, v)}


@router.post("/runbooks/{key}/versions/{v}/submit")
def submit(key: str, v: int, body: SubmitIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.runbook"))):
    rb = _rb(svc, acc, key)
    check_team(acc, rb["team_id"], "act.runbook", "page.auto")
    return _call(svc.act.runbooks.submit, key, v, user["email"], tabletop=body.tabletop)


@router.post("/runbooks/{key}/versions/{v}/review")
def review(key: str, v: int, body: ReviewIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.publish"))):
    rb = _rb(svc, acc, key)
    check_team(acc, rb["team_id"], "act.publish", "page.auto")
    ver = svc.act.runbooks.version(key, v)
    risk = ver.get("risk_effective") or "R0"
    method = ""
    if risk in RISKS and RISKS.index(risk) >= 3:
        method = stepup(svc, user)["method"]
    tags = ["policy"] if acc.install("act.policy") else []
    if diagnose_only(ver["body"]) is False and user["email"].lower() == (ver.get("author") or "").lower():
        raise err(403, "act_sod", "Yazar kendi runbook'unu yayınlayamaz", "The author cannot publish their own runbook")
    return _call(svc.act.runbooks.review, key, v, user["email"], body.decision, body.reason, tags=tags, stepup_method=method)


@router.post("/runbooks/{key}/retire")
def retire(key: str, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.publish"))):
    rb = _rb(svc, acc, key)
    check_team(acc, rb["team_id"], "act.publish", "page.auto")
    return svc.act.runbooks.retire(key, user["email"])


@router.post("/runbooks/draft-from", status_code=201)
def draft_from(body: DraftFromIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.runbook"))):
    if not body.playbook_key:
        raise err(422, "act_param_invalid", "Şimdilik yalnız playbook notlarından taslak", "Only playbook notes for now (cases and text: later)")
    if not acc.can("page.pb"):
        raise err(403, "act_forbidden", "Playbook görme yetkisi gerekli", "Permission page.pb required")
    tid = pick_team(acc, "act.runbook", body.team_id)
    from ...act.importer import draft_from_playbook, slug
    entry = svc.playbook.to_runbook_draft(body.playbook_key)
    key = body.key or slug(entry["title"])
    draft = draft_from_playbook(entry, svc.act.catalogue.approved(), key=key, team_id=tid)
    return _call(svc.act.runbooks.create, key, draft["title"], tid, draft, user["email"], origin="playbook", origin_ref=body.playbook_key[:200],
                 playbook_key=body.playbook_key)


@router.get("/templates")
def templates(status: str = "", svc=Depends(act_on), user=Depends(require("page.auto"))):
    rows = svc.act.catalogue.list(status or None)
    for r in rows:
        r["coverage"] = {"installed": 0, "hosts": 0}         # actuators report their allowlists from WP11a on
    return rows
