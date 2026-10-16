"""WP13: the model selector on frozen inputs (docs/design/action-agent.md §8.3, §8.4; C25–C28, I2, I12) with a fake LLM:
no network, the operator's LLM settings are never touched."""
import copy
import json
from pathlib import Path

import pytest

from watchover.act import select as S
from watchover.act.importer import starter_bodies
from watchover.act.types import Trigger
from watchover.llm import LLMConfig

ROOT = Path(__file__).resolve().parents[1]
NOSPACE = "ERROR orders write failed: No space left on device (/var/log/app/orders.log)"
LOCAL = LLMConfig("http://127.0.0.1:11434", "qwen-act-test", "", provider="ollama")


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cfg, messages, fmt=None, seed=None, max_tokens=0, kind=""):
        self.calls.append({"messages": messages, "fmt": fmt, "seed": seed, "max_tokens": max_tokens, "kind": kind})
        a = self.answers.pop(0) if self.answers else "{}"
        return a(messages) if callable(a) else a


@pytest.fixture
def c(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WATCHOVER_KEYD", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app()
    svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        svc.act.set_mode("guided", by="admin@watchover.local")
        for email in ("a@corp.com", "b@corp.com"):
            svc.users.register(email, "Str0ng!Passw0rd", name=email[0], role="viewer")
        body = copy.deepcopy(starter_bodies()["linux-varlog-full"])
        svc.act.runbooks.create("varlog-team", body["title"], None, body, "a@corp.com")
        svc.act.runbooks.submit("varlog-team", 1, "a@corp.com", tabletop=True)
        svc.act.runbooks.review("varlog-team", 1, "b@corp.com", "approve")
        cl.cfg = {"cfg": LOCAL}
        monkeypatch.setattr(svc, "llm_cfg", lambda purpose="chat": cl.cfg["cfg"])     # a fake local model; settings untouched
        monkeypatch.setattr("watchover.llm.ollama_version", lambda cfg: (0, 5, 1))
        yield cl


def case(c, host="app-lnx-07", key=None, extra=()):
    ev = [{"kind": "pasted", "host": host, "text": NOSPACE}] + list(extra)
    return c.svc.act.cases.route(Trigger(source="threshold", key=key or f"threshold:{host}:disk", host=host, env="prod", severity="high",
                                         title="Disk dolu: /var/log", facts={"metric": "disk_pct", "value": 93, "limit": 90},
                                         evidence=ev))["case_id"]


def selector(c, answers):
    fake = FakeLLM(answers)
    c.svc.act.selector.call = fake
    return c.svc.act.selector, fake


def answer_for(fi, choice="R1", label=None, cite=True):
    aliases, labels = S.Selector.maps(fi)
    params = {}
    key = aliases.get(choice, "")
    if label and key and labels.get(key):
        p = sorted(labels[key])[0]
        params[p] = label
    shown = S.Selector.shown(fi)
    return json.dumps({"choice": choice, "params": params, "cite": shown[:1] if cite else []})


def test_prompt_files_equal_the_embedded_text():
    for name, text in S.SYSTEMS.items():
        assert (ROOT / "prompts" / name).read_text(encoding="utf-8") == text, name


def test_select_valid_seed_schema_budget_and_cache(c):
    cid = case(c)
    fi = c.svc.act.shadow.frozen(cid, 1)
    sel, fake = selector(c, [answer_for(fi)])
    assert sel.budget_ok(fi) and fi["prompt_tokens"] <= 1200
    res = sel.select(cid, 1)
    assert res["valid"] and res["choice"] == "varlog-team" and not res["late"]
    call = fake.calls[0]
    assert call["seed"] == cid and call["max_tokens"] == 64 and call["kind"] == "act_select"
    assert call["fmt"]["properties"]["choice"]["enum"][-2:] == ["NONE", "ESCALATE"]            # Ollama >= 0.5: the per-case schema
    assert call["messages"][0]["content"] == S.SELECT_SYSTEM and call["messages"][1]["content"] == fi["prompt"]
    prop = [p for p in c.svc.act.shadow.proposals(cid, 1) if p["selector"] == "qwen-act-test"][0]
    assert prop["valid"] == 1 and prop["runbook_key"] == "varlog-team" and prop["sealed_hash"]
    c.svc.act.db._exec("DELETE FROM act_proposals WHERE selector='qwen-act-test'")
    sel.select(cid, 1)                                                                     # same model, same frozen input
    assert len(fake.calls) == 1 and sel.stats["cached"] == 1


def test_old_ollama_gets_format_json(c, monkeypatch):
    monkeypatch.setattr("watchover.llm.ollama_version", lambda cfg: (0, 4, 7))
    cid = case(c)
    fi = c.svc.act.shadow.frozen(cid, 1)
    sel, fake = selector(c, [answer_for(fi)])
    sel._fmt.clear()
    assert sel.select(cid, 1)["valid"] and fake.calls[0]["fmt"] == "json"


@pytest.mark.parametrize("bad, why", [
    ('{"choice": "R9", "params": {}, "cite": []}', "choice_outside"),
    ('{"choice": "rm -rf /", "params": {}, "cite": []}', "choice_outside"),                   # I2: never a command
    ('{"choice": "R1", "params": {"days": "V1"}, "cite": []}', "param_outside"),
    ('{"choice": "R1", "params": {}, "cite": ["E99"]}', "cite_outside"),
    ('{"choice": "R1", "params": {}, "cite": [], "explain": "x"}', "keys"),
    ('{"choice": "R1", "params": {}, "cite": ["' + "E1\", \"" * 300 + 'E1"]}', "object_too_long"),
    ("sorry, no json here", "no_json"),
])
def test_invalid_outputs_one_repair_then_the_rules_proposal_stands(c, bad, why):
    cid = case(c)
    sel, fake = selector(c, [bad, bad])
    res = sel.select(cid, 1)
    assert not res["valid"] and res["reason"].startswith(why) and res["repaired"] and len(fake.calls) == 2
    assert "Yanıt geçersiz" in fake.calls[1]["messages"][-1]["content"]
    view = c.svc.act.shadow.view(dict(c.svc.act.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0]))
    model = [p for p in c.svc.act.shadow.proposals(cid, 1) if p["selector"] == "qwen-act-test"][0]
    assert model["valid"] == 0 and model["invalid_reasons"].startswith(why)
    if view["state"] in ("visible", "revealed"):
        assert view["rules"]["choice"] == "varlog-team"


