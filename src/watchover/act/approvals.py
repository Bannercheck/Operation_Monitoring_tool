"""Generic approvals and the identity rules (docs/design/action-agent.md §10.5): one `act_approvals` row per request
(runbook_version, step, promotion, kill_resume, template, connector, …) and one `act_votes` row per voter.

Identity rules hold for administrators too (I6): the requester never approves their own request, every voter counts once,
the system and MCP keys never count as a person, votes of disabled or deleted accounts are void. A request can name
requirements a vote must bring (e.g. "policy": one voter holds installation-wide act.policy for an R3 runbook)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional

from .types import now

SUBJECTS = ("step", "run_start", "promotion", "runbook_version", "kill_resume", "freeze_override", "template", "connector", "model", "golden",
            "install_change")
NOT_A_PERSON = ("system", "mcp", "agent", "model")


class ApprovalError(Exception):
    def __init__(self, code: str, tr: str, en: str, status: int = 403, **extra):
        super().__init__(en)
        self.code, self.tr, self.en, self.status, self.extra = code, tr, en, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en, **self.extra}


def _person(who: str) -> bool:
    w = (who or "").strip().lower()
    return bool(w) and w not in NOT_A_PERSON and not w.startswith(("mcp:", "mcp-", "system:")) and "@" in w


class Approvals:
    def __init__(self, db, audit, active_fn: Optional[Callable[[str], bool]] = None):
        self.db, self.audit = db, audit
        self.active_fn = active_fn or (lambda email: True)

    def request(self, subject: str, subject_id: int, requester: str, need: int = 1, team_id: Optional[int] = None,
                four_eyes: bool = False, stepup: bool = False, requires: Iterable[str] = (), reason: str = "", job_hash: str = "",
                minutes: int = 0, exclude: Iterable[str] = ()) -> int:
        if subject not in SUBJECTS:
            raise ValueError(f"unknown approval subject {subject}")
        ts = now()
        exp = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds") if minutes else ""
        body = {"exclude": sorted({e.strip().lower() for e in exclude if e}), "requires": sorted(set(requires))}
        own = self.db.in_tx

        def go():
            aid = self.db._insert("INSERT INTO act_approvals (subject, subject_id, team_id, job_hash, need, four_eyes, stepup, requester, requested_at, "
                                  "expires_at, status, reason, requires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (subject, subject_id, team_id, job_hash, int(need), int(bool(four_eyes)), int(bool(stepup)), requester, ts, exp,
                                   "pending", reason[:300], json.dumps(body)))
            self.audit.write("approval.requested", object_kind="approval", object_id=aid, actor=requester,
                             actor_kind="user" if "@" in (requester or "") else "system", team_id=team_id,
                             payload={"subject": subject, "subject_id": subject_id, "need": need, "requires": body["requires"],
                                      "job_hash": job_hash})
            return aid
        if own:
            return go()
        with self.db.tx():
            return go()

    def get(self, aid: int) -> dict:
        rows = self.db._exec("SELECT * FROM act_approvals WHERE id=?", (aid,))
        if not rows:
            raise KeyError(aid)
        a = dict(rows[0])
        a["requires"] = json.loads(a.get("requires") or "{}") if (a.get("requires") or "").startswith("{") else {"requires": [], "exclude": []}
        a["votes"] = self.db._exec("SELECT voter, decision, reason, ts, stepup_method, fast, tags, void FROM act_votes WHERE approval_id=? ORDER BY id", (aid,))
        return a

    def vote(self, aid: int, voter: str, decision: str, reason: str = "", tags: Iterable[str] = (), job_hash: str = "", shown_at: str = "",
             stepup_method: str = "") -> dict:
        """Record one vote under the identity rules; returns {"status", "votes", "need"}."""
        if decision not in ("approve", "reject"):
            raise ApprovalError("act_param_invalid", "Karar onay ya da red olmalı", "Decision must be approve or reject", 422)
        a = self.get(aid)
        if a["status"] != "pending":
            raise ApprovalError("act_not_pending", "Bu istek artık beklemede değil", "This request is no longer pending", 409)
        who = (voter or "").strip().lower()
        if not _person(who):
            raise ApprovalError("act_sod", "Sistem ve MCP anahtarı onaylayamaz", "The system and MCP keys never approve")
        req = a["requires"]
        if who == (a["requester"] or "").strip().lower() or who in req.get("exclude", []):
            raise ApprovalError("act_sod", "Bu isteği başka biri onaylamalı", "Someone else must approve this request")
        if any((v["voter"] or "").lower() == who for v in a["votes"]):
            raise ApprovalError("act_sod", "Her kişi bir kez oy verir", "Each person votes once", 409)
        if a.get("job_hash") and job_hash and job_hash != a["job_hash"]:
            raise ApprovalError("act_plan_changed", "Plan değişti; yeniden onay gerekiyor", "The plan changed; approve again", 409)
        fast = 0
        if shown_at:
            try:
                fast = int((datetime.now(timezone.utc) - datetime.fromisoformat(shown_at.replace("Z", "+00:00"))).total_seconds() < 5)
            except ValueError:
                fast = 0
        tags = sorted(set(tags))
        with self.db.tx():
            seq = self.audit.write("vote", object_kind="approval", object_id=aid, actor=who, actor_kind="user", team_id=a.get("team_id"),
                                   payload={"decision": decision, "job_hash": job_hash or a.get("job_hash") or "", "fast": bool(fast), "tags": tags})
            self.db._exec("INSERT INTO act_votes (approval_id, voter, decision, reason, ts, stepup_at, stepup_method, shown_at, fast, audit_seq, tags) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (aid, who, decision, reason[:300], now(), now() if stepup_method else "", stepup_method,
                                                              shown_at, fast, seq, json.dumps(tags)))
            st = self._settle(aid)
        return st

    def _settle(self, aid: int) -> dict:
        a = self.get(aid)
        live = [v for v in a["votes"] if not v["void"] and self.active_fn(v["voter"])]
        if any(v["decision"] == "reject" for v in live):
            status = "rejected"
        else:
            ok = [v for v in live if v["decision"] == "approve"]
            tags = set()
            for v in ok:
                tags |= set(json.loads(v["tags"] or "[]"))
            status = "approved" if len(ok) >= int(a["need"] or 1) and set(a["requires"].get("requires", [])) <= tags else "pending"
        if status != "pending":
            self.db._exec("UPDATE act_approvals SET status=?, decided_at=? WHERE id=?", (status, now(), aid))
            self.audit.write(f"approval.{status}", object_kind="approval", object_id=aid, team_id=a.get("team_id"),
                             payload={"subject": a["subject"], "subject_id": a["subject_id"], "votes": len(live)})
        return {"status": status, "votes": len([v for v in live if v["decision"] == "approve"]), "need": int(a["need"] or 1),
                "requires": a["requires"].get("requires", [])}

    def pending(self, subject: Optional[str] = None, team_ids: Optional[List[int]] = None) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_approvals WHERE status='pending'" + (" AND subject=?" if subject else "") + " ORDER BY id",
                             (subject,) if subject else ())
        if team_ids is not None:
            rows = [r for r in rows if r["team_id"] is None or r["team_id"] in team_ids]
        return rows
