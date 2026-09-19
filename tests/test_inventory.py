"""Inventory: CRUD, CSV with aliased columns, matching by name / alias / IP, event enrichment, discovery, engine shape."""
from watchover.inventory import Inventory
from watchover.knowledge import Knowledge
from watchover.live import LiveStore


def test_inventory_crud_csv_and_matching(tmp_path):
    inv = Inventory(Knowledge(str(tmp_path / "k.db")))
    r = inv.upsert({"hostname": "DB-01", "ip": "10.20.1.15", "aliases": "db01.corp.local, oradb1", "env": "PROD", "dc": "IST-DC1", "rack": "R12", "vlan": "VLAN-120",
                    "role": "database", "owner": "DBA", "criticality": "kritik", "storage": "SAN LUN-042"})
    assert r["hostname"] == "db-01" and r["env"] == "prod" and r["criticality"] == "critical" and r["status"] == "active"
    assert inv.upsert({"hostname": "db-01", "owner": "DBA-2"})["owner"] == "DBA-2" and len(inv.list()) == 1
    import pytest
    with pytest.raises(ValueError):
        inv.upsert({"hostname": " "})
    n, errs = inv.import_csv("Host;IP Adresi;Ortam;Veri Merkezi;Kabin;Servis;Ekip;Kritiklik;Depolama\napp-02;10.20.2.5;prod;ANK-DC2;R3;app server;Platform;yüksek;NAS-1\n".encode("utf-8-sig"))
    assert n == 1 and not errs and inv.get("app-02")["dc"] == "ANK-DC2" and inv.get("app-02")["criticality"] == "high" and inv.get("app-02")["source"] == "csv"
    n, _ = inv.import_csv(Inventory.csv_template())
    assert n == 1 and inv.get("db-01")["vendor_model"] == "Dell R760"
    assert b"hostname,ip" in inv.export_csv() and "app-02" in inv.export_csv().decode("utf-8-sig")
    assert inv.resolve("DB01.CORP.LOCAL")["hostname"] == "db-01" and inv.resolve("10.20.2.5")["hostname"] == "app-02" and inv.resolve("db-01.corp.local")["hostname"] == "db-01"
    assert inv.resolve("nope") is None
    # enrichment on ingest: IP host becomes hostname, env / dc / criticality travel with the event
    store = LiveStore(); store.enricher = inv.enrich
    store.ingest("x.jsonl", b'{"level":"error","msg":"disk full","host":"10.20.2.5"}\n{"level":"error","msg":"x","host":"ghost-9"}\n', agent="a")
    obs = store.snapshot()
    assert obs[0].host == "app-02" and obs[0].attributes["ip"] == "10.20.2.5" and obs[0].environment == "prod" and obs[0].attributes["inv_dc"] == "ANK-DC2"
    assert obs[0].attributes["inv_criticality"] == "high" and obs[0].attributes["site"] == "ANK-DC2" and store.matched == 1 and obs[1].host == "ghost-9"
    disc = inv.discovered(store.hosts(), [{"name": "web-07", "env": "test", "site": "IST", "last_ip": "10.1.1.7"}])
    assert [d["hostname"] for d in disc] == ["ghost-9", "web-07"] and disc[1]["dc"] == "IST"
    eng = inv.as_engine()
    assert eng["db-01"]["criticality"] == "critical" and eng["app-02"]["dc"] == "ANK-DC2" and eng["app-02"]["service"] == "app server"
    st = inv.stats(store.hosts())
    assert st["hosts"] == 2 and st["critical"] == 1 and st["matched"] == 1 and st["unmatched"] == 1
    inv.upsert({"hostname": "app-02", "status": "decommissioned"})
    assert inv.resolve("app-02") is None
    inv.delete("db-01"); assert inv.get("db-01") is None
