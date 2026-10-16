"""Live rollups for 200+ servers: per-minute counters kept at ingest answer the operations screen (totals, SLO, environments),
pools (inventory cluster, else the name stem), the per-host table with silent hosts, and deduplicated log groups with host /
pool filters and team scope."""
import json
from datetime import datetime, timedelta, timezone

from watchover.live import LiveStore
from watchover.rollup import pool_stem

UTC = timezone.utc


def _feed(st, now=None):
    now = (now or datetime.now(UTC)).isoformat()
    for i in range(30):
        host = f"prd-web-{i % 10 + 1:02d}" if i < 20 else f"prd-db-{i % 3 + 1:02d}"
        lines = [json.dumps({"ts": now, "level": "error" if j % 10 == 0 else "info", "service": "web" if i < 20 else "db", "env": "prod",
                             "msg": f"Timeout to bank after {j} ms id={j}" if j % 10 == 0 else f"GET /x/{j} 200 {j * 3}ms"}) for j in range(50)]
        st.ingest("app.jsonl", ("\n".join(lines) + "\n").encode(), agent=host)       # lines without a host: the agent's server


def test_rollup_counts_pools_and_groups():
    assert [pool_stem(h) for h in ("prd-sap-app-01", "web07.corp.local", "ik-db-a", "10.0.0.1")] == ["prd-sap-app", "web", "ik-db-a", "-"]
    st = LiveStore(spool=None)
    _feed(st)
    s = st.stats(15)
    assert s["errors"] == 150 and s["per_minute_now"] == 1500 and dict(s["services"]) == {"web": 1000, "db": 500}
    assert st.stats(15, host=("prd-db-01",))["total"] == 150
    slo = st.slo(15)
    assert slo["total"] == 1500 and abs(slo["availability"] - 0.9) < 1e-9 and slo["p95_ms"]
    assert st.slo_detail(15)["templates"][0]["count"] == 100 and st.env_summary(15)[0]["hosts"] == 13
    assert "prd-web-07" in st.hosts()
    pools = {p["pool"]: p for p in st.rollup.pools(15)}
    assert pools["prd-web"]["host_count"] == 10 and pools["prd-db"]["events"] == 500 and pools["prd-db"]["errors"] == 50
    g = st.rollup.groups(15)
    assert g["total"] == 1500 and g["distinct"] == 4                                 # 2 templates × 2 services
    top = g["groups"][0]
    assert top["count"] == 900 and top["host_count"] == 10 and top["pools"][0]["pool"] == "prd-web"
    err = st.rollup.groups(15, host=("prd-db-01", "prd-db-02"), min_sev="ERROR")["groups"]
    assert [(x["service"], x["count"], x["host_count"]) for x in err] == [("db", 30, 2)]   # db-01: 3 batches, db-02: 3 batches
    assert st.rollup.groups(15, q="bank")["groups"][0]["template"].startswith("timeout to bank")
    st.rollup.pool_fn = lambda h: "web-cluster-a" if h.startswith("prd-web") else None    # inventory cluster wins over the stem
    st.rollup._pool_cache.clear()
    assert {p["pool"] for p in st.rollup.pools(15)} == {"web-cluster-a", "prd-db"}


def test_silent_hosts_and_old_minutes():
    st = LiveStore(spool=None)
    _feed(st, datetime.now(UTC) - timedelta(minutes=8))
    rows = {r["host"]: r for r in st.rollup.host_table(15)}
    assert rows["prd-web-01"]["silent"] and rows["prd-web-01"]["events"] == 100
    _feed(st, datetime.now(UTC) - timedelta(hours=3))                                 # older than the kept minutes: not counted
    assert st.stats(15)["errors"] == 150


def test_live_api_pools_groups_and_scope(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("db@corp.com", "Str0ng!Passw0rd", role="operator")
    _feed(svc.live)
    with TestClient(a) as c:
        login = lambda e, p: {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": e, "password": p}).json()["token"]}   # noqa: E731
        admin = login("admin@watchover.local", "Test-Yonetici-Parola-42")
        p = c.get("/api/live/pools?window=15", headers=admin).json()
        assert p["hosts"] == 13 and {x["pool"] for x in p["pools"]} == {"prd-web", "prd-db"}
        assert c.get("/api/live/stats?window=15&pool=prd-db", headers=admin).json()["errors"] == 50
        g = c.get("/api/live/groups?window=15&pool=prd-db&severity=ERROR", headers=admin).json()
        assert g["total"] == 50 and g["groups"][0]["host_count"] == 3
        assert c.get("/api/live/groups?window=15&host=prd-web-01", headers=admin).json()["total"] == 100
        c.post("/api/teams", json={"name": "DB", "rules": [{"field": "host", "op": "glob", "value": "prd-db-*"}], "members": ["db@corp.com"]}, headers=admin)
        h = login("db@corp.com", "Str0ng!Passw0rd")
        assert {x["pool"] for x in c.get("/api/live/pools", headers=h).json()["pools"]} == {"prd-db"}
        assert c.get("/api/live/groups?window=15", headers=h).json()["total"] == 500
        assert c.get("/api/live/groups?window=15&pool=prd-web", headers=h).json()["total"] == 0          # another team's pool: nothing
