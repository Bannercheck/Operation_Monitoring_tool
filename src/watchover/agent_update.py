"""Agent self-update and the verified one-line install command (docs/KURULUM_DOCKER.md, "Ajan güncellemesi ve güvenli kurulum").

Trust chain:
* keyd (act/keyd.py) holds release.key and signs the manifest of the wo-agent release it hashes itself in its own image
  (`release`), and host-bound "go back to this version" pins (`sign_pin`), never hashes this process hands it;
* this module only fetches those signatures (KeyClient), decides per agent (global switch "Ajanlar kendini güncellesin",
  pause, staged rollout, pin) and publishes the result: agent-update.json + the signed files under <spool>/release/ for the Go
  receiver (receiver/agentupdate.go), the same answers from the Python receiver (live.py);
* the installer pins the release key on the host (--release-key fingerprint in the command, else the https answer) and
  `wo-agent update` accepts a binary only with a valid signature by that key, the signed sha256, and a newer version (or a
  pin signed for that host). agent.py on Linux + systemd migrates itself through the same verified installer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

STATES = ("current", "updating", "failed", "rolled_back", "paused", "waiting", "off", "unsupported", "unsigned", "unknown")
REFRESH_S = 600                    # keyd is asked for the signed release this often (and at once when it is missing)
PIN_TTL_S = 7 * 86400              # a pin is re-signed a day before it expires, for as long as the operator keeps it
_ROOT = Path(__file__).resolve().parents[2]


def bucket(agent_id: int, version: str) -> int:
    """The agent's place in the staged rollout of `version` (0..99); receiver/agentupdate.go computes the same number."""
    return int(hashlib.sha256(f"{int(agent_id)}:{version}".encode()).hexdigest()[:8], 16) % 100


