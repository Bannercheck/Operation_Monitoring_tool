"""Team isolation review (role-based teams): a team's agent or pull source posts only for the team's hosts, on the Python receiver
path (LiveStore.ingest) and on the Go parse path (ingest_parsed, the spool consumer, the real `wo-receiver parse-spool`); a team
manager hands out only roles within their own permissions and cannot squat another team's host names or narrow an
installation-wide account; shared saved log searches stay inside the team; installation-wide switches (simulator, live buffer,
parser patterns, LLM endpoint) need an installation-wide account."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)
ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0",
                 "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email, role in (("mgra@corp.com", "viewer"), ("opa@corp.com", "viewer"), ("mgrb@corp.com", "viewer"), ("opb@corp.com", "viewer"),
                        ("inst@corp.com", "operator"), ("noc@corp.com", "operator"), ("hr@corp.com", "viewer")):
        svc.users.register(email, PW, name=email.split("@")[0].title(), role=role)
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def lines(*rows) -> bytes:
    now = datetime.now(UTC).isoformat()
    return ("\n".join(json.dumps({"ts": now, "level": "error", "service": "svc", **r}) for r in rows) + "\n").encode()


def setup_teams(c):
    """Team A (host rule a-*) and team B (b-*): managers, operators (members, no installation permission), an installation
    operator in A, each team's agent enrolled with the team key, B's web host posting its own lines."""
    svc = c.svc; admin = login(c, *ADMIN)
    ta = c.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    tb = c.post("/api/teams", json={"name": "B", "rules": [{"field": "host", "op": "glob", "value": "b-*"}]}, headers=admin).json()
    rid = lambda t, k: next(r["id"] for r in t["roles"] if r["key"] == k)   # noqa: E731
    for t, email, key in ((ta, "mgra@corp.com", "manager"), (ta, "opa@corp.com", "operator"), (tb, "mgrb@corp.com", "manager"), (tb, "opb@corp.com", "operator"),
                          (ta, "inst@corp.com", "viewer")):
        assert c.put(f"/api/teams/{t['id']}/members/{email}", json={"role_id": rid(t, key)}, headers=admin).status_code == 200
        if email != "inst@corp.com":
            svc.users.set_role(svc.users.get(email)["id"], "member", svc.roles.names())
    ka, kb = svc.teams.rotate_enroll_key(ta["id"]), svc.teams.rotate_enroll_key(tb["id"])
    agent_a, _ = svc.agents.self_enroll(ka, "a-app-01", "prod")
    agent_b, _ = svc.agents.self_enroll(kb, "b-web-01", "prod")
    svc.live.ingest("app.jsonl", lines({"host": "b-web-01", "msg": "BREAL ok"}), "b-web-01", agent_id=agent_b["id"])
    svc.inventory.upsert({"hostname": "un-01", "ip": "10.9.0.1", "application": "Nobody"})
    svc.teams.invalidate()
    return ta, tb, rid, agent_a, agent_b


def poison() -> bytes:
    return lines({"host": "b-web-01", "msg": "APOISON spoofed"}, {"host": "b-new-77", "msg": "APOISON2 brand-new B name"}, {"host": "a-db-02", "msg": "AOWN new"},
                 {"host": "z-new-1", "msg": "ACLAIM brand-new unowned"}, {"host": "un-01", "msg": "AUNOWNED known"}, {"msg": "ANOHOST"})


def check_ring(svc, ta, tb):
    by = {o.message: o for o in svc.live.buf}
    assert by["APOISON spoofed"].host == "a-app-01" and by["APOISON spoofed"].attributes["host.reported"] == "b-web-01"
    assert by["APOISON2 brand-new B name"].host == "a-app-01"                 # B's rule takes it the moment it is seen
    assert by["AUNOWNED known"].host == "a-app-01"                           # known, no team: an installation admin assigns it
    assert by["AOWN new"].host == "a-db-02" and "host.reported" not in by["AOWN new"].attributes
    assert by["ACLAIM brand-new unowned"].host == "z-new-1" and by["ANOHOST"].host == "a-app-01"
    a = svc.teams.get(ta["id"])
    assert "z-new-1" in a["hosts"] and next(h for h in a["host_meta"] if h["host"] == "z-new-1")["origin"] == "agent"
    assert "b-new-77" not in a["hosts"] and "un-01" not in a["hosts"] and "b-web-01" not in a["hosts"]


def b_sees_poison(c) -> bool:
    ob = login(c, "opb@corp.com")
    found = c.get("/api/logs/search", params={"q": "", "last": 60, "limit": 500}, headers=ob).json()["lines"]
    txt = c.get("/api/live/events?n=500", headers=ob).text + json.dumps(found)
    assert "BREAL" in txt                                                         # B still sees its own line
    return "APOISON" in txt or "ACLAIM" in txt


# ---------------------------------------------------------------- the agent guard, Python receiver path
def test_team_agent_cannot_post_for_another_teams_hosts(app):
    c = app; svc = c.svc
    ta, tb, _rid, agent_a, _agent_b = setup_teams(c)
    assert svc.live.ingest("evil.jsonl", poison(), "a-app-01", agent_id=agent_a["id"]) == 6
    check_ring(svc, ta, tb)
    assert not b_sees_poison(c)
    oa = login(c, "opa@corp.com")
    mine = {e["host"] for e in c.get("/api/live/events?n=500", headers=oa).json()}
    assert {"a-app-01", "a-db-02", "z-new-1"} <= mine and "b-web-01" not in mine
    # an installation-wide agent (fleet key, no team) on a host no team owns is not limited
    fleet = svc.agents.enroll_key()
    rec, _ = svc.agents.self_enroll(fleet, "relay-01", "prod")
    svc.live.ingest("relay.jsonl", lines({"host": "b-web-01", "msg": "RELAYED"}), "relay-01", agent_id=rec["id"])
    assert next(o for o in svc.live.buf if o.message == "RELAYED").host == "b-web-01"
    # ... but a fleet-key (or pre-v1.46) agent on a host team A owns is team A's sender: existing tokens are covered too
    old, _ = svc.agents.self_enroll(fleet, "a-old-01", "prod")
    assert old.get("team_id") is None
    svc.live.ingest("old.jsonl", lines({"host": "b-web-01", "msg": "OLDSPOOF"}), "a-old-01", agent_id=old["id"])
    assert next(o for o in svc.live.buf if o.message == "OLDSPOOF").host == "a-old-01"


def test_receiver_http_uses_the_token_identity(app):
    import urllib.request
    from watchover.live import start_receiver
    c = app; svc = c.svc
    ta, tb, _rid, agent_a, _b = setup_teams(c)
    tok = svc.agents.rotate(agent_a["id"])
    srv = start_receiver(svc.live, 0, None, svc.agents, svc.rum)
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/ingest", data=poison(), method="POST",
                                     headers={"X-API-Key": tok, "X-File-Name": "evil.jsonl", "X-Agent": "b-web-01"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.loads(r.read())["agent"] == "a-app-01"
    finally:
        srv.shutdown()
    check_ring(svc, ta, tb)
    assert not b_sees_poison(c)


# ---------------------------------------------------------------- the Go parse path: v=2 records (ingest_parsed, spool consumer)
def canon(o) -> str:
    d = {"ts": o.timestamp.isoformat(), "message": o.message, "kind": o.kind, "severity": o.severity, "service": o.service, "host": o.host,
         "environment": o.environment, "origin": o.origin, "attributes": o.attributes, "source": o.source, "line_no": o.line_no, "parser": o.parser,
         "parser_confidence": round(float(o.parser_confidence), 4), "raw": o.raw}
    return json.dumps({k: d[k] for k in sorted(d)}, ensure_ascii=False, default=str)


def v2_record(data: bytes, agent: str, agent_id: int) -> tuple:
    from watchover.pipeline import ingest_bytes
    obs, rep = ingest_bytes("evil.jsonl", data)
    body = ("\n".join(canon(o) for o in obs) + "\n").encode()
    h = {"v": 2, "kind": "parsed", "path": "/ingest", "file": "evil.jsonl", "agent": agent, "agent_id": agent_id, "env": "prod", "site": "",
         "ts": datetime.now(UTC).isoformat(), "len": len(body), "n": len(obs), "raw_len": len(data), "format": rep[0].get("format"),
         "family": rep[0].get("family", ""), "confidence": rep[0].get("confidence", 0.9), "hint_format": rep[0].get("format"), "report": {}}
    return h, body


def test_go_parsed_batches_get_the_same_guard(app, tmp_path):
    c = app; svc = c.svc
    ta, tb, _rid, agent_a, _b = setup_teams(c)
    h, body = v2_record(poison(), "a-app-01", agent_a["id"])
    assert svc.live.ingest_parsed(body, h) == 6
    check_ring(svc, ta, tb)
    # the same through the spool consumer (a pseg segment as the Go stage writes it)
    from watchover.spool_consumer import SpoolConsumer
    svc.live.buf.clear()
    d = tmp_path / "spool"; d.mkdir()
    h2, body2 = v2_record(lines({"host": "b-web-01", "msg": "SPOOLED spoof"}), "a-app-01", agent_a["id"])
    (d / "pseg-0000000000000000001.wo").write_bytes(json.dumps(h2).encode() + b"\n" + body2 + b"\n")
    sc = SpoolConsumer(str(d), svc.live, svc.agents)
    sc.run_once()
    assert sc.parsed_records == 1
    o = next(o for o in svc.live.buf if o.message == "SPOOLED spoof")
    assert o.host == "a-app-01" and o.attributes["host.reported"] == "b-web-01"
    assert not b_sees_poison(c) and "SPOOLED" not in c.get("/api/live/events?n=500", headers=login(c, "opb@corp.com")).text


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    if GO is None:
        pytest.skip("no Go toolchain")
    out = tmp_path_factory.mktemp("bin") / "wo-receiver"
    subprocess.run([GO, "build", "-o", str(out), "."], cwd=ROOT / "receiver", check=True, capture_output=True, timeout=600)
    return out


def test_real_go_parse_stage(app, binary, tmp_path):
    """A raw segment as the Go receiver spools it (agent identity from the token), parsed by `wo-receiver parse-spool`, consumed
    by the dashboard: the foreign host is re-attributed exactly as on the Python path."""
    c = app; svc = c.svc
    ta, tb, _rid, agent_a, _b = setup_teams(c)
    d = tmp_path / "go"; d.mkdir()
    data = poison()
    hdr = {"v": 1, "path": "/ingest", "file": "evil.jsonl", "ip": "10.0.0.9", "ts": datetime.now(UTC).isoformat(), "len": len(data), "identity": "token",
           "agent": "a-app-01", "agent_id": agent_a["id"], "env": "prod", "site": ""}
    (d / "seg-0000000000000000001.raw").write_bytes(json.dumps(hdr, separators=(",", ":")).encode() + b"\n" + data + b"\n")
    r = subprocess.run([str(binary), "parse-spool", str(d)], capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["parse_batches_parsed"] == 1
    from watchover.spool_consumer import SpoolConsumer
    sc = SpoolConsumer(str(d), svc.live, svc.agents)
    sc.run_once()
    assert sc.parsed_records == 1
    check_ring(svc, ta, tb)
    assert not b_sees_poison(c)


# ---------------------------------------------------------------- a team's pull source
def test_team_source_posts_only_for_team_hosts(app, monkeypatch):
    c = app; svc = c.svc; admin = login(c, *ADMIN)
    ta, tb, _rid, _a, _b = setup_teams(c)
    s = c.post("/api/sources", json={"name": "a-es", "kind": "http", "url": "http://es.example/logs", "team_id": ta["id"]}, headers=admin)
    assert s.status_code == 201, s.text
    from watchover import sources as wo_sources
    now = datetime.now(UTC).isoformat()
    monkeypatch.setattr(wo_sources, "fetch", lambda src, limit=500, since=None: ([{"ts": now, "host": "b-web-01", "msg": "SRCPOISON"},
                                                                                   {"ts": now, "host": "a-app-01", "msg": "SRCOWN"}], now))
    wo_sources.Poller(svc.sources, svc.live).poll_one(svc.sources.get(s.json()["id"]))
    by = {o.message: o for o in svc.live.buf}
    assert by["SRCOWN"].host == "a-app-01"
    assert by["SRCPOISON"].host == "a-es" and by["SRCPOISON"].attributes["host.reported"] == "b-web-01"   # the source's own (claimed) name
    assert "SRCPOISON" not in c.get("/api/live/events?n=500", headers=login(c, "opb@corp.com")).text


# ---------------------------------------------------------------- team manager limits
def test_manager_hands_out_only_roles_within_own_permissions(app):
    c = app; svc = c.svc; admin = login(c, *ADMIN)
    ta, tb, rid, _a, _b = setup_teams(c)
    power = c.post(f"/api/teams/{ta['id']}/roles", json={"label": "Power", "perms": ["page.ops", "act.scan", "act.sim"]}, headers=admin).json()
    assert "act.sim" not in power["perms"]                                          # installation-only keys never enter a team role
    c.put(f"/api/teams/{ta['id']}/roles/{rid(ta, 'manager')}", json={"label": "Ekip yöneticisi", "perms": ["page.ops", "page.inv", "team.manage"]}, headers=admin)
    small = c.post(f"/api/teams/{ta['id']}/roles", json={"label": "Envanter", "perms": ["page.inv"]}, headers=admin).json()
    m = login(c, "mgra@corp.com")
    c.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": small["id"]}, headers=admin)
    r = c.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": power["id"]}, headers=m)
    assert r.status_code == 403 and "act.scan" in r.text
    assert c.post(f"/api/teams/{ta['id']}/roles/{power['id']}/default", headers=m).status_code == 403
    assert c.delete(f"/api/teams/{ta['id']}/roles/{small['id']}?move_to={power['id']}", headers=m).status_code == 403   # opa would gain act.scan
    assert c.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": small["id"]}, headers=m).status_code == 200
    assert c.post(f"/api/teams/{ta['id']}/roles/{small['id']}/default", headers=m).status_code == 200
    # an admin moved opa into Power: the manager may leave them there (keep) but not move others in
    c.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": power["id"]}, headers=admin)
    assert c.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": power["id"]}, headers=m).status_code == 200
    # installation-wide accounts: joining a team would narrow them (noc sees every host as an installation operator)
    noc = c.put(f"/api/teams/{ta['id']}/members/noc@corp.com", json={"role_id": small["id"]}, headers=m)
    assert noc.status_code == 403 and "every host" in noc.text
    assert c.get("/api/live/events?n=5", headers=login(c, "noc@corp.com")).status_code == 200 and svc.access_of(svc.users.get("noc@corp.com")).wide
    svc.users.set_role(svc.users.get("hr@corp.com")["id"], "member", svc.roles.names())                       # a 'member' account joins freely
    assert c.put(f"/api/teams/{ta['id']}/members/hr@corp.com", json={"role_id": small["id"]}, headers=m).status_code == 200
    assert c.put(f"/api/teams/{ta['id']}/members/opb@corp.com", json={"role_id": small["id"]}, headers=m).status_code == 200   # already in a team


def test_claims_respect_other_teams_host_rules(app):
    c = app; admin = login(c, *ADMIN)
    ta, tb, _rid, _a, _b = setup_teams(c)
    m = login(c, "mgra@corp.com")
    r = c.post(f"/api/teams/{ta['id']}/hosts", json={"hosts": ["b-web-99", "a-new-5", "q-new-5"]}, headers=m).json()
    assert r["added"] == ["a-new-5", "q-new-5"] and r["rejected"] == [{"host": "b-web-99", "reason": "other_team"}]
    assert c.post("/api/agents", json={"name": "b-web-98", "team_id": ta["id"]}, headers=login(c, "opa@corp.com")).status_code == 409
    assert c.post("/api/agents", json={"name": "a-web-98", "team_id": ta["id"]}, headers=login(c, "opa@corp.com")).status_code == 201
    with pytest.raises(ValueError):
        c.svc.agents.self_enroll(c.svc.teams.enroll_key(ta["id"]), "b-web-97")
    mv = c.post(f"/api/teams/{ta['id']}/hosts", json={"hosts": ["b-web-96"], "move": True}, headers=admin).json()
    assert mv["added"] == [] and mv["rejected"][0]["reason"] == "other_team"             # a rule, not an explicit row: change the rules


# ---------------------------------------------------------------- shared saved searches
def test_shared_saved_searches_stay_in_the_team(app):
    c = app; admin = login(c, *ADMIN)
    ta, tb, _rid, _a, _b = setup_teams(c)
    mb = login(c, "mgrb@corp.com")
    assert c.post("/api/logs/saved", json={"name": "B secret", "query": "host:b-web-01 BSECRET", "shared": True}, headers=mb).status_code == 200
    assert "BSECRET" not in c.get("/api/logs/saved", headers=login(c, "opa@corp.com")).text
    assert "BSECRET" not in c.get("/api/logs/saved", headers=login(c, "inst@corp.com")).text
    assert "BSECRET" in c.get("/api/logs/saved", headers=login(c, "opb@corp.com")).text
    assert "BSECRET" in c.get("/api/logs/saved", headers=admin).text


# ---------------------------------------------------------------- installation-wide switches
def test_installation_wide_switches_need_an_installation_wide_account(app):
    c = app
    setup_teams(c)
    inst, noc = login(c, "inst@corp.com"), login(c, "noc@corp.com")        # both installation operators; inst is in team A
    for verb, path, body in (("post", "/api/live/simulate", {"on": False}), ("post", "/api/live/clear", None),
                             ("post", "/api/grok", {"name": "T_X", "pattern": "%{GREEDYDATA:message}"}), ("put", "/api/llm", {"llm_base": "http://evil.example"}),
                             ("put", "/api/llm/autotrain", {"train_auto": False}), ("delete", "/api/grok/profiles/nope", None)):
        r = getattr(c, verb)(path, headers=inst, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code, r.text[:200])
    assert c.post("/api/live/simulate", json={"on": False}, headers=noc).status_code == 200
    assert c.post("/api/grok", json={"name": "T_X", "pattern": "%{GREEDYDATA:message}"}, headers=noc).status_code == 201
    assert c.get("/api/llm", headers=inst).status_code == 200                  # reading stays on page.llm


def test_team_membership_on_account_creation_needs_page_teams(app):
    c = app; svc = c.svc; admin = login(c, *ADMIN)
    ta, _tb, rid, _a, _b = setup_teams(c)
    c.put("/api/roles/helpdesk", json={"perms": ["page.users"], "label": "Helpdesk"}, headers=admin)
    svc.users.set_role(svc.users.get("hr@corp.com")["id"], "helpdesk", svc.roles.names())
    hd = login(c, "hr@corp.com")
    r = c.post("/api/users", json={"email": "x@corp.com", "password": PW, "team_id": ta["id"], "team_role_id": rid(ta, "manager")}, headers=hd)
    assert r.status_code == 403 and not svc.teams.is_member(ta["id"], "x@corp.com")
    assert c.post("/api/users", json={"email": "y@corp.com", "password": PW, "team_id": ta["id"], "team_role_id": rid(ta, "viewer")}, headers=admin).status_code == 201


# ---------------------------------------------------------------- APM: spans by host, not by a shared service name
def test_traces_are_scoped_by_span_host(app):
    c = app; svc = c.svc
    ta, tb, _rid, agent_a, agent_b = setup_teams(c)
    now = datetime.now(UTC).isoformat()
    b_lines = [{"ts": now, "level": "error", "service": "b-api", "host": "b-web-01", "msg": f"BTRACE GET /pay {i} 500 {40 + i}ms", "trace_id": f"btr-{i}",
                "duration_ms": 40 + i} for i in range(6)]
    svc.live.ingest("b.jsonl", ("\n".join(json.dumps(x) for x in b_lines) + "\n").encode(), "b-web-01", agent_id=agent_b["id"])
    a_lines = [{"ts": now, "level": "error", "service": "b-api", "msg": f"ATRACE GET /x {i} 500 {30 + i}ms", "trace_id": f"atr-{i}", "duration_ms": 30 + i} for i in range(6)]
    svc.live.ingest("a.jsonl", ("\n".join(json.dumps(x) for x in a_lines) + "\n").encode(), "a-app-01", agent_id=agent_a["id"])   # A "joins" b-api by name
    ids = lambda h: {t["trace_id"].split("-")[0] for t in c.get("/api/traces?minutes=60&limit=200", headers=h).json()}   # noqa: E731
    va, vb = login(c, "opa@corp.com"), login(c, "opb@corp.com")
    assert ids(vb) == {"btr"} and ids(va) == {"atr"}
    assert c.get("/api/traces/btr-1", headers=va).status_code == 404 and c.get("/api/traces/btr-1", headers=vb).status_code == 200
    assert {s["host"] for s in c.get("/api/traces/atr-1", headers=va).json()["spans"]} == {"a-app-01"}
    # a team cannot pin another team's running service as a RUM app (the pin would open that service by name)
    r = c.post("/api/rum/apps", json={"name": "b-api", "platform": "web"}, headers=va)
    assert r.status_code == 409, r.text
    assert c.post("/api/rum/apps", json={"name": "a-portal", "platform": "web"}, headers=va).status_code == 201


def test_rotating_an_agent_token_binds_it_to_the_team(app):
    """A team operator who rotates an installation-wide agent on a team host gets a token limited to the team's hosts; another
    team's agent stays out of reach even on a host both teams see."""
    c = app; svc = c.svc; admin = login(c, *ADMIN)
    ta, tb, _rid, _a, agent_b = setup_teams(c)
    fleet = svc.agents.enroll_key()
    relay, _ = svc.agents.self_enroll(fleet, "a-relay-01", "prod")
    assert relay.get("team_id") is None
    oa = login(c, "opa@corp.com")
    tok = c.post(f"/api/agents/{relay['id']}/rotate", headers=oa)
    assert tok.status_code == 200 and svc.agents.get(relay["id"])["team_id"] == ta["id"]
    rec = svc.agents.verify(tok.json()["token"])
    svc.live.ingest("relay.jsonl", lines({"host": "b-web-01", "msg": "RELAYSPOOF"}), rec["name"], agent_id=rec["id"])
    assert next(o for o in svc.live.buf if o.message == "RELAYSPOOF").host == "a-relay-01"
    # b-web-01 shared with A by an explicit host: B's agent is still B's
    c.post(f"/api/teams/{ta['id']}/hosts", json={"hosts": ["b-web-01"], "move": False}, headers=admin)
    svc.kb._exec("INSERT INTO team_hosts (team_id, host, origin) VALUES (?, 'b-web-01', 'manual')", (ta["id"],)); svc.teams.invalidate()
    assert c.post(f"/api/agents/{agent_b['id']}/rotate", headers=login(c, "opa@corp.com")).status_code == 404
