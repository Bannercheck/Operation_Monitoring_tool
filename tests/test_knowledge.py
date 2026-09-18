"""Knowledge base: pattern lessons, docs, feedback -> rule proposals -> approved rules change the engine; assistant with and without an LLM."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from watchover import analysis as an, scenario
from watchover.assistant import answer, build_context, fallback_answer
from watchover.knowledge import Knowledge, chunk_text
from watchover.llm import LLMConfig
from watchover.pipeline import ingest_path

ROOT = Path(__file__).resolve().parents[1]


def _storm():
    obs, rep = ingest_path(str(ROOT / "samples" / "alarm_storm.zip"))
    return an.Analysis(obs, rep)


def test_pattern_lessons_dedupe_and_search(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    a = _storm()
    n = kb.record(a, "storm.zip")
    assert n == len(a.incidents) and kb.stats()["lessons"]["pattern"] == n
    assert kb.record(a, "storm.zip") == 0                       # same dataset again: nothing new
    assert kb.record(a, "storm_day2.zip") == n                  # same patterns, new dataset: occurrences bump, no new rows
    assert kb.stats()["lessons"]["pattern"] == n and all(d["occurrences"] == 2 for d in kb.all("pattern"))
    root = a.signal_by_id[a.incidents[0].root_cause_signal]
    hits = kb.search(root.template.split()[0] + " " + " ".join(root.services))
    assert hits and hits[0]["kind"] == "pattern" and hits[0]["score"] > 0.1
    assert kb.related(a.incidents[0], root) is not None


def test_notes_docs_and_hash_dedupe(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    i1 = kb.add_note("Tablespace doldu", "billing-db tablespace genişletilemedi; ALTER TABLESPACE ... ADD DATAFILE çözdü.", tags=["db"])
    assert kb.add_note("Tablespace doldu", "billing-db tablespace genişletilemedi; ALTER TABLESPACE ... ADD DATAFILE çözdü.", tags=["db"]) == i1
    doc = "# Runbook payment-provider-gw\n\n" + ("Sağlayıcı zaman aşımında failover'ı aç. " * 40) + "\n\n## Kontrol\n\nStatus sayfasına bak."
    ids = kb.add_doc("runbook.md", doc)
    assert len(ids) >= 2 and all(len(c) <= 1000 for c in chunk_text(doc))
    hit = kb.search("tablespace genişletilemedi")[0]
    assert hit["id"] == i1
    kb.delete(i1)
    assert kb.get(i1) is None


def test_feedback_proposes_rules_and_approval_changes_engine(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    a = _storm()
    inc = a.incidents[0]
    root = a.signal_by_id[inc.root_cause_signal]
    root_type = str(root.observations[0].attributes.get("alarm_type", "")).lower()
    out = kb.add_feedback("storm.zip", inc, root, "down", correct="noise", mark_noise=True, root_type=root_type)
    assert out["proposals"] and kb.rules("proposed")[0]["kind"] == "noise_type" and kb.rules("proposed")[0]["key"] == root_type
    before = {r["reason"] for r in a.noise_audit()["rows"]}
    assert "n_rule" not in before
    kb.decide(out["proposals"][0], approve=True)
    applied = kb.apply_rules()
    assert applied.get("noise_type") == 1 and root_type in scenario.NOISE_TYPES
    try:
        a2 = _storm()
        assert a2.rule_noise and all(str(o.attributes.get("alarm_type", "")).lower() == root_type for o in a2.rule_noise)
        assert "n_rule" in a2.noise_audit()["totals"] and a2.funnel()["raw_events"] == a.funnel()["raw_events"]
        # owner + cause_rank rules
        kb.decide(kb.propose("owner", "billing", "Faturalama ekibi", "manual"), True)
        kb.decide(kb.propose("cause_rank", "cache_miss", "4.5", "manual"), True)
        kb.apply_rules()
        assert scenario.OWNERS["billing"] == "Faturalama ekibi" and scenario.CAUSE_RANK["cache_miss"] == 4.5
    finally:
        for r in kb.rules("approved"):
            kb.decide(r["id"], False)
        kb.apply_rules()                                          # pristine again for the other tests
        assert not scenario.NOISE_TYPES and "billing" not in scenario.OWNERS or scenario.OWNERS.get("billing") != "Faturalama ekibi"


class FakeLLM(BaseHTTPRequestHandler):
    last: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLLM.last = body
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Kök neden [INC-1]; geçmişte [L1] aynı şekilde çözüldü."}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


def test_assistant_with_and_without_llm(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    a = _storm()
    kb.record(a, "storm.zip")
    kb.add_note("payment-db failover", "payment-db replica lag olunca failover'ı elle tetikledik, 4 dk'da düzeldi.")
    ctx, sources = build_context("INC-1 neden oldu, daha önce payment-db failover yaptık mı?", a, kb, "tr")
    assert "[INC-1]" in ctx and "[L" in ctx and any(s["kind"] == "incident" for s in sources) and "evidence" in ctx
    fb = fallback_answer("INC-1 neden oldu?", a, kb, "tr")
    assert "[INC-1]" in fb and "LLM bağlı değil" in fb
    assert "bulamadım" in fallback_answer("zzzz qqqq", None, None, "tr")
    srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = LLMConfig(f"http://127.0.0.1:{srv.server_address[1]}/v1", "llama3.1", "tok")
        res = answer(cfg, "INC-1 neden oldu?", [{"role": "user", "content": "selam"}, {"role": "assistant", "content": "merhaba"}], a, kb, "tr")
        assert res["used_llm"] and "[INC-1]" in res["text"] and res["sources"]
        msgs = FakeLLM.last["messages"]
        assert msgs[0]["role"] == "system" and "CONTEXT" in msgs[0]["content"] and "[INC-1]" in msgs[0]["content"] and msgs[-1]["content"] == "INC-1 neden oldu?"
        assert [m["role"] for m in msgs[1:-1]] == ["user", "assistant"]
        bad = answer(LLMConfig("http://127.0.0.1:1/v1", "x"), "INC-1?", [], a, kb, "tr")
        assert not bad["used_llm"] and bad["error"] and "[INC-1]" in bad["text"]
    finally:
        srv.shutdown()


def test_noise_template_rule_for_log_data_without_alarm_types(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    obs, rep = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    a = an.Analysis(obs, rep)
    inc = a.incidents[0]; root = a.signal_by_id[inc.root_cause_signal]
    out = kb.add_feedback("demo", inc, root, "down", correct="noise", mark_noise=True, root_type="")
    r = kb.rules("proposed")[0]
    assert r["kind"] == "noise_template" and r["key"] == root.template[:200]
    kb.decide(r["id"], True); kb.apply_rules()
    try:
        a2 = an.Analysis(obs, rep)
        assert a2.rule_noise and all(o.template == root.template for o in a2.rule_noise) and "n_rule" in a2.noise_audit()["totals"]
        assert a2.funnel()["raw_events"] == a.funnel()["raw_events"]
    finally:
        kb.decide(r["id"], False); kb.apply_rules()
        assert not scenario.NOISE_TEMPLATES


class FakeRuleLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        content = ("Öneriler:\n[{\"kind\": \"cause_rank\", \"key\": \"DISK_FULL\", \"value\": 5.5, \"reason\": \"[INC-2] disk full preceded every symptom\"},"
                   " {\"kind\": \"owner\", \"key\": \"payment\", \"value\": \"Ödeme ekibi\", \"reason\": \"[L2]\"},"
                   " {\"kind\": \"noise_type\", \"key\": \"NOT_A_TYPE\", \"value\": \"1\", \"reason\": \"x\"},"
                   " {\"kind\": \"dependency\", \"key\": \"checkout-api -> payment-api\", \"value\": \"sync\", \"reason\": \"[INC-1]\"},"
                   " {\"kind\": \"delete_everything\", \"key\": \"*\", \"value\": \"1\"}]")
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


def test_llm_rule_proposals_are_validated_and_only_proposed(tmp_path):
    from watchover.assistant import propose_rules, rule_context
    kb = Knowledge(str(tmp_path / "k.db"))
    a = _storm()
    kb.record(a, "storm.zip")
    ctx = rule_context(a, kb, "tr")
    assert "## ALARM TYPES" in ctx and "disk_full" in ctx and "## INCIDENTS" in ctx
    srv = HTTPServer(("127.0.0.1", 0), FakeRuleLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = propose_rules(LLMConfig(f"http://127.0.0.1:{srv.server_address[1]}/v1", "llama3.1"), a, kb, "tr")
    finally:
        srv.shutdown()
    kinds = sorted((it["kind"], it["key"]) for it in out["items"])
    assert kinds == [("cause_rank", "disk_full"), ("dependency", "checkout-api->payment-api"), ("owner", "payment")]
    assert len(out["dropped"]) == 2 and len(out["proposed"]) == 3
    rules = kb.rules("proposed")
    assert all(r["source"] == "llm:llama3.1" for r in rules) and not kb.rules("approved")   # nothing applied without a human
    assert scenario.CAUSE_RANK.get("disk_full") != 5.5
