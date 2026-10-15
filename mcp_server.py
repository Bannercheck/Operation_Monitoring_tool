"""Watchover as an MCP server: any MCP client (Claude SAKA, Claude Desktop, Cursor, custom agents, this dashboard's
own Connection panel) can drive the engine over standard JSON-RPC.

    pip install "mcp>=2"
    python mcp_server.py --http --port 8765   # streamable HTTP at http://localhost:8765/mcp  (default for Docker)
    python mcp_server.py                      # stdio transport for desktop clients
    docker compose up mcp                     # see docker-compose.yml

Desktop client config example:
    {"mcpServers": {"watchover": {"command": "python", "args": ["/abs/path/hackathon/mcp_server.py"]}}}
Remote/HTTP client config example:  {"url": "http://<host>:8765/mcp", "headers": {"Authorization": "Bearer <MCP_API_KEY>"}}

Environment: MCP_API_KEY  - installation-wide READ key (Authorization: Bearer … or X-API-Key); unset = open, keep it on localhost
             MCP_API_KEY_WRITE=1 - MCP_API_KEY may also write (create_action), as before named keys existed
             MCP_REQUIRE_KEY=1 - require a key (MCP_API_KEY or a named key) even without MCP_API_KEY, and listen on the network
             WATCHOVER_DATASETS - folder that relative dataset paths resolve against (Docker: /data/datasets)
             WATCHOVER_API_URL - e.g. http://watchover:8501 : also offer the live product's tools (logs, dashboards, host detail,
                                 thresholds, anomalies, traces, flows, inventory, actions) by forwarding them, with the caller's key,
                                 to the API's own MCP endpoint (/mcp), which applies the key's team / account scope

Named keys (Ayarlar › Bağlantılar › MCP, stored hashed in the product database) are accepted too. Dataset tools work on the
installation's dataset folder, so they need MCP_API_KEY or an administrator's key; team keys use the forwarded tools.
Every HTTP call is audited (mcp_audit). Writes (create_action) need a key with the write permission.

Tools: analyze_dataset(path), list_incidents(), get_incident(id), list_signals(limit), evidence(ref),
       postmortem(id), create_action(...), list_actions(). Deterministic engine, no LLM inside.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import secrets
import sys
import time
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("src")))

from watchover.actions import open_store            # noqa: E402
from watchover.db import Database, default_url      # noqa: E402
from watchover.analysis import Analysis, incident_dict, postmortem_md, signal_dict   # noqa: E402
from watchover.pipeline import ingest_path          # noqa: E402
from watchover.profiler import profile              # noqa: E402

STATE: dict = {"analysis": None, "dataset": None}
STORE = open_store(Database(default_url()))         # the dashboard's shared database (DATABASE_URL, else KNOWLEDGE_DB / knowledge.db)


def _need() -> Analysis:
    if STATE["analysis"] is None:
        raise ValueError("no dataset loaded; call analyze_dataset(path) first")
    return STATE["analysis"]


# ---- tool implementations (plain functions, also unit-testable without the mcp package)
def _resolve(path: str) -> str:
    """Confine dataset paths to WATCHOVER_DATASETS (or, if unset, the current working directory). Absolute paths and
    ``..`` traversal that escape the root are refused, so the tool cannot read arbitrary files such as data/.api.key."""
    root = os.environ.get("WATCHOVER_DATASETS")
    if not root:
        return path                                          # local stdio use: the operator already has shell access, no confinement
    base = os.path.realpath(root)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("path is outside the allowed dataset directory")
    return full


def analyze_dataset(path: str) -> dict:
    """Ingest a file / ZIP / directory and run the full pipeline. Returns the funnel and a short profile.
    Relative paths are resolved under WATCHOVER_DATASETS (Docker: the ./datasets folder next to docker-compose.yml)."""
    path = _resolve(path)
    obs, report = ingest_path(path)
    a = Analysis(obs, report)
    STATE.update(analysis=a, dataset=path)
    prof = profile(obs, report)
    prof.pop("per_minute", None)
    return {"dataset": path, "funnel": a.funnel(), "files": report, "time_range": prof.get("time_range"),
            "services": prof.get("services"), "hosts": prof.get("hosts")}


def list_incidents() -> list[dict]:
    """Ranked incident candidates with root cause, score factors, evidence refs and recommendations."""
    return [incident_dict(i) for i in _need().incidents]


def get_incident(incident_id: str) -> dict:
    a = _need()
    inc = a.incident_by_id.get(incident_id)
    if not inc:
        raise ValueError(f"unknown incident {incident_id}")
    return incident_dict(inc)


def list_signals(limit: int = 50) -> list[dict]:
    """Signals ranked by burst score and severity."""
    return [signal_dict(s) for s in _need().signals[:limit]]


def evidence(ref: str) -> dict:
    """Raw observation behind an evidence reference such as 'app.jsonl:540'."""
    o = _need().obs_by_ref.get(ref)
    if not o:
        raise ValueError(f"unknown ref {ref}")
    return {"ref": o.ref, "timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service,
            "host": o.host, "message": o.message, "attributes": o.attributes, "template": o.template}


def postmortem(incident_id: str) -> str:
    a = _need()
    return postmortem_md(get_incident_obj(a, incident_id), a.signal_by_id)


def get_incident_obj(a: Analysis, incident_id: str):
    inc = a.incident_by_id.get(incident_id)
    if not inc:
        raise ValueError(f"unknown incident {incident_id}")
    return inc


def create_action(incident_id: str, title: str, priority: str = "P2", owner: str = "", recommendation: str = "") -> dict:
    return STORE.create(incident_id, title, priority, owner, recommendation)


def list_actions() -> list[dict]:
    return STORE.list()


TOOLS = [analyze_dataset, list_incidents, get_incident, list_signals, evidence, postmortem, create_action, list_actions]
WRITE_TOOLS = {"create_action"}
IDENT_HEADER = "x-watchover-mcp-ident"        # set by http_app after the key check; a client-sent copy is dropped


def _db():
    """The product database (named keys, audit); None when it cannot be opened."""
    if STATE.get("db") is None:
        try:
            from watchover.db import Database
            from watchover.mcpkeys import McpKeys
            STATE["db"] = McpKeys(Database())
        except Exception:  # noqa: BLE001
            STATE["db"] = False
    return STATE["db"] or None


def _who(given: str, env_key: str) -> dict | None:
    """A presented key -> {name, principal, scope, write, wide, key_id}; None when unknown."""
    if env_key and given and secrets.compare_digest(given.encode(), env_key.encode()):
        return {"name": "MCP_API_KEY", "principal": "install", "scope": "all", "write": os.environ.get("MCP_API_KEY_WRITE", "") == "1", "wide": True, "key_id": None}
    keys = _db() if given else None
    row = keys.verify(given) if keys else None
    if not row:
        return None
    wide = False
    if row["kind"] == "user":
        u = keys.kb._exec("SELECT role, status FROM users WHERE email=?", (row["email"],))
        if not u or u[0].get("status") != "active":
            return None
        wide = u[0].get("role") == "admin"
    keys.touch(row["id"])
    return {"name": row["name"], "principal": row["email"] or f"team:{row['team_id']}", "scope": "all" if wide else f"{row['kind']}:{row['email'] or row['team_id']}",
            "write": bool(row["can_write"]), "wide": wide, "key_id": row["id"]}


def _ident(ctx) -> dict:
    hdrs = getattr(ctx, "headers", None) if ctx is not None else None
    if hdrs is None:                                         # stdio: the local operator
        return {"name": "stdio", "principal": "local", "scope": "all", "write": True, "wide": True, "key_id": None}
    try:
        return json.loads({str(k).lower(): v for k, v in dict(hdrs).items()}.get(IDENT_HEADER) or "{}") or {}
    except ValueError:
        return {}


def _audit(who: dict, tool: str, args, ok: bool, err: str, t0: float) -> None:
    keys = _db()
    if keys and who.get("principal") not in (None, "local"):
        from types import SimpleNamespace
        ident = SimpleNamespace(key_id=who.get("key_id"), name=who.get("name", ""), principal=who.get("principal", ""), scope=who.get("scope", ""), team_id=None)
        keys.audit(ident, tool, args, ok, err, int((time.perf_counter() - t0) * 1000))


def _guarded(fn):
    """The tool with a Context parameter: dataset tools need an installation-wide identity, writes the write permission; audited."""
    from mcp.server.mcpserver import Context
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()]
    params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))

    def tool(*args, ctx=None, **kw):
        who, t0 = _ident(ctx), time.perf_counter()
        try:
            if not who.get("wide"):
                raise PermissionError("dataset tools need MCP_API_KEY or an administrator's key (team keys: use the live tools)")
            if fn.__name__ in WRITE_TOOLS and not who.get("write"):
                raise PermissionError("this key is read-only: create a key with the write (yazma) permission")
            out = fn(*args, **kw)
        except Exception as e:
            _audit(who, fn.__name__, kw.keys(), False, type(e).__name__, t0)
            raise
        _audit(who, fn.__name__, kw.keys(), True, "", t0)
        return out
    tool.__name__, tool.__doc__ = fn.__name__, fn.__doc__
    tool.__signature__ = sig.replace(parameters=params, return_annotation=hints.get("return", sig.return_annotation))
    tool.__annotations__ = {**hints, "ctx": Context}
    return tool


def _forwarder(t):
    """A live-product tool (watchover.mcpgive) forwarded to WATCHOVER_API_URL/mcp with the caller's own key."""
    from mcp.server.mcpserver import Context
    types_ = {"string": str, "integer": int, "number": float, "boolean": bool}
    params = []
    for name, p in sorted(t.props.items(), key=lambda kv: kv[0] not in t.required):
        typ = types_.get(p.get("type"), str)
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=typ) if name in t.required else
                      inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=typing.Optional[typ]))
    params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))

    def tool(ctx=None, **kw):
        from watchover.mcpclient import Client, _text
        from watchover.mcpkeys import presented
        hdrs = getattr(ctx, "headers", None) if ctx is not None else None
        key = presented(dict(hdrs)) if hdrs is not None else os.environ.get("MCP_API_KEY", "")
        with Client(os.environ["WATCHOVER_API_URL"].rstrip("/") + "/mcp", "http", {"Authorization": f"Bearer {key}"}, timeout=30, max_bytes=4 << 20) as c:
            res = c.rpc("tools/call", {"name": t.name, "arguments": {k: v for k, v in kw.items() if v is not None}})
        if res.get("isError"):
            raise ValueError(_text(res)[:500])
        return _text(res)
    tool.__name__, tool.__doc__ = t.name, t.description
    tool.__signature__ = inspect.Signature(params, return_annotation=str)
    tool.__annotations__ = {**{p.name: p.annotation for p in params}, "return": str}
    return tool


