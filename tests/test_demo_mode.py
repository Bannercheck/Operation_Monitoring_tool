"""Demo mode (WATCHOVER_DEMO) and the clean-up of simulator-made data: production installs never start the simulator nor seed
demo rows; the clean-up removes only what the simulator made and keeps a real host that happens to share a demo name."""
import json
import random
from datetime import datetime, timezone

import pytest

from watchover import demo


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    mp = pytest.MonkeyPatch()
    for k, v in {"KNOWLEDGE_DB": str(d / "k.db"), "ACTIONS_DB": str(d / "a.db"), "PLAYBOOK_DB": str(d / "pb.db"), "WATCHOVER_HOME": str(d / "home"),
                 "LIVE_SPOOL": str(d / "live" / "events.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0",
                 "WATCHOVER_LOG_STORE": "db"}.items():                      # stored lines in the main database: DELETE-able (hot files need a purger)
        mp.setenv(k, v)
    mp.delenv("DATABASE_URL", raising=False)
    mp.delenv("WATCHOVER_DEMO", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.auth import INITIAL_EMAIL
    app = create_app()
    svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0 WHERE email=?", (INITIAL_EMAIL,))
    with TestClient(app) as c:
        c.app = app
        yield c
    mp.undo()


def login(client, email, password) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def admin(client) -> dict:
    from watchover.auth import INITIAL_EMAIL, INITIAL_PASSWORD     # never written out in the chart or the docs
    return login(client, INITIAL_EMAIL, INITIAL_PASSWORD)


def test_flag_defaults_on_and_reads_off(monkeypatch):
    monkeypatch.delenv("WATCHOVER_DEMO", raising=False)
    assert demo.enabled()
    for off in ("0", "false", "off", " no "):
        monkeypatch.setenv("WATCHOVER_DEMO", off)
        assert not demo.enabled()
    monkeypatch.setenv("WATCHOVER_DEMO", "1")
    assert demo.enabled()


def test_sim_hosts_cover_every_host_the_simulator_writes():
    from watchover import live, techstack
    rng = random.Random(7); seen = set()
    for i in range(300):
        inc = i % 5 == 0
        chunks = [live.simulate_batch(rng, 10, inc, traces=True), live.simulate_metrics(rng, {}, inc), live.simulate_flows(rng), techstack.simulate_tech_lines(rng),
                  ("\n".join(live.simulate_rum(rng, inc)[1]) + "\n").encode()]
        for ch in chunks:
            seen |= {json.loads(ln).get("host") for ln in ch.decode().splitlines() if ln.strip()}
    seen.discard(None)
    assert seen and seen <= demo.sim_hosts(), seen - demo.sim_hosts()


def test_production_mode_refuses_the_simulator_and_seeds_nothing(client, monkeypatch):
    monkeypatch.setenv("WATCHOVER_DEMO", "0")
    h = admin(client); svc = client.app.state.services
    assert client.get("/api/health").json()["demo"] is False
    assert client.get("/api/system/status", headers=h).json()["live"]["demo"] is False
    r = client.post("/api/live/simulate", json={"on": True}, headers=h)
    assert r.status_code == 403 and "WATCHOVER_DEMO" in r.json()["detail"] and svc.sim_stop is None
    assert client.post("/api/live/simulate", json={"on": False}, headers=h).status_code == 200      # switching off stays harmless
    svc._simulate_users(random.Random(1), False)                  # even a direct call seeds nothing
    assert not [r for r in svc.inventory.list() if r.get("owner") == demo.DEMO_OWNER]
    assert not [a for a in svc.rum.apps() if a.get("note") == demo.DEMO_OWNER]
    assert not [f for f in svc.bizflows.list() if f["name"] == demo.DEMO_FLOW["name"]]


def _count(kb, sql, params=()):
    return int(kb._exec(sql, params)[0]["n"])


def test_demo_clean_removes_only_simulator_data(client, monkeypatch):
    monkeypatch.setenv("WATCHOVER_DEMO", "1")
    h = admin(client); svc = client.app.state.services; kb = svc.kb
    from watchover import live
    # a real server named like a demo one, known before the simulator ever ran: an agent's lines and a hand-made inventory row
    real = "\n".join(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "level": "error", "service": "postgres", "host": "db-01", "env": "prod",
                                 "message": f"real line {i}"}) for i in range(5)) + "\n"
    svc.live.ingest("pg.log", real.encode(), "dba-agent")
    svc.live.ingest("flows.jsonl", (json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "host": "db-01", "env": "prod", "msg": "flow in postgres 5432",
                                                "kind": "flow", "flow.dir": "in", "flow.process": "postgres", "flow.port": "5432", "flow.count": 3}) + "\n").encode(), "dba-agent")
    svc.inventory.upsert({"hostname": "db-01", "ip": "10.0.0.5", "env": "prod", "owner": "dba"})
    svc.rum.create_app("musteri-portal", "web", note="")
    svc.bizflows.save("Sipariş → Fatura (gerçek)", r"sipariş\s+(\d{7})", [{"name": "Oluşturuldu", "match": "oluşturuldu"}, {"name": "Fatura", "match": "fatura"}])
    # the simulator: its seeding and a few ticks of every kind of data it ships
    rng = random.Random(3)
    for i in range(6):
        svc._simulate_users(rng, i == 3)
        svc.live.ingest("sim.jsonl", live.simulate_batch(rng, 8, i == 3, traces=True), "simulator")
        svc.live.ingest("metrics.jsonl", live.simulate_metrics(rng, {}, False), "simulator")
        svc.live.ingest("flows.jsonl", live.simulate_flows(rng), "simulator")
    svc.history.flush()
    assert _count(kb, "SELECT COUNT(*) AS n FROM slo_minute WHERE sender='simulator'") > 0
    for host in ("prd-api-01", "db-01"):                          # host-keyed rows without a sender column
        kb._exec("INSERT INTO anomalies (kind, key, host, title, first_seen, last_seen) VALUES (?,?,?,?,?,?)", ("metric", f"cpu:{host}", host, "cpu", "2026-01-01", "2026-01-01"))
        kb._exec("INSERT INTO threshold_state (host, metric, level, since, updated_at) VALUES (?,?,?,?,?)", (host, "cpu", "warn", "2026-01-01", "2026-01-01"))
        kb._exec("INSERT INTO spans (trace_id, span_id, ts, service, host) VALUES (?,?,?,?,?)", (f"t-{host}", "s1", "2026-01-01T00:00:00", "svc", host))
        kb._exec("INSERT INTO tech_seen (host, source, k, v, n, last_seen) VALUES (?,?,?,?,?,?)", (host, "trace", "framework", "x", 1, "2026-01-01"))
    assert [r for r in svc.inventory.list() if r.get("owner") == demo.DEMO_OWNER]
    assert {a["name"] for a in svc.rum.apps() if a.get("note") == demo.DEMO_OWNER} == {n for n, _ in demo.DEMO_RUM_APPS}
    assert _count(kb, "SELECT COUNT(*) AS n FROM live_batches WHERE agent='simulator'") > 0
    spool = svc.live.spool.read_text(encoding="utf-8")
    assert '"agent": "simulator"}' in spool and '"agent": "dba-agent"}' in spool
    # dry run: counts, deletes nothing; a non-admin account may not run it
    r = client.get("/api/system/demo", headers=h).json()
    assert r["demo"] is True and r["dry_run"] is True and r["total"] > 0 and "db-01" in r["kept_hosts"] and "prd-api-01" in r["hosts"]
    assert _count(kb, "SELECT COUNT(*) AS n FROM live_batches WHERE agent='simulator'") > 0
    svc.users.register("ops@example.com", "Ops!Passw0rd-2026", "Ops", role="operator")
    op = login(client, "ops@example.com", "Ops!Passw0rd-2026")
    assert client.post("/api/system/demo-clean", headers=op).status_code == 403
    assert client.post("/api/system/demo-clean").status_code == 401
    # the clean-up
    r = client.post("/api/system/demo-clean", headers=h)
    assert r.status_code == 200, r.text
    out = r.json(); c = out["counts"]
    assert c["live_lines"] > 0 and c["inventory"] >= len(demo.DEMO_PEERS) and c["rum_apps"] == 2 and c["biz_flows"] == 1 and c["spool_lines"] > 0
    assert c["anomalies"] == 1 and c["threshold_states"] == 1 and c["tech_seen"] == 1 and c["spans"] >= 1
    assert out["kept_hosts"] == ["db-01"]
    # simulator data gone ...
    assert _count(kb, "SELECT COUNT(*) AS n FROM live_batches WHERE agent='simulator'") == 0
    assert _count(kb, "SELECT COUNT(*) AS n FROM slo_minute WHERE sender='simulator'") == 0
    assert not [r for r in svc.inventory.list() if r.get("owner") == demo.DEMO_OWNER]
    assert not [a for a in svc.rum.apps() if a.get("note") == demo.DEMO_OWNER] and _count(kb, "SELECT COUNT(*) AS n FROM rum_sessions WHERE app IN ('ik-mobil','ik-portal')") == 0
    assert not [f for f in svc.bizflows.list() if f["name"] == demo.DEMO_FLOW["name"]]
    for t in ("anomalies", "threshold_state", "spans", "tech_seen", "net_flows"):
        assert _count(kb, f"SELECT COUNT(*) AS n FROM {t} WHERE host='prd-api-01'") == 0, t
    assert "prd-api-01" not in svc.live.hosts() and all(o.attributes.get("agent") != "simulator" for o in svc.live.buf)
    assert '"agent": "simulator"}' not in svc.live.spool.read_text(encoding="utf-8")
    # ... the real db-01 and everything the operator made stay
    assert svc.inventory.get("db-01")["owner"] == "dba"
    for t in ("anomalies", "threshold_state", "tech_seen"):
        assert _count(kb, f"SELECT COUNT(*) AS n FROM {t} WHERE host='db-01'") == 1, t
    assert _count(kb, "SELECT COUNT(*) AS n FROM spans WHERE host='db-01' AND trace_id='t-db-01'") == 1   # (a claimed name keeps all its spans)
    assert _count(kb, "SELECT COUNT(*) AS n FROM net_flows WHERE host='db-01'") >= 1
    assert _count(kb, "SELECT COUNT(*) AS n FROM live_batches WHERE agent='dba-agent'") > 0
    assert any(o.host == "db-01" and o.attributes.get("agent") == "dba-agent" for o in svc.live.buf)
    assert '"agent": "dba-agent"}' in svc.live.spool.read_text(encoding="utf-8")
    assert {a["name"] for a in svc.rum.apps()} == {"musteri-portal"}
    assert [f["name"] for f in svc.bizflows.list()] == ["Sipariş → Fatura (gerçek)"]
    ev = svc.users.events(20)
    assert any(e["event"] == "demo-clean" and e["email"] == "admin@watchover.local" for e in ev)
    # idempotent
    assert client.post("/api/system/demo-clean", headers=h).json()["total"] == 0
    # a running simulator is stopped first (it would write everything again), the real db-01 still stays
    assert client.post("/api/live/simulate", json={"on": True}, headers=h).json()["simulator"] is True
    import time
    time.sleep(1.5)
    out = client.post("/api/system/demo-clean", headers=h).json()
    assert svc.sim_stop is None and out["total"] > 0 and out["kept_hosts"] == ["db-01"] and svc.inventory.get("db-01")["owner"] == "dba"
    assert all(o.attributes.get("agent") != "simulator" for o in svc.live.buf)


def test_cli_dry_run_and_clean(tmp_path, monkeypatch, capsys):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live" / "events.jsonl")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from watchover.api.services import Services
    svc = Services(background=False)
    svc.inventory.upsert({"hostname": "pay-api-01", "env": "prod", "owner": demo.DEMO_OWNER})
    svc.inventory.upsert({"hostname": "web-01", "env": "prod", "owner": "platform"})
    svc.close()
    assert demo._main(["clean", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["inventory"] == 1
    assert demo._main(["clean"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["inventory"] == 1
    svc = Services(background=False)
    try:
        assert [r["hostname"] for r in svc.inventory.list()] == ["web-01"]
        assert any(e["event"] == "demo-clean" and e["email"] == "cli" for e in svc.users.events(10))
    finally:
        svc.close()
    monkeypatch.setenv("WATCHOVER_DEMO", "0")
    assert demo._main(["status"]) == 0 and json.loads(capsys.readouterr().out) == {"demo": False}
