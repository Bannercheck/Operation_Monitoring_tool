"""Teams, their roles and their hosts: rules (name pattern, environment, inventory application / owner / role, agent site / tag, IP
network, service name) plus explicit hosts (the team's owned inventory) and members, each with one team role. A member sees only
the team's hosts everywhere; what they may do there is the team role (three template roles on creation) plus their installation
role. Installation admins (page.teams) manage every team; a team manager (team.manage in the team role) manages only their own
team's members, roles and explicit hosts, never beyond their own permissions."""
from __future__ import annotations

from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ... import rbac as wo_rbac
from ... import settings as wo_settings
from ...teams import FIELDS, OPS, OwnedError, RoleHeldError, RuleMatchedError, Teams
from ..security import access, current_user, services

router = APIRouter(prefix="/teams", tags=["teams"])
NOT_ADDABLE = "account not found or cannot be added"


class Rule(BaseModel):
    field: str
    op: str = "glob"
    value: str
    origin: Optional[str] = None


class MemberIn(BaseModel):
    email: str
    role: Optional[str] = None
    role_id: Optional[int] = None


class TeamIn(BaseModel):
    name: str
    description: str = ""
    rules: List[Rule] = []
    hosts: List[str] = []
    members: List[Union[str, MemberIn]] = []
    copy_roles_from: Optional[int] = None


class TeamRoleIn(BaseModel):
    label: str
    description: str = ""
    perms: List[str] = []


class MemberRoleIn(BaseModel):
    role_id: Optional[int] = None


class HostsIn(BaseModel):
    hosts: List[str]
    move: bool = False


def _admin(acc) -> bool:
    return "page.teams" in acc.global_perms


def _need_admin(acc) -> None:
    if not _admin(acc):
        raise HTTPException(403, "permission required: page.teams")


def _need_manage(acc, svc, tid: int) -> dict:
    """The team, when the caller manages it; 404 for a team they cannot see at all, 403 for a member without team.manage."""
    try:
        team = svc.teams.get(tid)
    except KeyError:
        raise KeyError(f"team {tid}")
    if acc.manages(tid):
        return team
    if acc.membership(tid) is not None:
        raise HTTPException(403, "permission required in that team: team.manage")
    raise KeyError(f"team {tid}")


def _audit(svc, user: dict, detail: str) -> None:
    try:
        svc.users._event(user.get("email", ""), "team", True, detail[:200])
    except Exception:  # noqa: BLE001 - the audit log never blocks the change
        pass


def _names(svc) -> dict:
    """users.id -> display name and e-mail -> (name, installation role, status): one read per request."""
    by_id, by_email = {}, {}
    for u in svc.users.list():
        nm = u.get("name") or u["email"].split("@")[0]
        by_id[u["id"]] = nm
        by_email[u["email"].lower()] = (nm, u.get("role") or "", u.get("status") or "")
    return {"id": by_id, "email": by_email}


def _view(svc, acc, t: dict, names: Optional[dict] = None) -> dict:
    """The Team document (see docs/design/team-roles.md 4.1), computed for the caller."""
    names = names or _names(svc)
    admin = _admin(acc)
    own = acc.membership(t["id"])
    own_perms = set(own.perms) if own else set()
    counts: dict = {}
    for m in t["memberships"]:
        counts[m["role_id"]] = counts.get(m["role_id"], 0) + 1
    labels = {r["id"]: r["label"] for r in t["roles"]}
    roles = []
    for r in t["roles"]:
        locked = not admin and ("team.manage" in r["perms"] or not acc.manages(t["id"]))
        roles.append({**r, "members": counts.get(r["id"], 0), "locked": locked})
    ms = []
    for m in t["memberships"]:
        nm, grole, _st = names["email"].get(m["email"], (m["email"].split("@")[0], "", ""))
        row = {"email": m["email"], "name": nm, "role_id": m["role_id"], "role_label": labels.get(m["role_id"], "")}
        if admin:
            gp = set(svc.roles.perms(grole)) if grole != "admin" else set(wo_rbac.ALL)
            tp = set(next((r["perms"] for r in t["roles"] if r["id"] == m["role_id"]), []))
            row.update({"global_role": grole, "wider": sorted((gp & wo_rbac.TEAM_PERMS) - tp - wo_rbac.TEAM_ONLY)})
        ms.append(row)
    out = {"id": t["id"], "name": t["name"], "description": t.get("description") or "", "rules": t["rules"], "hosts": t["hosts"], "matched": t["matched"],
           "host_meta": [{"host": h["host"], "origin": h["origin"], "added_by": names["id"].get(h.get("added_by"), "") if h.get("added_by") else "", "added_at": h["added_at"]}
                         for h in t["host_meta"]],
           "members": t["members"], "memberships": ms, "roles": roles, "default_role_id": t.get("default_role_id"),
           "owned": svc.teams.owned(t["id"]), "can_manage": acc.manages(t["id"]), "admin": admin,
           "own_perms": sorted(wo_rbac.TEAM_PERMS if admin else own_perms)}
    if admin:
        out["conflicts"] = [c for c in svc.teams.conflicts() if t["name"] in c["teams"]]
    return out


