"""Parser v2 step 4: Drain template mining, grok proposals, persistence, and learned column mappings."""
import io
import zipfile

from watchover import drain, profiles
from watchover.history import History
from watchover.knowledge import Knowledge
from watchover.pipeline import ingest_bytes


def test_drain_clusters_and_masks():
    d = drain.Drain()
    lines = ["connection timeout to db-01 (10.0.4.30) after 5000ms user=ali request=88213", "connection timeout to db-02 (10.0.4.31) after 3200ms user=ayse request=88214",
             "user ali logged in from 10.0.4.12", "user ayse logged in from 10.0.4.13", "payment failed order=1 reason=gateway_timeout"]
    for ln in lines:
        d.add(ln)
    tops = d.top()
    assert [c.count for c in tops][:2] == [2, 2] and len(tops) == 3
    assert tops[0].template.startswith("connection timeout to <*> <*> after <*> user=<*> request=<*>") or tops[1].template.startswith("connection timeout")
    login = next(c for c in tops if "logged" in c.template)
    assert login.template == "user <*> logged in from <*>" and login.count == 2
    assert d.match("user veli logged in from 10.0.4.99") is login and d.match("totally different line here") is None


def test_propose_grok_types_variables():
    pat = drain.propose_grok("connection timeout to <*> <*> after <*> user=<*> request=<*>", "connection timeout to db-01 (10.0.4.30) after 5000ms user=ali request=88213")
    assert "\\(%{IPORHOST:f2}\\)" in pat and "%{QUANTITY:f3}" in pat and "user=%{NOTSPACE:user}" in pat and pat.startswith("^connection")
    from watchover import grok
    assert grok.test_pattern(pat, "connection timeout to db-09 (10.0.4.40) after 12ms user=x request=1")["matched"] == 1


def test_template_store_persists(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db")); History(kb)
    st = drain.TemplateStore(kb)
    assert st.learn(["disk usage 91% on /var", "disk usage 93% on /opt", "disk usage 88% on /var"], source="t") == 1
    assert st.stats() == {"clusters": 1, "events": 3} and st.list()[0]["template"] == "disk usage <*> on <*>"
    st2 = drain.TemplateStore(kb)                                   # a fresh process loads the same clusters
    assert st2.stats()["clusters"] == 1 and st2.learn(["disk usage 50% on /home"]) == 0 and st2.stats()["events"] == 4


def test_learned_mapping_applies_to_the_next_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path))
    csv = "zaman,seviye,makina,uygulama,aciklama\n2026-09-22 09:15:03,ERROR,db-01,postgres,deadlock detected\n2026-09-22 09:15:04,INFO,db-01,postgres,checkpoint complete\n"
    keys = ["zaman", "seviye", "makina", "uygulama", "aciklama"]
    obs, rep = ingest_bytes("first.csv", csv.encode(), {"timestamp": "zaman", "severity": "seviye", "host": "makina", "service": "uygulama", "message": "aciklama"})
    assert obs[0].host == "db-01" and rep[0]["roles"]["message"] == "aciklama"
    p = profiles.lookup("csv", keys)
    assert p and p["source"] == "user" and p["mapping"]["host"] == "makina"
    obs2, rep2 = ingest_bytes("second.csv", csv.replace("db-01", "db-02").encode())      # no mapping given: the learned one applies
    assert obs2[0].host == "db-02" and obs2[0].service == "postgres" and rep2[0]["profile"] == p["id"]
    assert profiles.lookup("csv", keys)["hits"] == 2
    profiles.remember("csv", keys, {"host": "uygulama"}, source="auto")                  # an auto guess never overwrites a user mapping
    assert profiles.lookup("csv", keys)["mapping"]["host"] == "makina"
    assert profiles.update(p["id"], {"host": "uygulama", "message": "aciklama"})["source"] == "user"
    assert profiles.delete(p["id"]) and profiles.lookup("csv", keys) is None
