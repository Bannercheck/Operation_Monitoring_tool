"""The Watchover server's own agent (install-rhel.sh › host_agent): one row per host across re-runs, never the fleet key, and
the built-in in-container agent removed once the host has the real one."""
from watchover import selfagent, selfmon
from watchover.agents import AgentRegistry
from watchover.knowledge import Knowledge


def test_token_enrols_once_and_rotates_on_a_rerun(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "k.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("KNOWLEDGE_DB", db)
    reg = AgentRegistry(Knowledge(db))
    t1 = selfagent.token("wo-srv01")
    rows = [a for a in reg.list() if a["name"] == "wo-srv01"]
    assert len(rows) == 1 and rows[0]["env"] == "prod" and "watchover-server" in rows[0]["tags"] and reg.verify(t1)["name"] == "wo-srv01"
    reg.revoke(rows[0]["id"])
    assert selfagent.main(["token", "--name", "WO-SRV01"]) == 0           # a repair / upgrade: same row, active again, new token
    t2 = capsys.readouterr().out.strip()
    assert t2 != t1 and reg.verify(t2)["id"] == rows[0]["id"] and reg.verify(t1) is None
    assert len([a for a in reg.list() if a["name"].lower() == "wo-srv01"]) == 1


def test_builtin_off_removes_only_the_in_container_agent(tmp_path, monkeypatch):
    db = str(tmp_path / "k.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("KNOWLEDGE_DB", db)
    monkeypatch.setattr(selfmon, "_kill_stale", lambda: None)
    reg = AgentRegistry(Knowledge(db))
    reg.enroll("watchover-host", env="local", site="watchover", tags="self")
    reg.enroll("app-01", env="prod", tags="self")                       # tagged "self" but not the built-in row
    selfagent.token("wo-srv01")
    assert selfagent.builtin_off() == 1
    assert sorted(a["name"] for a in reg.list()) == ["app-01", "wo-srv01"]