def _guard_role(acc, tid: int, role: Optional[dict], new_perms=None) -> None:
    """A team manager (without page.teams) may not touch a role holding team.manage, may not grant team.manage, and may grant only
    permissions their own team role holds (keeping what the role already had is fine)."""
    if _admin(acc):
        return
    m = acc.membership(tid)
    own = set(m.perms) if m else set()
    if role is not None and "team.manage" in role["perms"]:
        raise HTTPException(403, "only installation admins edit the team manager role")
    if new_perms is not None:
        p = wo_rbac.normalize(new_perms, team=True)
        if "team.manage" in p:
            raise HTTPException(403, "only installation admins grant team.manage")
        extra = p - set(role["perms"] if role else []) - own
        if extra:
            raise HTTPException(403, f"you cannot grant permissions you do not hold: {', '.join(sorted(extra))}")


def _need_within(acc, tid: int, perms, keep=()) -> None:
    """A team manager hands out (assigns, makes default, moves members into) only roles whose permissions they hold themselves;
    what the member already had (`keep`) may stay."""
    if _admin(acc):
        return
    m = acc.membership(tid)
    extra = set(perms) - (set(m.perms) if m else set()) - set(keep)
    if extra:
        raise HTTPException(403, f"you cannot hand out a role holding permissions you do not hold: {', '.join(sorted(extra))}")


# ---------------------------------------------------------------- static paths first
@router.get("")
def list_teams(acc=Depends(access), svc=Depends(services)):
    """Installation admins: every team. Team managers: the teams they manage. Others: 403."""
    if _admin(acc):
        teams = svc.teams.list()
    else:
        ids = set(acc.managed())
        if not ids:
            raise HTTPException(403, "permission required: page.teams")
        teams = [t for t in svc.teams.list() if t["id"] in ids]
    names = _names(svc)
    return {"teams": [_view(svc, acc, t, names) for t in teams], "fields": FIELDS, "ops": OPS}


@router.get("/permissions")
def permissions(user=Depends(current_user)):
    """The permission catalogue grouped like the sidebar, with the three role templates."""
    return wo_rbac.catalogue()


@router.get("/mine")
def mine(user: dict = Depends(current_user), svc=Depends(services)):
    """The signed-in user's view: all hosts, or the teams and how many hosts they cover."""
    sc = svc.scope_of(user)
    return {"all": sc is None, "teams": sc.teams if sc else [t["name"] for t in svc.teams.teams_of(user["email"])], "hosts": len(sc.hosts) if sc else None}


@router.get("/known-hosts")
def known_hosts(acc=Depends(access), svc=Depends(services)):
    """Every host Watchover knows with the facts the rules can match (for building and checking rules)."""
    _need_admin(acc)
    return [{"host": h, **{k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items()}} for h, d in sorted(svc.teams.facts().items())]


@router.get("/unowned")
def unowned(acc=Depends(access), svc=Depends(services)):
    """Known hosts no team sees: an installation admin assigns them to a team."""
    _need_admin(acc)
    return svc.teams.unowned()


@router.post("/preview")
def preview(body: TeamIn, acc=Depends(access), svc=Depends(services)):
    """Which hosts a draft team would own (before saving)."""
    _need_admin(acc)
    rules = Teams._clean_rules([r.model_dump() for r in body.rules])
    return {"hosts": sorted(svc.teams.team_hosts({"rules": rules, "hosts": body.hosts}))}


def _members(body: TeamIn) -> list:
    return [m if isinstance(m, str) else m.model_dump() for m in body.members]


