"""The pull job channel, server side (docs/design/action-agent.md §9.3.2–§9.3.6, §9.7; C1, R4, R6, R9, I7, I13).

Actuators never accept connections: they poll `/agent/v1/` with a per-host actuator token (`wa_…`, only its sha256 is stored in
`act_actuators.token_sha256` and published to the Go receiver's auth.json under "actuators"). Collector tokens (`wo_`), the
legacy shared key and open mode are refused on this path, and `wa_` tokens are refused everywhere else (live.py, receiver/main.go).

    POST /agent/v1/hello                    registration, capabilities, heartbeat; signed with the agent key
    GET  /agent/v1/jobs?after=<seq>         204 + X-Poll-After from memory, or 200 {"jobs": [envelope…], "cancel": […], "halt", "poll_s"}
    POST /agent/v1/jobs/{job_id}/start      {"ok": true} | {"ok": false, "cancel": true}
    POST /agent/v1/jobs/{job_id}/result     signed result envelope; one result per job_id

* Until the agent key is confirmed by a person (Bağlantı ayarları › "Eylem anahtarını onayla") only `hello` is accepted and no job
  is ever issued. After confirmation every request carries `X-WO-Sig: <kid>:<Ed25519 over "wo-req-v1\\n" + method + "\\n" + path +
  "\\n" + ts + "\\n" + sha256(body)>` and `X-WO-Ts` (± 120 s); unsigned or stale requests are refused.
* `seq` is assigned when a job is leased, in delivery order, and signed by keyd (which refuses a seq not above the last it signed
  for that agent), so it strictly increases per agent (R6).
* The host lock is inserted in the same transaction as the `dispatched` transition (`act_host_locks`, R9): two concurrent
  dispatches to one host → one lock, the other gets `act_host_locked`.
* Fail closed (I13): keyd unavailable → nothing is leased; clock skew > 120 s → no job; a kill switch at fetch → no job; a result
  that does not verify is stored with `result_sig_ok = 0` and never becomes signed evidence; a ledger line the server never issued
  → host stop and token rotation."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .. import privacy
from .keyd import fingerprint
from .keys import KeyRefused, SigningUnavailable
from .types import canonical, now, sha256_hex

SKEW_S = 120
POLL_S = 10
FAST_POLL_S = 2
BODY_MAX = 1 << 20
ACT_MODES = ("execute", "rollback")                 # take the host lock; diagnose / verify / dry_run are read-only
WINDOW = {"execute": 300, "rollback": 300, "dry_run": 600, "diagnose": 600, "verify": 600}
LEASE_MAX = {"act": 1, "diag": 2}
RESULT_STATES = {"done": "done", "ok": "done", "failed": "failed", "rejected": "rejected", "timed_out": "failed", "cancelled": "cancelled"}
SECURITY_REJECTS = ("bad_sig", "wrong_host", "seq_replay")


class ChannelError(Exception):
    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status, self.error, self.extra = status, error, extra

    def body(self) -> dict:
        return {"error": self.error, **self.extra}


class HostLocked(RuntimeError):
    code = "act_host_locked"
    status = 409

    def __init__(self, host: str, run_id=None):
        super().__init__(f"{host} is locked by run {run_id}")
        self.host, self.run_id = host, run_id

    def detail(self) -> dict:
        return {"code": self.code, "tr": f"{self.host} üzerinde başka bir adım çalışıyor (koşu #{self.run_id})",
                "en": f"Another step is running on {self.host} (run #{self.run_id})", "run_id": self.run_id}


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _wire(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s) -> Optional[datetime]:
    try:
        if isinstance(s, (int, float)) or (isinstance(s, str) and re.fullmatch(r"\d{9,11}(\.\d+)?", s)):
            return datetime.fromtimestamp(float(s), timezone.utc)
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def ed25519_verify(pub: str, sig_b64: str, msg: bytes) -> bool:
    """Ed25519 with `cryptography` (WP11c adds the pure-Python verifier for hosts without it)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub.split(":", 1)[1])).verify(base64.b64decode(sig_b64), msg)
        return True
    except Exception:  # noqa: BLE001
        return False


def token_sha(tok: str) -> str:
    return hashlib.sha256((tok or "").encode()).hexdigest()