def test_label_outside_and_repair_that_succeeds(c):
    cid = case(c)
    fi = c.svc.act.shadow.frozen(cid, 1)
    _aliases, labels = S.Selector.maps(fi)
    sel, fake = selector(c, [answer_for(fi, label="V9"), answer_for(fi)])
    res = sel.select(cid, 1)
    assert res["valid"] and res["repaired"]
    assert c.svc.act.db._exec("SELECT repaired FROM act_proposals WHERE selector='qwen-act-test'")[0]["repaired"] == 1
    if labels:
        bad = answer_for(fi, label="V9")
        assert sel.validate(bad, fi, {"host": "app-lnx-07"})[1].startswith("label_outside")


def test_late_answer_is_stored_late_and_never_blocks(c):
    cid = case(c)
    fi = c.svc.act.shadow.frozen(cid, 1)
    c.svc.act.shadow.decide(cid, "a@corp.com", "varlog-team", params={"path": "/var/log"}, reason_code="wrong_param")
    sel, _ = selector(c, [answer_for(fi, choice="NONE")])
    res = sel.select(cid, 1)
    assert res["valid"] and res["late"]
    d = c.svc.act.shadow.decision(cid, 1)
    assert d["choice"] == "varlog-team" and d["agree_model"] == "differ"                     # the decision stays; agreement recorded


def test_injection_suspect_cases_are_skipped(c):
    cid = case(c)
    c.svc.act.db._exec("UPDATE actions SET injection_suspect=1 WHERE id=?", (cid,))
    sel, fake = selector(c, [])
    assert not sel.submit(cid) and sel.select(cid, 1)["reason"] == "injection_suspect" and not fake.calls
    v = c.svc.act.shadow.view(dict(c.svc.act.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0]))
    assert v["model"] == "skipped"


def test_cloud_config_switches_the_selector_off(c):
    c.cfg["cfg"] = LLMConfig("https://api.openai.com/v1", "gpt-x", "sk-test")
    st = c.svc.act.selector.state()
    assert not st["on"] and st["reason"] == "cloud"
    c.cfg["cfg"] = LLMConfig("http://10.1.2.3:8000/v1", "local-vllm", "")
    assert c.svc.act.selector.state()["on"]
    c.cfg["cfg"] = LLMConfig("", "", "")
    assert c.svc.act.selector.state()["reason"] == "no_model"


def test_probe_timeout_clamp_and_auto_off(c, monkeypatch):
    sel, _ = selector(c, ["ok"] * 10)
    t = {"now": 0.0, "step": 15.0}

    def clock():
        t["now"] += t["step"]
        return t["now"]
    monkeypatch.setattr(S.time, "perf_counter", clock)
    r = sel.probe()
    assert r["ok"] and r["p95_s"] == 15.0 and r["timeout_s"] == 30 and sel.state()["on"]
    t["step"] = 70.0
    r = sel.probe()
    assert r["p95_s"] == 70.0 and r["timeout_s"] == 90                                     # clamp(20, 90, 2 × p95)
    st = sel.state()
    assert not st["on"] and st["reason"] == "slow" and "yavaş" in st["banner"]["tr"]           # auto-off above 60 s (K7)
    cid = case(c)
    assert not sel.submit(cid)


def test_queue_priority_dedupe_and_bound():
    from watchover.act.llmq import LLMQueue
    q = LLMQueue(maxlen=3)
    order = []
    assert q.submit("explain", "e1", lambda: order.append("explain"))
    assert q.submit("select", "s1", lambda: order.append("select"))
    assert not q.submit("select", "s1", lambda: order.append("dup"))
    assert q.submit("draft", "d1", lambda: order.append("draft"))
    assert q.submit("next", "n1", lambda: order.append("next"))                            # full: the oldest lowest-priority drops
    while q.run_once():
        pass
    assert order == ["select", "next", "explain"] and q.stats["dropped"] == 1 and q.stats["deduped"] == 1


def test_lazy_explain_is_grounded(c):
    cid = case(c)
    fi = c.svc.act.shadow.frozen(cid, 1)
    shown = S.Selector.shown(fi)
    sel, fake = selector(c, [answer_for(fi), f"Disk dolu ve log yazılamıyor [{shown[0]}]."])
    c.svc.act.db._exec("UPDATE actions SET blind=0 WHERE id=?", (cid,))
    sel.select(cid, 1)
    res = sel.explain(cid)
    assert res["explain"].startswith("Disk dolu") and fake.calls[-1]["kind"] == "act_explain" and fake.calls[-1]["max_tokens"] == 160
    c.svc.act.db._exec("UPDATE act_proposals SET explain='' WHERE selector='qwen-act-test'")
    fake.answers = ["Uydurma kanıt [E77]."]
    assert sel.explain(cid)["reason"] == "ungrounded"
