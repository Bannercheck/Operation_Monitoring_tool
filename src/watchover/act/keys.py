"""KeyClient: the only way the API process gets signatures, audit MACs and actor pseudonyms (docs/design/action-agent.md
§9.3.1, §10.7). Custody modes:

* "keyd"      the separate `watchover-keyd` process holds the keys outside /data (WP2). Until WP2 lands, or while keyd is
              unreachable, signing is unavailable (503 act_signing_unavailable) and audit rows carry the placeholder MAC.
* "embedded"  demo and tests only (WATCHOVER_ACT_DEMO=1): keys under WATCHOVER_HOME/act_keys, in-process; caps every act step
              at L2 (the `key_custody` cap, I17).
* "none"      no usable keys: guided work continues, nothing is signed, audit rows carry the placeholder MAC.

Placeholder MAC (WP1, C44): "p0:" + sha256(prev_mac ‖ "\\n" ‖ row digest) — an unkeyed hash chain that WP2 replaces with the keyd
HMAC chain. Embedded MAC: "e1:" + HMAC-SHA256(audit.key, same input)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

from .policy import act_demo

PURPOSES = ("job", "result", "hello", "req", "anchor", "attest", "rotate")


class SigningUnavailable(RuntimeError):
    code = "act_signing_unavailable"
    status = 503
    tr = "İmza servisi yok: sistem yürütmesi kapalı, rehberli mod sürüyor"
    en = "Signing is unavailable: system execution is disabled, guided mode continues"

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en}


def _home() -> Path:
    return Path(os.environ.get("WATCHOVER_HOME") or "data")


class KeyClient:
    def __init__(self, policy=None, key_dir: Optional[str] = None):
        self.policy = policy
        self._dir = Path(key_dir) if key_dir else None
        self._cache = {}

    # ---------------------------------------------------------------- mode
    def wanted(self) -> str:
        return (self.policy.get("act_key_custody") if self.policy is not None else "keyd") or "keyd"

    def custody(self) -> str:
        want = self.wanted()
        if want == "embedded" and act_demo():
            return "embedded"
        if want == "keyd" and os.environ.get("WATCHOVER_KEYD", "").strip():
            return "keyd"                                  # configured; the client itself arrives with WP2
        return "none"

    def available(self) -> bool:
        """Can jobs be signed right now? keyd: not before WP2."""
        return self.custody() == "embedded"

    @property
    def key_dir(self) -> Path:
        return self._dir or (_home() / "act_keys")

    # ---------------------------------------------------------------- embedded key files (demo only)
    def _key(self, name: str, size: int = 32) -> bytes:
        if name in self._cache:
            return self._cache[name]
        d = self.key_dir
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        p = d / name
        if not p.exists():
            p.write_bytes(base64.b64encode(secrets.token_bytes(size)))
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        k = base64.b64decode(p.read_bytes().strip())
        self._cache[name] = k
        return k

    def _ed25519(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(self._key("dispatch.key", 32))

    # ---------------------------------------------------------------- operations
    def mac_audit(self, seq: int, prev_mac: str, digest: str) -> str:
        data = f"{prev_mac}\n{digest}".encode("utf-8")
        if self.custody() == "embedded":
            return "e1:" + hmac.new(self._key("audit.key"), data, hashlib.sha256).hexdigest()
        return "p0:" + hashlib.sha256(data).hexdigest()

    def verify_mac(self, seq: int, prev_mac: str, digest: str, mac: str) -> bool:
        data = f"{prev_mac}\n{digest}".encode("utf-8")
        if mac.startswith("e1:"):
            if not (self.key_dir / "audit.key").exists():
                return False
            want = "e1:" + hmac.new(self._key("audit.key"), data, hashlib.sha256).hexdigest()
        elif mac.startswith("p0:"):
            want = "p0:" + hashlib.sha256(data).hexdigest()
        else:
            return False                                   # keyd MACs are verified by keyd itself (WP2)
        return hmac.compare_digest(want, mac)

    def sign(self, purpose: str, body: bytes) -> str:
        """Ed25519 over "wo-<purpose>-v1\\n" + body; embedded custody only until keyd exists."""
        if purpose not in PURPOSES:
            raise ValueError(f"unknown signing purpose {purpose!r}")
        if self.custody() != "embedded":
            raise SigningUnavailable()
        sig = self._ed25519().sign(f"wo-{purpose}-v1\n".encode() + body)
        return base64.b64encode(sig).decode()

    def public_key(self) -> str:
        if self.custody() != "embedded":
            return ""
        from cryptography.hazmat.primitives import serialization
        raw = self._ed25519().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return "ed25519:" + base64.b64encode(raw).decode()

    def pseudonym(self, email: str) -> str:
        """op_<hex12> = HMAC(pseudo key, e-mail); keyd computes it from WP2 on."""
        e = (email or "").strip().lower()
        if not e:
            return ""
        if self.custody() == "embedded":
            key = self._key("pseudo.key")
        else:
            from .. import privacy
            key = privacy._key()
        return "op_" + hmac.new(key, e.encode(), hashlib.sha256).hexdigest()[:12]

    def status(self) -> dict:
        c = self.custody()
        warn = []
        if c == "embedded":
            warn.append("embedded custody: keys under WATCHOVER_HOME (demo only); every act step is capped at L2")
        if c == "none" and self.wanted() == "keyd":
            warn.append("watchover-keyd not configured (WP2): system execution disabled, guided mode only")
        fp = ""
        if c == "embedded" and (self.key_dir / "dispatch.key").exists():
            fp = hashlib.sha256(self.public_key().encode()).hexdigest()[:16]
        return {"custody": c, "wanted": self.wanted(), "available": self.available(), "key_dir": str(self.key_dir) if c == "embedded" else "",
                "dispatch_fingerprint": fp, "mac": "e1" if c == "embedded" else "p0", "warnings": warn}

    def cap(self) -> int:
        """The `key_custody` cap on act steps: embedded → L2; no usable keys → L1 (guided only); keyd (WP2) → none."""
        c = self.custody()
        return 2 if c == "embedded" else (4 if c == "keyd" and self.available() else 1)
