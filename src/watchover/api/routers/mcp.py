"""MCP both ways.

Giving (Ayarlar › Bağlantılar › MCP): named, revocable keys bound to an account or a team, read-only unless created with the
write permission; the tool catalogue; the audit of every call. The MCP endpoint itself is POST /mcp (and POST /api/mcp):
streamable HTTP with JSON answers, authenticated by a key (Authorization: Bearer wom_… or X-API-Key) or MCP_API_KEY.

Pulling (Ayarlar › Veri kaynakları › MCP kaynakları): external MCP servers, their tools / resources, and pulls (a tool call
with fixed arguments on an interval, ingested as log lines, inventory rows or ITSM tickets). A source belongs to a team or to
the installation; reading needs page.src, changing needs act.sources there (as for log sources)."""
from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ... import mcpclient, mcpgive, mcpkeys
from ..security import access, pick_team, require, services

router = APIRouter(prefix="/mcp", tags=["mcp"])
endpoint = APIRouter(tags=["mcp"])
MAX_BODY = 1_048_576


# ---------------------------------------------------------------- keys
class KeyIn(BaseModel):
    name: str
    kind: str = "user"                  # user | team
    email: Optional[str] = None         # user keys: default the caller
    team_id: Optional[int] = None       # team keys
    write: bool = False
    note: str = ""


def _key_visible(acc, k: dict) -> bool:
    if acc.install("act.connect"):
        return True
    if k["kind"] == "user":
        return k.get("email") == acc.email or k.get("created_by") == acc.email
    return k.get("team_id") is not None and acc.manages(k["team_id"])


def _teams(svc) -> dict:
    return {t["id"]: t["name"] for t in svc.teams.list()}


@router.get("/tools")
def tools(user=Depends(require("page.conn"))):
    """The tool catalogue: name, the permission it needs, read or write, a label."""
    return {"tools": [t.catalog() for t in mcpgive.TOOLS], "endpoint": "/mcp", "legacy_key": bool(os.environ.get("MCP_API_KEY"))}


@router.get("/keys")
def list_keys(acc=Depends(access), svc=Depends(services), user=Depends(require("page.conn"))):
    names = _teams(svc)
    return [{**k, "team": names.get(k.get("team_id"), "") if k.get("team_id") is not None else ""} for k in mcpkeys.store(svc).list() if _key_visible(acc, k)]


@router.post("/keys", status_code=201)
def create_key(body: KeyIn, acc=Depends(access), svc=Depends(services), user=Depends(require("page.conn"))):
    """A user key acts as that account (default: the caller; another account needs page.users installation-wide); a team key
    sees only that team's hosts and needs a team manager (or act.connect installation-wide). The clear key is returned once."""
    if body.kind == "user":
        email = (body.email or acc.email).strip().lower()
        if email != acc.email and not acc.install("page.users"):
            raise HTTPException(403, "a key for another account needs page.users installation-wide")
        u = svc.users.get(email)
        if not u or u.get("status") != "active":
            raise HTTPException(400, "no active account with that e-mail")
        row, clear = mcpkeys.store(svc).create(body.name, "user", email=email, write=body.write, created_by=acc.email, note=body.note)
    elif body.kind == "team":
        if body.team_id is None:
            raise HTTPException(400, "team_id is required for a team key")
        svc.teams.get(int(body.team_id))                                  # 404 for an unknown team
        if not (acc.manages(body.team_id) or acc.install("act.connect")):
            raise HTTPException(403, "a team key needs a manager of that team (or act.connect installation-wide)")
        row, clear = mcpkeys.store(svc).create(body.name, "team", team_id=int(body.team_id), write=body.write, created_by=acc.email, note=body.note)
    else:
        raise HTTPException(400, "kind must be user or team")
    try:
        svc.users._event(acc.email, "mcp_key", True, f"created {row['name']} ({row['kind']}{', write' if row['write'] else ''})"[:200])
    except Exception:  # noqa: BLE001
        pass
    return {"key": row, "token": clear, "endpoint": "/mcp"}


