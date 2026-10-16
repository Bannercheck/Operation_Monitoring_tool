"""Simulation off = spotless (watchover.demo). The simulator and its generators fill every subsystem they can reach (lines,
metrics, traces, RUM, anomalies, alerts, threshold states, topology, flows, applications, datasets, the assistant's memory, an
old demo-trained model ...), a real agent sends real data next to it, the simulator is switched off with confirmation - then
EVERY table of the database (SQLite here; PostgreSQL 16 too when WATCHOVER_TEST_DATABASE_URL points at a scratch database), the
hot log files, the playbook file and every file under the data directory are scanned for a demo marker or a simulator host /
service name, every real row is compared with its copy from before the purge, and the assistant's context, the API views and the
MCP tools show nothing of the simulation."""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from watchover import demo
from watchover.live import SIM_SERVICES

UTC = timezone.utc
PG_URL = os.environ.get("WATCHOVER_TEST_DATABASE_URL", "")
REAL_HOST, REAL_AGENT = "web-real-01", "real-agent"
NAMES = sorted(demo.sim_hosts() | set(SIM_SERVICES) | {n for n, _p in demo.DEMO_RUM_APPS}, key=len, reverse=True)
PAT = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(h) for h in NAMES) + r")(?![\w-])", re.I)
NEEDLES = (demo.DEMO_OWNER, '"agent": "simulator"', '"agent":"simulator"', demo.DEMO_ATTR, demo.DEMO_FLOW["name"], *demo.SIM_FILES)


