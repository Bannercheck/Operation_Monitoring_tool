"""The full reset of analysis data (demo.reset_all; System › Veri sıfırlama; ./watchover.sh reset-data), the cache clear-up, and the
built-in agent's single row (older versions enrolled one per container id) with the stale-agent prune."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from watchover import demo

UTC = timezone.utc


def lines(*rows) -> bytes:
    now = datetime.now(UTC).isoformat()
    return ("\n".join(json.dumps({"ts": now, **r}) for r in rows) + "\n").encode()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for k, v in {"WATCHOVER_HOME": str(home), "LIVE_SPOOL": str(home / "live" / "events.jsonl"), "WATCHOVER_MODELS_DIR": str(home / "models"),
                 "PLAYBOOK_DB": str(home / "playbook.db"), "ACTIONS_DB": str(home / "actions.db"), "KNOWLEDGE_DB": str(home / "knowledge.db"),
                 "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0", "WATCHOVER_LOG_STORE": "db", "OLLAMA_URL": "http://127.0.0.1:9"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(demo, "SIMULATOR", False)                       # the shipped state
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.auth import INITIAL_EMAIL, INITIAL_PASSWORD
    app = create_app()
    svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0 WHERE email=?", (INITIAL_EMAIL,))
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": INITIAL_EMAIL, "password": INITIAL_PASSWORD})
        c.h = {"Authorization": f"Bearer {r.json()['token']}"}
        c.pw, c.svc, c.home = INITIAL_PASSWORD, svc, home
        yield c
    svc.close()


def n(kb, table: str, where: str = "1 = 1") -> int:
    return int(kb._exec(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")[0]["n"]) if table in kb.tables() else 0


def fill(c) -> dict:
    """Real data of every kind the reset empties, configuration it keeps, and one leftover row of the retired simulator."""
    svc, kb, h = c.svc, c.svc.kb, c.h
    rec, _tok = svc.agents.enroll("real-agent", env="prod")
    svc.live.ingest("app.jsonl", lines(*[{"level": "error" if i % 2 else "info", "service": "shop-web", "host": "web-real-01", "env": "prod",
                                          "trace_id": f"t{i}", "duration_ms": 40 + i, "msg": f"order {i} checkout failed" if i % 2 else f"order {i} ok"}
                                         for i in range(40)]), "real-agent")
    svc.live.ingest("metrics.jsonl", lines({"host": "web-real-01", "env": "prod", "cpu": 99, "memory": 50}), "real-agent")
    svc.thresholds.save({"name": "cpu", "scope": "all", "metric": "cpu", "warn": 50, "crit": 95, "for_min": 0, "channels": "email", "targets": "ops@example.com"})
    svc.notifier.add_rule("hata", "errors", threshold=0.1, cooldown_min=0)
    svc.thresholds.evaluate(); svc.anomalies.scan(); svc.alerts.evaluate()
    for fn in (svc.history.flush, svc.traces.flush, svc.techstack.flush, svc.apps.flush_namespaces):
        fn()
    svc.inventory.upsert({"hostname": "web-real-01", "ip": "10.0.0.7", "env": "prod", "owner": "ops"})        # entered by hand: stays
    svc.inventory.upsert({"hostname": "db-01", "ip": "172.16.1.55", "env": "prod", "owner": demo.DEMO_OWNER, "demo": 1})   # old simulator row
    q, a = "web-real-01 neden yavaş?", "checkout hataları"
    assert c.post("/api/assist/save", json={"question": q, "answer": a}, headers=h).status_code == 201
    assert c.post("/api/assist/rate", json={"question": q, "answer": a, "verdict": "up"}, headers=h).status_code == 200
    kb.log_call({"provider": "ollama", "model": "m", "kind": "chat", "ok": 1, "prompt_chars": 10})
    kb.propose("owner", "shop-web", "team-x", "llm", "llm:m")
    kb.save_eval("m", "live", {"n": 1, "correct": 1, "cited": 1, "grounded": 1, "latency_ms": 3})
    from watchover.autotrain import AutoTrainer
    AutoTrainer(kb)                                          # its table
    run = c.home / "models" / "run1"
    run.mkdir(parents=True)
    (run / "train.jsonl").write_text("{}\n", encoding="utf-8")
    kb._exec("INSERT INTO train_runs (ts, status, model, note) VALUES (?, 'pass', 'watchover-ops', ?)", (datetime.now(UTC).isoformat(), f"ok · {run}"))
    from watchover import profiles
    profiles.save({"auto1": {"source": "auto", "name": "app.jsonl"}, "mine": {"source": "manual", "name": "x.log"}})
    return {"agent": rec["id"], "run": run}


def test_full_reset_empties_analysis_and_keeps_configuration(env):
    c, svc, kb = env, env.svc, env.svc.kb
    made = fill(c)
    for t in ("anomalies", "alerts", "threshold_state", "slo_minute", "spans", "lessons", "rules", "answer_feedback", "llm_calls", "eval_runs", "train_runs"):
        assert n(kb, t) > 0, t
    keep = {t: n(kb, t) for t in ("users", "agents", "threshold_policies", "notify_rules", "roles")}
    real_lines = n(kb, "live_batches", "agent = 'real-agent'")
    assert real_lines > 0
    # preview: counts, nothing deleted; the typed word and a fresh step-up guard the real thing
    pv = c.get("/api/system/data-reset", headers=c.h).json()
    assert pv["dry_run"] and pv["total"] > 0 and pv["word"] == "SIFIRLA" and n(kb, "anomalies") > 0
    assert c.post("/api/system/data-reset", json={"confirm": "evet"}, headers=c.h).status_code == 400
    r = c.post("/api/system/data-reset", json={"confirm": "sıfırla"}, headers=c.h)
    assert r.status_code == 428 and n(kb, "anomalies") > 0, r.text                         # no fresh step-up yet
    assert c.post("/api/auth/stepup", json={"next": "/system"}, headers=c.h).status_code == 200
    assert c.post("/api/auth/stepup/verify", json={"password": c.pw}, headers=c.h).status_code == 200
    out = c.post("/api/system/data-reset", json={"confirm": "sıfırla"}, headers=c.h).json()
    assert out["reset"] and not out["dry_run"] and out["total"] == pv["total"], (out["counts"], pv["counts"])
    for t, _k in demo.RESET_WIPE:
        assert n(kb, t) == 0, t
    assert n(kb, "train_runs") == 0 and not made["run"].exists()                         # fine-tunes and their adapters gone
    assert n(kb, "inventory", "hostname = 'web-real-01'") == 1 and n(kb, "inventory", "hostname = 'db-01'") == 0
    assert n(kb, "live_batches", "agent = 'real-agent'") == real_lines                   # stored log lines of real senders stay
    assert {t: n(kb, t) for t in keep} == keep
    assert svc.agents.get(made["agent"])["status"] == "active"
    from watchover import profiles
    assert set(profiles.load()) == {"mine"}
    ev = kb._exec("SELECT * FROM auth_events WHERE kind = 'data-reset'") if "kind" in kb.columns("auth_events") else kb._exec("SELECT * FROM auth_events")
    assert any("full reset" in json.dumps(dict(e), ensure_ascii=False) for e in ev)


def test_reset_requested_runs_at_start_before_threads(env):
    c, svc, kb = env, env.svc, env.svc.kb
    fill(c)
    assert demo._main(["reset", "--yes", "--at-start"]) == 0
    assert (c.home / demo.RESET_REQUEST).is_file()
    svc._reset_requested()                                   # what the API process does at its next start
    assert not (c.home / demo.RESET_REQUEST).exists()
    res = json.loads((c.home / demo.RESET_RESULT).read_text(encoding="utf-8"))
    assert res["reset"] and res["total"] > 0 and n(kb, "anomalies") == 0 and n(kb, "lessons") == 0


def test_reset_cli_needs_yes(env, capsys):
    with pytest.raises(SystemExit):
        demo._main(["reset"])
    assert demo._main(["reset", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_cache_clear_keeps_data(env):
    c, kb = env, env.svc.kb
    fill(c)
    before = n(kb, "anomalies")
    r = c.post("/api/system/cache-clear", headers=c.h)
    assert r.status_code == 200 and "agents" in r.json()["cleared"] and n(kb, "anomalies") == before


def test_leftovers_of_the_retired_simulator_go_at_start(env):
    svc, kb = env.svc, env.svc.kb
    svc.inventory.upsert({"hostname": "db-01", "ip": "172.16.1.55", "env": "prod", "owner": demo.DEMO_OWNER, "demo": 1})
    svc.inventory.upsert({"hostname": "web-real-01", "ip": "10.0.0.7", "env": "prod", "owner": "ops"})
    out = svc.purge_leftovers()
    assert out and out["counts"]["inventory"] == 1 and n(kb, "inventory") == 1
    assert svc.purge_leftovers() is None                     # nothing left: nothing to do


def test_builtin_agent_keeps_one_row_and_prune(env, monkeypatch):
    c, svc = env, env.svc
    from watchover import selfmon
    reg = svc.agents
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat(timespec="seconds")
    ids = []
    for i, name in enumerate(("0d81e00b4e24", "12d579176f04", "197ef56813b6")):          # one per container id, as older versions did
        rec, _t = reg.enroll(name, env="local", site="watchover", tags="self", note="the machine Watchover runs on (built-in monitoring)")
        reg.kb._exec("UPDATE agents SET last_seen=? WHERE id=?", ((datetime.now(UTC) - timedelta(days=3 - i)).isoformat(timespec="seconds"), rec["id"]))
        ids.append(rec["id"])
    real, _t = reg.enroll("app-01", env="prod")
    gone, _t = reg.enroll("old-01", env="prod")
    reg.kb._exec("UPDATE agents SET last_seen=? WHERE id=?", (old, gone["id"]))
    monkeypatch.setenv("WATCHOVER_DOCKER", "1")
    monkeypatch.delenv("WATCHOVER_SELF_NAME", raising=False)
    assert selfmon.host_name() == "watchover-host"
    keep = selfmon.collapse(reg, selfmon.host_name())
    mine = [a for a in reg.list() if selfmon.is_self(a)]
    assert keep["id"] == ids[-1] and [a["name"] for a in mine] == ["watchover-host"]       # the newest one, renamed; the rest gone
    assert reg.get(real["id"]) is not None
    r = c.post("/api/agents/prune?days=7", headers=c.h).json()
    assert [a["name"] for a in r["removed"]] == ["old-01"] and reg.get(keep["id"]) is not None and reg.get(real["id"]) is not None