@router.delete("/keys/{kid}")
def revoke_key(kid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("page.conn"))):
    st = mcpkeys.store(svc)
    k = st.get(kid)
    if not _key_visible(acc, k):
        raise KeyError(kid)
    row = st.revoke(kid, acc.email)
    try:
        svc.users._event(acc.email, "mcp_key", True, f"revoked {row['name']}"[:200])
    except Exception:  # noqa: BLE001
        pass
    return row


@router.get("/audit")
def audit(n: int = 200, key_id: Optional[int] = None, direction: Optional[str] = None, acc=Depends(access), svc=Depends(services),
          user=Depends(require("page.conn"))):
    """Calls into Watchover (direction in) and out to MCP sources (out). Installation-wide act.connect / sys.security see every
    row; others the rows of the keys they see and of their teams' pulls."""
    st = mcpkeys.store(svc)
    rows = st.audit_list(min(max(n, 1), 1000) if acc.wide else 2000, key_id, direction or None)
    if acc.install("act.connect") or acc.install("sys.security"):
        return rows[:n]
    keys = {k["id"] for k in st.list() if _key_visible(acc, k)}
    teams = set(acc.team_ids("page.src")) | set(acc.managed())
    return [r for r in rows if (r.get("key_id") in keys) or (r.get("direction") == "out" and (r.get("principal") == acc.email or r.get("team_id") in teams))][:n]


# ---------------------------------------------------------------- MCP sources and pulls
class SourceIn(BaseModel):
    name: str
    url: str
    transport: str = "http"
    auth: str = "none"
    header_name: str = ""
    secret: str = ""
    headers: str = ""
    verify_tls: bool = True
    enabled: bool = True
    timeout_s: int = 15
    max_kb: int = 1024
    assist_tools: str = ""
    team_id: Optional[int] = None