@router.post("", status_code=201)
def create(body: TeamIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    _need_admin(acc)
    t = svc.teams.save(body.name, body.description, [r.model_dump(exclude_none=True) for r in body.rules], body.hosts, _members(body),
                       copy_roles_from=body.copy_roles_from, by=user["id"])
    _audit(svc, user, f"{t['name']}: created ({len(t['roles'])} roles, {len(t['members'])} members)")
    return _view(svc, svc.access_of(user), t)


# ---------------------------------------------------------------- one team
@router.get("/{tid}")
def get_team(tid: int, acc=Depends(access), svc=Depends(services)):
    return _view(svc, acc, _need_manage(acc, svc, tid))


@router.put("/{tid}")
def update(tid: int, body: TeamIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    _need_admin(acc)
    svc.teams.get(tid)
    t = svc.teams.save(body.name, body.description, [r.model_dump(exclude_none=True) for r in body.rules], body.hosts, _members(body), tid=tid, by=user["id"])
    _audit(svc, user, f"{t['name']}: team saved (rules {len(t['rules'])}, hosts {len(t['hosts'])}, members {len(t['members'])})")
    return _view(svc, svc.access_of(user), t)


@router.delete("/{tid}", status_code=204)
def delete(tid: int, transfer: Optional[str] = None, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    _need_admin(acc)
    t = svc.teams.get(tid)
    try:
        svc.teams.delete(tid, transfer=transfer or None)
    except OwnedError as e:
        return JSONResponse({"detail": str(e), "owned": e.owned}, status_code=409)
    _audit(svc, user, f"{t['name']}: deleted" + (f" (records -> {transfer})" if transfer else ""))


# ---------------------------------------------------------------- roles
@router.post("/{tid}/roles", status_code=201)
def add_role(tid: int, body: TeamRoleIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    _guard_role(acc, tid, None, body.perms)
    r = svc.teams.create_role(tid, body.label, body.description, body.perms)
    _audit(svc, user, f"{t['name']}: role {r['key']} created ({len(r['perms'])} perms)")
    return r


@router.put("/{tid}/roles/{rid}")
def save_role(tid: int, rid: int, body: TeamRoleIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    cur = svc.teams.role(tid, rid)
    _guard_role(acc, tid, cur, body.perms)
    r = svc.teams.update_role(tid, rid, body.label, body.description, body.perms)
    if set(r["perms"]) != set(cur["perms"]) or r["label"] != cur["label"]:
        _audit(svc, user, f"{t['name']}: role {r['key']} perms changed ({len(cur['perms'])} -> {len(r['perms'])})")
    return r


@router.post("/{tid}/roles/{rid}/reset")
def reset_role(tid: int, rid: int, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    cur = svc.teams.role(tid, rid)
    tpl = wo_rbac.TEAM_TEMPLATES.get(cur.get("template") or "")
    _guard_role(acc, tid, cur, tpl["perms"] if tpl else None)
    r = svc.teams.reset_role(tid, rid)
    _audit(svc, user, f"{t['name']}: role {r['key']} reset to template")
    return r


@router.post("/{tid}/roles/{rid}/default")
def default_role(tid: int, rid: int, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    cur = svc.teams.role(tid, rid)
    _guard_role(acc, tid, cur)
    _need_within(acc, tid, cur["perms"])
    svc.teams.set_default_role(tid, rid)
    _audit(svc, user, f"{t['name']}: default role {cur['key']}")
    return {"default_role_id": rid}


@router.delete("/{tid}/roles/{rid}", status_code=204)
def delete_role(tid: int, rid: int, move_to: Optional[int] = None, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    cur = svc.teams.role(tid, rid)
    _guard_role(acc, tid, cur)
    if move_to and not _admin(acc):
        target = svc.teams.role(tid, move_to)
        _guard_role(acc, tid, target)
        _need_within(acc, tid, target["perms"], keep=cur["perms"])
    try:
        svc.teams.delete_role(tid, rid, move_to)
    except RoleHeldError as e:
        return JSONResponse({"detail": str(e), "held": e.held}, status_code=409)
    _audit(svc, user, f"{t['name']}: role {cur['key']} deleted")


# ---------------------------------------------------------------- members
@router.get("/{tid}/members")
def members(tid: int, acc=Depends(access), svc=Depends(services)):
    """Names and team roles (assignee pickers). E-mails only for the team's managers and installation admins."""
    try:
        t = svc.teams.get(tid)
    except KeyError:
        raise KeyError(f"team {tid}")
    if not (_admin(acc) or acc.membership(tid) is not None):
        raise KeyError(f"team {tid}")
    names = _names(svc)
    labels = {r["id"]: r["label"] for r in t["roles"]}
    show = acc.manages(tid)
    return [{"name": names["email"].get(m["email"], (m["email"].split("@")[0],))[0], "role_id": m["role_id"], "role_label": labels.get(m["role_id"], ""),
             **({"email": m["email"]} if show else {})} for m in t["memberships"]]


def _member_guard(acc, t: dict, email: str, new_role: Optional[dict]) -> None:
    if _admin(acc):
        return
    if email == acc.email:
        raise HTTPException(403, "you cannot change your own membership or role")
    if new_role is not None and "team.manage" in new_role["perms"]:
        raise HTTPException(403, "only installation admins assign the team manager role")
    cur = next((m for m in t["memberships"] if m["email"] == email), None)
    cur_role = None
    if cur is not None:
        cur_role = next((r for r in t["roles"] if r["id"] == cur["role_id"]), None)
        if cur_role is not None and "team.manage" in cur_role["perms"]:
            raise HTTPException(403, "only installation admins change a team manager")
    if new_role is not None:
        _need_within(acc, t["id"], new_role["perms"], keep=cur_role["perms"] if cur_role else ())


@router.put("/{tid}/members/{email}")
def set_member(tid: int, email: str, body: MemberRoleIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    """Add an existing account (by exact e-mail) or change a member's team role. Unknown, disabled and pending accounts all answer
    the same 404: no way to probe which accounts exist."""
    t = _need_manage(acc, svc, tid)
    email = email.strip().lower()
    u = svc.users.get(email)
    if not u or (u.get("status") or "") != "active":
        raise HTTPException(404, NOT_ADDABLE)
    rid = body.role_id or t.get("default_role_id")
    role = svc.teams.role(tid, rid) if rid else None
    _member_guard(acc, t, email, role)
    new = email not in t["members"]
    if new and not _admin(acc):                              # joining a team narrows an account that sees every host today: admins only
        tacc = svc.access_of({"email": email, "role": u.get("role") or ""})
        if tacc.wide and not tacc.all and set(tacc.global_perms) - set(wo_rbac.MEMBER_ROLE["perms"]):
            raise HTTPException(403, "this account sees every host today: an installation admin adds it to a team")
    m = svc.teams.set_member(tid, email, role["id"] if role else None)
    role = role or svc.teams.role(tid, m["role_id"])
    _audit(svc, user, f"{t['name']}: {'+member' if new else 'member role'} {role['key']}")
    return {**m, "name": u.get("name") or email.split("@")[0], "role_label": role["label"]}


@router.delete("/{tid}/members/{email}", status_code=204)
def remove_member(tid: int, email: str, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    email = email.strip().lower()
    if email not in t["members"]:
        raise HTTPException(404, NOT_ADDABLE)
    _member_guard(acc, t, email, None)
    svc.teams.remove_member(tid, email)
    _audit(svc, user, f"{t['name']}: -member")


# ---------------------------------------------------------------- hosts
@router.post("/{tid}/hosts")
def add_hosts(tid: int, body: HostsIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    """Add explicit hosts under the claim rules: hosts the team already sees, brand-new hosts, unowned ones only for installation
    admins (or with teams_self_claim). Another team's host is rejected without naming that team."""
    t = _need_manage(acc, svc, tid)
    added, rejected = svc.teams.claim(tid, body.hosts, "manual", by=user["id"], admin=_admin(acc), move=body.move and _admin(acc),
                                      self_claim=bool(wo_settings.load().get("teams_self_claim")))
    for h in added:
        if h not in t["hosts"]:
            _audit(svc, user, f"{t['name']}: host claimed {h} (manual)")
    return {"added": added, "rejected": rejected}


@router.delete("/{tid}/hosts/{host}", status_code=204)
def remove_host(tid: int, host: str, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_manage(acc, svc, tid)
    try:
        svc.teams.release(tid, host)
    except RuleMatchedError as e:
        raise HTTPException(409, str(e))
    _audit(svc, user, f"{t['name']}: host released {host}")


# ---------------------------------------------------------------- team enrolment key
def _need_connect(acc, svc, tid: int) -> dict:
    t = svc.teams.get(tid)
    if not acc.can_in(tid, "act.connect"):
        if acc.membership(tid) is not None or _admin(acc):
            raise HTTPException(403, "permission required in that team: act.connect")
        raise KeyError(f"team {tid}")
    return t


@router.get("/{tid}/enroll-key")
def enroll_key(tid: int, acc=Depends(access), svc=Depends(services)):
    _need_connect(acc, svc, tid)
    k = svc.teams.enroll_key(tid)
    return {"key": k, "enabled": bool(k)}


@router.post("/{tid}/enroll-key/rotate")
def rotate_enroll_key(tid: int, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_connect(acc, svc, tid)
    k = svc.teams.rotate_enroll_key(tid)
    _audit(svc, user, f"{t['name']}: enrolment key rotated")
    return {"key": k}


@router.delete("/{tid}/enroll-key")
def disable_enroll_key(tid: int, user=Depends(current_user), acc=Depends(access), svc=Depends(services)):
    t = _need_connect(acc, svc, tid)
    svc.teams.disable_enroll_key(tid)
    _audit(svc, user, f"{t['name']}: enrolment key disabled")
    return {"ok": True}
