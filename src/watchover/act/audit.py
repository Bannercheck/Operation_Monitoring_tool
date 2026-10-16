"""The action agent's audit log (docs/design/action-agent.md §10.7): append-only, keyed-hash-chained, one writer (the API process).

    with db.tx():                       # the state change and its audit row commit together (I9: audit fails -> no action)
        db._exec("UPDATE actions SET phase=? WHERE id=?", ("ready", cid))
        audit.write("case.ready", object_kind="case", object_id=cid, actor=email, actor_kind="user", payload={...})

The MAC comes from watchover-keyd (act/keyd.py), which keeps its own head and MACs only seq = head + 1, so history cannot be
re-MAC'd through it (I9). The head row is locked for the write (FOR UPDATE on PostgreSQL, the process lock + BEGIN IMMEDIATE on
SQLite); the keyd call is the one non-SQL step inside the transaction and is bounded (KeyClient timeout). When keyd's head is
ahead of the database (a restore of an older database, or rows deleted after the triggers were dropped) writing continues after
keyd's head with a `chain.gap` row first: the gap shows on verification, by design, and nothing is deleted. Triggers make
act_audit, act_audit_payload and act_anchors append-only (store.ensure_triggers); payloads are purgeable at or below the purge
horizon and the chain stays valid. Actors are pseudonyms (op_<hex>); the map lives in act_actors (erasable, KVKK)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import privacy
from .keyd import _canon
from .keys import KeyRefused, SigningUnavailable, p0
from .types import ACTOR_KINDS, canonical, now, sha256_hex

ROW_FIELDS = ("seq", "ts", "actor", "actor_kind", "event", "object_kind", "object_id", "team_id", "payload_sha256")


class AuditError(RuntimeError):
    code = "act_audit_failed"
    status = 503

    def detail(self) -> dict:
        if isinstance(self.__cause__, SigningUnavailable):
            return self.__cause__.detail()
        return {"code": self.code, "tr": "Denetim kaydı yazılamadı: işlem yapılmadı", "en": "The audit row could not be written: nothing was done",
                "reason": str(self)[:200]}


def _digest(row: dict) -> str:
    return sha256_hex({k: row[k] for k in ROW_FIELDS})


def verify_anchor_sig(a: dict, pub: str) -> bool:
    """Ed25519 by anchor.key over "wo-anchor-v1\\n" + canonical({seq, mac, ts})."""
    if not pub or not a.get("sig"):
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub.split(":", 1)[1])).verify(
            base64.b64decode(a["sig"]), b"wo-anchor-v1\n" + _canon({"seq": int(a["seq"]), "mac": a["mac"] or "", "ts": a["ts"]}))
        return True
    except Exception:  # noqa: BLE001 - any failure is "not verified"
        return False


class Audit:
    def __init__(self, db, keys):
        self.db, self.keys = db, keys
        self.last: Optional[dict] = None              # the last verification (chain cap, status chip)
        self.listeners: List = []                     # callables(event) after a write (anchors: kill / policy / promotion)

    # ---------------------------------------------------------------- write
    def actor(self, actor: str, actor_kind: str) -> str:
        """User e-mails become pseudonyms (op_<hex12>); system, agent, model and mcp actors keep their label."""
        if actor_kind == "user" and actor and "@" in actor:
            return self.keys.pseudonym(actor)
        return actor or actor_kind

    def _append(self, seq: int, prev: str, ts: str, actor: str, actor_kind: str, event: str, object_kind: str, object_id: str,
                team_id: Optional[int], psha: str, body: str) -> str:
        row = {"seq": seq, "ts": ts, "actor": actor, "actor_kind": actor_kind, "event": event, "object_kind": object_kind,
               "object_id": object_id, "team_id": team_id, "payload_sha256": psha}
        mac = self.keys.mac_audit(seq, prev, _digest(row))
        self.db._exec("INSERT INTO act_audit (seq, ts, actor, actor_kind, event, object_kind, object_id, team_id, payload_sha256, "
                      "prev_mac, mac) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (seq, ts, actor, actor_kind, event, object_kind, object_id, team_id, psha, prev, mac))
        self.db._exec("INSERT INTO act_audit_payload (seq, payload) VALUES (?,?)", (seq, body))
        return mac

    def write(self, event: str, *, object_kind: str = "", object_id: Any = "", actor: str = "system", actor_kind: str = "system",
              team_id: Optional[int] = None, payload: Optional[Dict[str, Any]] = None) -> int:
        """Append one event and return its seq. Inside an open db.tx() it joins that transaction; otherwise it opens one."""
        if actor_kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind {actor_kind!r}")
        body = canonical(privacy.scrub_fields(payload or {}))
        psha = sha256_hex(body)
        db = self.db

        def go() -> int:
            pseudo = self.actor(actor, actor_kind)
            lock = " FOR UPDATE" if db.pg else ""
            head = db._exec("SELECT seq, mac FROM act_audit_head WHERE id=1" + lock)
            if not head:
                db._exec("INSERT INTO act_audit_head (id, seq, mac) VALUES (1, 0, '')")
                head = [{"seq": 0, "mac": ""}]
            seq, prev = int(head[0]["seq"] or 0) + 1, head[0]["mac"] or ""
            ts = now()
            args = (pseudo, actor_kind, event, object_kind, str(object_id), team_id, psha, body)
            try:
                mac = self._append(seq, prev, ts, *args)
            except KeyRefused as e:
                kh = e.head or {}
                if e.code != "seq" or "seq" not in kh:
                    raise
                if int(kh["seq"]) < seq - 1:
                    raise AuditError(f"keyd's head #{kh['seq']} is behind the database (#{seq - 1}): rows keyd never MAC'd") from e
                # keyd is ahead: continue after its head; the missing rows show as a gap on verification (nothing is deleted)
                gap = {"missing_from": seq, "missing_to": int(kh["seq"]), "db_head": seq - 1}
                gbody = canonical(gap)
                seq, prev = int(kh["seq"]) + 1, kh.get("mac") or ""
                prev = self._append(seq, prev, ts, "system", "system", "chain.gap", "audit", str(gap["missing_from"]), None,
                                    sha256_hex(gbody), gbody)
                seq += 1
                mac = self._append(seq, prev, ts, *args)
            db._exec("UPDATE act_audit_head SET seq=?, mac=? WHERE id=1", (seq, mac))
            if actor_kind == "user" and pseudo.startswith("op_") and not db._exec("SELECT 1 AS x FROM act_actors WHERE pseudo=?", (pseudo,)):
                db._exec("INSERT INTO act_actors (pseudo, email, created_at) VALUES (?,?,?)", (pseudo, actor.strip().lower(), ts))
            return seq

        try:
            if db.in_tx:
                seq = go()
            else:
                with db.tx():
                    seq = go()
        except AuditError:
            raise
        except Exception as e:  # noqa: BLE001 - the caller's transaction rolls back with it: no action without its audit row
            raise AuditError(f"audit write failed: {e}") from e
        for fn in list(self.listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass
        return seq

    # ---------------------------------------------------------------- read
    def events(self, object_kind: str = "", object_id: str = "", since: str = "", until: str = "", limit: int = 200,
               team_ids: Optional[List[int]] = None, event: str = "") -> List[dict]:
        where, args = [], []
        if object_kind:
            where.append("object_kind=?"); args.append(object_kind)
        if object_id:
            where.append("object_id=?"); args.append(str(object_id))
        if event:
            where.append("event=?"); args.append(event)
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
            r["purged"] = r["seq"] not in pay
        return rows

    def head(self) -> dict:
        h = self.db._exec("SELECT * FROM act_audit_head WHERE id=1")
        return {"seq": int(h[0]["seq"] or 0), "mac": h[0]["mac"] or "", "purged_seq": int(h[0].get("purged_seq") or 0)} if h else \
            {"seq": 0, "mac": "", "purged_seq": 0}

    # ---------------------------------------------------------------- retention and erasure (the chain keeps verifying)
    def purge_payloads(self, older_than_days: Optional[int] = None, upto_seq: Optional[int] = None, by: str = "system") -> dict:
        """Delete payloads at or below a horizon (seq of the newest row older than the retention). Rows and MACs stay."""
        if upto_seq is None:
            days = older_than_days if older_than_days is not None else 400
            cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            r = self.db._exec("SELECT MAX(seq) AS s FROM act_audit WHERE ts<?", (cut,))
            upto_seq = int(r[0]["s"] or 0) if r else 0
        h = self.head()
        upto = max(int(upto_seq), h["purged_seq"])
        if upto <= h["purged_seq"]:
            return {"purged_seq": h["purged_seq"], "deleted": 0}
        with self.db.tx():
            n = self.db._exec("SELECT COUNT(*) AS n FROM act_audit_payload WHERE seq<=?", (upto,))[0]["n"]
            self.db._exec("UPDATE act_audit_head SET purged_seq=? WHERE id=1", (upto,))
            self.db._exec("DELETE FROM act_audit_payload WHERE seq<=?", (upto,))
            self.write("audit.purged", object_kind="audit", object_id=upto, actor=by, actor_kind="user" if "@" in by else "system",
                       payload={"upto_seq": upto, "payloads": int(n)})
        return {"purged_seq": upto, "deleted": int(n)}

    def erase_actor(self, who: str, by: str = "system") -> int:
        """KVKK erasure: the pseudonym stays in the chain, its e-mail goes (act.audit holders can no longer map it back)."""
        w = (who or "").strip().lower()
        rows = self.db._exec("SELECT pseudo FROM act_actors WHERE (pseudo=? OR email=?) AND erased_at=''", (w, w))
        if not rows:
            return 0
        with self.db.tx():
            for r in rows:
                self.db._exec("UPDATE act_actors SET email='', erased_at=? WHERE pseudo=?", (now(), r["pseudo"]))
                self.write("actor.erased", object_kind="actor", object_id=r["pseudo"], actor=by,
                           actor_kind="user" if "@" in by else "system", payload={"pseudo": r["pseudo"]})
        return len(rows)

    # ---------------------------------------------------------------- verification
    def verify(self, batch: int = 2000) -> dict:
        """Walk the chain: seq continuity, prev_mac links, each MAC (through keyd), each kept payload's hash, the head row,
        keyd's head and every anchor (act_anchors, keyd's anchor log, auth_events rows, actuator attestations).
        The first broken seq wins."""
        custody = self.keys.custody()
        try:
            kh = self.keys.head() if custody != "none" else None
        except (SigningUnavailable, KeyRefused):
            return self._done({"ok": False, "n": 0, "broken_at": None, "reason": "keyd_unreachable", "mac": []})
        adopted = int((kh or {}).get("adopted") or 0) if kh and kh.get("set") else None
        own = {"embedded": "e1", "keyd": "k1"}.get(custody, "")
        horizon = self.head()["purged_seq"]
        acks = self._acks()
        prev, expect, n, last, purged = "", 1, 0, 0, 0
        kinds, macs = set(), {}
        missing: List[tuple] = []                     # acknowledged gaps: the walk continues past them
        bad: Optional[tuple] = None
        while bad is None:
            rows = self.db._exec("SELECT * FROM act_audit WHERE seq>? ORDER BY seq LIMIT ?", (last, batch))
            if not rows:
                break
            pays = {r["seq"]: r["payload"] for r in self.db._exec(
                "SELECT seq, payload FROM act_audit_payload WHERE seq>? AND seq<=?", (last, rows[-1]["seq"]))}
            keyed: List[list] = []
            cands = []
            for r in rows:
                seq = int(r["seq"])
                mac = r["mac"] or ""
                pre = mac[:2]
                if seq != expect or (r["prev_mac"] or "") != prev:
                    at = expect if seq != expect else seq
                    if at not in acks:
                        cands.append((at, "gap" if seq != expect else "prev_mac"))
                        break
                    missing.append((expect, seq - 1))   # acknowledged by two act.policy holders: accept the new link
                    prev = r["prev_mac"] or ""
                row = {k: r[k] for k in ROW_FIELDS}
                row["seq"] = seq
                digest = _digest(row)
                if pre == "p0":
                    if p0(prev, digest) != mac:
                        cands.append((seq, "mac"))
                        break
                    if own and adopted is not None and seq > adopted:
                        cands.append((seq, "placeholder_after_keys"))      # an unkeyed row after keyd took over: forged
                        break
                elif pre == own and (adopted is None or seq > adopted):
                    keyed.append([seq, prev, digest, mac])
                else:
                    cands.append((seq, "unverifiable"))                      # a MAC of a custody we do not hold
                    break
                if seq in pays:
                    if sha256_hex(pays[seq]) != r["payload_sha256"]:
                        cands.append((seq, "payload"))
                        break
                elif seq > horizon:
                    cands.append((seq, "payload_missing"))
                    break
                else:
                    purged += 1
                kinds.add(pre)
                macs[seq] = mac
                prev, expect, n, last = mac, seq + 1, n + 1, seq
            if keyed:
                try:
                    i = self.keys.verify_macs(keyed)
                except (SigningUnavailable, KeyRefused):
                    return self._done({"ok": False, "n": n, "broken_at": keyed[0][0], "reason": "keyd_unreachable", "mac": sorted(kinds)})
                if i >= 0:
                    cands.append((keyed[i][0], "mac"))
            if cands:
                bad = min(cands)
            elif len(rows) < batch:
                break
        if bad is not None:
            return self._done({"ok": False, "n": n, "broken_at": bad[0], "reason": bad[1], "mac": sorted(kinds), "purged": purged})
        res = {"ok": True, "n": n, "broken_at": None, "reason": "", "mac": sorted(kinds), "purged": purged,
               "placeholder": "p0" in kinds or not kinds, "gaps": self._gaps(), "acknowledged": [list(m) for m in missing]}
        h = self.head()
        if h["seq"] != last or h["mac"] != prev:
            return self._done({**res, "ok": False, "broken_at": last + 1, "reason": "head"})
        if kh and kh.get("set"):
            ks = int(kh["seq"])
            res["keyd_head"] = {"seq": ks, "confirmed": bool(kh.get("confirmed"))}
            if ks == last and kh.get("mac") != prev and last:
                return self._done({**res, "ok": False, "broken_at": last, "reason": "keyd_head"})
            if ks > last and not (ks == last + 1 and not kh.get("confirmed")):
                return self._done({**res, "ok": False, "broken_at": last + 1, "reason": "tail"})
            if ks < last and macs.get(ks + 1, "")[:2] == own:
                return self._done({**res, "ok": False, "broken_at": ks + 1, "reason": "beyond_keyd"})
        anc = self._check_anchors(macs, last, missing)
        res["anchors"] = anc["summary"]
        if anc["bad"]:
            return self._done({**res, "ok": False, "broken_at": anc["bad"][0], "reason": anc["bad"][1]})
        return self._done(res)

    def _gaps(self) -> List[dict]:
        out = []
        for r in self.events(event="chain.gap", limit=50):
            p = r.get("payload") or {}
            out.append({"seq": r["seq"], "from": p.get("missing_from"), "to": p.get("missing_to"), "ts": r["ts"]})
        return out

    def _check_anchors(self, macs: Dict[int, str], last: int, missing=()) -> dict:
        """Anchors must agree with the rows they name: signed ones by anchor.key, the others by MAC prefix."""
        pub = self.keys.anchor_pub() if self.keys.custody() != "none" else ""
        bad: List[tuple] = []
        n = sig_ok = 0
        rows = self.db._exec("SELECT * FROM act_anchors ORDER BY id")
        try:
            keyd_log = self.keys.anchors(0)
        except (SigningUnavailable, KeyRefused):
            keyd_log = []
        for src, items in (("anchor", rows), ("anchor_keyd", keyd_log)):
            for a in items:
                s = int(a.get("seq") or 0)
                if s <= 0:
                    continue
                n += 1
                if pub:
                    if verify_anchor_sig(a, pub):
                        sig_ok += 1
                    elif src == "anchor":
                        bad.append((s, "anchor_sig"))
                if s <= last and macs.get(s) != a.get("mac") and not any(lo <= s <= hi for lo, hi in missing):
                    bad.append((s, src))
        if "auth_events" in self.db.tables():
            for r in self.db._exec("SELECT detail FROM auth_events WHERE event='act_anchor' ORDER BY id"):
                try:
                    parts = dict(p.split("=", 1) for p in (r["detail"] or "").split() if "=" in p)
                    s, m = int(parts.get("seq") or 0), parts.get("mac") or ""
                except ValueError:
                    continue
                if 0 < s <= last and m and (macs.get(s) or "")[3:35] != m and not any(lo <= s <= hi for lo, hi in missing):
                    bad.append((s, "anchor_auth_events"))
        for r in self.db._exec("SELECT anchor_seq, anchor_mac FROM act_anchor_attest"):
            s = int(r["anchor_seq"] or 0)
            if 0 < s <= last and r["anchor_mac"] and macs.get(s) != r["anchor_mac"]:
                bad.append((s, "anchor_attest"))
        attest = self.db._exec("SELECT COUNT(DISTINCT agent_id) AS n FROM act_anchor_attest")[0]["n"]
        lastrow = rows[-1] if rows else None
        return {"bad": min(bad) if bad else None,
                "summary": {"n": n, "signed_ok": sig_ok, "last_seq": int(lastrow["seq"]) if lastrow else None,
                            "last_ts": lastrow["ts"] if lastrow else None, "attesting_agents": int(attest or 0)}}

    def _done(self, res: dict) -> dict:
        res["checked_at"] = now()
        self.last = res
        return res

    # ---------------------------------------------------------------- chain break → L2 everywhere until two people acknowledge
    def _acks(self) -> set:
        out = set()
        for r in self.db._exec("SELECT body FROM act_changes WHERE kind='chain_ack' AND status='applied'"):
            try:
                out.add(int(json.loads(r["body"] or "{}").get("broken_at")))
            except (TypeError, ValueError):
                continue
        return out

    def acknowledged(self, broken_at) -> bool:
        return broken_at is None or int(broken_at) in self._acks()

    def chain_cap(self) -> int:
        """The `chain` cap: a broken chain caps every act step at L2 until two act.policy holders acknowledge it."""
        v = self.last
        if v is None or v.get("ok") or v.get("reason") == "keyd_unreachable":
            return 4
        return 4 if self.acknowledged(v.get("broken_at")) else 2

    def chain_state(self) -> dict:
        v = self.last
        if v is None:
            return {"state": "unchecked"}
        if v.get("ok"):
            return {"state": "placeholder" if v.get("placeholder") and v.get("mac") == ["p0"] else "ok", "n": v["n"], "checked_at": v["checked_at"]}
        return {"state": "broken", "broken_at": v.get("broken_at"), "reason": v.get("reason"), "checked_at": v["checked_at"],
                "acknowledged": self.acknowledged(v.get("broken_at"))}
