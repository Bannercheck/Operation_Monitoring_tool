"""Watchover as an MCP server: any MCP client (Claude SAKA, Claude Desktop, Cursor, custom agents, this dashboard's
own Connection panel) can drive the engine over standard JSON-RPC.

    pip install "mcp>=2"
    python mcp_server.py --http --port 8765   # streamable HTTP at http://localhost:8765/mcp  (default for Docker)
    python mcp_server.py                      # stdio transport for desktop clients
    docker compose up mcp                     # see docker-compose.yml

Desktop client config example:
    {"mcpServers": {"watchover": {"command": "python", "args": ["/abs/path/hackathon/mcp_server.py"]}}}
Remote/HTTP client config example:  {"url": "http://<host>:8765/mcp", "headers": {"Authorization": "Bearer <MCP_API_KEY>"}}

Environment: MCP_API_KEY  - when set, HTTP clients must send it (Authorization: Bearer … or X-API-Key); unset = open, keep it on localhost
             WATCHOVER_DATASETS - folder that relative dataset paths resolve against (Docker: /data/datasets)

Tools: analyze_dataset(path), list_incidents(), get_incident(id), list_signals(limit), evidence(ref),
       postmortem(id), create_action(...), list_actions(). Deterministic engine, no LLM inside.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("src")))

from watchover.actions import ActionStore           # noqa: E402
from watchover.analysis import Analysis, incident_dict, postmortem_md, signal_dict   # noqa: E402
from watchover.pipeline import ingest_path          # noqa: E402
from watchover.profiler import profile              # noqa: E402

STATE: dict = {"analysis": None, "dataset": None}
STORE = ActionStore(os.environ["DATABASE_URL"] if os.environ.get("DATABASE_URL") else os.environ.get("ACTIONS_DB", "actions.db"))   # same database as the dashboard


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


def build_server():
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("watchover", instructions="Operational noise -> signals -> explained incidents. Load a dataset with analyze_dataset first.")
    for fn in TOOLS:
        server.tool()(fn)
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
        bind = "0.0.0.0" if os.environ.get("MCP_API_KEY") else "127.0.0.1"
        if bind == "127.0.0.1":
            print("MCP_API_KEY not set: binding MCP HTTP to 127.0.0.1 only (set MCP_API_KEY to expose on the network)", file=sys.stderr)
        uvicorn.run(http_app(server, host=bind), host=bind, port=args.port, log_level="info")
    else:
        server.run()
    return 0


def http_app(server, api_key: str | None = None, host: str = "0.0.0.0"):
    """The streamable-HTTP ASGI app, gated by MCP_API_KEY when one is set (Bearer or X-API-Key); /health stays open for Docker."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse, PlainTextResponse
    inner = server.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),   # any client / Docker
                                       host=host)
    key = os.environ.get("MCP_API_KEY", "") if api_key is None else api_key

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/health":
            return await PlainTextResponse("ok")(scope, receive, send)
        if key and scope["type"] == "http":
            hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            given = hdrs.get("x-api-key") or hdrs.get("authorization", "").removeprefix("Bearer ").strip()
            if not secrets.compare_digest(given, key):
                return await JSONResponse({"error": "unauthorized: send Authorization: Bearer <MCP_API_KEY>"}, status_code=401)(scope, receive, send)
        return await inner(scope, receive, send)
    return app


if __name__ == "__main__":
    sys.exit(main())
