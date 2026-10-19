from __future__ import annotations

from typing import List, Optional

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
    role: Optional[str] = None             # default: operator; "member" (Ekip üyesi) when a team is given
    team_id: Optional[int] = None
    team_role_id: Optional[int] = None


class UserPatch(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    name: Optional[str] = None


class PasswordIn(BaseModel):
    password: str


class RoleIn(BaseModel):
    perms: List[str]
    label: str = ""
    description: str = ""


@router.get("/users")
def list_users(user=Depends(require("sys.users" if "sys.users" in wo_rbac.PERMISSIONS else "page.users")), svc=Depends(services)):
    """Accounts, each with its team memberships ({"id", "name", "role_label"})."""
    mem: dict = {}
    for t in svc.teams.list():
        labels = {r["id"]: r["label"] for r in t["roles"]}
        for m in t["memberships"]:
            mem.setdefault(m["email"], []).append({"id": t["id"], "name": t["name"], "role_id": m["role_id"], "role_label": labels.get(m["role_id"], "")})
    return [{**u, "teams": mem.get((u.get("email") or "").lower(), [])} for u in svc.users.list()]


@router.post("/users", status_code=201)
def add_user(body: UserIn, user=Depends(require("page.users")), svc=Depends(services)):
    if body.team_id is not None:
        if "page.teams" not in svc.access_of(user).global_perms:      # team membership (and its role) is the teams page's business
            raise HTTPException(403, "permission required: page.teams")
        svc.teams.get(body.team_id)
        if body.team_role_id is not None:
            svc.teams.role(body.team_id, body.team_role_id)
        svc.roles.ensure_member()
    role = body.role or ("member" if body.team_id is not None else "operator")
    if user["role"] != "admin" and role == "admin":
        raise HTTPException(403, "only an administrator may create administrator accounts")
    u = svc.users.register(body.email, body.password, body.name)
    svc.users.activate(int(u["id"]))
    if role in svc.roles.names():
        svc.users.set_role(int(u["id"]), role, svc.roles.names())
    if body.team_id is not None:
        svc.teams.set_member(body.team_id, body.email, body.team_role_id)
        svc.users._event(user.get("email", ""), "team", True, f"{svc.teams.get(body.team_id)['name']}: +member (new account)")
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
        raise HTTPException(400, wo_auth.policy_detail(why))
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
def events(n: int = 100, email: Optional[str] = None, user=Depends(require("sys.security")), svc=Depends(services)):
    return svc.users.events(n, email or None)


@router.get("/roles")
def roles(user=Depends(require("page.users")), svc=Depends(services)):
    """Installation roles, the flat permission map (compatibility) and the catalogue grouped like the sidebar (`sections`)."""
    cat = wo_rbac.catalogue()
    return {"roles": svc.roles.list(), "permissions": {k: {"group": v[0], "tr": v[1], "en": v[2]} for k, v in wo_rbac.PERMISSIONS.items()},
            "sections": cat["sections"]}


@router.put("/roles/{name}")
def save_role(name: str, body: RoleIn, user=Depends(require("page.users")), svc=Depends(services)):
    if user["role"] != "admin":
        if name == "admin":
            raise HTTPException(403, "only an administrator may edit the administrator role")
        held = svc.roles.perms(user["role"])
        extra = wo_rbac.normalize(body.perms) - set(held)
        if extra:                                            # no privilege escalation: cannot grant a permission you do not hold
            raise HTTPException(403, f"you cannot grant permissions you do not hold: {', '.join(sorted(extra))}")
    return svc.roles.save(name, body.perms, body.label, body.description)


@router.post("/roles/{name}/reset")
def reset_role(name: str, user=Depends(require("page.users")), svc=Depends(services)):
    svc.roles.reset(name); return svc.roles.get(name)


@router.delete("/roles/{name}")
def delete_role(name: str, user=Depends(require("page.users")), svc=Depends(services)):
    svc.roles.delete(name); return {"ok": True}
