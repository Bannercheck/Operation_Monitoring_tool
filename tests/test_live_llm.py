"""Live receiver/store/simulator, agent posting, and the OpenAI-compatible LLM client against a fake server."""
import json
import random
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from signal_sprint.live import LiveStore, simulate_batch, start_receiver, start_simulator
from signal_sprint.llm import LLMConfig, chat, list_models
from signal_sprint.llm import test_connection as llm_test_connection

ROOT = Path(__file__).resolve().parents[1]


def test_receiver_auth_formats_and_stats(tmp_path):
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, "k1")
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/ingest"
    try:
        def post(body: bytes, headers: dict):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        assert post(simulate_batch(random.Random(1), 5), {"X-API-Key": "k1", "X-Agent": "host-a"}) == {"accepted": 5}
        syslog = b"Sep 16 14:31:02 db-01 postgres[123]: latency increased\n"
        assert post(syslog, {"Authorization": "Bearer k1", "X-File-Name": "db.log"})["accepted"] == 1
        try:
            post(b"x", {"X-API-Key": "wrong"})
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 401
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
            assert json.loads(r.read())["received"] == 6
        s = store.stats(15)
        assert s["total"] == 6 and set(s["agents"]) == {"host-a", "127.0.0.1"} and len(s["rows"]) == 15 * 5
        assert all(o.timestamp.year >= 2026 for o in store.snapshot())   # syslog line without year got a sane time
        name, data = store.to_dataset()
        assert name.endswith(".jsonl") and data.count(b"\n") == 6
        assert (tmp_path / "live.jsonl").read_text().count("\n") == 6
    finally:
        srv.shutdown()


def test_simulator_feeds_store():
    store = LiveStore()
    stop = start_simulator(store, interval=0.02)
    import time
    time.sleep(0.3)
    stop.set()
    assert store.received >= 8 and "simulator" in store.agents


def test_agent_file_mode(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    import agent
    store = LiveStore()
    srv = start_receiver(store, 0, None)
    port = srv.server_address[1]
    try:
        assert agent.main(["--url", f"http://127.0.0.1:{port}/ingest", "--file", str(ROOT / "samples" / "demo_mixed.zip"), "--agent", "ci"]) == 0
        assert store.received == 914 and store.agents.get("ci")
    finally:
        srv.shutdown()


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: dict):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        assert self.path == "/v1/models"
        self._send({"data": [{"id": "llama3.1"}, {"id": "mistral"}]})

    def do_POST(self):
        assert self.path == "/v1/chat/completions" and self.headers.get("Authorization") == "Bearer tok"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["model"] == "llama3.1" and body["messages"][1]["role"] == "user"
        self._send({"choices": [{"message": {"role": "assistant", "content": "Root cause: db latency [db.log:46]."}}]})


def test_llm_client():
    srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = LLMConfig(f"http://127.0.0.1:{srv.server_address[1]}/v1", "llama3.1", "tok")
    try:
        assert cfg.enabled and list_models(cfg) == ["llama3.1", "mistral"]
        ok, info = llm_test_connection(cfg)
        assert ok and "2 model" in info
        assert chat(cfg, "why?").startswith("Root cause")
        assert not LLMConfig().enabled
        assert llm_test_connection(LLMConfig("http://127.0.0.1:1/v1", "x"))[0] is False
    finally:
        srv.shutdown()
