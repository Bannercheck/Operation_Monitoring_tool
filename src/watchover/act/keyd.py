"""watchover-keyd: the key service of the action agent (docs/design/action-agent.md §9.3.1, §10.7; R3b, R3c, I9, I17).

It holds the keys outside the API process, the database and /data, and offers a handful of operations that cannot be used
to rewrite history:

* mac_audit(seq, prev_mac, digest)  only for seq = head.seq + 1 with prev_mac = head.mac (it keeps its own head). The newest
                                    MAC stays *unconfirmed* until the API asks for the next one (or an anchor confirms it), so a
                                    transaction that rolled back after its MAC may ask again for that one seq (logged as a
                                    reissue); an older seq is never MAC'd again.
* verify(rows)                      recompute MACs for the verifier; the key never leaves this process.
* sign_anchor()                     Ed25519 (anchor.key) over {seq, mac, ts} of its *own* confirmed head, not of values the API
                                    sends; appended to anchors.log on the keys volume.
* sign_job(envelope)                Ed25519 (dispatch.key) only for well-formed envelopes: window <= 10 min, per-agent seq above the
                                    last it signed, within keyd.json limits (the API cannot write that file), never while KILL exists.
* pseudo(email)                     op_<hex12> actor pseudonyms (pseudo.key).
* rotate_dispatch, export_begin / export_finish (two pass-phrase halves, one per act.policy holder), status, create.

Every MAC, signature and refusal is appended to sig.log. Files: dispatch.key, anchor.key (Ed25519 seeds), audit.key, pseudo.key
(32 random bytes), head.json, keyd.json (limits), sig.log, anchors.log, jobs.json, KILL — all in one 0700 directory.

Transport: one JSON object per line over a Unix socket (single-host installs: own OS user, socket 0660) or TCP with mutual TLS
(compose: `python -m watchover.act.keyd serve --tcp 0.0.0.0:8650 --tls /keys/tls --client-out /secrets/keyd`).

    python -m watchover.act.keyd serve  --dir /keys (--unix /run/watchover-keyd/keyd.sock | --tcp HOST:PORT --tls DIR)
    python -m watchover.act.keyd init   --dir /keys            create the keys now (otherwise the API asks when act_mode leaves off)
    python -m watchover.act.keyd tls-init --dir /keys/tls --client-out /secrets/keyd
    python -m watchover.act.keyd status --dir /keys
    python -m watchover.act.keyd kill   --dir /keys [--resume]  (keyd refuses every job signature while KILL exists)
    python -m watchover.act.keyd export --dir /keys --out keys.wokx     (asks for two pass-phrase halves)
    python -m watchover.act.keyd import --dir /keys --in keys.wokx
"""
from __future__ import annotations

import argparse
import base64
import collections
import getpass
import hashlib
import hmac
import json
import os
import secrets
import socket
import socketserver
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ED_KEYS = ("dispatch.key", "anchor.key")
HMAC_KEYS = ("audit.key", "pseudo.key")
DEFAULT_LIMITS = {"act_per_hour": 120, "act_per_host_hour": 10, "max_window_s": 600}
JOB_FIELDS = ("job_id", "agent_id", "host", "seq", "mode", "not_before", "expires_at", "nonce")
EXPORT_TTL_S = 900
MAX_LINE = 8 << 20


