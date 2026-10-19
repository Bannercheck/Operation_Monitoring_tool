"""The chat model by measured speed (v1.59): a fake Ollama answers with the eval counts / durations a machine of a given speed
would report. The 14B is downloaded only when the 7B's speed predicts it can reach the threshold, and chosen only when it
measures so; a slow machine gets the 3B; the threads go to Ollama as num_thread; nothing is ever deleted."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from watchover import llm, llmbench


def ollama(speeds: dict, have: list):
    """speeds: model -> tokens/s here. Records every request."""
    seen = {"generate": [], "pull": [], "have": list(have)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, doc):
            b = json.dumps(doc).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            self._send({"models": [{"name": m} for m in seen["have"]]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/pull":
                seen["pull"].append(body["model"]); seen["have"].append(body["model"]); return self._send({"status": "success"})
            seen["generate"].append(body)
            n = body["options"]["num_predict"]; tps = speeds[body["model"]]
            self._send({"eval_count": n, "eval_duration": int(n / tps * 1e9), "prompt_eval_count": 120,
                        "prompt_eval_duration": int(120 / (tps * 6) * 1e9), "load_duration": int(2e9)})

    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", seen, srv


BASE = ["qwen2.5:7b-instruct", "qwen2.5:3b-instruct", "bge-m3"]


@pytest.mark.parametrize("speeds,mem,want,pulled", [
    ({"qwen2.5:7b-instruct": 12.0, "qwen2.5:14b-instruct": 6.1}, 64, "qwen2.5:14b-instruct", True),    # 16+ fast cores
    ({"qwen2.5:7b-instruct": 12.0, "qwen2.5:14b-instruct": 4.2}, 64, "qwen2.5:7b-instruct", True),     # predicted ok, measured not
    ({"qwen2.5:7b-instruct": 7.0}, 64, "qwen2.5:7b-instruct", False),                                  # 8 CPUs: 14B ~3.7 predicted
    ({"qwen2.5:7b-instruct": 12.0}, 32, "qwen2.5:7b-instruct", False),                                 # too little RAM for the 14B
    ({"qwen2.5:7b-instruct": 1.8, "qwen2.5:3b-instruct": 4.4}, 64, "qwen2.5:3b-instruct", False),      # 4 slow CPUs
])
def test_ladder(speeds, mem, want, pulled, capsys):
    url, seen, srv = ollama(speeds, BASE)
    try:
        rc = llmbench.main(["auto", "--url", url, "--mem-gb", str(mem), "--threads", "4", "--shell"])
        out = capsys.readouterr().out
        assert rc == 0 and f"CHAT={want}\n" in out, out
        assert (seen["pull"] == ["qwen2.5:14b-instruct"]) is pulled
        assert all(g["options"].get("num_thread") == 4 for g in seen["generate"])
        assert "SUMMARY=7b " in out
    finally:
        srv.shutdown()


def test_no_pull_and_nothing_to_measure(capsys):
    url, seen, srv = ollama({"qwen2.5:7b-instruct": 12.0, "qwen2.5:14b-instruct": 6.0}, BASE)
    try:
        assert llmbench.main(["auto", "--url", url, "--mem-gb", "64", "--no-pull", "--shell"]) == 0
        assert "CHAT=qwen2.5:7b-instruct" in capsys.readouterr().out and seen["pull"] == []     # disk too small: never downloads
    finally:
        srv.shutdown()
    url, seen, srv = ollama({}, ["bge-m3"])
    try:
        assert llmbench.main(["auto", "--url", url, "--mem-gb", "64", "--shell"]) == 1 and "CHAT=\n" in capsys.readouterr().out
    finally:
        srv.shutdown()


def test_bench_reports_both_rates(capsys):
    url, _seen, srv = ollama({"qwen2.5:7b-instruct": 8.0}, BASE)
    try:
        assert llmbench.main(["bench", "--url", url, "--model", "qwen2.5:7b-instruct"]) == 0
        r = json.loads(capsys.readouterr().out)
        assert r["tps"] == 8.0 and r["prompt_tps"] == 48.0 and r["load_s"] == 2.0
    finally:
        srv.shutdown()


def test_bundled_ollama_gets_the_thread_count(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "_http", lambda url, body, headers, timeout: sent.append(body) or {"message": {"content": "ok"}})
    monkeypatch.setenv("WATCHOVER_LLM_THREADS", "4")
    for root in ("http://ollama:11434", "http://10.0.0.9:11434"):
        try:
            llm._chat_raw(llm.LLMConfig(root, "qwen2.5:7b-instruct", provider="ollama"), [{"role": "user", "content": "x"}], 0.1, 8)
        except Exception:  # noqa: BLE001  (only the request body matters here)
            pass
    assert sent[0]["options"]["num_thread"] == 4 and "num_thread" not in sent[1]["options"]   # someone else's Ollama: its own setting
