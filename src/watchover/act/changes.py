"""Installation changes that need two act.policy holders (docs/design/action-agent.md §10.7, §12.1): external anchor sinks and
recipients, chain-break acknowledgement, dispatch-key rotation and the encrypted key export. The first holder requests
(`act_changes` row + an `install_change` approval excluding them); a second, different holder confirms and the change is
applied in the same breath. Both steps are audited; secrets in a pending body are vault-encrypted."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from .types import now

KINDS = ("anchor_sinks", "anchor_recipients", "chain_ack", "key_rotate", "key_export")


class ChangeError(RuntimeError):
    def __init__(self, code: str, tr: str, en: str, status: int = 409):
        super().__init__(en)
        self.code, self.tr, self.en, self.status = code, tr, en, status

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en}


class Changes:
    def __init__(self, db, audit, approvals):
        self.db, self.audit, self.approvals = db, audit, approvals
        self.appliers: Dict[str, Callable[[dict, str, dict], Any]] = {}

    def on(self, kind: str, fn: Callable[[dict, str, dict], Any]) -> None:
        self.appliers[kind] = fn

    def request(self, kind: str, body: dict, by: str, reason: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown change kind {kind}")
        with self.db.tx():
            cid = self.db._insert("INSERT INTO act_changes (kind, body, requested_by, requested_at, status) VALUES (?,?,?,?,?)",
                                  (kind, json.dumps(body, sort_keys=True), by, now(), "pending"))
            aid = self.approvals.request("install_change", cid, by, need=1, stepup=True, requires=(), reason=(reason or kind)[:300],
                                         exclude=[by])
            self.db._exec("UPDATE act_changes SET approval_id=? WHERE id=?", (aid, cid))
            self.audit.write("change.requested", object_kind="change", object_id=cid, actor=by, actor_kind="user",
                             payload={"kind": kind, "approval_id": aid})
        return self.get(cid)

    def get(self, cid: int) -> dict:
        rows = self.db._exec("SELECT * FROM act_changes WHERE id=?", (cid,))
        if not rows:
            raise KeyError(cid)
        r = dict(rows[0])
        try:
            body = json.loads(r.get("body") or "{}")
        except ValueError:
            body = {}
        for it in body.get("sinks", []) if isinstance(body, dict) else []:
            if isinstance(it, dict) and it.get("secret"):
                it["secret"] = "***"
        r["body"] = body
        return r

    def pending(self) -> list:
        return [self.get(r["id"]) for r in self.db._exec("SELECT id FROM act_changes WHERE status='pending' ORDER BY id")]

    def confirm(self, cid: int, by: str, decision: str = "approve", inputs: Optional[dict] = None, stepup_method: str = "") -> dict:
        """The second holder's vote; on approval the change is applied and its result returned under "result"."""
        rows = self.db._exec("SELECT * FROM act_changes WHERE id=?", (cid,))
        if not rows:
            raise KeyError(cid)
        ch = dict(rows[0])
        if ch["status"] != "pending":
            raise ChangeError("act_not_pending", "Bu değişiklik artık beklemede değil", "This change is no longer pending")
        st = self.approvals.vote(int(ch["approval_id"]), by, decision, stepup_method=stepup_method)
        if st["status"] == "rejected":
            self.db._exec("UPDATE act_changes SET status='rejected' WHERE id=?", (cid,))
            return {**self.get(cid), "result": None}
        if st["status"] != "approved":
            return {**self.get(cid), "result": None}
        fn = self.appliers.get(ch["kind"])
        body = json.loads(ch["body"] or "{}")
        result = fn(body, by, inputs or {}) if fn else None
        with self.db.tx():
            self.db._exec("UPDATE act_changes SET status='applied', applied_at=?, applied_by=? WHERE id=?", (now(), by, cid))
            self.audit.write("change.applied", object_kind="change", object_id=cid, actor=by, actor_kind="user",
                             payload={"kind": ch["kind"], "requested_by_pseudo": self.audit.actor(ch["requested_by"], "user")})
        return {**self.get(cid), "result": result}
