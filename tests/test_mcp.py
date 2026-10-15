"""MCP both ways. Giving: named keys bound to an account or a team (scope isolation), read-only unless written with the write
permission, MCP_API_KEY as an installation-wide read key, revocation, KVKK masking, the audit (no argument values), the
standalone server's write gate and forwarded tools. Pulling: an in-process fake MCP server (streamable HTTP with JSON and SSE
answers, the legacy HTTP+SSE transport) feeds pulls into the live store, the inventory and ITSM, with team ownership, limits,
timeouts and errors; the assistant calls only allowed read-only tools, with the question masked."""
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin@watchover.local", "Watchover!Admin2026")
PW = "Str0ng!Passw0rd"
TOKEN = "fake-token-123"


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    for k in ("DATABASE_URL", "MCP_API_KEY", "MCP_API_KEY_WRITE"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email in ("mgra@corp.com", "opb@corp.com"):
        svc.users.register(email, PW, name=email.split("@")[0].title(), role="viewer")
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def lines(*rows) -> bytes:
    now = datetime.now(UTC).isoformat()
    return ("\n".join(json.dumps({"ts": now, "level": "error", "service": "svc", **r}) for r in rows) + "\n").encode()


def teams(c):
    """Team A (a-*) managed by mgra, team B (b-*) with opb as operator; one line on a host of each."""
    svc, admin = c.svc, login(c, *ADMIN)
    svc.live.ingest("app.jsonl", lines({"host": "a-app-01", "msg": "AONLY disk full on a"}, {"host": "b-web-01", "msg": "BSECRET payroll timeout"},
                                       {"host": "a-app-01", "msg": "mail ali.veli@corp.com failed"}), "seed")
    ta = c.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    tb = c.post("/api/teams", json={"name": "B", "rules": [{"field": "host", "op": "glob", "value": "b-*"}]}, headers=admin).json()
    rid = lambda t, k: next(r["id"] for r in t["roles"] if r["key"] == k)   # noqa: E731
    for t, email, key in ((ta, "mgra@corp.com", "manager"), (tb, "opb@corp.com", "operator")):
        assert c.put(f"/api/teams/{t['id']}/members/{email}", json={"role_id": rid(t, key)}, headers=admin).status_code == 200
        svc.users.set_role(svc.users.get(email)["id"], "member", svc.roles.names())
    svc.teams.invalidate()
    return admin, ta, tb


def rpc(c, token, method, params=None, mid=1, path="/mcp"):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post(path, json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, headers=h)


def tool(c, token, name, args=None):
    r = rpc(c, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["isError"], json.loads(res["content"][0]["text"])


def new_key(c, h, **body):
    r = c.post("/api/mcp/keys", json={"name": "k", **body}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- giving
def test_team_key_sees_only_its_team_and_is_read_only(app):
    c = app
    admin, ta, tb = teams(c)
    k = new_key(c, admin, name="grafana-A", kind="team", team_id=ta["id"])
    tok = k["token"]
    assert tok.startswith("wom_") and k["key"]["write"] is False and "hash" not in k["key"]
    r = rpc(c, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18" and r.json()["result"]["serverInfo"]["name"] == "watchover"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Authorization": f"Bearer {tok}"}).status_code == 202
    names = {t["name"] for t in rpc(c, tok, "tools/list").json()["result"]["tools"]}
    assert {"log_search", "log_tail", "dash_infra", "dash_alarms", "dash_pools", "host_detail", "thresholds_state", "list_anomalies", "host_integrations",
            "get_trace", "trace_session", "list_flows", "inventory_lookup", "list_actions"} <= names
    assert not names & {"create_action", "ack_anomaly", "set_threshold"}             # read-only key: no write tools listed
    err, who = tool(c, tok, "whoami")
    assert not err and who["scope"] == "team:A" and who["teams"] == ["A"] and who["write"] is False

    err, out = tool(c, tok, "log_search", {"q": "", "last": 60, "limit": 100})
    txt = json.dumps(out)
    assert not err and "AONLY" in txt and "BSECRET" not in txt                     # team A never sees team B's host
    assert "ali.veli@corp.com" not in txt                                          # KVKK: masked
    err, out = tool(c, tok, "log_tail", {"limit": 50})
    assert not err and "BSECRET" not in json.dumps(out)
    err, out = tool(c, tok, "host_detail", {"host": "b-web-01"})
    assert err and out["error"] == "not_found"
    err, out = tool(c, tok, "host_detail", {"host": "a-app-01"})
    assert not err and out["host"] == "a-app-01" and "summary" in out and "breaches" in out
    err, out = tool(c, tok, "inventory_lookup", {"hostname": "b-web-01"})
    assert err and out["error"] == "not_found"
    for name in ("dash_infra", "dash_alarms", "dash_pools", "thresholds_state", "list_anomalies", "integration_hosts", "list_flows", "list_actions"):
        err, out = tool(c, tok, name, {})
        assert not err, (name, out)
        assert "BSECRET" not in json.dumps(out) and "b-web-01" not in json.dumps(out), name
    err, out = tool(c, tok, "create_action", {"title": "x"})                       # hidden and refused
    assert err and out["error"] == "read_only"
    err, out = tool(c, tok, "log_search", {"bogus": 1})
    assert err and out["error"] == "bad_args"
    assert rpc(c, tok, "tools/call", {"name": "nope"}).json()["error"]["code"] == -32602
    assert rpc(c, tok, "no/such").json()["error"]["code"] == -32601

    # the audit: who, tool, scope; argument names, never their values
    rows = c.get("/api/mcp/audit", headers=admin).json()
    mine = [r for r in rows if r["key_id"] == k["key"]["id"]]
    assert {"log_search", "host_detail", "create_action", "whoami"} <= {r["tool"] for r in mine}
    assert all(r["scope"] == "team:A" and r["principal"] == "team:A" for r in mine)
    assert any(r["tool"] == "create_action" and not r["ok"] and r["error"] == "read_only" for r in mine)
    assert "b-web-01" not in json.dumps(rows) and any(r["args"] == "last,limit,q" for r in mine)
    # revoked: refused at once
    assert c.delete(f"/api/mcp/keys/{k['key']['id']}", headers=admin).status_code == 200
    assert rpc(c, tok, "tools/list").status_code == 401


def test_write_key_user_key_and_legacy_key(app, monkeypatch):
    c = app
    admin, ta, tb = teams(c)
    w = new_key(c, admin, name="ops-bot", kind="team", team_id=ta["id"], write=True)["token"]
    names = {t["name"] for t in rpc(c, w, "tools/list").json()["result"]["tools"]}
    assert {"create_action", "ack_anomaly", "set_threshold"} <= names
    err, a = tool(c, w, "create_action", {"title": "Restart a-app-01 worker", "host": "a-app-01", "priority": "P1"})
    assert not err and a["host"] == "a-app-01"
    err, out = tool(c, w, "create_action", {"title": "spoof", "host": "b-web-01"})
    assert err and out["error"] == "not_found"
    err, out = tool(c, w, "set_threshold", {"metric": "cpu", "warn": 80, "crit": 95, "scope": "host", "scope_value": "b-web-01"})
    assert err and out["error"] == "not_found"                                     # never on another team's host
    err, p = tool(c, w, "set_threshold", {"metric": "cpu", "warn": 80, "crit": 95, "scope": "host", "scope_value": "a-app-01"})
    assert not err and p["team_id"] == ta["id"] and p["scope_value"] == "a-app-01"
    err, out = tool(c, w, "ack_anomaly", {"id": 999})
    assert err and out["error"] == "not_found"
    mgra = login(c, "mgra@corp.com")
    assert any(x["title"].startswith("Restart a-app-01") for x in c.get("/api/actions", headers=mgra).json())

    # a user key acts as that account: opb (team B) sees B only
    opb = login(c, "opb@corp.com")
    u = new_key(c, opb, name="my-cursor", kind="user")
    assert u["key"]["email"] == "opb@corp.com"
    err, out = tool(c, u["token"], "log_search", {"q": "", "last": 60})
    assert not err and "BSECRET" in json.dumps(out) and "AONLY" not in json.dumps(out)
    assert c.post("/api/mcp/keys", json={"name": "x", "kind": "team", "team_id": ta["id"]}, headers=opb).status_code == 403
    assert c.post("/api/mcp/keys", json={"name": "x", "kind": "user", "email": "mgra@corp.com"}, headers=opb).status_code == 403
    assert new_key(c, mgra, name="a-team", kind="team", team_id=ta["id"])["key"]["team_id"] == ta["id"]   # a team manager may
    assert c.post("/api/mcp/keys", json={"name": "x", "kind": "team", "team_id": tb["id"]}, headers=mgra).status_code == 403
    assert all(k["kind"] == "user" for k in c.get("/api/mcp/keys", headers=opb).json())     # B's operator does not see A's keys
    assert len(c.get("/api/mcp/keys", headers=admin).json()) == 3
    # a disabled account's key stops working
    svc = c.svc
    svc.kb._exec("UPDATE users SET status='disabled' WHERE email='opb@corp.com'")
    assert rpc(c, u["token"], "tools/list").status_code == 401

    # MCP_API_KEY: installation-wide READ key (sees every team), no write tools unless MCP_API_KEY_WRITE=1
    monkeypatch.setenv("MCP_API_KEY", "legacy-s3cret")
    names = {t["name"] for t in rpc(c, "legacy-s3cret", "tools/list", path="/api/mcp").json()["result"]["tools"]}
    assert "log_search" in names and "create_action" not in names
    err, out = tool(c, "legacy-s3cret", "log_search", {"q": "", "last": 60})
    assert "AONLY" in json.dumps(out) and "BSECRET" in json.dumps(out)
    assert rpc(c, "wrong", "tools/list").status_code == 401 and rpc(c, None, "tools/list").status_code == 401
    monkeypatch.setenv("MCP_API_KEY_WRITE", "1")
    assert "create_action" in {t["name"] for t in rpc(c, "legacy-s3cret", "tools/list").json()["result"]["tools"]}
    assert c.post("/mcp", content=b"{nope", headers={"Authorization": "Bearer legacy-s3cret"}).json()["error"]["code"] == -32700
    assert c.get("/mcp").status_code == 405
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer legacy-s3cret", "Origin": "http://evil.example"}).status_code == 403


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def test_standalone_server_named_keys_write_gate_and_forwarding(app, tmp_path, monkeypatch):
    """mcp_server.py (8765): MCP_API_KEY reads but does not write; a named write key of an administrator writes; a team key is
    refused the installation-wide dataset tools; with WATCHOVER_API_URL the live tools are forwarded with the caller's key."""
    import importlib, shutil, sys
    import uvicorn
    from watchover.mcpclient import Client, McpClientError, _text
    c = app
    admin, ta, tb = teams(c)
    team_tok = new_key(c, admin, name="t", kind="team", team_id=ta["id"])["token"]
    adm_w = new_key(c, admin, name="adm", kind="user", write=True)["token"]
    monkeypatch.setenv("WATCHOVER_DATASETS", str(tmp_path)); shutil.copy(ROOT / "samples" / "demo_mixed.zip", tmp_path / "demo.zip")
    api_port, mcp_port = _free_port(), _free_port()
    monkeypatch.setenv("WATCHOVER_API_URL", f"http://127.0.0.1:{api_port}")
    sys.path.insert(0, str(ROOT))
    import mcp_server
    importlib.reload(mcp_server)
    servers = []
    for asgi, port in ((c.app, api_port), (mcp_server.http_app(mcp_server.build_server(), api_key="s3cret"), mcp_port)):
        srv = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start(); servers.append(srv)
    for _ in range(200):
        if all(s.started for s in servers):
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{mcp_port}/mcp"
    try:
        with Client(url, headers={"Authorization": "Bearer s3cret"}) as m:
            names = {t["name"] for t in m.list_tools()}
            assert {"analyze_dataset", "create_action", "log_search", "host_detail"} <= names
            assert "funnel" in _text(m.call_tool("analyze_dataset", {"path": "demo.zip"}))
            with pytest.raises(McpClientError):
                m.call_tool("create_action", {"incident_id": "INC-1", "title": "x"})      # MCP_API_KEY is a read key
        with Client(url, headers={"Authorization": f"Bearer {adm_w}"}) as m:
            assert '"id"' in _text(m.call_tool("create_action", {"incident_id": "INC-1", "title": "via 8765"}))
        with Client(url, headers={"Authorization": f"Bearer {team_tok}"}) as m:
            with pytest.raises(McpClientError):
                m.call_tool("list_actions", {})                                           # installation-wide dataset tool
            out = _text(m.call_tool("log_search", {"q": "", "last": 60}))                  # forwarded with the team key
            assert "AONLY" in out and "BSECRET" not in out
        with pytest.raises(McpClientError):
            Client(url, headers={"Authorization": "Bearer wom_nope"}).initialize()
        tools = {r["tool"] for r in c.get("/api/mcp/audit", headers=admin).json()}
        assert {"analyze_dataset", "create_action", "log_search"} <= tools
    finally:
        for s in servers:
            s.should_exit = True


# ---------------------------------------------------------------- pulling: a fake external MCP server
class Fake:
    """Streamable HTTP at /mcp (JSON answers, SSE answers for get_hosts) and the legacy transport at /sse + /messages."""
    calls: list = []
    args: list = []
    now = ""

    TOOLS = [
        {"name": "get_logs", "description": "logs", "inputSchema": {"type": "object", "properties": {"since": {"type": "string"}}}},
        {"name": "get_hosts", "description": "cmdb", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_tickets", "description": "tickets", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "team_logs", "description": "a and b", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "search_docs", "description": "wiki", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
         "annotations": {"readOnlyHint": True}},
        {"name": "delete_all", "description": "danger", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
         "annotations": {"destructiveHint": True}},
        {"name": "slow", "description": "sleeps", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "big", "description": "300 KB", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "broken", "description": "isError", "inputSchema": {"type": "object", "properties": {}}},
    ]

    @staticmethod
    def result(name, args):
        Fake.calls.append(name); Fake.args.append(args)
        now = Fake.now                                                   # the same records twice: the second run adds nothing
        if name == "get_logs":
            return {"items": [{"timestamp": now, "level": "ERROR", "host": "ext-01", "service": "billing", "message": "EXTPULL invoice timeout", "order_id": "O-77"},
                              {"timestamp": now, "level": "INFO", "host": "ext-01", "service": "billing", "message": "EXTPULL retry ok", "attempt": 2}]}, None
        if name == "team_logs":
            return None, "\n".join(json.dumps({"ts": now, "host": h, "msg": f"APULL {h}"}) for h in ("a-app-01", "b-web-01"))
        if name == "get_hosts":
            return {"hosts": [{"hostname": "a-app-01", "ip": "10.0.0.5", "os": "RHEL 8.8", "owner": "ops-a"},
                              {"hostname": "b-web-01", "ip": "10.0.1.5", "os": "Ubuntu", "owner": "ops-b"},
                              {"name": "ext-02", "ip": "10.0.2.2", "role": "db"}]}, None
        if name == "get_tickets":
            return [{"id": "INC0099", "title": "billing invoice timeout on ext-01", "priority": "P2", "status": "open", "service": "billing",
                     "url": "https://itsm.example/INC0099"}], None
        if name == "search_docs":
            return None, f"Runbook: restart billing worker (asked: {args.get('query')})"
        if name == "delete_all":
            return None, "deleted"
        if name == "slow":
            time.sleep(2.5); return None, "late"
        if name == "big":
            return None, "x" * 300_000
        raise RuntimeError("tool failed")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_response(401); self.end_headers(); self.wfile.write(b"no"); return False
            return True

        def _answer(self, msg):
            m, p = msg.get("method"), msg.get("params") or {}
            if m == "initialize":
                return {"protocolVersion": p.get("protocolVersion"), "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
            if m == "tools/list":
                return {"tools": Fake.TOOLS}
            if m == "resources/list":
                return {"resources": [{"uri": "doc://runbook", "name": "runbook", "mimeType": "text/plain"}]}
            if m == "tools/call":
                try:
                    obj, text = Fake.result(p["name"], p.get("arguments") or {})
                except RuntimeError as e:
                    return {"content": [{"type": "text", "text": str(e)}], "isError": True}
                if obj is not None:
                    return {"content": [{"type": "text", "text": json.dumps(obj)}], "isError": False}
                return {"content": [{"type": "text", "text": text}], "isError": False}
            return None

        def do_POST(self):
            if not self._auth():
                return
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            u = urlparse(self.path)
            if u.path == "/messages":                                    # legacy SSE: answer on the stream
                q = Fake.streams[parse_qs(u.query)["sid"][0]]
                if "id" in msg:
                    q.append({"jsonrpc": "2.0", "id": msg["id"], "result": self._answer(msg)})
                self.send_response(202); self.end_headers(); return
            if "id" not in msg:
                self.send_response(202); self.end_headers(); return
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": self._answer(msg)})
            sse = msg.get("method") == "tools/call" and (msg.get("params") or {}).get("name") == "get_hosts"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Mcp-Session-Id", "sess-1")
            data = (f"event: message\ndata: {body}\n\n" if sse else body).encode()
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_DELETE(self):
            self.send_response(200); self.end_headers()

        def do_GET(self):
            if self.path != "/sse" or not self._auth():
                if self.path != "/sse":
                    self.send_response(404); self.end_headers()
                return
            sid = str(len(Fake.streams) + 1)
            q: list = []
            Fake.streams[sid] = q
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(f"event: endpoint\ndata: /messages?sid={sid}\n\n".encode()); self.wfile.flush()
            end = time.time() + 10
            while time.time() < end:
                while q:
                    self.wfile.write(f"event: message\ndata: {json.dumps(q.pop(0))}\n\n".encode()); self.wfile.flush()
                time.sleep(0.02)

    streams: dict = {}


@pytest.fixture
def fake():
    Fake.calls, Fake.args, Fake.streams, Fake.now = [], [], {}, datetime.now(UTC).isoformat()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake.H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def add_source(c, h, base, **kw):
    body = {"name": "ext", "url": base + "/mcp", "auth": "bearer", "secret": TOKEN, **kw}
    r = c.post("/api/mcp/sources", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def add_pull(c, h, sid, **kw):
    r = c.post(f"/api/mcp/sources/{sid}/pulls", json={"name": "p", "tool": "get_logs", **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_pull_logs_inventory_tickets_with_team_ownership(app, fake):
    c = app
    admin, ta, tb = teams(c)
    svc = c.svc
    s = add_source(c, admin, fake)
    assert "secret" not in s and s["has_secret"] is True
    raw = svc.kb._exec("SELECT secret FROM mcp_sources WHERE id=?", (s["id"],))[0]["secret"]
    assert raw != TOKEN                                                             # stored through the vault
    d = c.post(f"/api/mcp/sources/{s['id']}/discover", headers=admin).json()
    assert d["ok"] and "get_logs" in {t["name"] for t in d["tools"]} and d["resources"][0]["uri"] == "doc://runbook"

    # logs: JSON objects keep their fields, under the source name; a second run adds nothing new
    p = add_pull(c, admin, s["id"], name="billing logs", arguments={"since": "5m"}, source_name="billing-mcp", env="prod")
    r = c.post(f"/api/mcp/pulls/{p['id']}/run", headers=admin).json()
    assert r["ok"] and r["rows"] == 2, r
    assert Fake.args[-1] == {"since": "5m"}
    hit = [o for o in svc.live.buf if "EXTPULL invoice" in o.message]
    assert hit and hit[0].service == "billing" and hit[0].attributes.get("order_id") == "O-77"
    assert c.post(f"/api/mcp/pulls/{p['id']}/run", headers=admin).json()["rows"] == 0
    row = c.get("/api/mcp/sources", headers=admin).json()["sources"][0]["pulls"][0]
    assert row["status"] == "ok" and row["runs"] == 2 and row["total_rows"] == 2 and row["last_ok"]

    # inventory: installation-wide pull merges every host; a team's pull only touches the team's hosts
    inv = add_pull(c, admin, s["id"], name="cmdb", tool="get_hosts", target="inventory")
    assert c.post(f"/api/mcp/pulls/{inv['id']}/run", headers=admin).json()["rows"] == 3          # (answered as SSE)
    assert svc.inventory.get("ext-02")["role"] == "db" and svc.inventory.get("a-app-01")["os"] == "RHEL 8.8"
    sa = add_source(c, admin, fake, name="ext-A", team_id=ta["id"])
    assert sa["team"] == "A"
    svc.inventory.upsert({"hostname": "b-web-01", "os": "Debian"})
    ia = add_pull(c, admin, sa["id"], name="cmdb-A", tool="get_hosts", target="inventory")
    r = c.post(f"/api/mcp/pulls/{ia['id']}/run", headers=admin).json()
    assert r["rows"] == 1 and r["skipped"] == 2 and svc.inventory.get("b-web-01")["os"] == "Debian"

    # a team's log pull cannot post for another team's host
    la = add_pull(c, admin, sa["id"], name="logs-A", tool="team_logs")
    c.post(f"/api/mcp/pulls/{la['id']}/run", headers=admin)
    opb, mgra = login(c, "opb@corp.com"), login(c, "mgra@corp.com")
    seen_b = json.dumps(c.get("/api/logs/search", params={"q": "", "last": 60, "limit": 500}, headers=opb).json())
    assert "BSECRET" in seen_b and "APULL" not in seen_b
    seen_a = json.dumps(c.get("/api/logs/search", params={"q": "", "last": 60, "limit": 500}, headers=mgra).json())
    assert "APULL a-app-01" in seen_a

    # ITSM: the team's tickets for the team, installation-wide ones for everyone
    tk = add_pull(c, admin, sa["id"], name="tickets-A", tool="get_tickets", target="itsm")
    assert c.post(f"/api/mcp/pulls/{tk['id']}/run", headers=admin).json()["rows"] == 1
    ids = lambda h: {t["id"] for t in c.get("/api/itsm/tickets", headers=h).json()["tickets"]}   # noqa: E731
    assert "INC0099" in ids(admin) and "INC0099" in ids(mgra) and "INC0099" not in ids(opb)
    assert c.get("/api/mcp/sources", headers=opb).status_code == 403                # source settings are installation configuration (page.src)
    out = [r for r in c.get("/api/mcp/audit", params={"direction": "out"}, headers=admin).json()]
    assert {"ext/get_logs", "ext-A/get_hosts"} <= {r["tool"] for r in out} and all("5m" not in json.dumps(r) for r in out)


def test_pull_errors_limits_and_sse_transport(app, fake):
    c = app
    admin = login(c, *ADMIN)
    s = add_source(c, admin, fake, timeout_s=1, max_kb=64)
    for tool_name, code in (("slow", "timeout"), ("big", "too_large"), ("broken", "tool_error")):
        p = add_pull(c, admin, s["id"], name=tool_name, tool=tool_name)
        r = c.post(f"/api/mcp/pulls/{p['id']}/run", headers=admin).json()
        assert not r["ok"] and r["error"] == code, r
        row = next(x for x in c.get("/api/mcp/sources", headers=admin).json()["sources"][0]["pulls"] if x["id"] == p["id"])
        assert row["status"] == "error" and row["last_err"].startswith(code) and row["fails"] == 1
    bad = add_source(c, admin, fake, name="bad-token", secret="wrong")
    p = add_pull(c, admin, bad["id"], name="x")
    r = c.post(f"/api/mcp/pulls/{p['id']}/run", headers=admin).json()
    assert not r["ok"] and "401" in r["message"]
    assert c.post("/api/mcp/sources", json={"name": "cmd", "url": "stdio:///usr/bin/thing"}, headers=admin).status_code == 400
    assert c.post("/api/mcp/sources", json={"name": "cmd", "url": fake + "/mcp", "transport": "stdio"}, headers=admin).status_code == 400
    assert c.post(f"/api/mcp/sources/{s['id']}/pulls", json={"name": "x", "tool": "t", "target": "nowhere"}, headers=admin).status_code == 400
    p = add_pull(c, admin, s["id"], name="fast", interval_s=1)
    assert p["interval_s"] == 30                                                     # the floor
    # the legacy HTTP+SSE transport
    sse = add_source(c, admin, fake, name="legacy-sse", url=fake + "/sse", transport="sse", timeout_s=5)
    d = c.post(f"/api/mcp/sources/{sse['id']}/discover", headers=admin).json()
    assert d["ok"] and "get_logs" in {t["name"] for t in d["tools"]}
    p = add_pull(c, admin, sse["id"], name="via sse")
    assert c.post(f"/api/mcp/pulls/{p['id']}/run", headers=admin).json()["rows"] == 2


def test_assistant_calls_only_allowed_read_only_tools(app, fake):
    c = app
    admin, ta, tb = teams(c)
    s = add_source(c, admin, fake, assist_tools="search_docs, delete_all, get_logs_missing")
    c.post(f"/api/mcp/sources/{s['id']}/discover", headers=admin)
    Fake.calls.clear()
    r = c.post("/api/assist/ask", json={"question": "billing timeout? mail ali.veli@corp.com", "lang": "en"}, headers=admin).json()
    m = [x for x in r["sources"] if x["kind"] == "mcp"]
    assert m and m[0]["id"] == "M1" and m[0]["title"] == "ext/search_docs" and "Runbook" in m[0]["text"]
    assert "[M1]" in r["text"] or "[M1]" in r["context"]
    assert Fake.calls == ["search_docs"]                                            # destructive / unknown tools are never called
    assert "ali.veli@corp.com" not in json.dumps(Fake.args)                          # the question leaves masked
    assert c.post("/api/assist/ask", json={"question": "x", "mcp": False}, headers=admin).status_code == 200 and Fake.calls == ["search_docs"]
    # a team's assistant source is not asked for another team's member
    c.patch(f"/api/mcp/sources/{s['id']}", json={"enabled": False}, headers=admin)
    sa = add_source(c, admin, fake, name="docs-A", team_id=ta["id"], assist_tools="search_docs")
    c.post(f"/api/mcp/sources/{sa['id']}/discover", headers=admin)
    Fake.calls.clear()
    assert c.post("/api/assist/ask", json={"question": "billing"}, headers=login(c, "opb@corp.com")).status_code == 200 and Fake.calls == []
    assert c.post("/api/assist/ask", json={"question": "billing"}, headers=login(c, "mgra@corp.com")).status_code == 200 and Fake.calls == ["search_docs"]
