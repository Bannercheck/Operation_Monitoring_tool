"""Business flows (one key followed across systems: funnel, stuck and failed items, one key's path, step suggestions) and the
exceptions view (error lines grouped by what failed and the service)."""
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from watchover.bizflow import BizFlows
from watchover.db import Database
from watchover.exceptions import exception_type, group
from watchover.live import simulate_orders
from watchover.pipeline import ingest_bytes

UTC = timezone.utc
STEPS = [{"name": "Oluşturuldu", "match": "oluşturuldu"}, {"name": "Kredi onayı", "match": "kredi kontrolü onaylandı"},
         {"name": "Sevkiyat", "match": "sevkiyat"}, {"name": "Fatura", "match": "fatura"}]


def _orders(bf, minutes=40, incident=(200, 260)):
    rng = random.Random(1); t0 = datetime.now(UTC) - timedelta(minutes=minutes); obs_all = []
    for i in range(minutes * 15):
        lines = simulate_orders(rng, t0 + timedelta(seconds=4 * i), incident=incident[0] < i < incident[1])
        if lines:
            obs, _ = ingest_bytes("o.jsonl", ("\n".join(lines) + "\n").encode())
            bf.add_events(obs); obs_all += obs
    return obs_all


def test_funnel_stuck_and_failures():
    bf = BizFlows(Database(":memory:"))
    with pytest.raises(ValueError):
        bf.save("x", r"sipariş \d+", STEPS)                  # no capture group
    with pytest.raises(ValueError):
        bf.save("x", r"sipariş (\d+)", STEPS[:1])            # one step is no flow
    f = bf.save("Sipariş → Fatura", r"sipariş\s+(\d{7})", STEPS)
    obs = _orders(bf)
    fun = bf.funnel(f["id"], None)
    s = fun["steps"]
    assert fun["items"] > 500 and 0.85 < fun["completion"] < 1
    assert s[0]["reached"] >= s[1]["reached"] >= s[2]["reached"] >= s[3]["reached"] == fun["completed"]
    assert s[0]["stuck"] > 10 and s[1]["errors"] == s[0]["stuck"]          # timed-out credit checks: stuck after step 1, failed at step 2
    assert s[0]["to_next_p50_s"] and s[3]["to_next_p50_s"] is None and s[0]["stuck_after_s"] >= 60
    stuck = bf.items(f["id"], "stuck", None, 500)
    assert len(stuck) == s[0]["stuck"] and all(x["last_step"] == "Oluşturuldu" and x["failure"]["at"] == "Kredi onayı" for x in stuck)
    one = bf.item(f["id"], stuck[0]["key"])
    assert one["steps"][0]["service"] == "checkout-api" and one["steps"][1]["missing"] and "zaman aşımı" in one["failure"]["message"]
    done = bf.items(f["id"], "done", None, 5)
    assert done and all(x["steps_done"] == 4 and not x["stuck"] for x in done)
    assert bf.funnel(f["id"], None, hosts={"nope"})["items"] == 0        # team scope: hosts outside the team see nothing
    sug = BizFlows.suggest(r"sipariş\s+(\d{7})", obs)
    assert [x["service"] for x in sug[:4]] == ["checkout-api", "payment-api", "worker", "billing-api"]


def test_hashed_keys_and_exceptions():
    bf = BizFlows(Database(":memory:"))
    f = bf.save("Başvuru", r"tckn[=: ]+(\d{11})", [{"name": "Alındı", "match": "alındı"}, {"name": "Onay", "match": "onay"}], hash_keys=True)
    now = datetime.now(UTC).isoformat()
    body = "\n".join(json.dumps({"ts": now, "level": "info", "service": "ik", "msg": m}) for m in ("Başvuru alındı tckn=12345678901", "Başvuru onaylandı tckn=12345678901"))
    obs, _ = ingest_bytes("a.jsonl", (body + "\n").encode())
    bf.add_events(obs)
    rows = bf.items(f["id"], "done", None)
    assert len(rows) == 1 and "12345678901" not in json.dumps(rows) and "12345678901" not in json.dumps(bf.item(f["id"], rows[0]["key"]))
    assert exception_type("java.lang.NullPointerException at Foo") == "java.lang.NullPointerException"
    assert exception_type("ORA-00060: deadlock detected") == "ORA-00060" and exception_type("upstream status=503") == "HTTP 503"
    lines = [{"ts": now, "level": "error", "service": "pay", "host": f"h{i % 3}", "msg": f"Timeout calling bank after 5000 ms id={i} user=a{i}@corp.com"} for i in range(9)]
    lines += [{"ts": now, "level": "error", "service": "pay", "host": "h1", "msg": "ORA-00060: deadlock"}, {"ts": now, "level": "info", "service": "pay", "msg": "ok"}]
    obs, _ = ingest_bytes("e.jsonl", ("\n".join(json.dumps(x) for x in lines) + "\n").encode())
    g = group(obs, 60)
    assert [(x["type"], x["count"], x["host_count"]) for x in g] == [("Timeout", 9, 3), ("ORA-00060", 1, 1)]
    assert "@corp.com" not in g[0]["sample"] and sum(g[0]["trend"]) == 9


def test_flows_api(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as c:
        h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Test-Yonetici-Parola-42"}).json()["token"]}
        f = c.post("/api/flows", json={"name": "Sipariş", "key_regex": r"sipariş\s+(\d{7})", "steps": STEPS}, headers=h)
        assert f.status_code == 201, f.text
        fid = f.json()["id"]
        rng = random.Random(2); now = datetime.now(UTC) - timedelta(minutes=10)
        for i in range(120):
            lines = simulate_orders(rng, now + timedelta(seconds=4 * i), incident=40 < i < 70)
            if lines:
                svc.live.ingest("o.jsonl", ("\n".join(lines) + "\n").encode(), "agent")
        lst = c.get("/api/flows", headers=h).json()
        assert lst[0]["id"] == fid and lst[0]["items"] > 50 and len(lst[0]["funnel"]) == 4
        assert c.get(f"/api/flows/{fid}/funnel", headers=h).json()["steps"][1]["errors"] > 0
        err = c.get(f"/api/flows/{fid}/items?state=error", headers=h).json()
        assert err and c.get(f"/api/flows/{fid}/items/{err[0]['key']}", headers=h).json()["failure"]
        sg = c.post("/api/flows/suggest", json={"key_regex": r"sipariş\s+(\d{7})"}, headers=h).json()["steps"]
        assert len(sg) >= 4
        ex = c.get("/api/apm/exceptions?minutes=60", headers=h).json()["groups"]
        assert ex[0]["type"] == "Timeout" and ex[0]["service"] == "payment-api"
        assert c.put(f"/api/flows/{fid}", json={"name": "Sipariş 2", "key_regex": r"sipariş\s+(\d{7})", "steps": STEPS[:2]}, headers=h).json()["name"] == "Sipariş 2"
        assert c.delete(f"/api/flows/{fid}", headers=h).status_code == 204 and c.get("/api/flows", headers=h).json() == []
