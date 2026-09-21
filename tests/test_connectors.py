"""HTTP and MCP connectors against local in-process servers; MCP server tool functions directly."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from watchover.connectors import McpClient, fetch_http, fetch_mcp, mcp_tools, parse_headers, dig
from watchover.pipeline import ingest_bytes

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    """Serves the demo zip, a JSON doc, and a minimal MCP JSON-RPC endpoint."""

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/demo.zip":
            self._send(200, (ROOT / "samples" / "demo_mixed.zip").read_bytes(), "application/zip")
        elif self.path == "/alerts":
            doc = {"status": "success", "data": {"alerts": [{"activeAt": "2026-09-16T14:31:20Z", "labels": {"severity": "warning", "job": "postgres"},
                                                             "annotations": {"summary": "latency high"}}]}}
            self._send(200, json.dumps(doc).encode(), "application/json")
        else:
            self._send(404, b"nope", "text/plain")

    def do_POST(self):
        assert self.headers.get("Authorization") == "Bearer secret"
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in msg:
            return self._send(202, b"", "text/plain")
        if msg["method"] == "initialize":
            res = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}).encode(), "application/json", {"Mcp-Session-Id": "s1"})
        assert self.headers.get("Mcp-Session-Id") == "s1"
        if msg["method"] == "tools/list":
            res = {"tools": [{"name": "export_logs", "description": "x", "inputSchema": {"type": "object"}}]}
        elif msg["method"] == "tools/call" and msg["params"]["name"] == "export_logs":
            lines = "\n".join(f'{{"ts":"2026-09-16T14:3{i}:00Z","level":"error","service":"api","msg":"timeout {i}"}}' for i in range(3))
            res = {"content": [{"type": "text", "text": lines}]}
        else:
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unknown"}}).encode(), "application/json")
        # answer as SSE to exercise the event-stream path
        body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': res})}\n\n".encode()
        self._send(200, body, "text/event-stream")


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_http_connector_zip_and_json_path():
    srv, base = serve()
    try:
        name, data = fetch_http(base + "/demo.zip")
        obs, _ = ingest_bytes(name, data)
        assert name.endswith(".zip") and len(obs) == 916
        name, data = fetch_http(base + "/alerts", json_path="data.alerts")
        obs, rep = ingest_bytes(name, data)
        assert rep[0]["format"] == "json" and len(obs) == 1 and obs[0].service == "postgres"
    finally:
        srv.shutdown()


def test_mcp_client_and_fetch():
    srv, base = serve()
    try:
        headers = parse_headers("Authorization: Bearer secret\nX-Team: sre")
        assert headers == {"Authorization": "Bearer secret", "X-Team": "sre"}
        assert [t["name"] for t in mcp_tools(base + "/mcp", headers)] == ["export_logs"]
        name, data = fetch_mcp(base + "/mcp", "export_logs", {}, headers)
        obs, rep = ingest_bytes(name, data)
        assert rep[0]["format"] == "jsonl" and len(obs) == 3 and obs[0].severity == "ERROR"
        c = McpClient(base + "/mcp", headers); c.initialize()
        try:
            c.call_tool("missing")
            assert False, "expected error"
        except RuntimeError as e:
            assert "unknown" in str(e)
    finally:
        srv.shutdown()


def test_dig():
    assert dig({"a": [{"b": 1}, {"b": 2}]}, "a[1].b") == 2


def test_mcp_server_tools_direct(tmp_path, monkeypatch):
    import importlib, sys
    sys.path.insert(0, str(ROOT))
    monkeypatch.chdir(tmp_path)
    import mcp_server
    importlib.reload(mcp_server)
    out = mcp_server.analyze_dataset(str(ROOT / "samples" / "demo_mixed.zip"))
    assert out["funnel"]["incidents"] == 2
    inc = mcp_server.list_incidents()[0]
    assert mcp_server.get_incident(inc["id"])["id"] == inc["id"]
    assert mcp_server.evidence(inc["evidence"][0])["ref"] == inc["evidence"][0]
    assert mcp_server.postmortem(inc["id"]).startswith("# Postmortem")
    a = mcp_server.create_action(inc["id"], "Failover db-01", "P1", "db-team")
    assert mcp_server.list_actions()[0]["id"] == a["id"]


def test_mcp_server_http_api_key(tmp_path, monkeypatch):
    """The HTTP transport refuses calls without MCP_API_KEY and serves them with it; /health stays open; relative dataset paths
    resolve under WATCHOVER_DATASETS."""
    import importlib, socket, threading, time, urllib.request, shutil
    monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "a.db")); monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WATCHOVER_DATASETS", str(tmp_path)); shutil.copy(ROOT / "samples" / "demo_mixed.zip", tmp_path / "demo.zip")
    import mcp_server
    importlib.reload(mcp_server)
    import uvicorn
    port = _free_port()
    cfg = uvicorn.Config(mcp_server.http_app(mcp_server.build_server(), api_key="s3cret"), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg); th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started: break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        assert urllib.request.urlopen(base + "/health").read() == b"ok"
        try:
            mcp_tools(base + "/mcp", {})
            assert False, "expected 401"
        except Exception as e:  # noqa: BLE001
            assert "401" in str(e)
        names = [t["name"] for t in mcp_tools(base + "/mcp", {"Authorization": "Bearer s3cret"})]
        assert "analyze_dataset" in names and "list_actions" in names
        c = McpClient(base + "/mcp", {"X-API-Key": "s3cret"}); c.initialize()
        out = c.call_tool("analyze_dataset", {"path": "demo.zip"})
        assert "raw_events" in out and "916" in out
    finally:
        srv.should_exit = True; th.join(timeout=5)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