# ---------------------------------------------------------------- actuators: tokens and keys
class Actuators:
    def __init__(self, act):
        self.act, self.db, self.audit = act, act.db, act.audit
        self._cache: Dict[str, Tuple[float, Optional[dict]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def get(self, agent_id: int) -> Optional[dict]:
        r = self.db._exec("SELECT * FROM act_actuators WHERE agent_id=?", (int(agent_id),))
        return dict(r[0]) if r else None

    def for_host(self, host: str) -> Optional[dict]:
        r = self.db._exec("SELECT * FROM act_actuators WHERE host=? AND token_sha256 NOT LIKE 'revoked:%' ORDER BY agent_id DESC LIMIT 1", (host,))
        return dict(r[0]) if r else None

    def by_token(self, tok: str) -> Optional[dict]:
        """The actuator record of a `wa_` token (5 s cache: polls are answered without a database hit)."""
        if not tok or not tok.startswith("wa_"):
            return None
        sha = token_sha(tok)
        hit = self._cache.get(sha)
        if hit and time.monotonic() - hit[0] < 5:
            return hit[1]
        r = self.db._exec("SELECT * FROM act_actuators WHERE token_sha256=?", (sha,))
        rec = dict(r[0]) if r else None
        self._cache[sha] = (time.monotonic(), rec)
        return rec

    def list(self, hosts: Optional[List[str]] = None) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_actuators ORDER BY host")
        out = []
        for r in rows:
            if hosts is not None and r["host"] not in hosts:
                continue
            r = {k: v for k, v in r.items() if k != "token_sha256"}
            for k in ("templates", "policy"):
                try:
                    r[k] = json.loads(r.get(k) or ("[]" if k == "templates" else "{}"))
                except ValueError:
                    pass
            r["revoked"] = False
            out.append(r)
        return out

    def install_line(self, token: str, criticality: str = "normal", base_url: str = "https://<edge>:8600") -> str:
        keys = self.act.keys
        pub = keys.public() if keys.custody() != "none" else {}
        return (f"sudo ./agent-install.sh --url {base_url} --actions --actions-token {token} "
                f"--actions-archive <release mirror>/watchover-actuator-0.7.0.tar.gz --actions-sha256 <release sha256> "
                f"--actions-packs linux-diag --dispatch-pub \"{pub.get('dispatch', '<keyd not ready>')}\" "
                f"--anchor-pub \"{pub.get('anchor', '<keyd not ready>')}\" --criticality {criticality}")

    def issue(self, agent_id: int, host: str, by: str, criticality: str = "normal", base_url: str = "") -> dict:
        """A new actuator token for the agent record of `host` (the previous one, if any, stops working at once)."""
        tok = "wa_" + secrets.token_urlsafe(32)
        with self.db.tx():
            if self.get(agent_id):
                self.db._exec("UPDATE act_actuators SET host=?, token_sha256=? WHERE agent_id=?", (host, token_sha(tok), int(agent_id)))
            else:
                self.db._exec("INSERT INTO act_actuators (agent_id, host, token_sha256, key_status, next_seq) VALUES (?,?,?,?,?)",
                              (int(agent_id), host, token_sha(tok), "none", 1))
            self.audit.write("actuator.token_issued", object_kind="actuator", object_id=agent_id, actor=by,
                             actor_kind="user" if "@" in by else "system", payload={"host": host, "token_sha256": token_sha(tok)[:16],
                                                                                    "criticality": criticality})
        self.invalidate()
        return {"agent_id": int(agent_id), "host": host, "token": tok,
                "install": self.install_line(tok, criticality, base_url or "https://<edge>:8600")}

    def rotate_token(self, agent_id: int, by: str, reason: str = "") -> dict:
        rec = self.get(agent_id)
        if not rec:
            raise KeyError(agent_id)
        tok = "wa_" + secrets.token_urlsafe(32)
        with self.db.tx():
            self.db._exec("UPDATE act_actuators SET token_sha256=? WHERE agent_id=?", (token_sha(tok), int(agent_id)))
            self.audit.write("actuator.token_rotated", object_kind="actuator", object_id=agent_id, actor=by,
                             actor_kind="user" if "@" in by else "system", payload={"host": rec["host"], "reason": reason[:200]})
        self.invalidate()
        return {"agent_id": int(agent_id), "host": rec["host"], "token": tok, "install": self.install_line(tok)}

    def confirm_key(self, agent_id: int, fp: str, by: str) -> dict:
        rec = self.get(agent_id)
        if not rec or not rec.get("pubkey"):
            raise ChannelError(409, "no key reported yet: start the actuator first (hello)")
        if (fp or "").strip() != rec.get("fingerprint"):
            raise ChannelError(409, "fingerprint does not match the key the actuator reported")
        with self.db.tx():
            self.db._exec("UPDATE act_actuators SET key_status='confirmed', confirmed_by=?, confirmed_at=? WHERE agent_id=?",
                          (self.audit.actor(by, "user"), now(), int(agent_id)))
            self.audit.write("key.confirmed", object_kind="actuator", object_id=agent_id, actor=by, actor_kind="user",
                             payload={"host": rec["host"], "fingerprint": rec["fingerprint"]})
        self.invalidate()
        return self.get(agent_id)

    def revoke_key(self, agent_id: int, by: str, reason: str = "") -> dict:
        rec = self.get(agent_id)
        if not rec:
            raise KeyError(agent_id)
        with self.db.tx():
            self.db._exec("UPDATE act_actuators SET key_status='revoked', pubkey='', fingerprint='' WHERE agent_id=?", (int(agent_id),))
            self.audit.write("key.revoked", object_kind="actuator", object_id=agent_id, actor=by,
                             actor_kind="user" if "@" in by else "system", payload={"host": rec["host"], "reason": reason[:200]})
        self.invalidate()
        return self.get(agent_id)


# ---------------------------------------------------------------- jobs
class Jobs:
    def __init__(self, act):
        self.act, self.db, self.audit = act, act.db, act.audit
        self.actuators = Actuators(act)
        self.pending: Dict[int, int] = {}            # agent_id -> queued jobs (polls are answered from here, no DB hit)
        self.cancels: Dict[int, set] = {}            # agent_id -> job ids to cancel on the next poll
        self.last_poll: Dict[int, float] = {}         # presence in memory (§2.3)
        self._lock = threading.Lock()
        self.rebuild()

    def rebuild(self) -> None:
        with self._lock:
            self.pending = {}
            try:
                for r in self.db._exec("SELECT agent_id, COUNT(*) AS n FROM act_jobs WHERE state='queued' GROUP BY agent_id"):
                    self.pending[int(r["agent_id"])] = int(r["n"])
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- dispatch (the engine calls this; WP8 / WP12)
    def dispatch(self, *, host: str, template_id: str, template_sha256: str, params: Dict[str, Any], mode: str, step_id: Optional[int] = None,
                 run_id: Optional[int] = None, case_id: Optional[int] = None, step_key: str = "", job_hash: str = "",
                 approvals: Optional[List[dict]] = None, timeout_s: int = 120, max_output_kb: int = 64, team_id: Optional[int] = None,
                 by: str = "system") -> dict:
        """Queue one job for the host's actuator. Act modes take the host lock in this same transaction (R9)."""
        if mode not in WINDOW:
            raise ValueError(f"unknown job mode {mode!r}")
        rec = self.actuators.for_host(host)
        if not rec:
            raise ChannelError(409, f"no actuator for {host}")
        k = self.act.kill.killed(team_id=team_id, host=host, template=template_id)
        if k:                                                        # I7: kill checked at dispatch
            raise ChannelError(409, f"stopped ({k['scope']})")
        nb = datetime.now(timezone.utc)
        ex = nb + timedelta(seconds=WINDOW[mode])
        job_id = f"J-{nb.strftime('%Y%m%d')}-{secrets.token_hex(4)}"
        nonce = secrets.token_urlsafe(12)
        with self.db.tx():
            if mode in ACT_MODES:
                held = self.db._exec("SELECT run_id, step_id FROM act_host_locks WHERE host=?" + (" FOR UPDATE" if self.db.pg else ""), (host,))
                if held and held[0]["step_id"] != step_id:
                    raise HostLocked(host, held[0]["run_id"])
                if not held:
                    try:
                        self.db._exec("INSERT INTO act_host_locks (host, run_id, step_id, since) VALUES (?,?,?,?)", (host, run_id, step_id, now()))
                    except Exception as e:  # noqa: BLE001 - a concurrent dispatch won the lock
                        raise HostLocked(host, run_id) from e
            meta = {"case_id": case_id, "run_id": run_id, "step_key": step_key, "template_id": template_id, "template_sha256": template_sha256,
                    "params": params, "timeout_s": int(timeout_s), "max_output_kb": int(max_output_kb), "job_hash": job_hash,
                    "approvals": approvals or [], "team_id": team_id}
            seq_a = self.audit.write("job.queued", object_kind="job", object_id=job_id, actor=by, actor_kind="user" if "@" in by else "system",
                                     team_id=team_id, payload={"host": host, "mode": mode, "template_id": template_id, "job_hash": job_hash,
                                                               "step_id": step_id})
            meta["audit_seq"] = seq_a
            self.db._exec("INSERT INTO act_jobs (job_id, step_id, agent_id, host, mode, executor, envelope, nonce, not_before, expires_at, state) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (job_id, step_id, int(rec["agent_id"]), host, mode, "agent", json.dumps(meta, sort_keys=True, default=str), nonce,
                           _ts(nb), _ts(ex), "queued"))
            if step_id is not None:
                self.db._exec("UPDATE act_steps SET status='dispatched', job_id=?, started_at=? WHERE id=?", (job_id, now(), step_id))
        with self._lock:
            self.pending[int(rec["agent_id"])] = self.pending.get(int(rec["agent_id"]), 0) + 1
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        r = self.db._exec("SELECT * FROM act_jobs WHERE job_id=?", (job_id,))
        if not r:
            raise KeyError(job_id)
        return dict(r[0])

    def cancel(self, job_id: str, by: str = "system") -> bool:
        j = self.get(job_id)
        if j["state"] in ("done", "failed", "rejected", "expired", "cancelled", "unknown"):
            return False
        with self.db.tx():
            if j["state"] == "queued":
                self.db._exec("UPDATE act_jobs SET state='cancelled', finished_at=? WHERE job_id=?", (now(), job_id))
                self._release(j)
            self.audit.write("job.cancel", object_kind="job", object_id=job_id, actor=by, actor_kind="user" if "@" in by else "system",
                             payload={"state": j["state"]})
        with self._lock:
            if j["state"] == "queued":
                self.pending[j["agent_id"]] = max(0, self.pending.get(j["agent_id"], 1) - 1)
            else:
                self.cancels.setdefault(int(j["agent_id"]), set()).add(job_id)
        return True

    def _release(self, j: dict) -> None:
        if j.get("mode") in ACT_MODES:
            self.db._exec("DELETE FROM act_host_locks WHERE host=? AND (step_id=? OR step_id IS NULL)", (j["host"], j.get("step_id")))

    def sweep(self, at: Optional[datetime] = None) -> Dict[str, int]:
        """Queued past expires_at → expired (lock released); leased / running past expires_at + timeout_s + 60 s → unknown
        (an act job keeps the host lock: the read-only verify decides, §9.7)."""
        t = at or datetime.now(timezone.utc)
        out = {"expired": 0, "unknown": 0}
        for j in self.db._exec("SELECT * FROM act_jobs WHERE state IN ('queued','leased','running')"):
            ex = _parse(j["expires_at"])
            if ex is None:
                continue
            meta = json.loads(j["envelope"] or "{}")
            meta = meta.get("body", meta)
            if j["state"] == "queued" and t > ex:
                with self.db.tx():
                    self.db._exec("UPDATE act_jobs SET state='expired', finished_at=? WHERE job_id=? AND state='queued'", (_ts(t), j["job_id"]))
                    self._release(j)
                    self.audit.write("job.expired", object_kind="job", object_id=j["job_id"], payload={"host": j["host"]})
                with self._lock:
                    self.pending[j["agent_id"]] = max(0, self.pending.get(j["agent_id"], 1) - 1)
                out["expired"] += 1
            elif j["state"] in ("leased", "running") and t > ex + timedelta(seconds=int(meta.get("timeout_s") or 120) + 60):
                with self.db.tx():
                    self.db._exec("UPDATE act_jobs SET state='unknown' WHERE job_id=? AND state IN ('leased','running')", (j["job_id"],))
                    if j.get("step_id") is not None:
                        self.db._exec("UPDATE act_steps SET status='unknown' WHERE id=?", (j["step_id"],))
                    self.audit.write("job.unknown", object_kind="job", object_id=j["job_id"], payload={"host": j["host"], "mode": j["mode"]})
                out["unknown"] += 1
        return out

    # ---------------------------------------------------------------- HTTP entry (live.py's receiver routes /agent/v1/* here)
    def handle(self, method: str, path: str, rec: Optional[dict], headers, body: bytes) -> Tuple[int, Dict[str, str], Optional[dict]]:
        try:
            if rec is None:
                raise ChannelError(401, "actuator token required")
            if len(body or b"") > BODY_MAX:
                raise ChannelError(413, "body too large")
            p = urlsplit(path)
            route = p.path.rstrip("/")
            confirmed = rec.get("key_status") == "confirmed"
            if route == "/agent/v1/hello" and method == "POST":
                if confirmed:
                    self._check_sig(rec, method, path, headers, body)
                return 200, {}, self.hello(rec, body)
            if not confirmed:
                raise ChannelError(403, "key not confirmed: only hello is accepted")
            self._check_sig(rec, method, path, headers, body)
            if route == "/agent/v1/jobs" and method == "GET":
                return self.poll(rec, int((parse_qs(p.query).get("after") or ["0"])[0] or 0))
            m = re.fullmatch(r"/agent/v1/jobs/([\w.-]{1,64})/(start|result)", route)
            if m and method == "POST":
                if m.group(2) == "start":
                    return 200, {}, self.start(rec, m.group(1))
                return 200, {}, self.result(rec, m.group(1), body)
            raise ChannelError(404, "not found")
        except ChannelError as e:
            return e.status, {}, e.body()

    def _check_sig(self, rec: dict, method: str, path: str, headers, body: bytes) -> None:
        """Signed requests after confirmation (§9.3.2): X-WO-Sig <kid>:<sig>, X-WO-Ts within ± 120 s."""
        sig = (headers.get("X-WO-Sig") or "") if headers is not None else ""
        ts = (headers.get("X-WO-Ts") or "") if headers is not None else ""
        if not sig or not ts:
            raise ChannelError(401, "signed request required")
        when = _parse(ts)
        if when is None or abs((datetime.now(timezone.utc) - when).total_seconds()) > SKEW_S:
            raise ChannelError(401, "stale or future request (clock skew > 120 s)")
        _kid, _, s = sig.partition(":")
        msg = f"wo-req-v1\n{method}\n{path}\n{ts}\n{hashlib.sha256(body or b'').hexdigest()}".encode()
        if not ed25519_verify(rec.get("pubkey") or "", s, msg):
            self._alarm(rec, "bad_sig", "request signature")
            raise ChannelError(401, "bad request signature")

    # ---------------------------------------------------------------- hello
    def hello(self, rec: dict, raw: bytes) -> dict:
        try:
            env = json.loads(raw or b"{}")
            b = env["body"]
        except (ValueError, KeyError, TypeError):
            raise ChannelError(400, "bad hello")
        pub = str(b.get("pubkey") or "")
        if not pub.startswith("ed25519:") or not ed25519_verify(pub, str(env.get("sig") or ""), b"wo-hello-v1\n" + canonical(b).encode()):
            raise ChannelError(401, "hello not signed with the reported key")
        agent_id = int(rec["agent_id"])
        if str(b.get("host") or "") != rec["host"]:
            self._alarm(rec, "wrong_host", f"hello for {b.get('host')!r}")
            raise ChannelError(403, "this token belongs to another host")
        fp = fingerprint(pub)
        status = rec.get("key_status") or "none"
        if rec.get("pubkey") and rec["pubkey"] != pub:
            if status == "confirmed":                               # a confirmed key changed: alarm, back to unconfirmed
                self.audit.write("actuator.key_changed", object_kind="actuator", object_id=agent_id,
                                 payload={"host": rec["host"], "old": rec.get("fingerprint"), "new": fp})
            status = "unconfirmed"
        elif not rec.get("pubkey"):
            status = "unconfirmed"
        clock = _parse(b.get("clock"))
        skew = int((clock - datetime.now(timezone.utc)).total_seconds()) if clock else 9999
        ledger = b.get("ledger") or {}
        tmpl = b.get("templates") or []
        pol = b.get("policy") or {}
        with self.db.tx():
            self.db._exec("UPDATE act_actuators SET os=?, version=?, pubkey=?, fingerprint=?, key_status=?, caps_sha256=?, templates=?, policy=?, "
                          "freeze_sha256=?, cosign_keys=?, insecure_install=?, allowlist_ok=?, halt=?, ledger_n=?, ledger_head=?, last_seq=?, "
                          "clock_skew_s=?, last_hello=? WHERE agent_id=?",
                          (str(b.get("os") or "")[:20], str(b.get("actuator") or "")[:20], pub, fp, status, str(b.get("caps_sha256") or "")[:64],
                           json.dumps(tmpl)[:20000], json.dumps(pol)[:4000], str(b.get("freeze_sha256") or "")[:64],
                           int(pol.get("cosign_keys") or 0), int(bool(b.get("insecure_install"))), int(bool(b.get("allowlist_ok"))),
                           int(bool(b.get("halt"))), int(ledger.get("n") or 0), str(ledger.get("head") or "")[:128], int(ledger.get("last_seq") or 0),
                           skew, now(), agent_id))
            att = b.get("anchor_attest") or {}
            if att.get("seq") and att.get("anchor_sig_ok"):
                self.act.anchors.attest(agent_id, int(att["seq"]), str(att.get("mac") or ""), str(ledger.get("head") or ""), str(env.get("sig") or ""))
        self.actuators.invalidate()
        res = {"key_status": status, "fingerprint": fp, "poll_s": POLL_S, "halt": bool(self._server_halt(rec)), "clock_skew_s": skew}
        if abs(skew) > SKEW_S:
            res["clock_error"] = "clock skew above 120 s: no job until the host clock is fixed"
        a = self.act.anchors.latest()
        if a:
            res["anchor"] = {"seq": a["seq"], "mac": a["mac"], "ts": a["ts"], "sig": a["sig"]}
        rot = getattr(self.act, "_rotation", None)
        if rot:
            res["dispatch_rotate"] = {"new": rot.get("new"), "sig": rot.get("sig")}
        res.update(self._ledger_check(rec, ledger, b.get("ledger_lines") or []))
        return res

    def _server_halt(self, rec: dict) -> Optional[dict]:
        from . import limits
        if limits.panic():
            return {"scope": "global"}
        return self.act.kill.killed(host=rec["host"])

    def _ledger_check(self, rec: dict, ledger: dict, lines: List[dict]) -> dict:
        """(n, head) must be accountable: lines the server never issued → "Ajan defteri ile sunucu kaydı uyuşmuyor"."""
        if lines:
            issued = {r["envelope_sha256"] for r in self.db._exec("SELECT envelope_sha256 FROM act_jobs WHERE agent_id=? AND envelope_sha256<>''",
                                                                   (int(rec["agent_id"]),))}
            bad = [ln for ln in lines if ln.get("envelope_sha256") and ln["envelope_sha256"] not in issued]
            if bad:
                self.ledger_mismatch(rec, f"{len(bad)} ledger line(s) for jobs the server never issued")
                return {"ledger_mismatch": True}
            return {}
        known = self.db._exec("SELECT ledger_n, ledger_head FROM act_jobs WHERE agent_id=? AND ledger_n IS NOT NULL ORDER BY id DESC LIMIT 1",
                              (int(rec["agent_id"]),))
        if known and ledger and int(ledger.get("n") or 0) != int(known[0]["ledger_n"] or 0):
            return {"ledger_from": int(known[0]["ledger_n"] or 0)}
        return {}

    def ledger_mismatch(self, rec: dict, why: str) -> None:
        """Critical: host stop (kill scope host) and token rotation (§9.3.2, §9.3.6)."""
        self.audit.write("ledger.mismatch", object_kind="actuator", object_id=rec["agent_id"], payload={"host": rec["host"], "why": why[:200]})
        self.act.kill.stop("host", rec["host"], by="system", reason=f"Ajan defteri ile sunucu kaydı uyuşmuyor: {why}"[:300])
        self.actuators.rotate_token(int(rec["agent_id"]), by="system", reason="ledger mismatch")

    def _alarm(self, rec: dict, code: str, why: str) -> None:
        """bad_sig / wrong_host / seq_replay: security alert, host stop, token rotation."""
        self.audit.write("actuator.alarm", object_kind="actuator", object_id=rec["agent_id"], payload={"host": rec["host"], "code": code,
                                                                                                       "why": why[:200]})
        try:
            if not self.act.kill.killed(host=rec["host"]):
                self.act.kill.stop("host", rec["host"], by="system", reason=f"Güvenlik alarmı ({code}): {why}"[:300])
            self.actuators.rotate_token(int(rec["agent_id"]), by="system", reason=code)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- poll and lease
    def poll(self, rec: dict, after: int = 0) -> Tuple[int, Dict[str, str], Optional[dict]]:
        aid = int(rec["agent_id"])
        self.last_poll[aid] = time.monotonic()
        with self._lock:
            n = self.pending.get(aid, 0)
            cancel = sorted(self.cancels.pop(aid, set()))
        if not n and not cancel:
            return 204, {"X-Poll-After": str(POLL_S)}, None                 # from memory: no database hit
        halt = self._server_halt(rec)
        if not cancel and (halt or abs(int(rec.get("clock_skew_s") or 0)) > SKEW_S):
            return 204, {"X-Poll-After": str(POLL_S)}, None
        jobs = [] if halt else self._lease(rec)
        return 200, {}, {"jobs": jobs, "cancel": cancel, "halt": bool(halt), "poll_s": FAST_POLL_S if jobs else POLL_S}

    def _lease(self, rec: dict) -> List[dict]:
        aid = int(rec["agent_id"])
        out: List[dict] = []
        counts = {"act": 0, "diag": 0}
        for j in self.db._exec("SELECT * FROM act_jobs WHERE agent_id=? AND state='queued' ORDER BY id", (aid,)):
            kind = "act" if j["mode"] in ACT_MODES else "diag"
            if counts[kind] >= LEASE_MAX[kind]:
                continue
            ex = _parse(j["expires_at"])
            if ex is not None and datetime.now(timezone.utc) > ex:
                continue                                                    # sweep() expires it
            meta = json.loads(j["envelope"] or "{}")
            if self.act.kill.killed(team_id=meta.get("team_id"), host=j["host"], template=meta.get("template_id", "")):
                continue
            cur = self.actuators.get(aid) or rec
            seq = max(int(cur.get("next_seq") or 1), int(cur.get("last_seq") or 0) + 1)
            body = {"v": 1, "job_id": j["job_id"], "seq": seq, "agent_id": aid, "host": j["host"], "case_id": meta.get("case_id"),
                    "run_id": meta.get("run_id"), "step_key": meta.get("step_key", ""), "mode": j["mode"], "template_id": meta.get("template_id", ""),
                    "template_sha256": meta.get("template_sha256", ""), "params": meta.get("params") or {}, "timeout_s": int(meta.get("timeout_s") or 120),
                    "max_output_kb": int(meta.get("max_output_kb") or 64), "not_before": _wire(_parse(j["not_before"])),
                    "expires_at": _wire(ex), "nonce": j["nonce"], "job_hash": meta.get("job_hash", ""), "approvals": meta.get("approvals") or [],
                    "audit_seq": meta.get("audit_seq")}
            try:
                s = self.act.keys.sign_job(body)                            # keyd refuses a seq not above the last it signed (R6)
            except (SigningUnavailable, KeyRefused):
                break                                                       # fail closed: nothing leased without keyd
            envelope = {"body": body, "kid": s["kid"], "sig": s["sig"]}
            esha = sha256_hex(canonical(envelope))
            with self.db.tx():
                upd = self.db._exec("UPDATE act_jobs SET state='leased', seq=?, envelope=?, envelope_sha256=?, leased_at=? WHERE job_id=? AND state='queued'"
                                    + (" RETURNING job_id" if self.db.pg else ""), (seq, json.dumps(envelope, sort_keys=True), esha, now(), j["job_id"]))
                self.db._exec("UPDATE act_actuators SET next_seq=?, last_poll=? WHERE agent_id=?", (seq + 1, now(), aid))
                self.audit.write("job.issued", object_kind="job", object_id=j["job_id"], team_id=meta.get("team_id"),
                                 payload={"host": j["host"], "seq": seq, "envelope_sha256": esha, "mode": j["mode"]})
            with self._lock:
                self.pending[aid] = max(0, self.pending.get(aid, 1) - 1)
            counts[kind] += 1
            out.append(envelope)
        out.sort(key=lambda e: e["body"]["seq"])
        return out

    # ---------------------------------------------------------------- start and result
    def _job_of(self, rec: dict, job_id: str) -> dict:
        r = self.db._exec("SELECT * FROM act_jobs WHERE job_id=? AND agent_id=?", (job_id, int(rec["agent_id"])))
        if not r:
            raise ChannelError(404, "unknown job")
        return dict(r[0])

    def start(self, rec: dict, job_id: str) -> dict:
        j = self._job_of(rec, job_id)
        meta = json.loads(j["envelope"] or "{}").get("body", {})
        if j["state"] != "leased" or self.act.kill.killed(team_id=None, host=j["host"], template=meta.get("template_id", "")):
            return {"ok": False, "cancel": True}
        with self.db.tx():
            self.db._exec("UPDATE act_jobs SET state='running', started_at=? WHERE job_id=?", (now(), job_id))
            if j.get("step_id") is not None:
                self.db._exec("UPDATE act_steps SET status='running' WHERE id=?", (j["step_id"],))
        return {"ok": True}

    def result(self, rec: dict, job_id: str, raw: bytes) -> dict:
        j = self._job_of(rec, job_id)
        if j.get("result"):                                                 # one result per job_id (re-sent results are acknowledged)
            return {"ok": True, "duplicate": True}
        try:
            env = json.loads(raw or b"{}")
            b = env["body"]
        except (ValueError, KeyError, TypeError):
            raise ChannelError(400, "bad result")
        sig_ok = ed25519_verify(rec.get("pubkey") or "", str(env.get("sig") or ""), b"wo-result-v1\n" + canonical(b).encode())
        if b.get("job_id") != job_id or b.get("nonce") != j["nonce"] or (j["seq"] is not None and int(b.get("seq") or -1) != int(j["seq"])):
            raise ChannelError(409, "result does not match the job")
        if j["state"] not in ("leased", "running", "unknown"):
            raise ChannelError(409, f"job is {j['state']}")
        state = RESULT_STATES.get(str(b.get("status") or ""), "failed")
        fields = privacy.scrub_fields(b.get("fields") or {})
        tail = privacy.scrub_strict(str(b.get("display_tail") or ""))[:64 * 1024]
        stored = {**{k: v for k, v in b.items() if k not in ("fields", "display_tail", "stderr_tail")}, "fields": fields,
                  "display_tail": tail, "stderr_tail": privacy.scrub_strict(str(b.get("stderr_tail") or ""))[:4096]}
        rsha = sha256_hex(canonical(stored))
        led = b.get("ledger") or {}
        with self.db.tx():
            self.db._exec("UPDATE act_jobs SET state=?, reject_code=?, finished_at=?, result=?, result_sha256=?, result_sig_ok=?, ledger_n=?, "
                          "ledger_head=? WHERE job_id=?",
                          (state, str(b.get("reject_code") or "")[:40], now(), json.dumps(stored, sort_keys=True, default=str), rsha, int(sig_ok),
                           int(led.get("n") or 0), str(led.get("head") or "")[:128], job_id))
            self.db._exec("UPDATE act_actuators SET ledger_n=?, ledger_head=?, last_seq=? WHERE agent_id=?",
                          (int(led.get("n") or 0), str(led.get("head") or "")[:128], int(led.get("last_seq") or j["seq"] or 0), int(rec["agent_id"])))
            if j["mode"] in ACT_MODES or state != "done":
                self._release(j)
            if j.get("step_id") is not None:
                self.db._exec("UPDATE act_steps SET status=?, exit_code=?, fields=?, display_tail=?, display_until=?, output_sha256=?, signed=?, "
                              "ended_at=? WHERE id=?",
                              ({"done": "succeeded", "failed": "failed", "rejected": "rejected_by_agent", "cancelled": "cancelled"}[state],
                               b.get("exit_code"), json.dumps(fields, default=str), tail,
                               (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds"), str(b.get("output_sha256") or ""),
                               int(sig_ok), now(), j["step_id"]))
            self.audit.write("job.result", object_kind="job", object_id=job_id, payload={"host": j["host"], "state": state, "sig_ok": sig_ok,
                                                                                         "result_sha256": rsha, "output_sha256": b.get("output_sha256", ""),
                                                                                         "reject_code": b.get("reject_code", "")})
            meta = json.loads(j["envelope"] or "{}").get("body", {})
            if state == "done" and meta.get("case_id") and j["mode"] in ("diagnose", "verify"):
                self.act.cases._add_evidence(int(meta["case_id"]), {"kind": "diag", "host": j["host"], "agent_id": rec["agent_id"],
                                                                    "template_id": meta.get("template_id"), "run_id": meta.get("run_id"),
                                                                    "signed": sig_ok, "fields": fields, "ts": b.get("ended"),
                                                                    "privacy": b.get("privacy") or "", "captures": fields,
                                                                    "ref": {"job_id": job_id}})
        if str(b.get("reject_code") or "") in SECURITY_REJECTS:
            self._alarm(rec, str(b["reject_code"]), f"job {job_id}")
        return {"ok": True}


def sign_request(key, method: str, path: str, body: bytes, ts: Optional[str] = None, kid: str = "agent") -> Dict[str, str]:
    """Headers of a signed /agent/v1/ request (the actuator side; used by tests and the fake actuator)."""
    ts = ts or str(int(time.time()))
    msg = f"wo-req-v1\n{method}\n{path}\n{ts}\n{hashlib.sha256(body or b'').hexdigest()}".encode()
    return {"X-WO-Sig": f"{kid}:{base64.b64encode(key.sign(msg)).decode()}", "X-WO-Ts": ts}
