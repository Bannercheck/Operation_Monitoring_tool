from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import notify as wo_notify
from ..security import require, services

router = APIRouter(prefix="/notify", tags=["notifications"])


class RecipientIn(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    groups: str = ""
    note: str = ""


class GroupIn(BaseModel):
    name: str
    email: str = ""
    note: str = ""


class RuleIn(BaseModel):
    name: str
    condition: str
    threshold: float = 0
    env: str = ""
    severity: str = "high"
    channels: str = "email"
    targets: str = ""
    cooldown_min: int = 30


class TestIn(BaseModel):
    channel: str
    target: str


@router.get("/conditions")
def conditions(user=Depends(require("sys.notify"))):
    return list(wo_notify.CONDITIONS)


@router.get("/recipients")
def recipients(user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.notifier.recipients()


@router.post("/recipients", status_code=201)
def add_recipient(body: RecipientIn, user=Depends(require("sys.notify")), svc=Depends(services)):
    return {"id": svc.notifier.add_recipient(**body.model_dump())}


@router.patch("/recipients/{rid}")
def update_recipient(rid: int, body: dict, user=Depends(require("sys.notify")), svc=Depends(services)):
    svc.notifier.update_recipient(rid, **body); return {"ok": True}


@router.delete("/recipients/{rid}")
def delete_recipient(rid: int, user=Depends(require("sys.notify")), svc=Depends(services)):
    svc.notifier.delete_recipient(rid); return {"ok": True}


@router.get("/groups")
def groups(user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.notifier.groups()


@router.post("/groups", status_code=201)
def add_group(body: GroupIn, user=Depends(require("sys.notify")), svc=Depends(services)):
    return {"id": svc.notifier.add_group(**body.model_dump())}


@router.delete("/groups/{gid}")
def delete_group(gid: int, user=Depends(require("sys.notify")), svc=Depends(services)):
    svc.notifier.delete_group(gid); return {"ok": True}


@router.get("/rules")
def rules(user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.notifier.rules()


@router.post("/rules", status_code=201)
def add_rule(body: RuleIn, user=Depends(require("sys.notify")), svc=Depends(services)):
    return {"id": svc.notifier.add_rule(**body.model_dump())}


@router.patch("/rules/{rid}")
def update_rule(rid: int, body: dict, user=Depends(require("sys.notify")), svc=Depends(services)):
    svc.notifier.update_rule(rid, **body); return {"ok": True}


@router.delete("/rules/{rid}")
def delete_rule(rid: int, user=Depends(require("sys.notify")), svc=Depends(services)):
    svc.notifier.delete_rule(rid); return {"ok": True}


@router.get("/alerts")
def alerts(n: int = 50, user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.notifier.alerts(n)


@router.post("/test")
def test(body: TestIn, user=Depends(require("sys.notify")), svc=Depends(services)):
    ok, msg = svc.notifier.test_channel(body.channel, body.target)
    return {"ok": ok, "message": msg}


@router.post("/evaluate")
def evaluate(user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.alerts.evaluate()
