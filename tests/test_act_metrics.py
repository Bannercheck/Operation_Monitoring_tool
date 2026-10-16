"""WP13: agreement metrics (docs/design/action-agent.md §8.5): κ, FULL, FALSE-ACT, WRONG-ACT, MISS on synthetic blind decisions."""
import json

import pytest

from watchover.act import metrics as M
from watchover.act import store
from watchover.actions import open_store
from watchover.db import Database

TS = "2026-10-14T10:00:00+00:00"          # ISO week 2026-W42


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "k.db"))
    open_store(d, str(tmp_path / "none.db"))
    store.ensure(d)
    act_body = {"steps": [{"id": "d1", "type": "diagnose"}, {"id": "a1", "type": "act"}]}
    diag_body = {"steps": [{"id": "d1", "type": "diagnose"}]}
    for i, (key, body) in enumerate((("disk", act_body), ("restart", act_body), ("look", diag_body)), 1):
        d._exec("INSERT INTO act_runbooks (id, key, title, status, current_version) VALUES (?,?,?,?,1)", (i, key, key, "active"))
        d._exec("INSERT INTO act_runbook_versions (runbook_id, version, status, body) VALUES (?,1,'approved',?)", (i, json.dumps(body)))
    return d


def add(db, cid, human, sel, hp=None, sp=None, reason="", ran="", label="", blind=1, peeked=0, team=1, changed=False):
    db._exec("INSERT INTO actions (id, title, kind, team_id, phase, status, created_at) VALUES (?,?,?,?,?,?,?)",
             (cid, f"case {cid}", "case", team, "resolved", "done", TS))
    db._exec("INSERT INTO act_decisions (case_id, attempt, actor, choice, runbook_key, params, reason_code, blind, peeked, decided_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?)", (cid, 1, "op", human, human if human not in ("NONE", "ESCALATE", "MANUAL") else "",
                                               json.dumps(hp or {}), reason, blind, peeked, TS))
    db._exec("INSERT INTO act_proposals (case_id, attempt, selector, role, choice, runbook_key, params, valid) VALUES (?,?,?,?,?,?,?,1)",
             (cid, 1, "qwen", "champion", sel, sel if sel not in ("NONE", "ESCALATE") else "", json.dumps(sp or {})))
    if ran:
        rid = db._insert("INSERT INTO act_runs (case_id, runbook_id, status, label, started_at) VALUES (?, (SELECT id FROM act_runbooks WHERE key=?), ?, ?, ?)",
                         (cid, ran, "completed_ok", label, TS))
        if changed:
            db._exec("INSERT INTO act_steps (run_id, seq, step_key, type, status, reason) VALUES (?,?,?,?,?,?)", (rid, 0, "a1", "act", "skipped", "person"))


def metric(rows, name, rb=""):
    return next((r["value"], r["n"]) for r in rows if r["metric"] == name and r["runbook_key"] == rb and r["selector"] == "qwen")


def test_kappa_formula():
    assert M.kappa([("a", "a"), ("b", "b")]) == 1.0
    # a dominant class cannot inflate it: 9/10 agree on "a", but the selector always says "a"
    k = M.kappa([("a", "a")] * 9 + [("a", "b")])
    assert k == pytest.approx(0.0, abs=1e-4)
    assert M.kappa([]) is None


def test_weekly_metrics(db):
    add(db, 1, "disk", "disk", hp={"path": "/var/log"}, sp={"path": "/var/log"}, ran="disk", label="ok")       # full
    add(db, 2, "disk", "disk", hp={"path": "/var/log/app"}, sp={"path": "/var/log"}, ran="disk", label="ok")   # choice only
    add(db, 3, "NONE", "restart")                                                                              # false action
    add(db, 4, "restart", "disk", ran="restart", label="ok")                                                   # wrong act
    add(db, 5, "disk", "NONE", ran="disk", label="ok")                                                         # miss
    add(db, 6, "ESCALATE", "ESCALATE")
    add(db, 7, "disk", "disk", hp={}, sp={}, ran="disk", label="ok", changed=True)                             # a person skipped a step
    add(db, 8, "disk", "restart", blind=1, peeked=1)                                                          # visible: not counted
    rows = M.weekly(db, "2026-W42")
    assert metric(rows, "RB@1")[1] == 7
    assert metric(rows, "RB@1")[0] == pytest.approx(4 / 7, abs=1e-4)
    assert metric(rows, "FULL")[0] == pytest.approx(2 / 7, abs=1e-4)                     # 1 and 6 (7 skipped a step)
    assert metric(rows, "PARAM-EM")[0] == pytest.approx(3 / 4, abs=1e-4)
    assert metric(rows, "FALSE-ACT") == (pytest.approx(0.5, abs=1e-4), 2)               # 1 of the 2 human NONE / ESCALATE
    assert metric(rows, "WRONG-ACT")[0] == pytest.approx(1 / 5, abs=1e-4)               # case 4 of 5 act proposals
    assert metric(rows, "WRONG-ACT", rb="disk")[0] == pytest.approx(1 / 4, abs=1e-4)     # per runbook (the selector's)
    assert metric(rows, "MISS")[0] == pytest.approx(1 / 5, abs=1e-4)
    k = metric(rows, "kappa")[0]
    assert -1.0 <= k <= 1.0 and k < metric(rows, "RB@1")[0]
    stored = db._exec("SELECT COUNT(*) AS n FROM act_metrics_weekly WHERE week='2026-W42'")[0]["n"]
    assert stored == len(rows)
    assert len(M.weekly(db, "2026-W42")) == len(rows)                        # idempotent per week
