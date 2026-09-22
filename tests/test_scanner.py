"""Vulnerability scanner: version compare, advisory match with confidence, posture checks, store and RBAC-relevant flow."""
from watchover import scanner as sc
from watchover.knowledge import Knowledge
from watchover.scanner import ScanStore


def test_version_compare_edge_cases():
    assert sc._lt("9.3p2", "9.8") and not sc._lt("9.8", "9.8") and not sc._lt("9.9", "9.8")
    assert sc._lt("1.9.4", "1.9.5p2") and not sc._lt("1.9.5p2", "1.9.5p2")
    assert sc._lt("1.18.0", "1.25.3") and not sc._lt("2.0", "1.25.3")


def test_detect_and_match_confirmed_vs_unconfirmed():
    cat = sc.load_catalog()
    conf = sc.match_advisories(sc.detect_products("OpenSSH 8.2 nginx 1.18.0"), cat)
    assert any(f["id"] == "CVE-2024-6387" and f["confidence"] == "confirmed" for f in conf)
    # a product with no version is unconfirmed, never dropped
    unc = sc.match_advisories([("openssh", "")], cat)
    assert unc and all(f["confidence"] == "unconfirmed" for f in unc)
    # a patched version yields nothing
    assert not sc.match_advisories([("openssh", "9.9")], cat)


def test_posture_checks():
    ids = {f["id"] for f in sc.posture_checks({"os": "CentOS 7", "criticality": "critical", "status": "active", "monitoring": "", "owner": ""})}
    assert {"EOL-OS", "NO-MONITORING", "NO-OWNER"} <= ids
    assert not any(f["id"] == "EOL-OS" for f in sc.posture_checks({"os": "Ubuntu 24.04", "criticality": "low", "monitoring": "agent"}))


def test_scan_record_sorted_and_worst():
    cat = sc.load_catalog()
    res = sc.scan_record({"hostname": "db-01", "os": "CentOS 7", "application": "OpenSSH 8.2", "criticality": "critical", "status": "active", "monitoring": "", "owner": ""}, cat)
    sevs = [sc.SEV_RANK[f["severity"]] for f in res["findings"]]
    assert sevs == sorted(sevs, reverse=True) and res["worst"] == "high" and res["counts"]["high"] >= 1
    assert res["probed"] is False and res["open_ports"] == []


def test_store_persists_and_stats(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    st = ScanStore(kb)
    hosts = [{"hostname": "web-01", "os": "Ubuntu 18.04", "application": "nginx 1.18.0", "criticality": "high", "monitoring": "", "owner": "ali", "status": "active"},
             {"hostname": "web-02", "os": "Ubuntu 24.04", "application": "nginx 1.27", "criticality": "normal", "monitoring": "agent", "owner": "veli", "status": "active"}]
    run = st.scan_all(hosts)
    assert run["hosts"] == 2 and run["findings"] >= 1
    rows = st.results()
    assert rows[0]["hostname"] == "web-01" and rows[0]["worst"] in ("high", "critical")
    assert st.result("web-01")["findings"] and st.stats()["hosts_scanned"] == 2 and st.stats()["catalog"] >= 40
    # rescan is idempotent (upsert, not duplicate)
    st.scan_host(hosts[0]); assert len(st.results()) == 2
