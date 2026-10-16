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


# ---------------------------------------------------------------- runs and steps (WP8, act/engine.py)
class RunIn(BaseModel):
    runbook: str = ""
    version: Optional[int] = None
    params: Dict[str, Any] = {}


class StepDoneIn(BaseModel):
    result: str = "ok"
    output: str = ""
    note: str = ""


class TextIn(BaseModel):
    text: str = ""


class ChooseIn(BaseModel):
    to: str


class UnknownIn(BaseModel):
    choice: str


def _engine(fn, *a, **k):
    from ...act.engine import EngineError
    try:
        return fn(*a, **k)
    except EngineError as e:
        raise err(e.status, e.code, e.tr, e.en)
    except KeyError:
        raise err(404, "act_not_found", "Bulunamadı", "Not found")


def _run_case(svc, acc, sc, rid: int) -> dict:
    run = svc.act.engine.run(rid)
    c = _case(svc, acc, sc, run["case_id"])
    _write_guard(svc, acc, c)
    return run


@router.post("/cases/{cid}/runs", status_code=201)
def run_start(cid: int, body: RunIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    """"Başlat": a guided run of the decided (or named) runbook; diagnose / verify steps run through the actuator when they may."""
    c = _case(svc, acc, sc, cid)
    _write_guard(svc, acc, c)
    return _engine(svc.act.engine.start, cid, user["email"], body.runbook or None, body.version, body.params)


@router.get("/runs/{rid}")
def run_get(rid: int, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions"))):
    run = _engine(svc.act.engine.get, rid)
    _case(svc, acc, sc, run["case_id"])
    return run


@router.post("/runs/{rid}/steps/{sid}/done")
def step_done(rid: int, sid: int, body: StepDoneIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
              user=Depends(current_user)):
    """"Yaptım" / paste: the claim is stored next to the verified result and never replaces it."""
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.done, rid, sid, user["email"], body.result, body.output, body.note)


@router.post("/runs/{rid}/steps/{sid}/deviate")
def step_deviate(rid: int, sid: int, body: TextIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
                 user=Depends(current_user)):
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.deviate, rid, sid, user["email"], body.text)


@router.post("/runs/{rid}/steps/{sid}/skip")
def step_skip(rid: int, sid: int, body: TextIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
              user=Depends(current_user)):
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.skip, rid, sid, user["email"], body.text)


@router.post("/runs/{rid}/steps/{sid}/choose")
def step_choose(rid: int, sid: int, body: ChooseIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
                user=Depends(current_user)):
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.choose, rid, sid, user["email"], body.to)


@router.post("/runs/{rid}/steps/{sid}/takeover")
def step_takeover(rid: int, sid: int, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    """"Bana bırak"."""
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.takeover, rid, sid, user["email"])


@router.post("/runs/{rid}/steps/{sid}/unknown")
def step_unknown(rid: int, sid: int, body: UnknownIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
                 user=Depends(current_user)):
    """"Başarılı say" (count_ok) · "Tekrar dene" (retry) · "Bana bırak" (takeover)."""
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.resolve_unknown, rid, sid, user["email"], body.choice)


@router.post("/runs/{rid}/params")
def run_params(rid: int, body: Dict[str, Any], svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")),
               user=Depends(current_user)):
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.answer, rid, user["email"], body)


@router.post("/runs/{rid}/proceed")
def run_proceed(rid: int, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    """"Yine de devam" for a precondition that asked."""
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.proceed, rid, user["email"])


@router.post("/runs/{rid}/abort")
def run_abort(rid: int, body: TextIn, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions")), user=Depends(current_user)):
    _run_case(svc, acc, sc, rid)
    return _engine(svc.act.engine.abort, rid, user["email"], body.text)


# ---------------------------------------------------------------- the model selector (WP13)
@router.post("/cases/{cid}/explain")
def case_explain(cid: int, svc=Depends(act_on), acc=Depends(access), sc=Depends(perm_scope("page.actions"))):
    """Lazy model explanation (queued on the act-llm worker; ask again for the text). Never for blind or injection-suspect cases."""
    c = _case(svc, acc, sc, cid)
    if svc.act.shadow.hidden(c):
        raise err(409, "act_blind", "Karar verilmeden açıklama yok", "No explanation before the decision")
    props = [p for p in svc.act.shadow.proposals(cid, svc.act.shadow.attempt_of(c)) if not str(p["selector"]).startswith("rules")]
    if props and props[-1].get("explain"):
        return {"explain": props[-1]["explain"], "queued": False}
    return {"explain": "", "queued": svc.act.selector.request_explain(cid), "model": svc.act.selector.state()}


@router.get("/model")
def model_state(svc=Depends(act_on), user=Depends(require_install("page.actions"))):
    return {**svc.act.selector.state(), "queue": len(svc.act.selector.queue), "stats": svc.act.selector.stats}


@router.post("/model/probe")
def model_probe(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    """Speed probe of the configured local model (≈ 1 000-token prompt, 32-token answer): timeout clamp and auto-off."""
    return svc.act.selector.probe()


# ---------------------------------------------------------------- demo controls (act_demo only, §17)
@router.post("/demo/disk", status_code=201)
def demo_disk(host: str = "web-prod-07", svc=Depends(act_on), user=Depends(require_install("act.case"))):
    from ...act import demo
    try:
        return demo.disk(svc.act, host=host, reviewer=user["email"])
    except demo.DemoError as e:
        raise err(409, e.code, e.detail()["tr"], e.detail()["en"])


@router.post("/demo/run-guided/{cid}")
def demo_run_guided(cid: int, svc=Depends(act_on), user=Depends(require_install("act.case"))):
    from ...act import demo
    try:
        return demo.run_guided(svc.act, cid, by=user["email"])
    except demo.DemoError as e:
        raise err(409, e.code, e.detail()["tr"], e.detail()["en"])
