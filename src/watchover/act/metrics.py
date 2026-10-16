"""Agreement metrics per week, team, runbook and selector (docs/design/action-agent.md §8.5; `act_metrics_weekly`).

Only blind decisions count (the human did not see the proposal; a peeked case is visible): anchoring would otherwise inflate
every number. Selector choices and human choices are runbook keys, NONE or ESCALATE (MANUAL counts as ESCALATE; the reason
code "Şimdi gerekmiyor" counts as NONE).

    RB@1      share where the selector's choice equals the human's
    κ         Cohen's kappa (p_o − p_e) / (1 − p_e), p_e = Σ_k p_sel(k)·p_hum(k): a dominant runbook cannot inflate it
    FULL      choice and every parameter equal, and the person skipped or replaced no step of the run
    PARAM-EM  parameters equal among cases with an equal choice
    FALSE-ACT selector proposed an act runbook ∧ human chose NONE / ESCALATE / not now, ÷ human NONE / ESCALATE count
    WRONG-ACT selector proposed an act runbook ∧ human ran a different act runbook, ÷ selector act proposals (per runbook)
    MISS      selector proposed NONE / ESCALATE ∧ human ran a runbook that verified ok, ÷ human runs"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .match import ESCALATE, NONE
from .runbooks import diagnose_only
from .shadow import agreement, iso_week
from .types import team_key

PASSIVE = (NONE, ESCALATE)


def kappa(pairs: Iterable[Tuple[str, str]]) -> Optional[float]:
    pairs = list(pairs)
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] / n * cb.get(k, 0) / n for k in ca)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def _norm_human(choice: str, reason: str) -> str:
    if reason == "not_now":
        return NONE
    if choice == "MANUAL":
        return ESCALATE
    return choice or ESCALATE


def collect(db, since: str, until: str) -> List[Dict[str, Any]]:
    """One row per blind decision with a valid champion proposal of a selector, with what the human then ran."""
    out = []
    acts: Dict[str, bool] = {}

    def is_act(key: str) -> bool:
        if key in PASSIVE or not key:
            return False
        if key not in acts:
            r = db._exec("SELECT v.body FROM act_runbook_versions v JOIN act_runbooks b ON b.id=v.runbook_id WHERE b.key=? ORDER BY v.version DESC "
                         "LIMIT 1", (key,))
            acts[key] = bool(r) and not diagnose_only(json.loads(r[0]["body"] or "{}"))
        return acts[key]

    decs = db._exec("SELECT d.*, a.team_id FROM act_decisions d JOIN actions a ON a.id=d.case_id WHERE d.decided_at>=? AND d.decided_at<? "
                    "AND d.blind=1 AND d.peeked=0", (since, until))
    for d in decs:
        props = db._exec("SELECT * FROM act_proposals WHERE case_id=? AND attempt=? AND valid=1 AND role='champion' ORDER BY id",
                         (d["case_id"], d["attempt"]))
        runs = db._exec("SELECT r.id, r.label, r.status, b.key FROM act_runs r JOIN act_runbooks b ON b.id=r.runbook_id WHERE r.case_id=? "
                        "ORDER BY r.id", (d["case_id"],))
        ran = [r for r in runs if r["status"] not in ("aborted",)]
        ran_key = ran[-1]["key"] if ran else ""
        verified_ok = any(r["label"] in ("ok", "ok_corrected") for r in ran)
        person_changed = False
        for r in ran:
            if db._exec("SELECT 1 AS x FROM act_steps WHERE run_id=? AND (status='deviated' OR (status='skipped' AND reason='person'))", (r["id"],)):
                person_changed = True
        human = _norm_human(d["choice"], d.get("reason_code") or "")
        hp = json.loads(d.get("params") or "{}") if isinstance(d.get("params"), str) else (d.get("params") or {})
        seen = set()
        for p in props:
            sel = p["selector"]
            if sel in seen:
                continue
            seen.add(sel)
            choice = p.get("runbook_key") or p.get("choice") or ESCALATE
            pp = json.loads(p.get("params") or "{}") if isinstance(p.get("params"), str) else (p.get("params") or {})
            out.append({"case_id": d["case_id"], "team_id": d.get("team_id"), "selector": "rules" if str(sel).startswith("rules") else sel,
                        "sel": choice, "hum": human, "agree": agreement({"choice": choice, "params": pp, "valid": 1}, human, hp),
                        "sel_act": is_act(choice), "hum_act": is_act(human), "ran": ran_key, "ran_act": is_act(ran_key),
                        "verified_ok": verified_ok, "person_changed": person_changed, "runbook": choice if choice not in PASSIVE else human})
    return out


def compute(rows: List[Dict[str, Any]]) -> Dict[str, Tuple[Optional[float], int]]:
    """{metric: (value or None, n)} over the rows of one group."""
    res: Dict[str, Tuple[Optional[float], int]] = {}
    n = len(rows)
    res["RB@1"] = (sum(1 for r in rows if r["sel"] == r["hum"]) / n if n else None, n)
    res["kappa"] = (kappa((r["sel"], r["hum"]) for r in rows), n)
    res["FULL"] = (sum(1 for r in rows if r["agree"] == "full" and not r["person_changed"]) / n if n else None, n)
    same = [r for r in rows if r["sel"] == r["hum"]]
    res["PARAM-EM"] = (sum(1 for r in same if r["agree"] == "full") / len(same) if same else None, len(same))
    passive = [r for r in rows if r["hum"] in PASSIVE]
    res["FALSE-ACT"] = (sum(1 for r in passive if r["sel_act"]) / len(passive) if passive else None, len(passive))
    proposed = [r for r in rows if r["sel_act"]]
    res["WRONG-ACT"] = (sum(1 for r in proposed if r["ran_act"] and r["ran"] != r["sel"]) / len(proposed) if proposed else None, len(proposed))
    runs = [r for r in rows if r["ran"]]
    res["MISS"] = (sum(1 for r in runs if r["sel"] in PASSIVE and r["verified_ok"]) / len(runs) if runs else None, len(runs))
    return res


def weekly(db, week: Optional[str] = None) -> List[Dict[str, Any]]:
    """Compute and store one week (default: the last full week) for every (team, runbook, selector) and the team totals."""
    if week is None:
        week = iso_week(datetime.now(timezone.utc) - timedelta(days=7))
    y, w = week.split("-W")
    start = datetime.fromisocalendar(int(y), int(w), 1).replace(tzinfo=timezone.utc)
    since, until = start.isoformat(timespec="seconds"), (start + timedelta(days=7)).isoformat(timespec="seconds")
    rows = collect(db, since, until)
    groups: Dict[Tuple[Optional[int], str, str], List[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["team_id"], "", r["selector"])].append(r)
        groups[(r["team_id"], r["runbook"], r["selector"])].append(r)
    out = []
    with db.tx():
        db._exec("DELETE FROM act_metrics_weekly WHERE week=?", (week,))
        for (tid, rb, sel), grp in sorted(groups.items(), key=lambda x: (str(x[0][0]), x[0][1], x[0][2])):
            for metric, (val, n) in compute(grp).items():
                if val is None:
                    continue
                db._exec("INSERT INTO act_metrics_weekly (week, team_key, team_id, runbook_key, selector, metric, value, n) VALUES (?,?,?,?,?,?,?,?)",
                         (week, team_key(tid), tid, rb, sel, metric, round(float(val), 4), int(n)))
                out.append({"week": week, "team_id": tid, "runbook_key": rb, "selector": sel, "metric": metric, "value": round(float(val), 4), "n": n})
    return out


__all__ = ["kappa", "collect", "compute", "weekly"]