def _vkey(v: str) -> tuple:
    v = (v or "").strip()
    core, suf = v, ""
    for i, ch in enumerate(v):
        if ch in "-+":
            core, suf = v[:i], v[i:]
            break
    nums = []
    for p in (core.split(".") + ["0", "0", "0"])[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    return (*nums, 1 if not suf else 0, suf)


def newer(a: str, b: str) -> bool:
    """a > b, numerically by major.minor.patch; a pre-release suffix sorts before the release (as the Go side)."""
    return _vkey(a) > _vkey(b)


def parse_update(h: str) -> dict:
    """X-Agent-Update "phase;to;from;at;error" (goagent/internal/update State.Header; agent.py writes the same)."""
    parts = (h or "").split(";") + [""] * 5
    return {"phase": parts[0].strip(), "to": parts[1].strip(), "from": parts[2].strip(), "at": parts[3].strip(), "error": parts[4].strip()}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class AgentUpdates:
    def __init__(self, registry, keys_fn: Callable[[], object], home: Optional[str] = None):
        self.reg = registry
        self.kb = registry.kb
        self.keys_fn = keys_fn
        self.home = Path(home or os.environ.get("WATCHOVER_HOME") or "data")
        self._lock = threading.RLock()
        self._rel: Optional[dict] = None
        self._rel_at = 0.0
        self.error = ""
        self._pins: dict = {}                       # agent id -> signed pin (keyd sign_pin)
        self._export_blob = ""
        self._copied: dict = {}                     # name -> sha256 copied into <spool>/release
        self._pol: tuple = (0.0, {})                # (at, {agent id: (paused, pin)}): the receiver asks on every batch
        try:
            d = json.loads((self.home / "agent-release.json").read_text())
            if isinstance(d, dict) and d.get("body") and d.get("sig"):
                self._rel = d                       # the last signed release survives a restart while keyd is away
        except (OSError, ValueError):
            pass

    # ---------------------------------------------------------------- settings
    def _setting(self, key: str, default: str) -> str:
        self.kb._exec("CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT)")
        rows = self.kb._exec("SELECT value FROM agent_settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def _set(self, key: str, value: str) -> None:
        self.kb._exec("CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT)")
        self.kb._exec("DELETE FROM agent_settings WHERE key=?", (key,))
        self.kb._exec("INSERT INTO agent_settings (key, value) VALUES (?, ?)", (key, value))

    def settings(self) -> dict:
        try:
            pct = int(self._setting("update_rollout_pct", "100"))
        except ValueError:
            pct = 100
        return {"enabled": self._setting("self_update", "1") == "1", "pct": max(0, min(pct, 100))}

    def set_settings(self, enabled: Optional[bool] = None, pct: Optional[int] = None) -> dict:
        if enabled is not None:
            self._set("self_update", "1" if enabled else "0")
        if pct is not None:
            self._set("update_rollout_pct", str(max(0, min(int(pct), 100))))
        return self.settings()

    # ---------------------------------------------------------------- the signed release (keyd)
    def _keys(self):
        try:
            return self.keys_fn()
        except Exception:  # noqa: BLE001 - no action-agent service in this process
            return None

    def release(self, force: bool = False) -> Optional[dict]:
        """keyd's signed release ({body, sig, key, fp, version, seq}); the last good one while keyd is unreachable."""
        with self._lock:
            if not force and self._rel is not None and time.time() - self._rel_at < REFRESH_S:
                return self._rel
            if not force and self._rel is None and time.time() - self._rel_at < 30:
                return None                          # keyd away: ask again in 30 s, not on every request
            self._rel_at = time.time()
            k = self._keys()
            if k is None:
                self.error = "no key service"
                return self._rel
            try:
                rel = k.release()
            except Exception as e:  # noqa: BLE001 - SigningUnavailable / KeyRefused / transport: keep serving the last one
                self.error = str(getattr(e, "code", "") or e)[:200]
                return self._rel
            self.error = ""
            if rel != self._rel:
                self._rel = rel
                try:
                    self.home.mkdir(parents=True, exist_ok=True)
                    tmp = self.home / "agent-release.json.tmp"
                    tmp.write_text(json.dumps(rel))
                    os.replace(tmp, self.home / "agent-release.json")
                except OSError:
                    pass
            return self._rel

    def release_body(self) -> dict:
        rel = self.release()
        if not rel:
            return {}
        try:
            return json.loads(base64.b64decode(rel["body"]))
        except (ValueError, KeyError):
            return {}

    def status(self) -> dict:
        rel = self.release()
        out = {"signed": bool(rel), "error": self.error, **self.settings()}
        if rel:
            out.update({"version": rel.get("version"), "fp": rel.get("fp"), "key": rel.get("key"), "seq": rel.get("seq")})
        return out

    def versions(self) -> list:
        """Releases keyd signed (newest first): the choices of "bu sürüme döndür"."""
        k = self._keys()
        try:
            return list(reversed(k.releases())) if k is not None else []
        except Exception:  # noqa: BLE001
            return []

    # ---------------------------------------------------------------- per agent
    def _policy(self, aid: int) -> tuple:
        """(paused, pin version) of an agent, read fresh at most every 5 s (cached agent records do not carry them)."""
        at, pol = self._pol
        if time.time() - at > 5:
            pol = {int(r["id"]): (bool(int(r["update_paused"] or 0)), (r["update_pin"] or "").strip())
                   for r in self.kb._exec("SELECT id, update_paused, update_pin FROM agents")}
            self._pol = (time.time(), pol)
        return pol.get(int(aid), (False, ""))

    def set_paused(self, aid: int, paused: bool) -> None:
        self.kb._exec("UPDATE agents SET update_paused=? WHERE id=?", (1 if paused else 0, int(aid)))
        self._pol = (0.0, {})

    def set_pin(self, aid: int, version: str = "") -> None:
        """Pin an agent to a version keyd signed before (it switches to it, older or not) or clear the pin ("")."""
        a = self.reg.get(int(aid))
        if not a:
            raise KeyError(aid)
        version = (version or "").strip()
        if version:
            k = self._keys()
            if k is None:
                raise ValueError("no key service: a pin needs keyd")
            self._pins[int(aid)] = k.sign_pin(a["name"], version, PIN_TTL_S)     # KeyRefused: keyd never signed that version
        else:
            self._pins.pop(int(aid), None)
        self.kb._exec("UPDATE agents SET update_pin=? WHERE id=?", (version, int(aid)))
        self._pol = (0.0, {})

    def _pin(self, a: dict) -> Optional[dict]:
        v = (a.get("update_pin") or "").strip()
        if not v:
            return None
        p = self._pins.get(int(a["id"]))
        fresh = p and p.get("version") == v and p.get("expires_at", "") > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 86400))
        if not fresh:
            k = self._keys()
            try:
                p = k.sign_pin(a["name"], v, PIN_TTL_S) if k is not None else p
                self._pins[int(a["id"])] = p
            except Exception:  # noqa: BLE001 - keyd away: the old pin until it expires
                pass
        return p or None

    def decide(self, a: dict, engine: str, version: str) -> dict:
        """The answer of GET /agent/update for agent record `a` (receiver/agentupdate.go decide, same rules)."""
        rel = self.release()
        if not rel:
            return {"release": None, "update": False, "reason": "unsigned"}
        out = {"release": {"body": rel["body"], "sig": rel["sig"], "key": rel.get("key", ""), "version": rel.get("version", "")},
               "update": False, "version": rel.get("version", ""), "agent": a.get("name", "")}
        paused, pinv = self._policy(int(a["id"]))
        a = {**a, "update_paused": int(paused), "update_pin": pinv}
        pin = self._pin(a)
        if pin:
            out.update(pin=pin, version=pin["version"], reason="pinned", update=engine == "agent.py" or version != pin["version"])
            return out
        st = self.settings()
        if int(a.get("update_paused") or 0):
            out["reason"] = "paused"
        elif not st["enabled"]:
            out["reason"] = "disabled"
        elif bucket(int(a["id"]), out["version"]) >= st["pct"]:
            out["reason"] = "rollout"
        elif engine == "agent.py":
            out.update(update=True, reason="migrate")
        elif newer(out["version"], version):
            out.update(update=True, reason="newer")
        else:
            out["reason"] = "current"
        return out

    def manifest_fields(self) -> dict:
        rel = self.release()
        if not rel:
            return {}
        return {"release_body": rel["body"], "release_sig": rel["sig"], "release_key": rel.get("key", ""),
                "release_fp": rel.get("fp", ""), "release_version": rel.get("version", "")}

    # ---------------------------------------------------------------- the Go receiver's view
    def export_doc(self) -> dict:
        rel = self.release()
        st = self.settings()
        doc = {"release": None, "enabled": st["enabled"], "pct": st["pct"], "paused": [], "pins": {}}
        if rel:
            doc["release"] = {k: rel.get(k, "") for k in ("body", "sig", "key", "fp", "version")}
        for a in self.reg.list():
            if a.get("status") != "active":
                continue
            if int(a.get("update_paused") or 0):
                doc["paused"].append(int(a["id"]))
            p = self._pin(a) if rel else None
            if p:
                doc["pins"][str(a["id"])] = {"body": p["body"], "sig": p["sig"], "version": p["version"]}
        return doc

    def export(self, spool_dir) -> bool:
        """agent-update.json for the Go receiver (when it changed) and the signed files under <spool>/release/."""
        d = Path(spool_dir)
        doc = self.export_doc()
        self._copy_release(d)
        blob = json.dumps(doc, sort_keys=True)
        if blob == self._export_blob and (d / "agent-update.json").exists():
            return False
        tmp = d / "agent-update.json.tmp"
        tmp.write_text(blob)
        os.replace(tmp, d / "agent-update.json")
        self._export_blob = blob
        return True

    def _copy_release(self, d: Path) -> None:
        """The files keyd signed, from this image (keyd hashed the same paths in the same image), so the receiver serves
        exactly the signed bytes even when its own image was built apart. A local file that differs is not copied."""
        from .act.keyd import release_sources
        files = (self.release_body() or {}).get("files") or {}
        if not files:
            return
        src = release_sources()
        out = d / "release"
        for name, want in files.items():
            p = src.get(name)
            if p is None or self._copied.get(name) == want.get("sha256"):
                continue
            try:
                if (out / name).exists() and _sha(out / name) == want.get("sha256"):
                    self._copied[name] = want["sha256"]
                    continue
                if not p.exists() or _sha(p) != want.get("sha256"):
                    self.error = f"{name}: this image's copy is not the one keyd signed"
                    continue
                out.mkdir(parents=True, exist_ok=True)
                tmp = out / (name + ".tmp")
                shutil.copyfile(p, tmp)
                os.chmod(tmp, 0o755 if name.startswith("wo-agent") else 0o644)
                os.replace(tmp, out / name)
                self._copied[name] = want["sha256"]
            except OSError as e:
                self.error = f"{name}: {e}"[:200]

    # ---------------------------------------------------------------- what the Agents panel shows
    def state(self, a: dict, rel_version: Optional[str] = None, st: Optional[dict] = None) -> dict:
        ver, eng = (a.get("agent_version") or ""), (a.get("agent_engine") or "")
        upd = parse_update(a.get("agent_update") or "")
        rel_version = rel_version if rel_version is not None else (self.release() or {}).get("version", "")
        st = st or self.settings()
        pin = (a.get("update_pin") or "").strip()
        target = pin or rel_version
        out = {"version": ver, "engine": eng, "target": target, "pin": pin, "paused": bool(int(a.get("update_paused") or 0)),
               "phase": upd["phase"], "error": upd["error"], "at": upd["at"]}

        def s(state: str) -> dict:
            out["state"] = state
            return out
        if not ver:
            return s("unknown")
        if upd["phase"] == "unsupported":
            return s("unsupported")
        if not target:
            return s("unsigned")
        if eng != "agent.py" and ver == target:
            return s("current")
        if upd["phase"] in ("failed", "rolled_back") and upd["to"] == target:
            return s(upd["phase"])
        if out["paused"]:
            return s("paused")
        if upd["phase"] in ("pending", "migrating"):
            return s("updating")
        if not pin and not st["enabled"]:
            return s("off")
        if not pin and bucket(int(a["id"]), target) >= st["pct"]:
            return s("waiting")
        if eng == "agent.py" or pin or newer(target, ver):
            return s("updating")
        return s("current")                          # newer than the dashboard's release: never pushed back

    # ---------------------------------------------------------------- the install command
    def ca_pem(self, spool_dir: Optional[str] = None) -> bytes:
        """The https edge's CA the receiver serves at /agent/ca.crt (Go: exported to <spool>/agent-ca.pem)."""
        cands = []
        if spool_dir:
            cands.append(Path(spool_dir) / "agent-ca.pem")
        cands += [Path(x.strip()) for x in (os.environ.get("WATCHOVER_AGENT_CA") or "").split(",") if x.strip()]
        for p in cands:
            try:
                b = p.read_bytes()
            except OSError:
                continue
            if b"BEGIN CERTIFICATE" in b:
                return b
        return b""

    def install_info(self, spool_dir: Optional[str] = None) -> dict:
        """The current one-line install commands ({BASE}: the receiver address the panel uses, {AUTH}: --enroll-key / --token)."""
        rel = self.release()
        fp = (rel or {}).get("fp", "")
        boot = _ROOT / "scripts" / "agent-boot.sh"
        boot_sha = _sha(boot) if boot.exists() else ""
        ca = self.ca_pem(spool_dir)
        ca_sha = hashlib.sha256(ca).hexdigest() if ca else ""
        https = ""
        if os.environ.get("WATCHOVER_EDGE") == "1" and os.environ.get("WATCHOVER_EDGE_HOST"):
            port = (os.environ.get("WATCHOVER_EDGE_HTTPS_PORT") or "443").strip()
            https = f"https://{os.environ['WATCHOVER_EDGE_HOST'].strip()}" + ("" if port == "443" else f":{port}")
        ing = (os.environ.get("WATCHOVER_INGEST_URL") or "").strip().rstrip("/")
        if not https and ing.startswith("https://"):
            https = ing

        def cmd(base: str, ca_sha256: str = "") -> str:
            k = "k" if ca_sha256 else ""          # the edge's own CA: boot.sh is checked by its sha256, the CA by --ca-sha256
            c = (f'f=$(mktemp) && curl -fsSL{k} -o "$f" {base}/agent/boot.sh && echo "{boot_sha}  $f" | sha256sum -c --quiet '
                 f'&& sudo bash "$f" --url {base}/ingest')
            if ca_sha256:
                c += f" --ca-sha256 {ca_sha256}"
            if fp:
                c += f" --release-key {fp}"
            return c + " {AUTH}"
        legacy = "curl -fsSL {BASE}/agent/install.sh | sudo bash -s -- --url {BASE}/ingest {AUTH}"
        return {"signed": bool(rel), "release_fp": fp, "release_version": (rel or {}).get("version", ""), "boot_sha256": boot_sha,
                "ca_sha256": ca_sha, "https_base": https,
                "http": cmd("{BASE}") if (fp and boot_sha) else legacy,
                "https": cmd(https, ca_sha) if (https and boot_sha) else "",
                "legacy": legacy, "error": self.error}