def build_server():
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("watchover", instructions="Operational noise -> signals -> explained incidents. Load a dataset with analyze_dataset first.")
    for fn in TOOLS:
        server.add_tool(_guarded(fn), name=fn.__name__, description=fn.__doc__)
    if os.environ.get("WATCHOVER_API_URL"):
        from watchover.mcpgive import TOOLS as LIVE
        for t in LIVE:
            if t.name not in {f.__name__ for f in TOOLS}:
                server.add_tool(_forwarder(t), name=t.name, description=t.description)
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    try:
        server = build_server()
    except ImportError:
        print("The 'mcp' package is missing: pip install \"mcp>=2\"", file=sys.stderr)
        return 1
    if args.http:
        import uvicorn
        # Without an API key the endpoint is unauthenticated, so it is bound to loopback only; set MCP_API_KEY to expose it.
        bind = "0.0.0.0" if (os.environ.get("MCP_API_KEY") or os.environ.get("MCP_REQUIRE_KEY") == "1") else "127.0.0.1"
        if bind == "127.0.0.1":
            print("MCP_API_KEY / MCP_REQUIRE_KEY not set: binding MCP HTTP to 127.0.0.1 only (set one to expose on the network)", file=sys.stderr)
        uvicorn.run(http_app(server, host=bind), host=bind, port=args.port, log_level="info")
    else:
        server.run()
    return 0


