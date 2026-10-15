"""Kill switches (docs/design/action-agent.md §10.3). Budgets, host locks, loop guard, windows / freeze and the circuit breaker
join this module with the run engine (WP8).

* Global stop without the database: `python -m watchover.act kill --global --reason "…"` writes <WATCHOVER_HOME>/act.KILL;
  the dispatcher reads it (cached ≤ 1 s) before every dispatch.
* Stops in the database (`act_kill`): global, team, runbook, host, template. Stopping takes one person with a reason;
  resuming a team / runbook / host stop takes one `act.promote` holder; a global stop takes two distinct `act.policy` holders.
Every stop and resume is an audit event."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional

from .types import now

SCOPES = ("global", "team", "runbook", "host", "template")
_PANIC = {"at": 0.0, "val": None}


def panic_file() -> Path:
    return Path(os.environ.get("WATCHOVER_HOME") or "data") / "act.KILL"


def panic_set(reason: str, by: str = "cli") -> Path:
    p = panic_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reason": reason, "by": by, "ts": now()}, ensure_ascii=False), encoding="utf-8")
    _PANIC["at"] = 0.0
    return p


def panic_clear() -> None:
    try:
        panic_file().unlink()
    except FileNotFoundError:
        pass
    _PANIC["at"] = 0.0


def panic() -> Optional[dict]:
    """The panic file's content, cached for one second."""
    if time.monotonic() - _PANIC["at"] < 1.0:
        return _PANIC["val"]
    p = panic_file()
    val = None
    if p.exists():
        try:
            val = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            val = {"reason": "act.KILL", "by": "file"}
        val.setdefault("reason", ""); val["scope"] = "global"; val["file"] = True
    _PANIC.update(at=time.monotonic(), val=val)
    return val


class KillError(ValueError):
    def __init__(self, code: str, tr: str, en: str, status: int = 409):
        super().__init__(en)
        self.code, self.tr, self.en, self.status = code, tr, en, status

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en}


class Kills:
    def __init__(self, db, audit):
        self.db, self.audit = db, audit

    def active(self, team_ids: Optional[List[int]] = None) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_kill WHERE active=1 ORDER BY id DESC")
        if team_ids is not None:
            rows = [r for r in rows if r["scope"] == "global" or r["team_id"] is None or r["team_id"] in team_ids]
        p = panic()
        if p:
            rows = [{"id": 0, "scope": "global", "ref": "", "team_id": None, "active": 1, "hard": 1, "by_": p.get("by", ""),
                     "reason": p.get("reason", ""), "ts": p.get("ts", ""), "file": True}] + rows
        return rows

    def killed(self, team_id: Optional[int] = None, host: str = "", runbook: str = "", template: str = "") -> Optional[dict]:
        """The first stop that applies (checked at proposal, start, dispatch, agent fetch and pre-exec, I7)."""
        for k in self.active():
            s, ref = k["scope"], str(k.get("ref") or "")
            if s == "global" or (s == "team" and team_id is not None and ref == str(team_id)) or (s == "host" and host and ref == host) \
                    or (s == "runbook" and runbook and ref == runbook) or (s == "template" and template and ref == template):
                return k
        return None

    def stop(self, scope: str, ref: str, by: str, reason: str, team_id: Optional[int] = None, hard: bool = False) -> dict:
        if scope not in SCOPES:
            raise KillError("act_param_invalid", f"Geçersiz kapsam: {scope}", f"Invalid scope: {scope}", 422)
        if not (reason or "").strip():
            raise KillError("act_param_invalid", "Durdurma nedeni gerekli", "A reason is required", 422)
        ref = "" if scope == "global" else str(ref or "")
        with self.db.tx():
            kid = self.db._insert("INSERT INTO act_kill (scope, ref, team_id, active, hard, by_, reason, ts) VALUES (?,?,?,?,?,?,?,?)",
                                  (scope, ref, team_id, 1, 1 if hard else 0, self.audit.actor(by, "user"), reason.strip()[:300], now()))
            self.audit.write("kill.on", object_kind="kill", object_id=kid, actor=by, actor_kind="user", team_id=team_id,
                             payload={"scope": scope, "ref": ref, "hard": bool(hard), "reason": reason.strip()[:300]})
        return {"id": kid, "effective_within_s": 12}

    def resume(self, kid: int, by: str, reason: str = "") -> dict:
        rows = self.db._exec("SELECT * FROM act_kill WHERE id=?", (kid,))
        if not rows or not rows[0]["active"]:
            raise KeyError(kid)
        k = rows[0]
        me = self.audit.actor(by, "user")
        if k["scope"] == "global":                       # two distinct act.policy holders (§10.3); the first one is recorded
            first = (k.get("resumed_by") or "")
            if not first:
                with self.db.tx():
                    self.db._exec("UPDATE act_kill SET resumed_by=?, resume_reason=? WHERE id=?", (me, (reason or "")[:300], kid))
                    self.audit.write("kill.resume_vote", object_kind="kill", object_id=kid, actor=by, actor_kind="user",
                                     payload={"scope": "global", "reason": (reason or "")[:300]})
                return {"id": kid, "active": True, "votes": 1, "need": 2}
            if first == me:
                raise KillError("act_sod", "Genel durdurmayı ikinci bir kişi kaldırmalı", "A second person must resume a global stop", 403)
        with self.db.tx():
            self.db._exec("UPDATE act_kill SET active=0, resumed_by=?, resumed_at=?, resume_reason=? WHERE id=?",
                          ((k.get("resumed_by") + "," if k["scope"] == "global" and k.get("resumed_by") else "") + me, now(),
                           (reason or "")[:300], kid))
            self.audit.write("kill.off", object_kind="kill", object_id=kid, actor=by, actor_kind="user", team_id=k["team_id"],
                             payload={"scope": k["scope"], "ref": k["ref"], "reason": (reason or "")[:300]})
        return {"id": kid, "active": False, "votes": 2 if k["scope"] == "global" else 1, "need": 2 if k["scope"] == "global" else 1}