class SourcePatch(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    transport: Optional[str] = None
    auth: Optional[str] = None
    header_name: Optional[str] = None
    secret: Optional[str] = None
    headers: Optional[str] = None
    verify_tls: Optional[bool] = None
    enabled: Optional[bool] = None
    timeout_s: Optional[int] = None
    max_kb: Optional[int] = None
    assist_tools: Optional[str] = None


class PullIn(BaseModel):
    name: str
    tool: str
    arguments: dict = {}
    interval_s: int = 300
    target: str = "logs"
    source_name: str = ""
    env: str = ""
    mapping: dict = {}
    max_rows: int = 1000
    enabled: bool = True


class PullPatch(BaseModel):
    name: Optional[str] = None
    tool: Optional[str] = None
    arguments: Optional[dict] = None
    interval_s: Optional[int] = None
    target: Optional[str] = None
    source_name: Optional[str] = None
    env: Optional[str] = None
    mapping: Optional[dict] = None
    max_rows: Optional[int] = None
    enabled: Optional[bool] = None


def _visible(acc, team_id) -> bool:
    return acc.wide or (team_id is not None and team_id in acc.team_ids("page.src"))


def _src(svc, acc, sid: int, write: bool = False) -> dict:
    s = mcpclient.store(svc).source(sid)
    if not _visible(acc, s.get("team_id")):
        raise KeyError(sid)
    tid = s.get("team_id")
    if write and not (acc.install("act.sources") if tid is None else acc.can_in(tid, "act.sources")):
        raise HTTPException(403, "permission required to change this MCP source")
    return s


def _out(svc, s: dict, names: dict) -> dict:
    s = dict(s)
    s["team"] = names.get(s.get("team_id"), "") if s.get("team_id") is not None else ""
    s["pulls"] = mcpclient.store(svc).pulls(s["id"])
    return s


@router.get("/sources")
def list_sources(acc=Depends(access), svc=Depends(services), user=Depends(require("page.src"))):
    names = _teams(svc)
    return {"sources": [_out(svc, s, names) for s in mcpclient.store(svc).sources() if _visible(acc, s.get("team_id"))],
            "transports": list(mcpclient.TRANSPORTS), "targets": list(mcpclient.TARGETS)}


@router.post("/sources", status_code=201)
def add_source(body: SourceIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    if acc.wide:
        if body.team_id is not None:
            svc.teams.get(int(body.team_id))
        tid = body.team_id
        if tid is None and not acc.install("act.sources"):
            raise HTTPException(403, "installation-wide permission required: act.sources")
    else:
        tid = pick_team(acc, "act.sources", body.team_id)
    s = mcpclient.store(svc).add_source(body.model_dump(exclude={"team_id"}), tid, acc.email)
    return _out(svc, s, _teams(svc))


@router.patch("/sources/{sid}")
def update_source(sid: int, body: SourcePatch, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    _src(svc, acc, sid, write=True)
    d = body.model_dump(exclude_none=True)
    if d.get("secret") == "•••":
        d.pop("secret")
    return _out(svc, mcpclient.store(svc).update_source(sid, d), _teams(svc))


@router.delete("/sources/{sid}", status_code=204)
def delete_source(sid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    _src(svc, acc, sid, write=True)
    mcpclient.store(svc).delete_source(sid)


@router.post("/sources/{sid}/discover")
def discover(sid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    """Connect and list the server's tools and resources (kept with the source)."""
    _src(svc, acc, sid, write=True)
    return mcpclient.discover(svc, sid)


@router.post("/sources/{sid}/pulls", status_code=201)
def add_pull(sid: int, body: PullIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    _src(svc, acc, sid, write=True)
    return mcpclient.store(svc).add_pull(sid, body.model_dump(), acc.email)


def _pull(svc, acc, pid: int) -> dict:
    p = mcpclient.store(svc).pull(pid)
    _src(svc, acc, p["source_id"], write=True)
    return p


@router.patch("/pulls/{pid}")
def update_pull(pid: int, body: PullPatch, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    _pull(svc, acc, pid)
    return mcpclient.store(svc).update_pull(pid, body.model_dump(exclude_none=True))


@router.delete("/pulls/{pid}", status_code=204)
def delete_pull(pid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    _pull(svc, acc, pid)
    mcpclient.store(svc).delete_pull(pid)


@router.post("/pulls/{pid}/run")
def run_pull(pid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("act.sources"))):
    """Run a pull now (its state and an audit row are recorded as for a scheduled run)."""
    _pull(svc, acc, pid)
    return mcpclient.runner(svc).run(pid)


# ---------------------------------------------------------------- the MCP endpoint
def _origin_ok(request: Request) -> bool:
    """DNS-rebinding guard of the MCP spec: a browser Origin must be this host (or one of WATCHOVER_CORS)."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = {o.strip().rstrip("/") for o in os.environ.get("WATCHOVER_CORS", "").split(",") if o.strip()}
    return origin.rstrip("/") in allowed or urlparse(origin).netloc == request.headers.get("host", "")


def _rpc_error(code: int, msg: str, status: int) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": msg}}, status_code=status)


async def _mcp_post(request: Request):
    if not _origin_ok(request):
        return _rpc_error(-32600, "origin not allowed", 403)
    svc = request.app.state.services
    ident = await run_in_threadpool(mcpkeys.identify, svc, mcpkeys.presented(request.headers))
    if ident is None:
        return JSONResponse({"error": "unauthorized: send Authorization: Bearer <MCP key> (Ayarlar › Bağlantılar › MCP)"}, status_code=401,
                            headers={"WWW-Authenticate": 'Bearer realm="watchover-mcp"'})
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return _rpc_error(-32600, "request too large", 413)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _rpc_error(-32600, "request too large", 413)
    try:
        body: Any = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        return _rpc_error(-32700, "parse error", 400)
    out = await run_in_threadpool(mcpgive.handle_body, svc, ident, body)
    if out is None:
        return Response(status_code=202)
    return JSONResponse(out)


def _mcp_get(request: Request):
    return Response(status_code=405, headers={"Allow": "POST"})


for _path in ("/mcp", "/api/mcp"):
    endpoint.add_api_route(_path, _mcp_post, methods=["POST"], include_in_schema=False)
    endpoint.add_api_route(_path, _mcp_get, methods=["GET", "DELETE"], include_in_schema=False)


def start(svc) -> None:
    """Start the pull runner with the other background jobs (not in tests / WATCHOVER_API_BACKGROUND=0)."""
    poller = getattr(svc, "poller", None)
    th = getattr(poller, "thread", None)
    if th is not None and th.is_alive():
        mcpclient.runner(svc).start()
