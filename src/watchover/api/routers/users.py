from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ... import auth as wo_auth
from ... import rbac as wo_rbac
from ..security import require, services

router = APIRouter(tags=["users"])


class UserIn(BaseModel):
    email: str
    password: str
    name: str = ""
    role: str = "operator"


class UserPatch(BaseModel):
    role: str | None = None
    status: str | None = None
    name: str | None = None


class PasswordIn(BaseModel):
    password: str


class RoleIn(BaseModel):
    perms: list[str]
    label: str = ""
    description: str = ""


@router.get("/users")
def list_users(user=Depends(require("sys.users" if "sys.users" in wo_rbac.PERMISSIONS else "page.users")), svc=Depends(services)):
    return svc.users.list()


@router.post("/users", status_code=201)
def add_user(body: UserIn, user=Depends(require("page.users")), svc=Depends(services)):
    u = svc.users.register(body.email, body.password, body.name)
    svc.users.activate(int(u["id"]))
    if body.role in svc.roles.names():
        svc.users.set_role(int(u["id"]), body.role, svc.roles.names())
    return svc.users.get(body.email) | {"pw_hash": "•••"}


@router.patch("/users/{uid}")
def update_user(uid: int, body: UserPatch, user=Depends(require("page.users")), svc=Depends(services)):
    target = next((u for u in svc.users.list() if int(u["id"]) == uid), None)
    if target is None:
        raise KeyError(uid)
    is_admin = user["role"] == "admin"
    if int(user["id"]) == uid and (body.role is not None or body.status is not None):
        raise HTTPException(403, "you cannot change your own role or status")   # no self-escalation / self-lockout
    if not is_admin and (target.get("role") == "admin" or body.role == "admin"):
        raise HTTPException(403, "only an administrator may manage administrator accounts")
    if body.role is not None:
        svc.users.set_role(uid, body.role, svc.roles.names())
    if body.status is not None:
        svc.users.set_status(uid, body.status)
    if body.name is not None:
        svc.users.set_name(uid, body.name)
    rows = [u for u in svc.users.list() if int(u["id"]) == uid]
    if not rows:
        raise KeyError(uid)
    return rows[0]


@router.post("/users/{uid}/password")
def set_password(uid: int, body: PasswordIn, user=Depends(require("page.users")), svc=Depends(services)):
    target = next((u for u in svc.users.list() if int(u["id"]) == uid), None)
    if target is None:
        raise KeyError(uid)
    if user["role"] != "admin" and target.get("role") == "admin" and int(user["id"]) != uid:
        raise HTTPException(403, "only an administrator may reset an administrator's password")
    why = wo_auth.password_policy(body.password)
    if why:
        raise HTTPException(400, why)
    svc.users.change_password(uid, body.password); return {"ok": True}


@router.post("/users/{uid}/unlock")
def unlock(uid: int, user=Depends(require("page.users")), svc=Depends(services)):
    rows = [u for u in svc.users.list() if int(u["id"]) == uid]
    if not rows:
        raise KeyError(uid)
    svc.users.unlock(rows[0]["email"]); return {"ok": True}


@router.delete("/users/{uid}")
def delete_user(uid: int, user=Depends(require("page.users")), svc=Depends(services)):
    if int(user["id"]) == uid:
        raise ValueError("you cannot delete your own account")
    svc.users.delete(uid); return {"ok": True}


@router.get("/users/events")
def events(n: int = 100, email: str | None = None, user=Depends(require("sys.security")), svc=Depends(services)):
    return svc.users.events(n, email or None)


@router.get("/roles")
def roles(user=Depends(require("page.users")), svc=Depends(services)):
    return {"roles": svc.roles.list(), "permissions": {k: {"group": v[0], "tr": v[1], "en": v[2]} for k, v in wo_rbac.PERMISSIONS.items()}}


@router.put("/roles/{name}")
def save_role(name: str, body: RoleIn, user=Depends(require("page.users")), svc=Depends(services)):
    if user["role"] != "admin":
        if name == "admin":
            raise HTTPException(403, "only an administrator may edit the administrator role")
        held = svc.roles.perms(user["role"])
        extra = set(body.perms) - set(held)
        if extra:                                            # no privilege escalation: cannot grant a permission you do not hold
            raise HTTPException(403, f"you cannot grant permissions you do not hold: {', '.join(sorted(extra))}")
    return svc.roles.save(name, body.perms, body.label, body.description)


@router.post("/roles/{name}/reset")
def reset_role(name: str, user=Depends(require("page.users")), svc=Depends(services)):
    svc.roles.reset(name); return svc.roles.get(name)


@router.delete("/roles/{name}")
def delete_role(name: str, user=Depends(require("page.users")), svc=Depends(services)):
    svc.roles.delete(name); return {"ok": True}
