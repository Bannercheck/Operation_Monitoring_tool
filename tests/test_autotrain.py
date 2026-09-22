"""Unattended learning: eligibility, dry run, a full run with a stubbed trainer + fake Ollama, gate pass/fail, model promotion, ops config."""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from watchover import autotrain, lora, settings as wo_settings
from watchover.knowledge import Knowledge
from watchover.memory import Memory

UTC = timezone.utc


def _kb(tmp_path, n=12):
    kb = Knowledge(str(tmp_path / "k.db"))
    mem = Memory(kb, live=None, interval_min=0)
    for i in range(n):
        mem.remember_anomaly({"id": i, "key": f"storm:prod:web-{i:02d}", "kind": "error_storm", "title": f"Error storm on web-{i:02d}", "detail": f"errors {40 + i}/min", "env": "prod", "host": f"web-{i:02d}", "service": "api", "observed": 40 + i, "baseline": 4, "score": 6, "first_seen": "2026-09-22T10:00"})
    return kb


class FakeOllama(BaseHTTPRequestHandler):
    calls: list = []
    models: set = set()

    def log_message(self, *a): pass

    def _j(self, o, c=200):
        b = json.dumps(o).encode(); self.send_response(c); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._j({"models": [{"name": m} for m in self.models]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        self.calls.append(("POST", self.path, len(body)))
        if self.path.startswith("/api/blobs/"):
            self._j({}, 201); return
        d = json.loads(body or b"{}")
        if self.path == "/api/create":
            self.models.add(d.get("model") or d.get("name")); self._j({"status": "success"}); return
        if self.path == "/api/copy":
            self.models.add(d["destination"]); self._j({}); return
        if self.path == "/api/pull":
            self.models.add(d["name"]); self._j({"status": "success"}); return
        self._j({})

    def do_DELETE(self):
        n = int(self.headers.get("Content-Length") or 0); d = json.loads(self.rfile.read(n) or b"{}")
        self.calls.append(("DELETE", self.path, d.get("name"))); self.models.discard(d.get("name")); self._j({})


@pytest.fixture
def fake_ollama():
    FakeOllama.calls = []; FakeOllama.models = set()
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_eligibility_thresholds_and_run_now_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    kb = _kb(tmp_path)
    t = autotrain.AutoTrainer(kb, ollama_url="http://none", models_dir=str(tmp_path / "m"))
    st = t.state()
    assert st["examples"] == 12 and not st["eligible"] and any("examples 12 < 200" in r for r in st["reasons"])
    wo_settings.save({"train_min_examples": 10, "train_min_new": 5})
    assert t.state()["eligible"] and t.should_run()
    wo_settings.save({"train_auto": False})
    assert not t.state()["eligible"] and "auto off" in t.state()["reasons"]
    autotrain.request_run()
    assert t.state()["forced"] and t.state()["eligible"]                       # "train now" overrides the thresholds


def test_dry_run_records_and_writes_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    kb = _kb(tmp_path)
    t = autotrain.AutoTrainer(kb, ollama_url="http://none", models_dir=str(tmp_path / "m"))
    res = t.run_once(force=True, dry=True)
    assert res["status"] == "dry" and res["examples"] == 11 and (Path(res["dir"]) / "Modelfile").exists() and (Path(res["dir"]) / "eval.jsonl").exists()   # 12 examples: 11 train + 1 held out
    assert t.runs()[0]["status"] == "dry" and t.runs()[0]["examples"] == 12 and t.last("pass") is None


def test_full_run_promotes_on_pass_and_keeps_previous_on_fail(tmp_path, monkeypatch, fake_ollama):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    kb = _kb(tmp_path)
    wo_settings.save({"train_min_examples": 5, "train_min_new": 1, "train_every_h": 0})

    def fake_train(rows, out, base, lang, **kw):            # no torch in CI: write a tiny 'adapter' and stats
        (out / "adapter").mkdir(parents=True, exist_ok=True)
        (out / "adapter" / "adapter_model.safetensors").write_bytes(b"\x00" * 64)
        (out / "adapter" / "adapter_config.json").write_text("{}")
        return {"examples": len(rows), "steps": 3, "final_loss": 0.42, "seconds": 1, "base": base, "hf_base": "x", "dry_run": False}

    monkeypatch.setattr(lora, "train", fake_train)
    monkeypatch.setattr(autotrain.AutoTrainer, "_gate_analysis", lambda self: (object(), "demo"))
    monkeypatch.setattr(lora, "gate", lambda *a, **k: {"passed": True, "score": 0.8, "base_score": 0.7, "n": 10, "correct": 8})
    t = autotrain.AutoTrainer(kb, ollama_url=fake_ollama, models_dir=str(tmp_path / "m"))
    res = t.run_once()
    assert res["status"] == "pass" and res["gate"]["score"] == 0.8
    paths = [c[1] for c in FakeOllama.calls]
    assert any(p.startswith("/api/blobs/") for p in paths) and "/api/create" in paths and paths.count("/api/copy") == 2
    assert "watchover-ops" in FakeOllama.models and autotrain.CANDIDATE not in FakeOllama.models and any(m.startswith("watchover-ops:") for m in FakeOllama.models)
    assert t.last("pass")["examples"] == 12 and t.state()["new_since_pass"] == 0
    # a failing candidate never replaces the passing model
    monkeypatch.setattr(lora, "gate", lambda *a, **k: {"passed": False, "score": 0.3, "base_score": 0.7})
    FakeOllama.calls = []
    autotrain.request_run()
    res = t.run_once()
    assert res["status"] == "fail" and "/api/copy" not in [c[1] for c in FakeOllama.calls] and "watchover-ops" in FakeOllama.models
    assert t.runs()[0]["status"] == "fail" and t.last("pass")["gate"] and json.loads(t.last("pass")["gate"])["passed"]


def test_ollama_create_falls_back_to_modelfile(tmp_path, fake_ollama, monkeypatch):
    from watchover import ollama as wo
    adapter = tmp_path / "adapter"; adapter.mkdir(); (adapter / "adapter_model.safetensors").write_bytes(b"\x01" * 16)
    r = wo.create_from_adapter(fake_ollama, "m1", str(adapter), "qwen2.5:1.5b-instruct", "sys")
    assert r["via"] == "blobs"
    monkeypatch.setattr(wo, "upload_blob", lambda *a, **k: (_ for _ in ()).throw(ValueError("old server")))
    r = wo.create_from_adapter(fake_ollama, "m2", str(adapter), "qwen2.5:1.5b-instruct", "sys", server_adapter_path="/models/x/adapter")
    assert r["via"] == "modelfile"