def http_app(server, api_key: str | None = None, host: str = "0.0.0.0"):
    """The streamable-HTTP ASGI app. A key is required when MCP_API_KEY (or `api_key`) is set or MCP_REQUIRE_KEY=1: MCP_API_KEY
    (installation-wide read) or a named key (Bearer or X-API-Key). The checked identity travels to the tools in a header the
    client cannot forge (any copy it sends is dropped); /health stays open for Docker."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse, PlainTextResponse
    from watchover.mcpkeys import presented
    inner = server.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),   # any client / Docker
                                       host=host)
    key = os.environ.get("MCP_API_KEY", "") if api_key is None else api_key
    need = bool(key) or os.environ.get("MCP_REQUIRE_KEY") == "1"

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/health":
            return await PlainTextResponse("ok")(scope, receive, send)
        if scope["type"] == "http":
            raw = [(k, v) for k, v in scope.get("headers", []) if k.decode().lower() != IDENT_HEADER]
            given = presented({k.decode(): v.decode() for k, v in raw})
            who = _who(given, key) if given else None
            if who is None:
                if need or given:
                    return await JSONResponse({"error": "unauthorized: send Authorization: Bearer <MCP_API_KEY or MCP key>"}, status_code=401)(scope, receive, send)
                who = {"name": "open", "principal": "loopback", "scope": "all", "write": True, "wide": True, "key_id": None}   # no key configured: localhost only
            scope = dict(scope, headers=raw + [(IDENT_HEADER.encode(), json.dumps(who).encode())])
        return await inner(scope, receive, send)
    return app


if __name__ == "__main__":
    sys.exit(main())
