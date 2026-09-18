"""Provider-agnostic LLM client (OpenAI-compatible, Ollama native, Anthropic, Gemini), call metrics, Ollama manager, grounding, benchmark."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from watchover import analysis as an, llm, ollama
from watchover.knowledge import Knowledge
from watchover.llm import LLMConfig
from watchover.llm_eval import grounding, run_benchmark
from watchover.pipeline import ingest_path

ROOT = Path(__file__).resolve().parents[1]


class Multi(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        Multi.seen.append(("GET", self.path, dict(self.headers)))
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen2.5:7b-instruct", "size": 4.7e9, "details": {"family": "qwen2", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"}}, {"name": "bge-m3", "size": 1.2e9, "details": {}}]})
        elif self.path == "/api/ps":
            self._send({"models": [{"name": "qwen2.5:7b-instruct", "size_vram": 5e9, "expires_at": "2026-09-18T12:00:00Z"}]})
        elif self.path == "/v1/models":
            self._send({"data": [{"id": "gpt-x"}, {"id": "claude-x"}]})
        elif self.path == "/v1beta/models":
            self._send({"models": [{"name": "models/gemini-2.0-flash"}]})
        else:
            self._send({"error": "nf"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        Multi.seen.append(("POST", self.path, body))
        if self.path == "/api/chat":
            self._send({"message": {"role": "assistant", "content": "ollama says [INC-1]"}})
        elif self.path == "/api/embeddings":
            self._send({"embedding": [0.1, 0.2, 0.3]})
        elif self.path == "/api/pull":
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
            for ev in ({"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 50}, {"status": "success"}):
                self.wfile.write((json.dumps(ev) + "\n").encode())
        elif self.path == "/v1/messages":
            self._send({"content": [{"type": "text", "text": "claude says [L1]"}]})
        elif self.path.endswith(":generateContent"):
            self._send({"candidates": [{"content": {"parts": [{"text": "gemini says [S1]"}]}}]})
        elif self.path == "/v1/chat/completions":
            self._send({"choices": [{"message": {"role": "assistant", "content": "openai says S1"}}]})
        elif self.path == "/v1/embeddings":
            self._send({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
        else:
            self._send({"error": "nf"}, 404)


def _srv():
    srv = HTTPServer(("127.0.0.1", 0), Multi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_providers_and_metrics_sink():
    srv, base = _srv()
    recs = []
    llm.set_sink(recs.append)
    try:
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
        assert LLMConfig("http://x:11434", "m").kind == "ollama" and LLMConfig("http://x:11434/v1", "m").kind == "openai"
        assert LLMConfig("https://api.anthropic.com", "m").kind == "anthropic" and LLMConfig("https://generativelanguage.googleapis.com", "m").kind == "gemini"
        assert llm.chat_messages(LLMConfig(base, "qwen2.5:7b-instruct", provider="ollama"), msgs) == "ollama says [INC-1]"
        assert llm.chat_messages(LLMConfig(base, "claude-x", "k", provider="anthropic"), msgs) == "claude says [L1]"
        assert llm.chat_messages(LLMConfig(base, "gemini-2.0-flash", "k", provider="gemini"), msgs) == "gemini says [S1]"
        assert llm.chat_messages(LLMConfig(base + "/v1", "gpt-x", "tok"), msgs) == "openai says S1"
        assert llm.embed(LLMConfig(base, "m", provider="ollama", embed_model="bge-m3"), ["a"]) == [[0.1, 0.2, 0.3]]
        assert llm.embed(LLMConfig(base + "/v1", "m", embed_model="e"), ["a"]) == [[1.0, 0.0]]
        assert llm.list_models(LLMConfig(base, "m", provider="ollama")) == ["qwen2.5:7b-instruct", "bge-m3"]
        assert llm.list_models(LLMConfig(base, "m", provider="gemini")) == ["gemini-2.0-flash"]
        assert llm.list_models(LLMConfig(base + "/v1", "gpt-x", "tok")) == ["gpt-x", "claude-x"]
        anth = next(h for m, p, h in Multi.seen if p == "/v1/messages")
        assert anth.get("system") == "sys" and anth["messages"][0]["role"] == "user"
        hdr = next(h for m, p, h in Multi.seen if p == "/v1/models")
        assert hdr.get("Authorization") == "Bearer tok"
        try:
            llm.chat_messages(LLMConfig("http://127.0.0.1:1", "x", provider="openai"), msgs)
        except Exception:
            pass
        assert len(recs) == 7 and all("latency_ms" in r for r in recs)
        assert [r["provider"] for r in recs[:4]] == ["ollama", "anthropic", "gemini", "openai"] and recs[-1]["ok"] == 0 and recs[-1]["error"]
        assert {r["kind"] for r in recs} == {"chat", "embed"}
    finally:
        llm.set_sink(None); srv.shutdown()


def test_ollama_manager():
    srv, base = _srv()
    try:
        assert ollama.discover((base,)) == base and ollama.discover(("http://127.0.0.1:1",)) is None
        inst = ollama.installed(base)
        assert inst[0]["name"] == "qwen2.5:7b-instruct" and inst[0]["size_gb"] == 4.7 and inst[0]["quant"] == "Q4_K_M"
        assert ollama.running(base)[0]["name"] == "qwen2.5:7b-instruct"
        evs = list(ollama.pull(base, "x"))
        assert evs[1]["pct"] == 0.5 and evs[-1]["status"] == "success"
    finally:
        srv.shutdown()


def test_grounding_and_benchmark(tmp_path):
    g = grounding("Root cause [INC-1], see [db.log:62] and [L9]; also [INC-7].", "[INC-1] ... [db.log:62] ... [L9] ...")
    assert g == {"citations": 4, "grounded": 3, "invalid": 1, "invalid_list": ["INC-7"]}
    obs, rep = ingest_path(str(ROOT / "samples" / "alarm_storm.zip"))
    a = an.Analysis(obs, rep)
    kb = Knowledge(str(tmp_path / "k.db"))
    llm.set_sink(kb.log_call)
    srv, base = _srv()
    try:
        res = run_benchmark(LLMConfig(base + "/v1", "gpt-x"), a, "tr")           # fake always answers "S1"
    finally:
        llm.set_sink(None); srv.shutdown()
    assert res["n"] >= 3 and 0 <= res["correct"] <= res["n"] and all(d["picked"] == "S1" for d in res["detail"])
    assert all(d["expected"] in d["candidates"] and len(d["candidates"]) >= 2 for d in res["detail"])
    rid = kb.save_eval("gpt-x", "storm", res)
    st = kb.llm_stats()
    assert st["calls"] == res["n"] and st["by_kind"] == {"eval": res["n"]} and st["last_eval"]["id"] == rid and st["eval_accuracy"] is not None
    kb.rate_answer("gpt-x", "q", "up"); kb.rate_answer("gpt-x", "q2", "down")
    st = kb.llm_stats()
    assert st["approval_rate"] == 0.5 and st["models"][0]["model"] == "gpt-x" and st["models"][0]["success"] == 1.0