class KeydError(Exception):
    """A refusal: `code` is stable (no_keys, seq, killed, limit, envelope, window, replay, purpose, export, bad_request)."""

    def __init__(self, code: str, msg: str = "", **extra):
        super().__init__(msg or code)
        self.code, self.extra = code, extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _now().isoformat(timespec="seconds")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_ts(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def fingerprint(pub: str) -> str:
    """SHA256:<base64(sha256(raw public key))> — the form people compare (§9.3.1)."""
    raw = base64.b64decode(pub.split(":", 1)[1]) if ":" in pub else b""
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


class KeyStore:
    """The keys and the head. Thread-safe; one instance per key directory (the keyd process, or the embedded demo custody)."""

    def __init__(self, path, prefix: str = "k1", durable: bool = True):
        self.dir = Path(path)
        self.prefix = prefix
        self.durable = durable                 # fsync head.json on every MAC (tests on tmpfs may turn it off)
        self._lock = threading.RLock()
        self._keys: Dict[str, bytes] = {}
        self._head: Optional[dict] = None
        self._jobs: "collections.deque" = collections.deque()
        self._exports: Dict[str, dict] = {}

    # ---------------------------------------------------------------- files
    def exists(self) -> bool:
        return all((self.dir / n).exists() for n in ED_KEYS + HMAC_KEYS)

    def _write_new(self, name: str, data: bytes) -> None:
        p = self.dir / name
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_atomic(self, name: str, data: bytes) -> None:
        p = self.dir / name
        tmp = p.with_name(name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            if self.durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
        if self.durable:
            _fsync_dir(self.dir)

    def _append(self, name: str, entry: dict) -> None:
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(str(self.dir / name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def log(self, op: str, **fields) -> None:
        try:
            self._append("sig.log", {"ts": _ts(), "op": op, **fields})
        except OSError:
            pass

    def create(self) -> dict:
        """Create the directory (0700) and every missing key (0600, never overwritten); idempotent."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            made = []
            for n in ED_KEYS + HMAC_KEYS:
                if not (self.dir / n).exists():
                    self._write_new(n, base64.b64encode(secrets.token_bytes(32)))
                    made.append(n)
            if not (self.dir / "keyd.json").exists():
                self._write_new("keyd.json", json.dumps(DEFAULT_LIMITS, indent=1).encode())
            if made:
                self.log("create", keys=made)
            return {"created": made, **self.status()}

    def _key(self, name: str) -> bytes:
        k = self._keys.get(name)
        if k is None:
            p = self.dir / name
            if not p.exists():
                raise KeydError("no_keys", f"{name} missing")
            k = base64.b64decode(p.read_bytes().strip())
            self._keys[name] = k
        return k

    def _ed(self, name: str):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(self._key(name))

    def _pub(self, name: str) -> str:
        from cryptography.hazmat.primitives import serialization
        raw = self._ed(name).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return "ed25519:" + base64.b64encode(raw).decode()

    def limits(self) -> dict:
        try:
            return {**DEFAULT_LIMITS, **json.loads((self.dir / "keyd.json").read_text())}
        except (OSError, ValueError):
            return dict(DEFAULT_LIMITS)

    def killed(self) -> bool:
        return (self.dir / "KILL").exists()

    # ---------------------------------------------------------------- head
    def _load_head(self) -> Optional[dict]:
        if self._head is None:
            p = self.dir / "head.json"
            if p.exists():
                try:
                    self._head = json.loads(p.read_text())
                except ValueError:
                    raise KeydError("head_corrupt", "head.json unreadable")
        return self._head

    def _save_head(self, h: dict) -> None:
        self._write_atomic("head.json", json.dumps(h, sort_keys=True).encode())
        self._head = h

    def head(self) -> dict:
        with self._lock:
            h = self._load_head()
            if not h:
                return {"seq": 0, "mac": "", "confirmed": True, "set": False, "confirmed_seq": 0, "adopted": 0}
            return {"seq": h["seq"], "mac": h["mac"], "confirmed": bool(h.get("confirmed")), "set": True,
                    "confirmed_seq": h["seq"] if h.get("confirmed") else h["seq"] - 1, "adopted": int(h.get("adopted") or 0)}

    def _mac(self, prev: str, digest: str) -> str:
        return self.prefix + ":" + hmac.new(self._key("audit.key"), f"{prev}\n{digest}".encode("utf-8"), hashlib.sha256).hexdigest()

    def mac_audit(self, seq: int, prev_mac: str, digest: str) -> str:
        """The MAC of row `seq`; refuses anything but the next row (or a reissue of the unconfirmed newest one)."""
        seq = int(seq)
        prev_mac = prev_mac or ""
        with self._lock:
            self._key("audit.key")
            h = self._load_head()
            if h is None:                                   # genesis: adopt the database's head once (rows before it are p0)
                h = {"seq": seq - 1, "mac": prev_mac, "prev": "", "digest": "", "confirmed": True, "adopted": seq - 1}
                self.log("adopt", seq=seq - 1, mac=prev_mac[:40])
            if seq == h["seq"] + 1 and prev_mac == h["mac"]:
                mac = self._mac(prev_mac, digest)
                self._save_head({"seq": seq, "mac": mac, "prev": prev_mac, "digest": digest, "confirmed": False,
                                 "adopted": h.get("adopted", 0)})
                self.log("mac", seq=seq, digest=digest, mac=mac)
                return mac
            if seq == h["seq"] and prev_mac == h.get("prev") and not h.get("confirmed"):
                mac = self._mac(prev_mac, digest)
                self._save_head({**h, "mac": mac, "digest": digest})
                self.log("reissue", seq=seq, digest=digest, old_digest=h.get("digest"), mac=mac)
                return mac
            self.log("refuse_mac", seq=seq, head=h["seq"])
            raise KeydError("seq", f"keyd MACs only seq {h['seq'] + 1} (asked {seq})",
                            head={"seq": h["seq"], "mac": h["mac"], "confirmed": bool(h.get("confirmed"))})

    def verify(self, rows: List[list]) -> dict:
        """rows: [[seq, prev_mac, digest, mac], …] → {"bad": index of the first wrong MAC or -1}."""
        with self._lock:
            k = self._key("audit.key")
        for i, (seq, prev, digest, mac) in enumerate(rows):
            want = self.prefix + ":" + hmac.new(k, f"{prev or ''}\n{digest}".encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, mac or ""):
                return {"bad": i}
        return {"bad": -1}

    def confirm(self, seq: int, mac: str) -> dict:
        with self._lock:
            h = self._load_head()
            if h and not h.get("confirmed") and int(seq) == h["seq"] and mac == h["mac"]:
                self._save_head({**h, "confirmed": True})
            return self.head()

    # ---------------------------------------------------------------- anchors
    def sign_anchor(self, confirm: Optional[dict] = None) -> dict:
        """Sign {seq, mac, ts} of keyd's own confirmed head. `confirm` (the API's committed head) only confirms keyd's newest MAC
        when it matches it exactly; it never changes what is signed otherwise."""
        with self._lock:
            if confirm:
                self.confirm(int(confirm.get("seq") or 0), str(confirm.get("mac") or ""))
            h = self._load_head() or {"seq": 0, "mac": "", "confirmed": True}
            if h.get("confirmed"):
                seq, mac = h["seq"], h["mac"]
            else:
                seq, mac = h["seq"] - 1, h.get("prev") or ""
            body = {"seq": seq, "mac": mac, "ts": _ts()}
            sig = base64.b64encode(self._ed("anchor.key").sign(b"wo-anchor-v1\n" + _canon(body))).decode()
            a = {**body, "sig": sig, "kid": "a-" + fingerprint(self._pub("anchor.key"))[7:15]}
            self._append("anchors.log", a)
            self.log("anchor", seq=seq, mac=mac)
            return a

    def anchors(self, since_seq: int = 0) -> List[dict]:
        out = []
        p = self.dir / "anchors.log"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if int(a.get("seq") or 0) >= since_seq:
                    out.append(a)
        return out

    # ---------------------------------------------------------------- jobs
    def _job_state(self) -> dict:
        try:
            return json.loads((self.dir / "jobs.json").read_text())
        except (OSError, ValueError):
            return {}

    def sign_job(self, env: Dict[str, Any]) -> dict:
        """Ed25519 (dispatch.key) over "wo-job-v1\\n" + canonical(envelope), under keyd's own rules."""
        with self._lock:
            if self.killed():
                self.log("refuse_job", code="killed")
                raise KeydError("killed", "keyd KILL file present")
            missing = [f for f in JOB_FIELDS if env.get(f) in (None, "")]
            if missing or not isinstance(env.get("seq"), int):
                raise KeydError("envelope", "malformed envelope: " + ", ".join(missing or ["seq"]))
            lim = self.limits()
            try:
                nb, ex = _parse_ts(env["not_before"]), _parse_ts(env["expires_at"])
            except ValueError:
                raise KeydError("envelope", "bad timestamps")
            if not (timedelta(0) < ex - nb <= timedelta(seconds=int(lim["max_window_s"]))):
                raise KeydError("window", "expires_at - not_before must be within (0, 10 min]")
            st = self._job_state()
            agent = str(env["agent_id"])
            if int(env["seq"]) <= int(st.get(agent, 0)):
                self.log("refuse_job", code="replay", agent=agent, seq=env["seq"])
                raise KeydError("replay", f"seq {env['seq']} <= last signed {st.get(agent)} for agent {agent}")
            is_act = env.get("mode") in ("execute", "rollback", "act")      # diagnose / verify / dry_run are read-only
            now = time.time()
            while self._jobs and now - self._jobs[0][0] > 3600:
                self._jobs.popleft()
            if is_act:
                n_all = sum(1 for j in self._jobs if j[2])
                n_host = sum(1 for j in self._jobs if j[2] and j[1] == env["host"])
                if n_all >= int(lim["act_per_hour"]) or n_host >= int(lim["act_per_host_hour"]):
                    self.log("refuse_job", code="limit", host=env["host"])
                    raise KeydError("limit", "keyd job limit reached")
            sig = base64.b64encode(self._ed("dispatch.key").sign(b"wo-job-v1\n" + _canon(env))).decode()
            st[agent] = int(env["seq"])
            self._write_atomic("jobs.json", json.dumps(st).encode())
            self._jobs.append((now, env["host"], is_act))
            self.log("job", job_id=env["job_id"], agent=agent, seq=env["seq"], sha256=hashlib.sha256(_canon(env)).hexdigest())
            return {"sig": sig, "kid": self.kid()}

    def kid(self) -> str:
        return "d-" + fingerprint(self._pub("dispatch.key"))[7:15]

    def sign(self, purpose: str, body: bytes) -> dict:
        """Other server-side signatures (hello answers): never `job` (sign_job) and never `anchor` (sign_anchor)."""
        if purpose not in ("hello", "rotate", "req"):
            raise KeydError("purpose", f"keyd does not sign {purpose!r} here")
        with self._lock:
            sig = base64.b64encode(self._ed("dispatch.key").sign(f"wo-{purpose}-v1\n".encode() + body)).decode()
            self.log("sign", purpose=purpose, sha256=hashlib.sha256(body).hexdigest())
            return {"sig": sig, "kid": self.kid()}

    def rotate_dispatch(self) -> dict:
        """New dispatch key; the old one signs 'wo-rotate-v1\\n' + new public key and is kept as dispatch.key.<ts>."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        with self._lock:
            old = self._ed("dispatch.key")
            new = Ed25519PrivateKey.generate()
            seed = new.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            raw = new.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            pub = "ed25519:" + base64.b64encode(raw).decode()
            sig = base64.b64encode(old.sign(b"wo-rotate-v1\n" + pub.encode())).decode()
            stamp = _now().strftime("%Y%m%d%H%M%S")
            os.replace(self.dir / "dispatch.key", self.dir / f"dispatch.key.{stamp}")
            self._write_new("dispatch.key", base64.b64encode(seed))
            self._keys.pop("dispatch.key", None)
            self.log("rotate", new=fingerprint(pub))
            return {"new": pub, "sig": sig, "fingerprint": fingerprint(pub)}

    # ---------------------------------------------------------------- pseudonyms
    def pseudo(self, email: str) -> str:
        e = (email or "").strip().lower()
        if not e:
            return ""
        with self._lock:
            k = self._key("pseudo.key")
        return "op_" + hmac.new(k, e.encode(), hashlib.sha256).hexdigest()[:12]

    # ---------------------------------------------------------------- encrypted export (two pass-phrase halves)
    def export_begin(self, part: str, by: str = "") -> dict:
        if len(part or "") < 8:
            raise KeydError("export", "each pass-phrase half needs at least 8 characters")
        with self._lock:
            now = time.time()
            for t in [t for t, v in self._exports.items() if now - v["at"] > EXPORT_TTL_S]:
                self._exports.pop(t, None)
            token = secrets.token_urlsafe(16)
            self._exports[token] = {"part": part, "by": by, "at": now}
            self.log("export_begin", by=by)
            return {"token": token, "expires_s": EXPORT_TTL_S}

    def export_finish(self, token: str, part: str, by: str = "") -> dict:
        with self._lock:
            p = self._exports.pop(token, None)
            if not p or time.time() - p["at"] > EXPORT_TTL_S:
                raise KeydError("export", "unknown or expired export")
            if by and p["by"] and by == p["by"]:
                raise KeydError("export", "the second half must come from a second person")
            if len(part or "") < 8:
                raise KeydError("export", "each pass-phrase half needs at least 8 characters")
            files = {n: (self.dir / n).read_text().strip() for n in ED_KEYS + HMAC_KEYS + ("keyd.json", "head.json", "jobs.json")
                     if (self.dir / n).exists()}
            blob = seal_export(files, p["part"], part)
            self.log("export", by=[p["by"], by])
            return {"blob": blob}

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        with self._lock:
            ok = self.exists()
            out = {"keys": ok, "dir": str(self.dir), "prefix": self.prefix, "killed": self.killed(), "head": self.head(),
                   "limits": self.limits()}
            try:
                out["st_dev"] = os.stat(self.dir).st_dev
            except OSError:
                out["st_dev"] = None
            if ok:
                d, a = self._pub("dispatch.key"), self._pub("anchor.key")
                out.update({"dispatch_pub": d, "anchor_pub": a, "dispatch_fingerprint": fingerprint(d),
                            "anchor_fingerprint": fingerprint(a), "dispatch_kid": self.kid()})
            return out

    def public(self) -> dict:
        with self._lock:
            return {"dispatch": self._pub("dispatch.key"), "anchor": self._pub("anchor.key"), "dispatch_kid": self.kid()}

    # ---------------------------------------------------------------- RPC dispatch
    def call(self, op: str, args: Dict[str, Any]) -> Any:
        a = args or {}
        if op == "status":
            return self.status()
        if op == "create":
            return self.create()
        if op == "head":
            return self.head()
        if op == "mac_audit":
            return self.mac_audit(int(a["seq"]), str(a.get("prev_mac") or ""), str(a["digest"]))
        if op == "verify":
            return self.verify(list(a.get("rows") or []))
        if op == "confirm":
            return self.confirm(int(a.get("seq") or 0), str(a.get("mac") or ""))
        if op == "sign_anchor":
            return self.sign_anchor(a.get("confirm"))
        if op == "anchors":
            return self.anchors(int(a.get("since_seq") or 0))
        if op == "sign_job":
            return self.sign_job(dict(a.get("envelope") or {}))
        if op == "sign":
            return self.sign(str(a.get("purpose")), base64.b64decode(a.get("body_b64") or ""))
        if op == "pseudo":
            return self.pseudo(str(a.get("email") or ""))
        if op == "public":
            return self.public()
        if op == "rotate_dispatch":
            return self.rotate_dispatch()
        if op == "export_begin":
            return self.export_begin(str(a.get("part") or ""), str(a.get("by") or ""))
        if op == "export_finish":
            return self.export_finish(str(a.get("token") or ""), str(a.get("part") or ""), str(a.get("by") or ""))
        raise KeydError("bad_request", f"unknown op {op!r}")


# ---------------------------------------------------------------- export format
def _kdf(p1: str, p2: str, salt: bytes) -> bytes:
    k = hashlib.scrypt((p1 + "\n" + p2).encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(k)


def seal_export(files: Dict[str, str], p1: str, p2: str) -> str:
    from cryptography.fernet import Fernet
    salt = secrets.token_bytes(16)
    tok = Fernet(_kdf(p1, p2, salt)).encrypt(_canon({"v": 1, "files": files, "ts": _ts()}))
    return json.dumps({"wokx": 1, "salt": base64.b64encode(salt).decode(), "data": tok.decode()})


def open_export(blob: str, p1: str, p2: str) -> Dict[str, str]:
    from cryptography.fernet import Fernet, InvalidToken
    d = json.loads(blob)
    try:
        body = Fernet(_kdf(p1, p2, base64.b64decode(d["salt"]))).decrypt(d["data"].encode())
    except InvalidToken:
        raise KeydError("export", "wrong pass-phrase halves")
    return json.loads(body)["files"]


# ---------------------------------------------------------------- server
def _respond(store: KeyStore, raw: bytes) -> bytes:
    try:
        req = json.loads(raw)
        res = store.call(str(req.get("op") or ""), req.get("args") or {})
        out = {"ok": True, "result": res}
    except KeydError as e:
        out = {"ok": False, "code": e.code, "error": str(e), **e.extra}
    except Exception as e:  # noqa: BLE001 - never crash the service on one bad request
        out = {"ok": False, "code": "bad_request", "error": str(e)[:300]}
    return (json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            try:
                line = self.rfile.readline(MAX_LINE)
            except (OSError, ssl.SSLError):
                return
            if not line:
                return
            try:
                self.wfile.write(_respond(self.server.store, line))
                self.wfile.flush()
            except (OSError, ssl.SSLError):
                return


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, ctx: Optional[ssl.SSLContext]):
        self.ctx = ctx
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = super().get_request()
        if self.ctx is not None:
            sock.settimeout(10)
            sock = self.ctx.wrap_socket(sock, server_side=True)
            sock.settimeout(None)
        return sock, addr


def server_context(tls_dir: str) -> ssl.SSLContext:
    d = Path(tls_dir)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(d / "ca.crt"))
    ctx.load_cert_chain(str(d / "server.crt"), str(d / "server.key"))
    ctx.verify_mode = ssl.CERT_REQUIRED                     # mutual TLS: only the API's client certificate gets in
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def make_server(store: KeyStore, unix: str = "", tcp: str = "", tls: str = ""):
    if unix:
        p = Path(unix)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        srv = UnixServer(str(p), _Handler)
        try:
            os.chmod(str(p), 0o660)
        except OSError:
            pass
    else:
        host, _, port = tcp.rpartition(":")
        srv = TCPServer((host or "127.0.0.1", int(port)), _Handler, server_context(tls) if tls else None)
    srv.store = store
    return srv


def start_thread(store: KeyStore, unix: str = "", tcp: str = "", tls: str = ""):
    """Run a keyd server in a background thread (tests, single-process demos); returns the server (call .shutdown())."""
    srv = make_server(store, unix=unix, tcp=tcp, tls=tls)
    t = threading.Thread(target=srv.serve_forever, name="keyd", daemon=True)
    t.start()
    return srv


# ---------------------------------------------------------------- TLS material (mutual TLS between the API and keyd)
def tls_init(tls_dir: str, client_out: str, names=("watchover-keyd", "keyd", "localhost")) -> dict:
    """A private CA, the keyd server certificate and the API's client certificate (EC P-256, 5 years). Existing files are kept."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress
    d, c = Path(tls_dir), Path(client_out)
    if (d / "server.crt").exists() and (c / "client.crt").exists():
        return {"created": False, "dir": str(d), "client": str(c)}
    for x in (d, c):
        x.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(x, 0o700)
        except OSError:
            pass
    now = _now() - timedelta(minutes=5)

    def key():
        return ec.generate_private_key(ec.SECP256R1())

    def pem_key(k):
        return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    ca_key = key()
    ca = (x509.CertificateBuilder().subject_name(name("watchover-keyd CA")).issuer_name(name("watchover-keyd CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now)
          .not_valid_after(now + timedelta(days=1825)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))

    def leaf(cn, usage, san=None):
        k = key()
        b = (x509.CertificateBuilder().subject_name(name(cn)).issuer_name(ca.subject).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + timedelta(days=1825))
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        return k, b.sign(ca_key, hashes.SHA256())
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    sk, sc = leaf("watchover-keyd", ExtendedKeyUsageOID.SERVER_AUTH, san)
    ck, cc = leaf("watchover-api", ExtendedKeyUsageOID.CLIENT_AUTH)
    pem = serialization.Encoding.PEM
    for path, data in ((d / "ca.crt", ca.public_bytes(pem)), (d / "server.crt", sc.public_bytes(pem)), (d / "server.key", pem_key(sk)),
                       (c / "ca.crt", ca.public_bytes(pem)), (c / "client.crt", cc.public_bytes(pem)), (c / "client.key", pem_key(ck))):
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600 if path.suffix == ".key" else 0o644)
        except OSError:
            pass
    return {"created": True, "dir": str(d), "client": str(c)}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="watchover.act.keyd", description="watchover-keyd: the action agent's key service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "init", "status", "kill", "export", "import", "tls-init"):
        p = sub.add_parser(name)
        p.add_argument("--dir", default=os.environ.get("WATCHOVER_KEYD_DIR", "/var/lib/watchover-keys"))
        if name == "serve":
            p.add_argument("--unix", default="")
            p.add_argument("--tcp", default="")
            p.add_argument("--tls", default="", help="directory with ca.crt, server.crt, server.key (mutual TLS)")
            p.add_argument("--client-out", default="", help="write the API's client certificate here on first start")
            p.add_argument("--create", action="store_true", help="create the keys at start (normally the API asks)")
        if name == "kill":
            p.add_argument("--resume", action="store_true")
            p.add_argument("--reason", default="")
        if name == "export":
            p.add_argument("--out", required=True)
        if name == "import":
            p.add_argument("--in", dest="inp", required=True)
        if name == "tls-init":
            p.add_argument("--client-out", required=True)
    args = ap.parse_args(argv)
    store = KeyStore(args.dir)
    if args.cmd == "tls-init":
        print(json.dumps(tls_init(args.dir, args.client_out)))
        return 0
    if args.cmd == "init":
        print(json.dumps(store.create(), default=str))
        return 0
    if args.cmd == "status":
        print(json.dumps(store.status(), default=str, indent=1))
        return 0
    if args.cmd == "kill":
        p = Path(args.dir) / "KILL"
        if args.resume:
            p.unlink(missing_ok=True)
            store.log("kill_off")
            print("KILL removed")
        else:
            p.write_text(json.dumps({"ts": _ts(), "reason": args.reason}))
            store.log("kill_on", reason=args.reason[:200])
            print(f"KILL written: {p} (keyd signs no job until it is removed)")
        return 0
    if args.cmd == "export":
        p1 = getpass.getpass("first holder's pass-phrase half: ")
        p2 = getpass.getpass("second holder's pass-phrase half: ")
        t = store.export_begin(p1, "cli-1")["token"]
        Path(args.out).write_text(store.export_finish(t, p2, "cli-2")["blob"])
        print(f"encrypted export written: {args.out}")
        return 0
    if args.cmd == "import":
        p1 = getpass.getpass("first holder's pass-phrase half: ")
        p2 = getpass.getpass("second holder's pass-phrase half: ")
        files = open_export(Path(args.inp).read_text(), p1, p2)
        store.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(store.dir, 0o700)
        for n, v in files.items():
            if (store.dir / n).exists():
                print(f"kept existing {n}")
                continue
            store._write_new(n, v.encode())
        print(f"imported {len(files)} file(s) into {store.dir}")
        return 0
    # serve
    if args.tls and args.client_out:
        tls_init(args.tls, args.client_out)
    if args.create:
        store.create()
    if not args.unix and not args.tcp:
        print("--unix or --tcp is required", file=sys.stderr)
        return 2
    srv = make_server(store, unix=args.unix, tcp=args.tcp, tls=args.tls)
    where = args.unix or args.tcp
    print(f"watchover-keyd on {where}{' (mutual TLS)' if args.tls else ''}; keys in {store.dir} "
          f"({'present' if store.exists() else 'not created yet'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
