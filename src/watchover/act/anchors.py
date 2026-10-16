"""External anchors of the audit chain (docs/design/action-agent.md §10.7; R3b, I19).

Hourly (`act_anchor_every_min`) and right after every kill, promotion, policy change and model promotion, keyd signs
{seq, mac, ts} of its *own* head with anchor.key. Each anchor is
  1. delivered to every enabled external sink and acknowledged (`act_anchor_acks`; failed deliveries are retried with the same
     content, never re-sent with different content);
  2. appended to keyd's own anchors.log on the keys volume (keyd does it);
  3. written as an `auth_events` row `act_anchor` whose detail holds `seq=<n> mac=<32 hex>`;
  4. stored in `act_anchors` (append-only) for hello answers (WP11a) and verification;
  5. e-mailed daily to `act_anchor_recipients` (outside the administrators' own mailboxes).
While no sink is configured, or the newest acknowledged anchor is older than `act_anchor_max_age_min` (120), every act step is
capped at L2 (`anchor` cap, I19). Sinks: syslog over TLS (RFC 5425 framing), an S3-compatible bucket with object lock (HTTP PUT
with the object-lock headers; the target is a pre-signed or gateway URL), a WORM share (create-only files in a directory)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import vault
from .keys import KeyRefused, SigningUnavailable
from .types import now

SINK_KINDS = ("syslog_tls", "s3_object_lock", "worm_share")
ANCHOR_EVENTS = ("kill.on", "kill.off", "policy.changed", "promotion.signed", "level.promoted", "model.champion", "chain.acknowledged")


def _age_min(ts: str) -> Optional[float]:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds() / 60.0


def anchor_doc(a: dict) -> bytes:
    """What leaves the installation: the anchor exactly as keyd signed it (sig and kid included), one JSON line."""
    return (json.dumps({"watchover_anchor": 1, "seq": int(a["seq"]), "mac": a["mac"], "ts": a["ts"], "sig": a.get("sig", ""),
                        "kid": a.get("kid", "")}, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class SinkError(RuntimeError):
    pass


def deliver(kind: str, target: str, secret: str, a: dict, timeout: float = 10.0) -> str:
    """Send one anchor to one sink; returns a short acknowledgement text or raises SinkError."""
    doc = anchor_doc(a)
    if kind == "worm_share":
        d = Path(target)
        if not d.is_dir():
            raise SinkError(f"not a directory: {target}")
        name = f"watchover-anchor-{int(a['seq']):012d}-{hashlib.sha256(doc).hexdigest()[:12]}.json"
        p = d / name
        if p.exists():
            if p.read_bytes() == doc:
                return f"already stored: {name}"
            raise SinkError(f"{name} exists with other content")
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except OSError as e:
            raise SinkError(str(e))
        try:
            os.write(fd, doc)
            os.fsync(fd)
        finally:
            os.close(fd)
        return f"stored: {name}"
    if kind == "syslog_tls":
        host, _, port = target.rpartition(":")
        ctx = ssl.create_default_context(cadata=secret) if secret and "BEGIN CERTIFICATE" in secret else ssl.create_default_context()
        msg = f"<110>1 {a['ts']} {socket.gethostname()} watchover - act_anchor - ".encode() + doc.rstrip(b"\n")
        frame = str(len(msg)).encode() + b" " + msg                     # RFC 5425 octet counting
        try:
            with socket.create_connection((host, int(port or 6514)), timeout=timeout) as s:
                with ctx.wrap_socket(s, server_hostname=host) as t:
                    t.sendall(frame)
        except (OSError, ssl.SSLError, ValueError) as e:
            raise SinkError(str(e))
        return f"sent {len(frame)} bytes"
    if kind == "s3_object_lock":
        url = target.replace("{seq}", str(int(a["seq"]))) if "{seq}" in target else target.rstrip("/") + f"/watchover-anchor-{int(a['seq']):012d}.json"
        until = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
        req = urllib.request.Request(url, data=doc, method="PUT", headers={
            "Content-Type": "application/json", "Content-MD5": base64.b64encode(hashlib.md5(doc).digest()).decode(),  # noqa: S324 - S3 integrity header
            "x-amz-object-lock-mode": "COMPLIANCE", "x-amz-object-lock-retain-until-date": until,
            **({"Authorization": f"Bearer {secret}"} if secret and "BEGIN" not in secret else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - admin-configured sink URL
                if r.status >= 300:
                    raise SinkError(f"HTTP {r.status}")
                return f"HTTP {r.status}"
        except SinkError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SinkError(str(e)[:200])
    raise SinkError(f"unknown sink kind {kind}")


class Anchors:
    def __init__(self, db, keys, audit, policy):
        self.db, self.keys, self.audit, self.policy = db, keys, audit, policy
        self._pending = False
        self._lock = threading.Lock()
        audit.listeners.append(self.on_event)

    # ---------------------------------------------------------------- sinks
    def sinks(self, enabled_only: bool = False) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_anchor_sinks" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id")
        for r in rows:
            r["has_secret"] = bool(r.get("secret"))
        return rows

    def public_sinks(self) -> List[dict]:
        return [{k: v for k, v in r.items() if k != "secret"} for r in self.sinks()]

    @staticmethod
    def check_sinks(items: List[dict]) -> List[dict]:
        out = []
        for it in items or []:
            kind = str(it.get("kind") or "")
            target = str(it.get("target") or "").strip()
            if kind not in SINK_KINDS:
                raise ValueError(f"sink kind must be one of {', '.join(SINK_KINDS)}")
            if not target:
                raise ValueError("sink target is required")
            if kind == "s3_object_lock" and not target.startswith("https://") and not target.startswith("http://127.0.0.1"):
                raise ValueError("an object-lock bucket URL must be https")
            out.append({"kind": kind, "target": target[:500], "secret": str(it.get("secret") or ""), "enabled": bool(it.get("enabled", True)),
                        "id": it.get("id")})
        return out

    def set_sinks(self, items: List[dict], by: str) -> List[dict]:
        """Replace the sink list (after the second act.policy holder confirmed). Secrets go into the vault; an item that keeps its
        id and sends no secret keeps the stored one."""
        items = self.check_sinks(items)
        old = {r["id"]: r for r in self.sinks()}
        with self.db.tx():
            keep = set()
            for it in items:
                sec = vault.encrypt(it["secret"]) if it["secret"] else (old.get(it["id"], {}).get("secret") or "")
                if it["id"] in old:
                    self.db._exec("UPDATE act_anchor_sinks SET kind=?, target=?, secret=?, enabled=?, set_by=?, set_at=? WHERE id=?",
                                  (it["kind"], it["target"], sec, int(it["enabled"]), by, now(), it["id"]))
                    keep.add(it["id"])
                else:
                    keep.add(self.db._insert("INSERT INTO act_anchor_sinks (kind, target, secret, enabled, set_by, set_at) VALUES (?,?,?,?,?,?)",
                                             (it["kind"], it["target"], sec, int(it["enabled"]), by, now())))
            for sid in set(old) - keep:
                self.db._exec("DELETE FROM act_anchor_sinks WHERE id=?", (sid,))
            self.audit.write("anchor.sinks_changed", object_kind="anchor_sinks", actor=by, actor_kind="user" if "@" in by else "system",
                             payload={"sinks": [{"kind": i["kind"], "target": i["target"], "enabled": i["enabled"]} for i in items]})
        return self.public_sinks()

    def test_sink(self, sid: int) -> dict:
        rows = [r for r in self.sinks() if r["id"] == sid]
        if not rows:
            raise KeyError(sid)
        a = self.latest() or {"seq": 0, "mac": "", "ts": now(), "sig": "", "kid": "test"}
        try:
            msg = deliver(rows[0]["kind"], rows[0]["target"], vault.decrypt(rows[0].get("secret") or ""), {**a, "kid": "test"})
            self.db._exec("UPDATE act_anchor_sinks SET last_ok=?, last_error='' WHERE id=?", (now(), sid))
            return {"ok": True, "detail": msg}
        except SinkError as e:
            self.db._exec("UPDATE act_anchor_sinks SET last_error=? WHERE id=?", (str(e)[:300], sid))
            return {"ok": False, "detail": str(e)[:300]}

    # ---------------------------------------------------------------- anchoring
    def on_event(self, event: str) -> None:
        if event in ANCHOR_EVENTS:
            self._pending = True

    def latest(self) -> Optional[dict]:
        rows = self.db._exec("SELECT * FROM act_anchors ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def due(self) -> bool:
        if self._pending:
            return True
        last = self.latest()
        every = float(self.policy.get("act_anchor_every_min", 60) or 60)
        return last is None or (_age_min(last["ts"]) or 1e9) >= every

    def anchor(self, reason: str = "hourly") -> Optional[dict]:
        """Ask keyd for an anchor of its own head, deliver it, store it. None without keys or with nothing to anchor."""
        with self._lock:
            self._pending = False
            if self.db.in_tx:
                raise RuntimeError("anchor() runs after the commit, never inside a transaction")
            h = self.audit.head()                                   # committed rows only: confirms keyd's newest MAC when it matches
            try:
                a = self.keys.sign_anchor({"seq": h["seq"], "mac": h["mac"]})
            except (SigningUnavailable, KeyRefused):
                return None
            if not a or int(a.get("seq") or 0) <= 0:
                return None
            sinks = self.sinks(enabled_only=True)
            acks = {}
            for s in sinks:
                try:
                    acks[s["id"]] = (1, deliver(s["kind"], s["target"], vault.decrypt(s.get("secret") or ""), a))
                except SinkError as e:
                    acks[s["id"]] = (0, str(e)[:300])
            ts = now()
            with self.db.tx():
                aid = self.db._insert("INSERT INTO act_anchors (seq, mac, ts, sig, sinks) VALUES (?,?,?,?,?)",
                                      (int(a["seq"]), a["mac"], a["ts"], a["sig"],
                                       json.dumps({str(k): (ts if v[0] else "") for k, v in acks.items()}, sort_keys=True)))
                for sid, (ok, detail) in acks.items():
                    self.db._exec("INSERT INTO act_anchor_acks (anchor_id, sink_id, ts, ok, detail) VALUES (?,?,?,?,?)", (aid, sid, ts, ok, detail))
                    self.db._exec("UPDATE act_anchor_sinks SET " + ("last_ok=?, last_error='' " if ok else "last_error=? ") + "WHERE id=?",
                                  (ts if ok else detail, sid))
                if "auth_events" in self.db.tables():
                    self.db._exec("INSERT INTO auth_events (ts, email, event, ok, detail) VALUES (?,?,?,?,?)",
                                  (ts, "act@watchover", "act_anchor", 1, f"seq={int(a['seq'])} mac={(a['mac'] or '')[3:35]} reason={reason}"[:200]))
                self.audit.write("anchor", object_kind="anchor", object_id=aid,
                                 payload={"seq": int(a["seq"]), "mac": (a["mac"] or "")[:35], "reason": reason, "kid": a.get("kid", ""),
                                          "sinks_ok": sum(1 for v in acks.values() if v[0]), "sinks": len(acks)})
            self._pending = False
            return {**a, "id": aid, "acks": {k: {"ok": bool(v[0]), "detail": v[1]} for k, v in acks.items()}}

    def retry(self, hours: int = 24) -> int:
        """Deliver anchors a sink has not acknowledged yet (same content); returns the number of new acknowledgements."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        n = 0
        for s in self.sinks(enabled_only=True):
            done = {r["anchor_id"] for r in self.db._exec("SELECT anchor_id FROM act_anchor_acks WHERE sink_id=? AND ok=1", (s["id"],))}
            for a in self.db._exec("SELECT * FROM act_anchors WHERE ts>=? ORDER BY id", (since,)):
                if a["id"] in done:
                    continue
                try:
                    detail, ok = deliver(s["kind"], s["target"], vault.decrypt(s.get("secret") or ""), a), 1
                except SinkError as e:
                    detail, ok = str(e)[:300], 0
                ts = now()
                if self.db._exec("SELECT id FROM act_anchor_acks WHERE anchor_id=? AND sink_id=?", (a["id"], s["id"])):
                    self.db._exec("UPDATE act_anchor_acks SET ts=?, ok=?, detail=? WHERE anchor_id=? AND sink_id=?", (ts, ok, detail, a["id"], s["id"]))
                else:
                    self.db._exec("INSERT INTO act_anchor_acks (anchor_id, sink_id, ts, ok, detail) VALUES (?,?,?,?,?)", (a["id"], s["id"], ts, ok, detail))
                n += ok
                if not ok:
                    break                                            # the sink is still down: try again next time
        return n

    def tick(self) -> Optional[dict]:
        """From the act thread every few seconds: an anchor when due (hourly or after an anchoring event), then retries."""
        res = None
        if self.keys.custody() != "none" and self.due():
            res = self.anchor("event" if self._pending else "hourly")
        try:
            self.retry()
        except Exception:  # noqa: BLE001
            pass
        return res

    # ---------------------------------------------------------------- freshness → the `anchor` cap (I19)
    def freshness(self) -> dict:
        sinks = self.sinks(enabled_only=True)
        max_age = float(self.policy.get("act_anchor_max_age_min", 120) or 120)
        if not sinks:
            return {"configured": False, "fresh": False, "age_min": None, "last_ts": None, "max_age_min": max_age}
        ids = ",".join(str(int(s["id"])) for s in sinks)
        r = self.db._exec(f"SELECT MAX(a.ts) AS ts FROM act_anchors a JOIN act_anchor_acks k ON k.anchor_id=a.id "
                          f"WHERE k.ok=1 AND k.sink_id IN ({ids})")
        ts = r[0]["ts"] if r else None
        age = _age_min(ts) if ts else None
        return {"configured": True, "fresh": age is not None and age <= max_age, "age_min": None if age is None else round(age, 1),
                "last_ts": ts, "max_age_min": max_age}

    def cap(self) -> int:
        """The `anchor` cap: no sink, or the last acknowledged external anchor older than act_anchor_max_age_min → L2."""
        return 4 if self.freshness()["fresh"] else 2

    # ---------------------------------------------------------------- actuator counter-signatures (stored by WP11a's hello)
    def attest(self, agent_id: int, anchor_seq: int, anchor_mac: str, ledger_head: str = "", sig: str = "") -> bool:
        if not anchor_seq:
            return False
        if self.db._exec("SELECT 1 AS x FROM act_anchor_attest WHERE agent_id=? AND anchor_seq=?", (int(agent_id), int(anchor_seq))):
            return False
        self.db._exec("INSERT INTO act_anchor_attest (agent_id, anchor_seq, anchor_mac, ledger_head, sig, ts) VALUES (?,?,?,?,?,?)",
                      (int(agent_id), int(anchor_seq), anchor_mac or "", ledger_head or "", sig or "", now()))
        return True

    # ---------------------------------------------------------------- daily mail to recipients outside the administrators
    def recipients(self) -> List[str]:
        r = self.policy.get("act_anchor_recipients") or []
        return [str(x).strip().lower() for x in r if str(x).strip()]

    def daily_mail(self, send=None) -> Optional[dict]:
        a = self.latest()
        to = self.recipients()
        if not a or not to:
            return None
        text = (f"Watchover denetim zinciri çapası / audit chain anchor\nseq: {a['seq']}\nmac: {a['mac']}\nts: {a['ts']}\n"
                f"sig: {a['sig']}\n\nBu iletiyi saklayın; zincir doğrulaması bu değerle karşılaştırılabilir.\n"
                f"Keep this message; the chain can be checked against it.\n")
        msg = {"to": to, "subject": f"Watchover anchor #{a['seq']}", "text": text}
        if send is None:
            try:
                from ..api.services import notify_channels
                from ..notify import send_email
                cfg = notify_channels().get("email") or {}
                if cfg.get("host"):
                    send_email(cfg, to, msg["subject"], text)
                    msg["sent"] = True
            except Exception as e:  # noqa: BLE001
                msg["error"] = str(e)[:200]
        else:
            send(msg)
            msg["sent"] = True
        return msg