def hit(value) -> str | None:
    """The demo marker / simulator name a stored value carries, or None."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        try:
            value = zlib.decompress(bytes(value)).decode("utf-8", "replace")          # stored lines (v2 rows)
        except zlib.error:
            return None                                                              # embeddings
    if not isinstance(value, str):
        return None
    m = PAT.search(value)
    if m:
        return m.group(0)
    return next((n for n in NEEDLES if n in value), None)


def row_hits(table: str, r: dict) -> list:
    out = []
    for k, v in r.items():
        if k == demo.DEMO_COL and v not in (None, 0, "0", False):
            out.append(f"{table}.{k}={v}")
        elif k in ("agent", "sender") and v == demo.SIM_AGENT:
            out.append(f"{table}.{k}=simulator")
        else:
            h = hit(v)
            if h:
                out.append(f"{table}.{k}: {h!r} in {str(v)[:120]!r}")
    return out


def dump(db) -> dict:
    return {t: [dict(r) for r in db._exec(f"SELECT * FROM {t}")] for t in db.tables()}


def dump_sqlite(path: Path) -> dict:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: [dict(r) for r in c.execute(f"SELECT * FROM {t}")] for t in tables}
    finally:
        c.close()


def canon(r: dict) -> str:
    return json.dumps({k: (v.tobytes().hex() if isinstance(v, memoryview) else v.hex() if isinstance(v, (bytes, bytearray)) else v)
                       for k, v in sorted(r.items())}, default=str, sort_keys=True)


def file_hits(root: Path) -> list:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix == ".db" or p.name.endswith((".db-wal", ".db-shm")):
            if p.suffix == ".db":
                for t, rows in dump_sqlite(p).items():
                    out += [f"{p.name}:{h}" for r in rows for h in row_hits(t, r)]
            continue
        data = p.read_bytes()
        if p.suffix == ".gz":
            import gzip
            data = gzip.decompress(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue                                                                 # key files
        h = hit(text)
        if h:
            out.append(f"{p.relative_to(root)}: {h!r}")
    return out


def lines(*rows) -> bytes:
    now = datetime.now(UTC).isoformat()
    return ("\n".join(json.dumps({"ts": now, **r}) for r in rows) + "\n").encode()


@pytest.fixture(params=["sqlite", "postgresql"])
def env(request, tmp_path):
    if request.param == "postgresql" and not PG_URL:
        pytest.skip("WATCHOVER_TEST_DATABASE_URL not set")
    home = tmp_path / "home"
    mp = pytest.MonkeyPatch()
    vals = {"WATCHOVER_HOME": str(home), "LIVE_SPOOL": str(home / "live" / "events.jsonl"), "WATCHOVER_MODELS_DIR": str(home / "models"),
            "PLAYBOOK_DB": str(home / "playbook.db"), "ACTIONS_DB": str(home / "actions.db"), "WATCHOVER_API_BACKGROUND": "0",
            "WATCHOVER_SELFMON": "0", "WATCHOVER_DEMO": "1", "OLLAMA_URL": "http://127.0.0.1:9"}
    if request.param == "postgresql":
        from watchover.db import Database
        scratch = Database(PG_URL)
        scratch._exec("DROP SCHEMA public CASCADE"); scratch._exec("CREATE SCHEMA public"); scratch.close()
        vals.update(DATABASE_URL=PG_URL, WATCHOVER_LOG_STORE="files")             # the OpenShift layout: PostgreSQL + hot / cold log files
    else:
        vals.update(KNOWLEDGE_DB=str(home / "knowledge.db"), WATCHOVER_LOG_STORE="db")
        mp.delenv("DATABASE_URL", raising=False)
    for k, v in vals.items():
        mp.setenv(k, v)
    home.mkdir(parents=True)
    mp.chdir(tmp_path)                                       # no stray SQLite file of the working folder is migrated into PostgreSQL
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.auth import INITIAL_EMAIL, INITIAL_PASSWORD
    app = create_app()
    svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0 WHERE email=?", (INITIAL_EMAIL,))
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": INITIAL_EMAIL, "password": INITIAL_PASSWORD})
        c.h = {"Authorization": f"Bearer {r.json()['token']}"}
        c.svc, c.home, c.backend = svc, home, request.param
        yield c
    svc.close()
    mp.undo()


def wait_jobs(c) -> None:
    for _ in range(120):
        if all(j["state"] != "running" for j in c.get("/api/datasets/jobs", headers=c.h).json()):
            return
        time.sleep(0.25)


def scan_db(c) -> list:
    svc = c.svc
    out = [h for t, rows in dump(svc.kb).items() for r in rows for h in row_hits(t, r)]
    if svc.playbook.db is not svc.kb:
        out += [f"playbook:{h}" for t, rows in dump(svc.playbook.db).items() for r in rows for h in row_hits(t, r)]
    return out


def test_simulation_off_leaves_nothing(env):
    c, svc, kb, h = env, env.svc, env.svc.kb, env.h
    from watchover import live as wo_live, techstack, autolearn, profiles
    # ---- the simulator on an empty installation; its generators run many ticks (incident bursts included)
    r = c.post("/api/live/simulate", json={"on": True}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "on", r.text
    rng, mstate = random.Random(5), {}
    for i in range(40):
        inc = 10 <= i < 20
        svc._simulate_users(rng, inc)
        svc.live.ingest("sim.jsonl", wo_live.simulate_batch(rng, 12, inc, traces=True), demo.SIM_AGENT)
        svc.live.ingest("metrics.jsonl", wo_live.simulate_metrics(rng, mstate, inc), demo.SIM_AGENT)
        if i % 5 == 0:
            svc.live.ingest("flows.jsonl", wo_live.simulate_flows(rng), demo.SIM_AGENT)
            svc.live.ingest("otel.jsonl", techstack.simulate_tech_lines(rng), demo.SIM_AGENT)
    svc.live.ingest("metrics.jsonl", lines({"host": "prd-api-01", "env": "prod", "cpu": 99, "memory": 50}), demo.SIM_AGENT)
    assert svc.sim_stop is not None and svc.live.is_demo_host("prd-api-01") and not svc.live.is_demo_host(REAL_HOST)
    assert all(demo.is_demo(o) for o in svc.live.buf)
    # derived data of the demo hosts: thresholds, anomalies, alerts, rollups, topology, inventory promotion
    svc.thresholds.save({"name": "cpu", "scope": "all", "metric": "cpu", "warn": 50, "crit": 95, "for_min": 0, "channels": "email", "targets": "ops@example.com"})
    svc.notifier.add_rule("hata", "errors", threshold=0.1, cooldown_min=0)
    svc.notifier.add_rule("anomali", "anomaly", threshold=0, cooldown_min=0)
    svc.thresholds.evaluate(); svc.anomalies.scan(); svc.alerts.evaluate()
    svc.inventory.promote([{"hostname": "ml-02", "env": "qa"}])
    for fn in (svc.history.flush, svc.traces.flush, svc.techstack.flush, svc.apps.flush_namespaces):
        fn()
    act_on = True
    try:
        svc.act.set_mode("suggest", "admin@watchover.local")
    except Exception:  # noqa: BLE001 - key custody unavailable here: the agent stays off
        act_on = False
    an = svc.anomalies.list("open")
    assert an and all(a["demo"] for a in an), an
    for a in an:                                              # simulator anomalies reach neither the company memory nor the action agent
        svc._on_anomaly_open(a)
    svc.act.drain()
    assert not kb._exec("SELECT id FROM actions WHERE kind='case'")
    assert kb._exec("SELECT COUNT(*) AS n FROM alerts WHERE demo=1 AND title LIKE '[DEMO]%'")[0]["n"] > 0
    assert kb._exec("SELECT COUNT(*) AS n FROM threshold_state WHERE demo=1")[0]["n"] > 0
    for t in ("spans", "svc_minute", "net_flows", "tech_seen", "biz_events", "rum_sessions", "inventory", "rum_apps", "biz_flows"):
        assert kb._exec(f"SELECT COUNT(*) AS n FROM {t} WHERE demo=1")[0]["n"] > 0, t
    # the assistant side in demo context: a saved chat, a rating, an LLM call, an LLM rule proposal, an evaluation run
    q, a_ = "prd-api-01 neden yavaş?", "prd-api-01 üzerinde db-01 zaman aşımı (payment-api)"
    assert c.post("/api/assist/save", json={"question": q, "answer": a_}, headers=h).status_code == 201
    assert c.post("/api/assist/rate", json={"question": q, "answer": a_, "verdict": "up"}, headers=h).status_code == 200
    kb.log_call({"provider": "ollama", "model": "m", "kind": "chat", "ok": 1, "prompt_chars": 10})
    kb.propose("owner", "checkout-api", "team-x", "llm", "llm:m")
    kb.save_eval("m", "live", {"n": 1, "correct": 1, "cited": 1, "grounded": 1, "latency_ms": 3})
    assert c.post("/api/assist/ask", json={"question": q, "dataset": "live", "mcp": False}, headers=h).status_code == 200
    for t in ("lessons", "answer_feedback", "llm_calls", "rules", "eval_runs"):
        assert kb._exec(f"SELECT COUNT(*) AS n FROM {t} WHERE demo=1")[0]["n"] == 1, t
    # demo datasets: the Datasets demo button, a live snapshot while demo data is shown; the ITSM demo set
    assert c.post("/api/datasets/demo", headers=h).json()["demo"] is True
    assert c.post("/api/live/analyze", headers=h).status_code == 202
    wait_jobs(c)
    assert c.put("/api/itsm/config", json={"system": "demo"}, headers=h).status_code == 200
    assert c.get("/api/itsm/tickets", headers=h).json()["tickets"]
    # ---- nothing above was learned
    svc.memory.learner.min_events = 1
    assert svc.memory.learner.learn_once()["events"] == 0                      # the learner's window holds no simulator line
    assert not kb._exec("SELECT id FROM lessons WHERE kind IN ('anomaly', 'pattern', 'resolution') OR dataset = 'demo_mixed.zip'")
    for t in ("anomaly_learning", "anomaly_signal", "anomaly_templates", "drain_clusters"):
        assert not kb._exec(f"SELECT * FROM {t}"), t
    assert not [r for r in kb._exec("SELECT * FROM playbook")] if svc.playbook.db is kb else not svc.playbook.db._exec("SELECT * FROM playbook")
    assert not profiles.load(), "column profiles learned from simulator streams"
    ex, _n = autolearn.training_examples(kb)
    assert not [x for x in ex if hit(json.dumps(x, ensure_ascii=False))]
    # ---- a real agent arrives: the simulator stops at once (its data waits for the purge, nothing real is refused)
    rec, _tok = svc.agents.enroll(REAL_AGENT, env="prod")
    with pytest.raises(ValueError):
        svc.agents.enroll("simulator")
    svc.live.ingest("app.jsonl", lines(*[{"level": "error" if i % 3 == 0 else "info", "service": "shop-web", "host": REAL_HOST, "env": "prod",
                                          "msg": f"order {i} checkout failed: card declined" if i % 3 == 0 else f"order {i} accepted"} for i in range(30)]), REAL_AGENT)
    assert svc.sim_stop is None and c.get("/api/live/simulate", headers=h).json()["state"] == "paused"
    assert svc.demo_notice["sender"] == REAL_AGENT
    assert c.post("/api/live/simulate", json={"on": True}, headers=h).status_code == 409    # no mixing: never again next to real data
    svc.live.ingest("app.jsonl", lines({"level": "info", "service": "shop-web", "host": REAL_HOST, "env": "prod", "msg": "GET /basket 200 1500ms",
                                        "trace_id": "ab" * 16, "duration_ms": 1500}), REAL_AGENT)
    svc.live.ingest("hostload.jsonl", lines({"host": REAL_HOST, "env": "prod", "cpu": 98, "memory": 40}), REAL_AGENT)
    svc.live.ingest("conns.jsonl", lines({"host": REAL_HOST, "env": "prod", "msg": "flow out nginx 10.9.9.9:5432", "kind": "flow", "flow.dir": "out",
                                              "flow.process": "nginx", "flow.remote": "10.9.9.9:5432", "flow.count": 2}), REAL_AGENT)
    svc.live.ingest("otel-real.jsonl", lines({"level": "info", "service": "shop-api", "host": REAL_HOST, "env": "prod", "msg": "GET /api/items 200",
                                              "trace_id": "cd" * 16, "duration_ms": 12, "telemetry.sdk.language": "python", "otel.scope.name": "opentelemetry.instrumentation.fastapi"}), REAL_AGENT)
    flow = svc.bizflows.save("Gerçek akış", r"talep\s+(\d{5})", [{"name": "Açıldı", "match": "açıldı"}, {"name": "Kapandı", "match": "kapandı"}])
    svc.live.ingest("app.jsonl", lines({"level": "info", "service": "shop-web", "host": REAL_HOST, "env": "prod", "msg": "talep 12345 açıldı"}), REAL_AGENT)
    rapp = svc.rum.create_app("musteri-portal", "web")
    now_ms = datetime.now(UTC).timestamp() * 1000
    svc.rum.ingest(rapp, {"session": "realsess1", "platform": "web", "events": [{"t": "view", "ts": now_ms, "name": "/sepet", "dur": 120},
                                                                              {"t": "error", "ts": now_ms, "msg": "TypeError in cart"}]}, "Mozilla/5.0 Chrome/130.0")
    svc.inventory.upsert({"hostname": REAL_HOST, "env": "prod", "owner": "platform"})
    svc.thresholds.evaluate(); svc.anomalies.scan(); svc.alerts.evaluate()
    for fn in (svc.history.flush, svc.traces.flush, svc.techstack.flush, svc.apps.flush_namespaces):
        fn()
    real_an = [a for a in svc.anomalies.list("open") if a["host"] == REAL_HOST]
    assert real_an and not any(a["demo"] for a in real_an)
    for a in real_an:
        svc._on_anomaly_open(a)                               # real anomalies are remembered (and reach the agent when it is on)
    svc.act.drain()
    print("act mode on:", act_on, "cases:", [r["host"] for r in kb._exec("SELECT host FROM actions WHERE kind='case'")])
    if act_on:
        assert {r["host"] for r in kb._exec("SELECT host FROM actions WHERE kind='case'")} <= {REAL_HOST}
    assert c.post("/api/knowledge/notes", json={"title": "kart", "text": "card declined: ödeme sağlayıcısını arayın"}, headers=h).status_code in (200, 201)
    files = [("files", ("real.jsonl", lines(*[{"level": "error", "service": "billing", "host": "bill-01", "msg": f"invoice {i} export failed: disk full"}
                                              for i in range(40)]), "application/octet-stream"))]
    assert c.post("/api/datasets", files=files, headers=h).status_code == 202
    wait_jobs(c)
    # ---- old data (written before the markers existed): rows the fallback recognises by the simulator's names / templates
    kb._exec("INSERT INTO anomaly_learning (host, env, started, graduated_at) VALUES ('prd-api-02', 'prod', '2026-01-01', '')")
    kb._exec("INSERT INTO team_hosts (team_id, host) VALUES (99, 'worker-01')")
    kb._exec("INSERT INTO integ_tech (host, target, port, owner) VALUES ('prd-api-03', '10.0.0.1', 5432, '')")
    kb._exec("INSERT INTO scan_results (hostname, ip) VALUES ('ml-01', '172.16.9.9')")
    kb._exec("INSERT INTO lessons (kind, key, title, text, created_at, updated_at) VALUES ('anomaly', 'anomaly:x', 'prd-api-02: cpu', 'host: prd-api-02', '2026-01-01', '2026-01-01')")
    kb._exec("INSERT INTO actions (incident_id, title, kind, host, case_key, created_at, updated_at) VALUES ('C-1', 'old demo case', 'case', 'prd-api-01', 'k', '2026-01-01', '2026-01-01')")
    cid = kb._exec("SELECT id FROM actions WHERE case_key='k'")[0]["id"]
    kb._exec("INSERT INTO act_triggers (case_id, ts, source, key, host) VALUES (?, '2026-01-01', 'anomaly', 'k', 'prd-api-01')", (cid,))
    pr = profiles.load(); pr["old"] = {"id": "old", "format": "jsonl", "keys": ["a"], "mapping": {"message": "a"}, "name": "sim.jsonl", "source": "auto"}
    profiles.save(pr)
    from watchover.autotrain import AutoTrainer
    at = AutoTrainer(kb)
    mdir = c.home / "models"
    for name, text, ts in (("run-real", "web-real-01 card declined → kartı yenile", "2026-01-01T00:00:00+00:00"),
                           ("run-demo", "prd-api-01 timeout to db-01 → restart", "2026-01-02T00:00:00+00:00")):
        (mdir / name / "adapter").mkdir(parents=True)
        (mdir / name / "train.jsonl").write_text(json.dumps({"messages": [{"role": "user", "content": text}]}, ensure_ascii=False) + "\n", encoding="utf-8")
        at._record(ts=ts, status="pass", examples=1, model="watchover-ops", note=str(mdir / name))
    svc._ops_at = 0
    assert svc.ops_ready()["ts"].startswith("2026-01-02")                        # the demo-trained fine-tune is the active one
    # ---- switch off: the counts first (409), then confirmed
    before = dump(kb)
    hot = sorted((c.home / "logs" / "hot").glob("logs-*.db")) if c.backend == "postgresql" else []
    hot_before = {p: dump_sqlite(p) for p in hot}
    pb_before = dump(svc.playbook.db) if svc.playbook.db is not kb else {}
    spool_before = svc.live.spool.read_text(encoding="utf-8")
    r = c.post("/api/live/simulate", json={"on": False}, headers=h)
    assert r.status_code == 409, r.text
    pv = r.json()["detail"]["preview"]
    assert r.json()["detail"]["code"] == "confirm" and "LLM hafızası" in r.json()["detail"]["tr"]
    for cat in ("lines", "metrics", "traces", "anomalies", "alerts", "topology", "bizflow", "actions", "datasets", "llm_memory", "models"):
        assert pv["categories"][cat] > 0, (cat, pv["counts"])
    assert c.get("/api/live/simulate/preview", headers=h).json()["categories"] == pv["categories"]
    assert dump(kb) == before                                                   # the preview deleted nothing
    out = c.post("/api/live/simulate", json={"on": False, "confirm": True}, headers=h).json()
    assert out["state"] == "off" and out["purge"]["total"] >= pv["total"] > 0, out
    print("purge counts:", json.dumps(out["purge"]["categories"], sort_keys=True), json.dumps(out["purge"]["counts"], sort_keys=True))
    # ---- spotless: every table, every file under the data directory
    assert scan_db(c) == []
    assert file_hits(c.home) == []
    # ---- real data fully intact: every real row from before is still there, unchanged
    def real_rows(d: dict, skip=("meta", "train_runs")) -> dict:      # train_runs: checked below (Ollama is unreachable here)
        return {t: {canon(r) for r in rows if not row_hits(t, r) and not (t == "train_runs" and "run-demo" in str(r.get("note")))}
                for t, rows in d.items() if t not in skip}
    after = dump(kb)
    for t, rows in real_rows(before).items():
        assert rows <= {canon(r) for r in after.get(t, [])}, (t, sorted(rows - {canon(r) for r in after.get(t, [])})[:3])
    for p, d in hot_before.items():
        a2 = dump_sqlite(p) if p.exists() else {}
        for t, rows in real_rows(d).items():
            assert rows <= {canon(r) for r in a2.get(t, [])}, (p.name, t)
    for t, rows in real_rows(pb_before).items():
        assert rows <= {canon(r) for r in dump(svc.playbook.db).get(t, [])}, t
    real_spool = [ln for ln in spool_before.splitlines() if not demo.spool_line_demo(ln)]
    assert real_spool and svc.live.spool.read_text(encoding="utf-8").splitlines() == real_spool
    assert (mdir / "run-real" / "train.jsonl").exists() and not (mdir / "run-demo").exists()
    real_run = kb._exec("SELECT status, note FROM train_runs WHERE note LIKE '%run-real%'")             # the real run's row and adapter stay;
    assert real_run and real_run[0]["status"] == "retired" and "re-pointed" in real_run[0]["note"]     # it waits for a new training only
    assert real_run[0]["note"].endswith("run-real") or "run-real" in real_run[0]["note"]                # because Ollama could not be reached
    assert svc.ops_ready() is None                                               # the demo-trained fine-tune is out of use (Ollama is
    assert kb._exec("SELECT status FROM train_runs WHERE note LIKE '%run-demo%'")[0]["status"] == "retired"   # unreachable here, so
    assert svc.llm_cfg("ops").model != svc.OPS_MODEL                             # "watchover-ops" itself is never used again)
    assert [x["name"] for x in profiles.all_profiles()] and all(x["name"] not in demo.SIM_FILES for x in profiles.all_profiles())
    assert {f["name"] for f in svc.bizflows.list()} == {"Gerçek akış"} and kb._exec("SELECT COUNT(*) AS n FROM biz_events WHERE flow_id=?", (flow["id"],))[0]["n"] == 1
    assert {a["name"] for a in svc.rum.apps()} == {"musteri-portal"} and [d["name"] for d in c.get("/api/datasets", headers=h).json()] == ["real.jsonl"]
    assert kb._exec("SELECT COUNT(*) AS n FROM lessons WHERE kind='note'")[0]["n"] == 1 and kb._exec("SELECT COUNT(*) AS n FROM lessons WHERE kind='anomaly'")[0]["n"] >= 1
    # ---- memory, views, the assistant, MCP tools and exports show nothing of the simulation
    assert not svc.live.demo_hosts and demo.SIM_AGENT not in svc.live.agents and not [o for o in svc.live.buf if demo.is_demo(o)]
    assert set(svc.live.hosts()) == {REAL_HOST, "rum:musteri-portal"} and not [m for m in svc.live.metrics if m[1] != REAL_HOST]
    assert REAL_HOST in json.dumps(c.get("/api/live/hosts", headers=h).json())
    from watchover import assistant
    ctx, src = assistant.build_context("prd-api-01 db-01 payment-api checkout-api timeout neden?", svc.live_analysis(), kb)
    assert not hit(ctx) and not hit(json.dumps(src, ensure_ascii=False)) and "## DATASET" in ctx     # the real live window, nothing else
    ans = c.post("/api/assist/ask", json={"question": "prd-api-01 db-01 payment-api timeout?", "dataset": "live", "mcp": False}, headers=h).json()
    assert not hit(json.dumps(ans, ensure_ascii=False))
    views = ["/api/live/hosts", "/api/live/stats", "/api/live/events?n=500", "/api/anomalies?status=all", "/api/anomalies/learning?host=web-real-01", "/api/anomalies/stats", "/api/inventory",
             "/api/inventory/export", "/api/inventory/discovered", "/api/rum/apps", "/api/rum/sessions", "/api/apps", "/api/flows", "/api/datasets",
             "/api/knowledge/lessons", "/api/knowledge/search?q=prd-api-01", "/api/knowledge/rules", "/api/thresholds/state", "/api/thresholds/overview",
             "/api/notify/alerts", "/api/apm/services", "/api/apm/topology", "/api/apm/hosts", "/api/traces", "/api/dash/infra", "/api/dash/alarms",
             "/api/actions", "/api/itsm/tickets", "/api/logs/search?q=", "/api/llm/quality", "/api/llm/training-export", "/api/system/status"]
    for v in views:
        r = c.get(v, headers=h)
        assert r.status_code == 200, (v, r.status_code, r.text[:200])
        assert not hit(r.text), (v, hit(r.text), r.text[:300])
    key = c.post("/api/mcp/keys", json={"name": "k", "kind": "user", "email": "admin@watchover.local"}, headers=h).json()["token"]
    for name, args in (("log_search", {"q": ""}), ("dash_infra", {}), ("dash_alarms", {}), ("list_anomalies", {"status": "all"}), ("thresholds_state", {"overview": True}),
                       ("integration_hosts", {}), ("search_traces", {}), ("list_flows", {}), ("list_actions", {}), ("list_applications", {})):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
                   headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200 and not hit(r.text), (name, r.text[:300])
    # ---- after the purge the assistant is real again: new rows are not demo, the installation is spotless and stays so
    assert c.post("/api/assist/save", json={"question": "kart reddi?", "answer": "sağlayıcıyı arayın"}, headers=h).status_code == 201
    kb.log_call({"provider": "ollama", "model": "m", "kind": "chat", "ok": 1})
    assert kb._exec("SELECT COUNT(*) AS n FROM lessons WHERE demo=1")[0]["n"] == 0 and kb._exec("SELECT COUNT(*) AS n FROM llm_calls WHERE demo=1")[0]["n"] == 0
    assert c.post("/api/live/simulate", json={"on": False}, headers=h).json()["purge"] is None    # nothing left to confirm
    assert scan_db(c) == [] and file_hits(c.home) == []


def test_every_table_is_classified(env):
    """A new store must be added to demo.STORES: every table is purged by a marker, by demo host names, or exempt for a reason."""
    svc = env.svc
    known = {t for t, _k in demo.FLAGGED} | {t for t, _c, _k in demo.HOST_TABLES} | set(demo.MARKED_TABLES) | set(demo.EXEMPT)
    missing = sorted(t for t in svc.kb.tables() if t not in known and not t.startswith(demo.ACT_EXEMPT_PREFIX))
    assert missing == []
    for t, _k in demo.FLAGGED:
        if t in svc.kb.tables():
            assert demo.DEMO_COL in svc.kb.columns(t), t


def test_production_hides_and_refuses_demo_content(env, monkeypatch):
    c, h = env, env.h
    monkeypatch.setenv("WATCHOVER_DEMO", "0")
    assert c.post("/api/live/simulate", json={"on": True}, headers=h).status_code == 403
    assert c.post("/api/datasets/demo", headers=h).status_code == 403
    assert c.get("/api/datasets/samples", headers=h).json() == []
    assert c.post("/api/datasets/samples/demo_mixed.zip", headers=h).status_code == 403
    cfg = c.get("/api/itsm/config", headers=h).json()
    assert "demo" not in cfg["systems"] and cfg["system"] == ""
    assert c.put("/api/itsm/config", json={"system": "demo"}, headers=h).status_code == 403
    assert c.get("/api/itsm/tickets", headers=h).json()["tickets"] == []


def test_reserved_sender_and_spoofed_marker_stay_real(env):
    """A real sender cannot pass for the simulator, and a real line carrying the marker attribute is stripped of it."""
    svc = env.svc
    svc.live.ingest("x.jsonl", lines({"host": "edge-01", "msg": "hello", demo.DEMO_ATTR: 1, "demo": 1}), "edge-agent")
    o = [o for o in svc.live.buf if o.host == "edge-01"][0]
    assert not demo.is_demo(o) and not svc.live.is_demo_host("edge-01")
    assert demo.sender_name("Simulator") == "Simulator-ext" and demo.sender_name("web-01") == "web-01"
    # an installation the simulator never ran on: a real server that happens to carry a simulator host name keeps everything
    svc.kb._exec("INSERT INTO team_hosts (team_id, host) VALUES (1, 'worker-01')")
    svc.kb._exec("INSERT INTO anomalies (kind, key, host, title, first_seen, last_seen) VALUES ('metric', 'cpu:worker-01', 'worker-01', 'cpu', '2026-01-01', '2026-01-01')")
    out = demo.purge(svc)
    assert svc.kb._exec("SELECT COUNT(*) AS n FROM team_hosts")[0]["n"] == 1 and svc.kb._exec("SELECT COUNT(*) AS n FROM anomalies")[0]["n"] == 1
    assert [o.host for o in svc.live.buf] == ["edge-01"] and out["total"] == 0, {k: v for k, v in out["counts"].items() if v}
