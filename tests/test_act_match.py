"""WP7: the deterministic matcher (docs/design/action-agent.md §8.2; C55 ambiguous precedence)."""
import copy
from types import SimpleNamespace

import pytest

from watchover.act import ActService
from watchover.act.importer import starter_bodies
from watchover.act.match import ESCALATE, Matcher, wilson_lower
from watchover.act.types import Trigger
from watchover.actions import open_store
from watchover.db import Database

NOSPACE = "ERROR billing-api write failed: No space left on device (/var/log/app/billing.log)"


class Inv:
    def __init__(self, recs):
        self.recs = recs

    def resolve(self, host):
        return self.recs.get(host)


@pytest.fixture
def act(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "k")
    monkeypatch.delenv("WATCHOVER_KEYD", raising=False)
    db = Database(str(tmp_path / "k.db"))
    open_store(db, str(tmp_path / "none.db"))
    a = ActService(db=db)
    a.svc = SimpleNamespace(inventory=Inv({"app-lnx-07": {"hostname": "app-lnx-07", "os": "linux", "env": "prod"},
                                            "win-01": {"hostname": "win-01", "os": "windows", "env": "prod"}}), agents=None,
                            _pool_of=lambda h: h.rsplit("-", 1)[0])
    a.cases.svc = a.svc
    a.set_mode("guided", by="admin@x.com")
    return a


def publish(act, key, body, team=4):
    act.runbooks.create(key, body.get("title", key), team, body, "a@corp.com")
    act.runbooks.submit(key, 1, "a@corp.com", tabletop=True)
    act.runbooks.review(key, 1, "b@corp.com", "approve")


def varlog():
    return copy.deepcopy(starter_bodies()["linux-varlog-full"])


def disk_case(act, host="app-lnx-07", team=4, key="threshold:app-lnx-07:disk"):
    return act.cases.route(Trigger(source="threshold", key=key, host=host, team_id=team, title="Disk dolu: /var/log",
                                   facts={"metric": "disk_pct", "value": 93, "limit": 90},
                                   evidence=[{"kind": "pasted", "host": host, "text": NOSPACE}]))["case_id"]


def test_varlog_matches_with_ambiguous_path_by_precedence(act):
    publish(act, "varlog-team", varlog())
    cid = disk_case(act)
    p = Matcher(act).propose(act.cases.get(cid))
    assert p["choice"] == "varlog-team" and p["version"] == 1
    c = p["candidates"][0]["components"]
    assert c["trig"] == 0.8 and c["hist"] == 0.5 and c["scope"] == 1.0           # 4 of 5 selectors (no anomaly kind); no history
    assert p["params"]["path"] == "/var/log" and "path" in p["ambiguous"]          # C55: trusted default before the log-derived value
    vals = {v["value"]: (v["origin"], v["provenance"]) for v in p["values"]["path"]}
    assert vals == {"/var/log/app": ("E1", "log"), "/var/log": ("default", "trusted")}
    assert p["params"]["days"] == 7 and p["params"]["host"] == "app-lnx-07" and "conf" not in p["params"]   # `ask` names nothing


def test_hard_filters_os_team_and_none(act):
    publish(act, "varlog-team", varlog())
    other = varlog()
    other["title"] = "Other team"
    publish(act, "other-team", other, team=5)
    keys = [c["key"] for c in Matcher(act).propose(act.cases.get(disk_case(act)))["candidates"]]
    assert keys == ["varlog-team"]                                                 # the other team's runbook is not a candidate
    win = Matcher(act).propose(act.cases.get(disk_case(act, host="win-01", key="threshold:win-01:disk")))
    assert win["candidates"] == [] and win["choice"] == ESCALATE                   # applies_to.os linux: not for a Windows host
    cid = act.cases.route(Trigger(source="external", key="external:x:1", host="app-lnx-07", team_id=4, title="CPU yüksek"))["case_id"]
    p = Matcher(act).propose(act.cases.get(cid))
    assert p["choice"] == ESCALATE and p["candidates"] == []                      # no hard match: ESCALATE, nothing listed


def test_close_scores_are_ambiguous(act):
    publish(act, "varlog-team", varlog())
    twin = varlog()
    twin["title"] = "Linux: /var/log doluluğu (ikiz)"
    publish(act, "linux-varlog-twin", twin)
    p = Matcher(act).propose(act.cases.get(disk_case(act)))
    assert len(p["candidates"]) == 2 and abs(p["candidates"][0]["score"] - p["candidates"][1]["score"]) < 0.15
    assert p["ambiguous"][0] == "choice" and p["choice"] == p["candidates"][0]["key"]   # still names top1, marked ambiguous


def test_history_and_wilson(act):
    assert wilson_lower(0, 0) == 0.0 and round(wilson_lower(8, 8), 2) == 0.75 and wilson_lower(31, 33) > 0.8
    publish(act, "varlog-team", varlog())
    first = disk_case(act)
    rb = act.db._exec("SELECT id FROM act_runbooks WHERE key='varlog-team'")[0]["id"]
    sig = act.cases.get(first)["signature"]
    for i in range(4):
        cid = act.db._insert("INSERT INTO actions (title, kind, signature, team_id, phase, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                             (f"old {i}", "case", sig, 4, "resolved", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
        act.db._exec("INSERT INTO act_runs (case_id, runbook_id, team_id, outcome, started_at) VALUES (?,?,?,?,?)",
                      (cid, rb, 4, "ok", "2026-10-01T00:00:00+00:00"))
    p = Matcher(act).propose(act.cases.get(first))
    assert p["candidates"][0]["components"]["hist"] == round(wilson_lower(4, 4), 4)
