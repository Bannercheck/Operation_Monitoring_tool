"""A fake actuator: the host side of the job channel in a few lines, for tests and demos (docs/design/action-agent.md §9.3,
WP11a). It holds its own Ed25519 key, says hello, polls, starts and answers jobs with canned structured results. The real
actuator (agent.py --actions-only, with its allowlist, validators and ledger) is WP11b; this one checks only what the server
must get right: the dispatch signature, the host, the seq order and the time window.

    fa = FakeActuator(token, "web-prod-07", direct(jobs))      # or http("http://127.0.0.1:8601")
    fa.hello(); …confirm the key…; fa.run_once(results={"linux.disk.usage": {"mount": "/var", "use_pct": 91}})"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .jobs import sign_request
from .keyd import fingerprint
from .types import canonical

Transport = Callable[[str, str, Dict[str, str], bytes], Tuple[int, Dict[str, str], Optional[dict]]]


def direct(jobs) -> Transport:
    """Call Jobs.handle in-process (the token is resolved like the receiver does)."""
    def go(method, path, headers, body):
        tok = (headers.get("Authorization") or "")[7:]
        return jobs.handle(method, path, jobs.actuators.by_token(tok), headers, body)
    return go


def http(base: str) -> Transport:
    def go(method, path, headers, body):
        req = urllib.request.Request(base.rstrip("/") + path, data=body if method == "POST" else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - test / demo transport
                raw = r.read()
                return r.status, dict(r.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, dict(e.headers), json.loads(raw) if raw else None
    return go


class FakeActuator:
    def __init__(self, token: str, host: str, transport: Transport, dispatch_pub: str = "", agent_id: Optional[int] = None, key=None):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self.token, self.host, self.t, self.dispatch_pub, self.agent_id = token, host, transport, dispatch_pub, agent_id
        self.key = key or Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.pub = "ed25519:" + base64.b64encode(raw).decode()
        self.fingerprint = fingerprint(self.pub)
        self.last_seq = 0
        self.ledger: List[dict] = []
        self.signed = False                     # sign every request (after the key was confirmed)
        self.rejected: List[Tuple[str, str]] = []

    def _sig(self, purpose: str, body: dict) -> str:
        return base64.b64encode(self.key.sign(f"wo-{purpose}-v1\n".encode() + canonical(body).encode())).decode()

    def request(self, method: str, path: str, body: Optional[dict] = None, sign: Optional[bool] = None, ts: Optional[str] = None):
        raw = json.dumps(body).encode() if body is not None else b""
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if self.signed if sign is None else sign:
            h.update(sign_request(self.key, method, path, raw, ts=ts))
        return self.t(method, path, h, raw)

    def ledger_head(self) -> str:
        return self.ledger[-1]["h"] if self.ledger else ""

    def hello(self, templates: Optional[List[dict]] = None, clock: Optional[str] = None, extra: Optional[dict] = None):
        b = {"v": 1, "host": self.host, "actuator": "0.7.0-fake", "os": "linux", "pubkey": self.pub,
             "clock": clock or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "templates": templates if templates is not None else [{"id": "linux.disk.usage", "rev": 1, "sha256": "", "risk": "R0", "kind": "diag"}],
             "policy": {"enabled": True, "max_risk": "R0", "deny": [], "cosign_keys": 0}, "allowlist_ok": True, "halt": False,
             "ledger": {"n": len(self.ledger), "head": self.ledger_head(), "last_seq": self.last_seq}, "running": []}
        b.update(extra or {})
        return self.request("POST", "/agent/v1/hello", {"body": b, "kid": f"agent-{self.agent_id}", "sig": self._sig("hello", b)})

    def check(self, env: dict) -> str:
        """The server-side relevant actuator checks (§9.3.4 #2–#4): a reject code or ''."""
        b = env["body"]
        if self.dispatch_pub:
            from .jobs import ed25519_verify
            if not ed25519_verify(self.dispatch_pub, env.get("sig", ""), b"wo-job-v1\n" + canonical(b).encode()):
                return "bad_sig"
        if b["host"] != self.host:
            return "wrong_host"
        now = datetime.now(timezone.utc)
        if now > datetime.fromisoformat(b["expires_at"].replace("Z", "+00:00")):
            return "expired"
        if int(b["seq"]) <= self.last_seq:
            return "seq_replay"
        return ""

    def run_once(self, results: Optional[Dict[str, Dict[str, Any]]] = None, after: Optional[int] = None) -> List[dict]:
        """One poll: every leased job is checked, started and answered (status done with the canned fields)."""
        code, hdrs, body = self.request("GET", f"/agent/v1/jobs?after={self.last_seq if after is None else after}")
        if code != 200:
            return []
        out = []
        for env in body.get("jobs") or []:
            b = env["body"]
            rc = self.check(env)
            if not rc:
                self.last_seq = int(b["seq"])
                self.request("POST", f"/agent/v1/jobs/{b['job_id']}/start", {})
            fields = (results or {}).get(b["template_id"], {})
            line = {"n": len(self.ledger) + 1, "job_id": b["job_id"], "seq": b["seq"],
                    "envelope_sha256": hashlib.sha256(canonical(env).encode()).hexdigest(), "status": "rejected" if rc else "done",
                    "prev": self.ledger_head()}
            line["h"] = hashlib.sha256((line["prev"] + canonical({k: v for k, v in line.items() if k != "h"})).encode()).hexdigest()
            self.ledger.append(line)
            rb = {"v": 1, "job_id": b["job_id"], "nonce": b["nonce"], "seq": b["seq"], "host": self.host,
                  "status": "rejected" if rc else "done", "reject_code": rc, "exit_code": None if rc else 0,
                  "template_sha256": b.get("template_sha256", ""), "privacy": "structured", "fields": {} if rc else fields,
                  "display_tail": "", "stderr_tail": "", "truncated": False, "output_sha256": hashlib.sha256(json.dumps(fields).encode()).hexdigest(),
                  "ledger": {"n": len(self.ledger), "head": self.ledger_head(), "last_seq": self.last_seq}}
            if rc:
                self.rejected.append((b["job_id"], rc))
            res = self.request("POST", f"/agent/v1/jobs/{b['job_id']}/result", {"body": rb, "kid": f"agent-{self.agent_id}", "sig": self._sig("result", rb)})
            out.append({"job_id": b["job_id"], "seq": b["seq"], "reject": rc, "answer": res})
        return out
