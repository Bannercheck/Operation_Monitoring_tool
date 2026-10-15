"""The action agent's audit log (docs/design/action-agent.md §10.7): append-only, hash-chained, one writer (the API process).

    with db.tx():                       # the state change and its audit row commit together (I9: audit fails -> no action)
        db._exec("UPDATE actions SET phase=? WHERE id=?", ("ready", cid))
        audit.write("case.ready", object_kind="case", object_id=cid, actor=email, actor_kind="user", payload={...})

WP1 ships this interface with the placeholder MAC of KeyClient (unkeyed sha256 chain, "p0:") or the demo-only embedded HMAC
("e1:"). WP2 replaces the MAC with watchover-keyd (which MACs only seq = head + 1), adds the append-only triggers, anchors and
external sinks; callers do not change. Actors are pseudonyms (op_<hex>); the map lives in act_actors (erasable, KVKK)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .. import privacy
from .types import ACTOR_KINDS, canonical, now, sha256_hex


class AuditError(RuntimeError):
    code = "act_audit_failed"


class Audit:
    def __init__(self, db, keys):
        self.db, self.keys = db, keys

    # ---------------------------------------------------------------- write
    def actor(self, actor: str, actor_kind: str) -> str:
        """User e-mails become pseudonyms (op_<hex12>); system, agent, model and mcp actors keep their label."""
        if actor_kind == "user" and actor and "@" in actor:
            return self.keys.pseudonym(actor)
        return actor or actor_kind

    def write(self, event: str, *, object_kind: str = "", object_id: Any = "", actor: str = "system", actor_kind: str = "system",
              team_id: Optional[int] = None, payload: Optional[Dict[str, Any]] = None) -> int:
        """Append one event and return its seq. Inside an open db.tx() it joins that transaction; otherwise it opens one."""
        if actor_kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind {actor_kind!r}")
        body = canonical(privacy.scrub_fields(payload or {}))
        psha = sha256_hex(body)
        pseudo = self.actor(actor, actor_kind)
        db = self.db

        def go() -> int:
            lock = " FOR UPDATE" if db.pg else ""
            head = db._exec("SELECT seq, mac FROM act_audit_head WHERE id=1" + lock)
            if not head:
                db._exec("INSERT INTO act_audit_head (id, seq, mac) VALUES (1, 0, '')")
                head = [{"seq": 0, "mac": ""}]
            seq, prev = int(head[0]["seq"] or 0) + 1, head[0]["mac"] or ""
            ts = now()
            row = {"seq": seq, "ts": ts, "actor": pseudo, "actor_kind": actor_kind, "event": event, "object_kind": object_kind,
                   "object_id": str(object_id), "team_id": team_id, "payload_sha256": psha}
            mac = self.keys.mac_audit(seq, prev, sha256_hex(row))
            db._exec("INSERT INTO act_audit (seq, ts, actor, actor_kind, event, object_kind, object_id, team_id, payload_sha256, "
                     "prev_mac, mac) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (seq, ts, pseudo, actor_kind, event, object_kind, str(object_id), team_id, psha, prev, mac))
            db._exec("INSERT INTO act_audit_payload (seq, payload) VALUES (?,?)", (seq, body))
            db._exec("UPDATE act_audit_head SET seq=?, mac=? WHERE id=1", (seq, mac))
            if actor_kind == "user" and pseudo.startswith("op_") and not db._exec("SELECT 1 AS x FROM act_actors WHERE pseudo=?", (pseudo,)):
                db._exec("INSERT INTO act_actors (pseudo, email, created_at) VALUES (?,?,?)", (pseudo, actor.strip().lower(), ts))
            return seq

        try:
            if db.in_tx:
                return go()
            with db.tx():
                return go()
        except Exception as e:  # noqa: BLE001 - the caller's transaction rolls back with it: no action without its audit row
            raise AuditError(f"audit write failed: {e}") from e

    # ---------------------------------------------------------------- read
    def events(self, object_kind: str = "", object_id: str = "", since: str = "", until: str = "", limit: int = 200,
               team_ids: Optional[List[int]] = None) -> List[dict]:
        where, args = [], []
        if object_kind:
            where.append("object_kind=?"); args.append(object_kind)
        if object_id:
            where.append("object_id=?"); args.append(str(object_id))
        if since:
            where.append("ts>=?"); args.append(since)
        if until:
            where.append("ts<?"); args.append(until)
        if team_ids is not None:
            where.append("(team_id IS NULL OR team_id IN (%s))" % ",".join("?" * len(team_ids)) if team_ids else "team_id IS NULL")
            args += list(team_ids)
        sql = "SELECT * FROM act_audit" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY seq DESC LIMIT ?"
        rows = self.db._exec(sql, tuple(args) + (int(limit),))
        pay = {r["seq"]: r["payload"] for r in self.db._exec(
            "SELECT seq, payload FROM act_audit_payload WHERE seq IN (%s)" % ",".join(str(int(r["seq"])) for r in rows))} if rows else {}
        for r in rows:
            try:
                r["payload"] = json.loads(pay.get(r["seq"]) or "{}")
            except ValueError:
                r["payload"] = {}
        return rows

    def head(self) -> dict:
        h = self.db._exec("SELECT seq, mac FROM act_audit_head WHERE id=1")
        return {"seq": int(h[0]["seq"] or 0), "mac": h[0]["mac"] or ""} if h else {"seq": 0, "mac": ""}

    def verify(self, batch: int = 5000) -> dict:
        """Walk the chain: seq continuity, prev_mac links, each MAC, each kept payload's hash. First broken seq wins."""
        prev, expect, n, kinds = "", 1, 0, set()
        last = 0
        while True:
            rows = self.db._exec("SELECT * FROM act_audit WHERE seq>? ORDER BY seq LIMIT ?", (last, batch))
            if not rows:
                break
            pays = {r["seq"]: r["payload"] for r in self.db._exec(
                "SELECT seq, payload FROM act_audit_payload WHERE seq>? AND seq<=?", (last, rows[-1]["seq"]))}
            for r in rows:
                seq = int(r["seq"])
                bad = None
                if seq != expect:
                    bad = "gap"
                elif (r["prev_mac"] or "") != prev:
                    bad = "prev_mac"
                else:
                    row = {k: r[k] for k in ("seq", "ts", "actor", "actor_kind", "event", "object_kind", "object_id", "team_id",
                                             "payload_sha256")}
                    row["seq"] = seq
                    if not self.keys.verify_mac(seq, prev, sha256_hex(row), r["mac"] or ""):
                        bad = "mac"
                    elif seq in pays and sha256_hex(pays[seq]) != r["payload_sha256"]:
                        bad = "payload"
                if bad:
                    return {"ok": False, "n": n, "broken_at": seq if bad != "gap" else expect, "reason": bad, "mac": sorted(kinds)}
                kinds.add((r["mac"] or "")[:2])
                prev, expect, n, last = r["mac"] or "", seq + 1, n + 1, seq
        head = self.head()
        if head["seq"] != n or head["mac"] != prev:
            return {"ok": False, "n": n, "broken_at": n + 1, "reason": "head", "mac": sorted(kinds)}
        return {"ok": True, "n": n, "broken_at": None, "reason": "", "mac": sorted(kinds),
                "placeholder": "p0" in kinds or not kinds}
