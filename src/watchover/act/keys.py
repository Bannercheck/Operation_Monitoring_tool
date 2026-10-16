"""KeyClient: the only way the API process gets signatures, audit MACs and actor pseudonyms (docs/design/action-agent.md
§9.3.1, §10.7). Custody modes:

* "keyd"      the separate `watchover-keyd` process (act/keyd.py) holds the keys outside /data; WATCHOVER_KEYD names it
              (`unix:/run/watchover-keyd/keyd.sock` or `tcp://watchover-keyd:8650` with WATCHOVER_KEYD_TLS = the directory of
              the client certificate). Every call is bounded (WATCHOVER_KEYD_TIMEOUT_MS, 200 ms by default); keyd unreachable
              → SigningUnavailable (503 act_signing_unavailable): no job is signed and no audit row is written.
* "embedded"  demo and tests only (WATCHOVER_ACT_DEMO=1): the same KeyStore in-process, keys under WATCHOVER_HOME/act_keys; caps
              every act step at L2 (the `key_custody` cap, I17).
* "none"      no usable keys: guided work continues, nothing is signed, audit rows carry the placeholder MAC.

MAC prefixes: "p0:" = sha256(prev_mac ‖ "\\n" ‖ row digest), an unkeyed chain used while no keys exist (keyd configured but its
keys not created yet, or custody none); "e1:" = embedded HMAC; "k1:" = keyd HMAC. When keys first appear keyd adopts the
database's head once (genesis) and from then on MACs only the next row."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .keyd import KeyStore, KeydError, fingerprint
from .policy import act_demo

PURPOSES = ("job", "result", "hello", "req", "anchor", "attest", "rotate")


class SigningUnavailable(RuntimeError):
    code = "act_signing_unavailable"
    status = 503
    tr = "İmza servisi yok: sistem yürütmesi kapalı, rehberli mod sürüyor"
    en = "Signing is unavailable: system execution is disabled, guided mode continues"

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en}


class KeyRefused(RuntimeError):
    """keyd said no (code: seq, killed, limit, envelope, window, replay, purpose, export, no_keys …)."""
    status = 409

    def __init__(self, code: str, msg: str = "", head: Optional[dict] = None):
        super().__init__(msg or code)
        self.code, self.head = code, head or {}

    def detail(self) -> dict:
        return {"code": f"act_keyd_{self.code}", "tr": f"Anahtar servisi reddetti: {self}", "en": f"keyd refused: {self}"}


def _home() -> Path:
    return Path(os.environ.get("WATCHOVER_HOME") or "data")


def p0(prev_mac: str, digest: str) -> str:
    return "p0:" + hashlib.sha256(f"{prev_mac}\n{digest}".encode("utf-8")).hexdigest()


class _Rpc:
    """One persistent connection to keyd (re-opened after an error); calls are serialised."""

    def __init__(self, endpoint: str, tls_dir: str = "", server_name: str = "watchover-keyd"):
        self.endpoint, self.tls_dir, self.server_name = endpoint, tls_dir, server_name
        self._sock = None
        self._file = None
        self._lock = threading.Lock()

    def _connect(self, timeout: float):
        ep = self.endpoint
        if ep.startswith("tcp://"):
            host, _, port = ep[6:].rpartition(":")
            s = socket.create_connection((host, int(port)), timeout=timeout)
            if self.tls_dir:
                d = Path(self.tls_dir)
                ctx = ssl.create_default_context(cafile=str(d / "ca.crt"))
                ctx.load_cert_chain(str(d / "client.crt"), str(d / "client.key"))
                s = ctx.wrap_socket(s, server_hostname=self.server_name)
        else:
            path = ep[5:] if ep.startswith("unix:") else ep
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(path)
        self._sock, self._file = s, s.makefile("rb")

    def close(self) -> None:
        for x in (self._file, self._sock):
            try:
                if x is not None:
                    x.close()
            except OSError:
                pass
        self._sock = self._file = None

    def call(self, op: str, args: Dict[str, Any], timeout: float) -> Any:
        line = (json.dumps({"op": op, "args": args}, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._connect(timeout)
                    self._sock.settimeout(timeout)
                    self._sock.sendall(line)
                    raw = self._file.readline()
                    if not raw:
                        raise OSError("keyd closed the connection")
                    break
                except (OSError, ssl.SSLError, ValueError) as e:
                    self.close()
                    if attempt or isinstance(e, socket.timeout):
                        raise SigningUnavailable(f"keyd unreachable: {e}") from e
        res = json.loads(raw)
        if not res.get("ok"):
            raise KeyRefused(res.get("code") or "error", res.get("error") or "", res.get("head"))
        return res.get("result")


class KeyClient:
    def __init__(self, policy=None, key_dir: Optional[str] = None, endpoint: Optional[str] = None):
        self.policy = policy
        self._dir = Path(key_dir) if key_dir else None
        self._endpoint = endpoint
        self._store: Optional[KeyStore] = None
        self._rpc: Optional[_Rpc] = None
        self._pseudo: Dict[str, str] = {}
        self._st = (0.0, None)                       # cached keyd status (5 s)

    # ---------------------------------------------------------------- mode
    def wanted(self) -> str:
        return (self.policy.get("act_key_custody") if self.policy is not None else "keyd") or "keyd"

    @property
    def endpoint(self) -> str:
        return (self._endpoint if self._endpoint is not None else os.environ.get("WATCHOVER_KEYD", "")).strip()

    def custody(self) -> str:
        want = self.wanted()
        if want == "embedded" and act_demo():
            return "embedded"
        if want == "keyd" and self.endpoint:
            return "keyd"
        return "none"

    @property
    def key_dir(self) -> Path:
        return self._dir or (_home() / "act_keys")

    def timeout(self, long: bool = False) -> float:
        try:
            ms = int(os.environ.get("WATCHOVER_KEYD_TIMEOUT_MS") or 200)
        except ValueError:
            ms = 200
        return max(ms, 5000) / 1000.0 if long else ms / 1000.0

    def _embedded(self) -> KeyStore:
        if self._store is None or self._store.dir != self.key_dir:
            self._store = KeyStore(self.key_dir, prefix="e1")
        if not self._store.exists():
            self._store.create()                       # demo custody: created on first use
        return self._store

    def _call(self, op: str, args: Optional[Dict[str, Any]] = None, long: bool = False) -> Any:
        c = self.custody()
        if c == "embedded":
            try:
                return self._embedded().call(op, args or {})
            except KeydError as e:
                raise KeyRefused(e.code, str(e), e.extra.get("head")) from e
        if c == "keyd":
            if self._rpc is None or self._rpc.endpoint != self.endpoint:
                self._rpc = _Rpc(self.endpoint, os.environ.get("WATCHOVER_KEYD_TLS", ""), os.environ.get("WATCHOVER_KEYD_NAME", "watchover-keyd"))
            return self._rpc.call(op, args or {}, self.timeout(long))
        raise SigningUnavailable("no key custody")

    def keyd_status(self, fresh: bool = False) -> Optional[dict]:
        """keyd's own status (cached 5 s); None when unreachable."""
        if self.custody() == "none":
            return None
        at, st = self._st
        if not fresh and time.monotonic() - at < (5 if st is not None else 1):
            return st
        try:
            st = self._call("status")
        except (SigningUnavailable, KeyRefused):
            st = None
        self._st = (time.monotonic(), st)
        return st

    def has_keys(self) -> bool:
        c = self.custody()
        if c == "embedded":
            return True
        st = self.keyd_status()
        if st is None and c == "keyd":
            raise SigningUnavailable("keyd unreachable")
        return bool(st and st.get("keys"))

    def ensure_keys(self) -> dict:
        """Create keyd's keys (when act_mode first leaves off, §15.3 step 6); idempotent. {} without custody."""
        if self.custody() == "none":
            return {}
        res = self._call("create", long=True)
        self._st = (0.0, None)
        return res

    def available(self) -> bool:
        """Can jobs be signed right now?"""
        c = self.custody()
        if c == "embedded":
            return True
        st = self.keyd_status()
        return bool(c == "keyd" and st and st.get("keys") and not st.get("killed"))

    # ---------------------------------------------------------------- audit MACs
    def mac_audit(self, seq: int, prev_mac: str, digest: str) -> str:
        if self.custody() == "none" or not self.has_keys():
            return p0(prev_mac, digest)
        return self._call("mac_audit", {"seq": int(seq), "prev_mac": prev_mac or "", "digest": digest})

    def head(self) -> Optional[dict]:
        if self.custody() == "none" or not self.has_keys():
            return None
        return self._call("head")

    def confirm(self, seq: int, mac: str) -> Optional[dict]:
        if self.custody() == "none" or not self.has_keys():
            return None
        return self._call("confirm", {"seq": int(seq), "mac": mac})

    def verify_macs(self, rows: List[list]) -> int:
        """rows [[seq, prev, digest, mac], …] all of one keyed prefix → index of the first wrong MAC, -1 when all are right."""
        if not rows:
            return -1
        pre = (rows[0][3] or "")[:3]
        want = {"embedded": "e1:", "keyd": "k1:"}.get(self.custody())
        if pre != want:
            return 0                                    # MACs of a custody we do not hold cannot be checked here
        return int(self._call("verify", {"rows": rows}, long=True)["bad"])

    def verify_mac(self, seq: int, prev_mac: str, digest: str, mac: str) -> bool:
        if (mac or "").startswith("p0:"):
            return hmac.compare_digest(p0(prev_mac, digest), mac)
        try:
            return self.verify_macs([[seq, prev_mac, digest, mac]]) == -1
        except (SigningUnavailable, KeyRefused):
            return False

    # ---------------------------------------------------------------- signatures
    def sign_job(self, envelope: Dict[str, Any]) -> dict:
        if self.custody() == "none":
            raise SigningUnavailable()
        return self._call("sign_job", {"envelope": envelope})

    def sign(self, purpose: str, body: bytes) -> str:
        """Ed25519 over "wo-<purpose>-v1\\n" + body. `job` goes through keyd's envelope rules (sign_job)."""
        if purpose not in PURPOSES:
            raise ValueError(f"unknown signing purpose {purpose!r}")
        if self.custody() == "none":
            raise SigningUnavailable()
        if purpose == "job":
            return self.sign_job(json.loads(body))["sig"]
        return self._call("sign", {"purpose": purpose, "body_b64": base64.b64encode(body).decode()})["sig"]

    def sign_anchor(self, confirm: Optional[dict] = None) -> Optional[dict]:
        if self.custody() == "none" or not self.has_keys():
            return None
        return self._call("sign_anchor", {"confirm": confirm or {}})

    def anchors(self, since_seq: int = 0) -> List[dict]:
        if self.custody() == "none" or not self.has_keys():
            return []
        return self._call("anchors", {"since_seq": int(since_seq)}, long=True)

    def public(self) -> dict:
        if self.custody() == "none" or not self.has_keys():
            return {}
        return self._call("public")

    def public_key(self) -> str:
        try:
            return self.public().get("dispatch", "")
        except (SigningUnavailable, KeyRefused):
            return ""

    def anchor_pub(self) -> str:
        try:
            return self.public().get("anchor", "")
        except (SigningUnavailable, KeyRefused):
            return ""

    def rotate_dispatch(self) -> dict:
        return self._call("rotate_dispatch", long=True)

    # ---------------------------------------------------------------- host-agent releases (agent self-update)
    def release(self) -> dict:
        """keyd's signed manifest of the wo-agent release in its own image (release.key; independent of act_mode and the act
        keys). SigningUnavailable without custody or when keyd is unreachable, KeyRefused when keyd has no valid release."""
        return self._call("release", long=True)

    def releases(self) -> List[dict]:
        return self._call("releases", long=True)

    def sign_pin(self, host: str, version: str, ttl_s: int = 7 * 86400) -> dict:
        return self._call("sign_pin", {"host": host, "version": version, "ttl_s": int(ttl_s)}, long=True)

    def export_begin(self, part: str, by: str) -> dict:
        return self._call("export_begin", {"part": part, "by": by}, long=True)

    def export_finish(self, token: str, part: str, by: str) -> dict:
        return self._call("export_finish", {"token": token, "part": part, "by": by}, long=True)

    def pseudonym(self, email: str) -> str:
        """op_<hex12> = HMAC(pseudo key, e-mail), computed by keyd; before keys exist the privacy key stands in."""
        e = (email or "").strip().lower()
        if not e:
            return ""
        if e in self._pseudo:
            return self._pseudo[e]
        if self.custody() != "none" and self.has_keys():
            p = self._call("pseudo", {"email": e})
        else:
            from .. import privacy
            return "op_" + hmac.new(privacy._key(), e.encode(), hashlib.sha256).hexdigest()[:12]   # not cached: keys may appear later
        self._pseudo[e] = p
        return p

    # ---------------------------------------------------------------- status
    def shared_mount(self, st: Optional[dict] = None) -> List[str]:
        """I17 / act doctor: the key directory must not share a file system with /data (WATCHOVER_HOME) or the database."""
        out: List[str] = []
        kdir = Path(st["dir"]) if st and st.get("dir") else (self.key_dir if self.custody() == "embedded" else None)
        if kdir is None:
            return out
        dev = st.get("st_dev") if st else None
        home = _home().resolve()
        kres = kdir.resolve()
        if kres == home or home in kres.parents:
            return [f"key directory {kdir} is inside WATCHOVER_HOME ({home}): it would be in /data backups"]
        try:
            if dev is None:
                dev = os.stat(kdir).st_dev
            if home.exists() and os.stat(home).st_dev == dev:
                out.append(f"key directory {kdir} shares a file system with WATCHOVER_HOME ({home})")
        except OSError:
            pass
        return out

    def status(self) -> dict:
        c = self.custody()
        warn: List[str] = []
        st = self.keyd_status() if c != "none" else None
        reachable = c == "embedded" or st is not None
        if c == "embedded":
            warn.append("embedded custody: keys under WATCHOVER_HOME (demo only); every act step is capped at L2")
        if c == "none" and self.wanted() == "keyd":
            warn.append("watchover-keyd not configured (WATCHOVER_KEYD): system execution disabled, guided mode only")
        if c == "keyd" and st is None:
            warn.append("watchover-keyd unreachable: no signatures and no audit writes until it answers")
        if c == "keyd" and st is not None and not st.get("keys"):
            warn.append("watchover-keyd has no keys yet: they are created when act_mode first leaves off")
        if st and st.get("killed"):
            warn.append("keyd KILL file present: keyd signs no job")
        if c == "keyd" and st is not None:
            warn += self.shared_mount(st)
        has = bool(st and st.get("keys"))
        mac = {"embedded": "e1", "keyd": "k1" if has else "p0"}.get(c, "p0")
        return {"custody": c, "wanted": self.wanted(), "available": self.available() if reachable else False, "reachable": reachable,
                "keys": has or c == "embedded", "key_dir": str(self.key_dir) if c == "embedded" else (st or {}).get("dir", ""),
                "dispatch_fingerprint": (st or {}).get("dispatch_fingerprint", ""), "anchor_fingerprint": (st or {}).get("anchor_fingerprint", ""),
                "dispatch_kid": (st or {}).get("dispatch_kid", ""), "head": (st or {}).get("head"), "killed": bool(st and st.get("killed")),
                "limits": (st or {}).get("limits"), "mac": mac, "endpoint": self.endpoint if c == "keyd" else "", "warnings": warn}

    def cap(self) -> int:
        """The `key_custody` cap on act steps: embedded → L2; no usable keys → L1 (guided only); keyd with keys → none (4)."""
        c = self.custody()
        return 2 if c == "embedded" else (4 if c == "keyd" and self.available() else 1)


__all__ = ["KeyClient", "KeyRefused", "SigningUnavailable", "fingerprint", "p0"]
