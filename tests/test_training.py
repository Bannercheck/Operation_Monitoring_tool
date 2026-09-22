"""Fine-tuning pipeline: memory -> chat JSONL (all sources, dedup, split), CLI export, Modelfile, trainer dry-run, watchover.sh llm export/import."""
import json
import subprocess
import sys
from pathlib import Path

from watchover import autolearn, training
from watchover.knowledge import Knowledge
from watchover.memory import Memory

ROOT = Path(__file__).resolve().parents[1]


def _kb(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    mem = Memory(kb, live=None, interval_min=0)
    for i in range(12):
        mem.remember_anomaly({"id": i, "key": f"storm:prod:web-{i:02d}", "kind": "error_storm", "title": f"Error storm on web-{i:02d}", "detail": f"errors {40 + i}/min vs 4/min", "env": "prod", "host": f"web-{i:02d}", "service": "api", "observed": 40 + i, "baseline": 4, "score": 6, "first_seen": "2026-09-22T10:00"})
    mem.remember_action({"id": 3, "status": "done", "title": "Restart api pods", "incident_id": "INC-1", "priority": "P1", "owner": "ali", "recommendation": "rollout restart", "evidence": "", "updated_at": "2026-09-22T11:00"})
    kb.add_note("VPN runbook", "Restart the tunnel, then check BGP.")
    kb.propose("owner", "payment-api", "ödeme ekibi", "team decision"); kb.decide(1, True)
    kb.rate_answer("m", "neden?", "up", answer="çünkü"); kb.rate_answer("m", "neden?", "up", answer="çünkü")   # duplicate thumbs-up: one example
    return kb


def test_examples_cover_every_source_and_dedupe(tmp_path):
    ex, counts = autolearn.training_examples(_kb(tmp_path), "tr")
    assert counts["anomaly"] == 12 and counts["resolution"] == 1 and counts["note"] == 1 and counts["rule"] == 1 and counts["feedback"] == 1
    assert len(ex) == sum(counts.values()) and all(e["messages"][0]["role"] == "system" and e["messages"][-1]["role"] == "assistant" for e in ex)
    b = autolearn.training_bundle(_kb(tmp_path), "tr", holdout=0.25)
    assert b["stats"]["total"] == 16 and b["stats"]["eval"] == 4 and b["stats"]["train"] == 12
    assert len(b["train"].splitlines()) == 12 and len(b["eval"].splitlines()) == 4
    en = autolearn.training_examples(_kb(tmp_path), "en")[0]
    assert en[0]["messages"][0]["content"].startswith("You are")


def test_cli_export_and_modelfile(tmp_path, monkeypatch):
    _kb(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db")); monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "t.jsonl"
    assert training.main(["export", "--out", str(out), "--part", "train"]) == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 15 and rows[0]["messages"][1]["role"] == "user"
    mf = training.modelfile("./adapter", "qwen2.5:3b-instruct", "tr")
    assert mf.startswith("FROM qwen2.5:3b-instruct\nADAPTER ./adapter\n") and "num_ctx" in mf and "Watchover" in mf
    assert "ADAPTER" not in training.modelfile("./adapter", merged="./merged") and training.modelfile(None).startswith("FROM qwen2.5:3b-instruct\nSYSTEM")


def test_trainer_dry_run_and_validation(tmp_path, monkeypatch):
    _kb(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db")); monkeypatch.delenv("DATABASE_URL", raising=False)
    data = tmp_path / "t.jsonl"; training.main(["export", "--out", str(data)])
    out = tmp_path / "models" / "watchover-ops"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "train_lora.py"), "--data", str(data), "--out", str(out), "--dry-run", "--base", "qwen2.5:1.5b-instruct"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    st = json.loads((out / "train_stats.json").read_text())
    assert st["examples"] == 16 and st["hf_base"] == "Qwen/Qwen2.5-1.5B-Instruct" and (out / "Modelfile").read_text().startswith("FROM qwen2.5:1.5b-instruct")
    bad = tmp_path / "bad.jsonl"; bad.write_text('{"messages": [{"role": "user", "content": "x"}]}\n')
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "train_lora.py"), "--data", str(bad), "--out", str(out), "--dry-run"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "assistant" in r.stderr
