"""/api/act: inbox, cases, triggers (docs/design/action-agent.md §4, §12.1).

Visibility reuses the legacy actions rule (`routers/actions.py::visible`); writes need act.case on the host (or in the team for
host-less cases). Decisions per attempt, peek and the blind rules are WP7's (act/shadow.py); runs and steps arrive with WP8."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...act.types import SEVERITIES, Trigger
from ..security import access, check_team, current_user, host_guard, perm_scope, pick_team, require_install
from .act_common import act_on, err
from .actions import visible

router = APIRouter(prefix="/act", tags=["act"])


class CaseIn(BaseModel):
    title: str
    host: str = ""
    severity: str = "medium"
    team_id: Optional[int] = None
    ref: Optional[Dict[str, Any]] = None


class AssignIn(BaseModel):
    owner: str = ""


class ReasonIn(BaseModel):
    reason: str = ""
    minutes: int = 60


class CloseIn(BaseModel):
    outcome: str = "ok"
    resolution: str = ""
    what_did_you_do: List[Dict[str, Any]] = []


class DecideIn(BaseModel):
    choice: str
    version: Optional[int] = None
    params: Dict[str, Any] = {}
    reason_code: str = ""
    note: str = ""


def _shadow(fn, *a, **k):
    from ...act.shadow import ShadowError
    try:
        return fn(*a, **k)
    except ShadowError as e:
        raise err(e.status, e.code, e.tr, e.en)


def _case(svc, acc, sc, cid: int) -> dict:
    c = svc.act.cases.get(cid)
    if not visible(acc, sc, c):
        raise KeyError(cid)
    return c


def _write_guard(svc, acc, c: dict) -> None:
    if c.get("host"):
        host_guard(acc, c["host"], "act.case", "page.actions")
    else:
        check_team(acc, c.get("team_id"), "act.case", "page.actions")


@router.get("/inbox")
def inbox(tab: str = "team", q: str = "", svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
          user=Depends(current_user)):
    if tab not in ("mine", "team", "approvals", "auto", "closed"):
        raise err(422, "act_param_invalid", "Geçersiz sekme", "Invalid tab")
    return svc.act.cases.inbox(user["email"], acc.team_ids("page.actions"), lambda r: visible(acc, sc, r), tab, q)


@router.get("/cases/{cid}")
def get_case(cid: int, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions"))):
    _case(svc, acc, sc, cid)
    c = svc.act.cases.detail(cid)
    c["action_id"] = cid
    c["can"] = {"case": acc.can("act.case"), "execute": acc.can("act.execute")}
    return c


@router.post("/cases", status_code=201)
def create_case(body: CaseIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    if not acc.can("act.case"):
        raise err(403, "act_forbidden", "Vaka açma yetkisi (act.case) gerekli", "Permission act.case required")
    if body.severity not in SEVERITIES:
        raise err(422, "act_param_invalid", "Geçersiz önem", "Invalid severity")
    tid = body.team_id
    if body.host:
        host_guard(acc, body.host, "act.case", "page.actions")
    else:
        tid = pick_team(acc, "act.case", body.team_id)
    res = svc.act.cases.manual(body.title[:200], body.host, body.severity, tid, body.ref, user["email"])
    return svc.act.cases.detail(res["case_id"]) | {"routed": res["action"]}


@router.post("/triggers", status_code=201)
def trigger(body: Dict[str, Any], svc=Depends(act_on), user=Depends(require_install("act.case"))):
    """External trigger (Trigger JSON), installation-wide act.case; routed at once and answered with the case."""
    allowed = {"source", "key", "host", "ts", "env", "service", "severity", "title", "team_id", "family", "facts", "refs", "evidence"}
    body = dict(body)
    # evidence posted over the API is what a person or tool says, never a sender identity or a signed result (§4.3, C2)
    body["evidence"] = [{"kind": "pasted", "text": str(e.get("text") or "")[:4000], "host": str(e.get("host") or ""), "ts": e.get("ts"),
                         "fields": e.get("fields") if isinstance(e.get("fields"), dict) else {}}
                        for e in (body.get("evidence") or []) if isinstance(e, dict)][:20]
    try:
        trig = Trigger(**{k: v for k, v in body.items() if k in allowed})
    except (TypeError, ValueError) as e:
        raise err(422, "act_param_invalid", f"Geçersiz tetik: {e}", f"Invalid trigger: {e}")
    res = svc.act.cases.route(trig)
    svc.act.cases.flush()
    return res


@router.post("/cases/{cid}/assign")
def assign(cid: int, body: AssignIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    return svc.act.cases.assign(cid, body.owner or user["email"], user["email"])


def _move(cid, phase, svc, acc, sc, user, **fields):
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    return svc.act.cases.transition(cid, phase, user["email"], **fields)


@router.post("/cases/{cid}/decide", status_code=201)
def decide(cid: int, body: DecideIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    """The human choice for this attempt (runbook + params, NONE or ESCALATE, with a reason code when it differs from a visible
    proposal); reveals blind proposals (§8.4). Planning the run is WP8's: `plan` is null until then."""
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    return _shadow(svc.act.shadow.decide, cid, user["email"], body.choice, body.version, body.params, body.reason_code, body.note,
                   c.get("level_hint"))


@router.post("/cases/{cid}/peek")
def peek(cid: int, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    """"Öneriyi şimdi göster": reveal a blind proposal before deciding; the decision is then scored as visible."""
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    return svc.act.shadow.peek(cid, user["email"])


@router.post("/cases/{cid}/dismiss")
def dismiss(cid: int, body: ReasonIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    _shadow(svc.act.shadow.implicit, cid, user["email"], "NONE", body.reason)             # the attempt's decision (C10)
    return _move(cid, "dismissed", svc, acc, sc, user, resolution=body.reason, outcome="not_needed", needs_human=0)


@router.post("/cases/{cid}/escalate")
def escalate(cid: int, body: ReasonIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    _shadow(svc.act.shadow.implicit, cid, user["email"], "ESCALATE", body.reason)
    return _move(cid, "escalated", svc, acc, sc, user, resolution=body.reason, outcome="escalated")


@router.post("/cases/{cid}/suppress")
def suppress(cid: int, body: ReasonIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(minutes=max(5, min(int(body.minutes or 60), 7 * 1440)))).isoformat(timespec="seconds")
    return _move(cid, "suppressed", svc, acc, sc, user, resolution=body.reason, suppressed_until=until, needs_human=0)


@router.post("/cases/{cid}/reopen")
def reopen(cid: int, body: ReasonIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    return _move(cid, "ready", svc, acc, sc, user, needs_human=1, suppressed_until="")


@router.post("/cases/{cid}/close")
def close(cid: int, body: CloseIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    """Close a case worked by hand ("Ne yaptın?"): the text is scrubbed; a claim without verification earns no trust (§7.3)."""
    if body.outcome not in ("ok", "unverified", "not_needed", "escalated", "deviated"):
        raise err(422, "act_param_invalid", "Geçersiz sonuç", "Invalid outcome")
    what = "\n".join(str(x.get("text") or "")[:500] for x in (body.what_did_you_do or [])[:10] if x.get("text"))
    text = "\n".join(t for t in (body.resolution, what) if t)
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    _shadow(svc.act.shadow.implicit, cid, user["email"], "NONE" if body.outcome == "not_needed" else "MANUAL", text[:300])
    return _move(cid, "resolved", svc, acc, sc, user, resolution=text, outcome=body.outcome, needs_human=0)
