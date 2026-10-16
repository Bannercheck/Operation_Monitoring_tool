"""Update packages for the in-app update (System › Güncelleme): inspection, extraction, signatures and the shared spool.

Two package kinds are accepted:
  zip     the source tree as `git archive --format=zip --prefix=<dir>/ HEAD` makes it: one top-level folder of any name (or the
          files at the root). The version is read from src/watchover/__init__.py as text; nothing in a package is ever executed
          before it has passed every check.
  bundle  the air-gapped bundle of `./watchover.sh bundle` (tar: MANIFEST, images.tar.gz, volumes/*.tar, source/...).
Checks: upload size, unpacked tree size / file count / compression ratio, no absolute path, no '..', no symlink, hard link or
device, no duplicate name, the files an install needs, and the in-app updater in the new release (a package without it could not
be rolled back from the interface).

Signatures: Ed25519 over b"watchover-package-v1:" + the package's SHA-256 (hex), shipped as a base64 .sig file next to the package
(python -m watchover.release sign). When a public key is configured (WATCHOVER_UPDATE_PUBKEY: base64 / PEM / a file path, or
update-pubkey.pem in the install folder) unsigned and badly signed packages are refused; the updater checks again on its side.

The spool (/updates, the watchover-updates volume): the dashboard writes inbox/<id>/package.* and requests/<id>.json, the updater
(updater.py, its own container with the engine socket) writes state.json, releases.json, updater.json and jobs/<id>.log.
Python 3.9, standard library; `cryptography` when present, else the openssl command."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REQUIRED = ("watchover.sh", "docker-compose.yml", "Dockerfile", "pyproject.toml", "src/watchover/__init__.py")
UPDATER_FILES = ("src/watchover/updater.py", "src/watchover/updates.py", "Dockerfile.updater")   # the release can be updated / rolled back from the UI
CLASSIC_FILES = ("app.py", "src/watchover/analysis.py")                                        # what a non-container install runs
SIG_PREFIX = b"watchover-package-v1:"
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}(?:[.-][0-9A-Za-z]{1,16})?$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,47}$")
MAX_TREE_BYTES = 1 << 30            # unpacked source tree
MAX_FILES = 30000
MAX_RATIO = 200                     # per member above 1 MB (zip bombs)
MAX_NAME = 400
ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")   # DER prefix of an Ed25519 SubjectPublicKeyInfo


class PackageError(ValueError):
    """A package that must not be installed; `code` is what the interface translates (upd_err_<code>)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


# ---------------------------------------------------------------- versions
def parse_version(v: str) -> Tuple[int, ...]:
    out = []
    for part in re.split(r"[.-]", v or "0")[:4]:
        m = re.match(r"\d+", part)
        out.append(int(m.group(0)) if m else 0)
    return tuple(out + [0] * (4 - len(out)))


def compare(a: str, b: str) -> int:
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def version_of_text(text: str) -> str:
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text or "")
    return m.group(1).strip() if m else ""


def read_version(tree: Path) -> str:
    p = Path(tree) / "src" / "watchover" / "__init__.py"
    try:
        return version_of_text(p.read_text(encoding="utf-8"))
    except OSError:
        return ""


def display(v: str) -> str:
    parts = (v.split(".") + ["0", "0"])[:3]
    return f"v{parts[0]}.{parts[1]}" + (f".{parts[2]}" if parts[2] not in ("0", "") else "")


def notes_for(changelog: str, version: str) -> dict:
    """The CHANGELOG.md entry of `version` (## v1.53 · 2026-10-16 · güncelleme): {date, kind, lines}; empty when absent."""
    want = display(version)
    for block in re.split(r"(?m)^## ", changelog or "")[1:]:
        title, _, body = block.partition("\n")
        parts = [x.strip() for x in title.split("·")]
        if parts and parts[0] in (want, f"v{version}", version):
            return {"date": parts[1] if len(parts) > 1 else "", "kind": parts[2] if len(parts) > 2 else "",
                    "lines": [ln.strip()[2:] for ln in body.splitlines() if ln.strip().startswith("- ")][:20]}
    return {"date": "", "kind": "", "lines": []}


# ---------------------------------------------------------------- limits and names
def max_upload_bytes() -> int:
    try:
        mb = int(os.environ.get("WATCHOVER_UPDATE_MAX_MB") or 8192)
    except ValueError:
        mb = 8192
    return max(16, mb) * 1024 * 1024


def safe_rel(name: str) -> Optional[str]:
    """A member name as a relative POSIX path, or None when it must be refused (absolute, '..', drive letter, backslash, NUL)."""
    if not name or "\x00" in name or "\\" in name or len(name) > MAX_NAME:
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or len(parts) > 40:
        return None
    return "/".join(parts)


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF if info.create_system == 3 else 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kind_of(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(512)
    if head[:2] == b"PK":
        return "zip"
    if head[257:262] == b"ustar":                                # POSIX / GNU tar header (what ./watchover.sh bundle writes)
        return "bundle"
    raise PackageError("not_a_package", "neither a zip nor a tar file")


# ---------------------------------------------------------------- inspection
def _check_tree(files: Dict[str, int], require_updater: bool, require: Tuple[str, ...] = REQUIRED) -> None:
    if len(files) > MAX_FILES:
        raise PackageError("too_many_files", f"{len(files)} > {MAX_FILES}")
    if sum(files.values()) > MAX_TREE_BYTES:
        raise PackageError("too_large", f"{sum(files.values()) // 1048576} MB unpacked > {MAX_TREE_BYTES // 1048576} MB")
    missing = [f for f in require if f not in files]
    if missing:
        raise PackageError("not_watchover", "missing " + ", ".join(missing))
    if require_updater:
        missing = [f for f in UPDATER_FILES if f not in files]
        if missing:
            raise PackageError("no_updater", "this release cannot be updated from the interface (missing " + ", ".join(missing) + ")")


def inspect_zip(path: Path, require_updater: bool = True, require: Tuple[str, ...] = REQUIRED) -> dict:
    try:
        zf = zipfile.ZipFile(str(path))
    except (zipfile.BadZipFile, OSError) as e:
        raise PackageError("bad_zip", str(e)[:120])
    with zf:
        infos = zf.infolist()
        names: Dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            rel = safe_rel(info.filename)
            if rel is None:
                raise PackageError("unsafe_path", info.filename[:120])
            mode = _zip_mode(info)
            if stat.S_ISLNK(mode):
                raise PackageError("symlink", info.filename[:120])
            if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise PackageError("special_file", info.filename[:120])
            if info.flag_bits & 0x1:
                raise PackageError("encrypted", info.filename[:120])
            if info.is_dir():
                continue
            if rel in names:
                raise PackageError("duplicate", rel[:120])
            if info.file_size > (1 << 20) and info.file_size > MAX_RATIO * max(1, info.compress_size):
                raise PackageError("zip_bomb", rel[:120])
            names[rel] = info
        tops = {n.split("/", 1)[0] for n in names}
        prefix = ""
        if len(tops) == 1 and all("/" in n for n in names):
            prefix = tops.pop() + "/"
        files = {n[len(prefix):]: i.file_size for n, i in names.items() if n.startswith(prefix)}
        _check_tree(files, require_updater, require)

        def text(rel: str, cap: int) -> str:
            info = names.get(prefix + rel)
            if info is None or info.file_size > cap:
                return ""
            return zf.read(info).decode("utf-8", "replace")
        version = version_of_text(text("src/watchover/__init__.py", 65536))
        changelog = text("CHANGELOG.md", 4 << 20)
        pyproject = text("pyproject.toml", 65536)
        comment = zf.comment.decode("ascii", "replace").strip()
    git = comment[:12] if re.fullmatch(r"[0-9a-f]{40}", comment) else ""    # git archive writes the commit id as the zip comment
    return _finish("zip", version, prefix, files, changelog, git, pyproject)


def _tar_rel(name: str) -> Optional[str]:
    return safe_rel(name[2:] if name.startswith("./") else name)


def inspect_tar(path: Path, require_updater: bool = True) -> dict:
    files: Dict[str, int] = {}
    seen = set()
    manifest = init = changelog = pyproject = ""
    has_images = False
    try:
        tf = tarfile.open(str(path), "r:*")
    except (tarfile.TarError, OSError) as e:
        raise PackageError("bad_tar", str(e)[:120])
    with tf:
        try:
            for m in tf:
                rel = _tar_rel(m.name)
                if rel is None:
                    raise PackageError("unsafe_path", m.name[:120])
                if m.issym() or m.islnk():
                    raise PackageError("symlink", m.name[:120])
                if not (m.isfile() or m.isdir()):
                    raise PackageError("special_file", m.name[:120])
                if m.isdir():
                    continue
                if rel in seen:
                    raise PackageError("duplicate", rel[:120])
                seen.add(rel)
                top = rel.split("/", 1)[0]
                if rel == "MANIFEST":
                    manifest = tf.extractfile(m).read(65536).decode("utf-8", "replace")
                elif rel == "images.tar.gz":
                    has_images = True
                elif top == "volumes" and rel.count("/") == 1 and rel.endswith(".tar"):
                    pass
                elif top == "source" and "/" in rel:
                    sub = rel.split("/", 1)[1]
                    files[sub] = m.size
                    if sub == "src/watchover/__init__.py" and m.size < 65536:
                        init = tf.extractfile(m).read().decode("utf-8", "replace")
                    elif sub == "CHANGELOG.md" and m.size < (4 << 20):
                        changelog = tf.extractfile(m).read().decode("utf-8", "replace")
                    elif sub == "pyproject.toml" and m.size < 65536:
                        pyproject = tf.extractfile(m).read().decode("utf-8", "replace")
                else:
                    raise PackageError("unexpected_member", rel[:120])
        except tarfile.TarError as e:
            raise PackageError("bad_tar", str(e)[:120])
    if not has_images:
        raise PackageError("not_watchover", "a bundle without images.tar.gz")
    _check_tree(files, require_updater)
    version = version_of_text(init)
    mv = re.search(r"(?m)^version=(\S+)", manifest)
    if mv and version and mv.group(1) != version:
        raise PackageError("version_mismatch", f"MANIFEST {mv.group(1)} != source {version}")
    return _finish("bundle", version, "source/", files, changelog, "", pyproject)


def _finish(kind: str, version: str, prefix: str, files: Dict[str, int], changelog: str, git: str, pyproject: str = "") -> dict:
    if not VERSION_RE.match(version or ""):
        raise PackageError("no_version", f"src/watchover/__init__.py: {version or 'not found'}")
    pv = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject or "")
    if pv and pv.group(1) != version:                            # watchover.sh tags the images with the pyproject version
        raise PackageError("version_mismatch", f"pyproject.toml {pv.group(1)} != src/watchover/__init__.py {version}")
    return {"kind": kind, "version": version, "display": display(version), "prefix": prefix, "files": len(files),
            "bytes": sum(files.values()), "git": git, "notes": notes_for(changelog, version)}


def inspect(path: Path, sig: str = "", pubkey: Optional[bytes] = None, current: str = "", require_updater: bool = True,
            max_bytes: Optional[int] = None, require: Tuple[str, ...] = REQUIRED) -> dict:
    """Every check on a package file; returns what the interface shows (current -> new, notes, signature) or raises PackageError."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PackageError("not_a_package", "not a regular file")
    size = path.stat().st_size
    if size > (max_bytes or max_upload_bytes()):
        raise PackageError("too_large", f"{size // 1048576} MB > {(max_bytes or max_upload_bytes()) // 1048576} MB")
    kind = kind_of(path)
    if kind == "bundle" and require != REQUIRED:
        raise PackageError("not_a_package", "an offline bundle is for the container install")
    info = inspect_zip(path, require_updater, require) if kind == "zip" else inspect_tar(path, require_updater)
    info.update(size=size, sha256=_sha256(path), current=current)
    info["signature"] = check_signature(info["sha256"], sig, pubkey)
    if current:
        c = compare(info["version"], current)
        info["direction"] = "same" if c == 0 else ("downgrade" if c < 0 else "upgrade")
    return info


def check_signature(sha256hex: str, sig: str, pubkey: Optional[bytes]) -> dict:
    """{"required", "status": ok | none | unverified, "fingerprint"}; raises when a key is configured and the signature is missing / bad."""
    if pubkey:
        if not (sig or "").strip():
            raise PackageError("signature_missing", "a signing key is configured: upload the package with its .sig file")
        if not verify(pubkey, sig_message(sha256hex), sig):
            raise PackageError("signature_bad", "the signature does not match this package and the configured key")
        return {"required": True, "status": "ok", "fingerprint": fingerprint(pubkey)}
    return {"required": False, "status": "unverified" if (sig or "").strip() else "none", "fingerprint": ""}


# ---------------------------------------------------------------- extraction
def extract(path: Path, info: dict, dest: Path) -> List[str]:
    """Unpack the source tree of a checked package into `dest` (a new folder); every member is checked again. Returns the file list."""
    dest = Path(dest)
    dest.mkdir(parents=True)
    prefix = info["prefix"]
    out: List[str] = []
    total = 0

    def write(rel: str, src, mode: int) -> None:
        nonlocal total
        target = dest / rel
        if not str(target.resolve()).startswith(str(dest.resolve()) + os.sep):
            raise PackageError("unsafe_path", rel[:120])
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TREE_BYTES:
                    raise PackageError("too_large", "unpacked tree")
                f.write(chunk)
        os.chmod(target, 0o755 if mode & 0o111 else 0o644)
        out.append(rel)

    if info["kind"] == "zip":
        with zipfile.ZipFile(str(path)) as zf:
            for m in zf.infolist():
                rel = safe_rel(m.filename)
                mode = _zip_mode(m)
                if rel is None or stat.S_ISLNK(mode):
                    raise PackageError("unsafe_path", m.filename[:120])
                if m.is_dir() or not rel.startswith(prefix):
                    continue
                with zf.open(m) as src:
                    write(rel[len(prefix):], src, mode)
    else:
        with tarfile.open(str(path), "r:*") as tf:
            for m in tf:
                rel = _tar_rel(m.name)
                if rel is None or m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                    raise PackageError("unsafe_path", m.name[:120])
                if m.isdir() or not rel.startswith(prefix):
                    continue
                src = tf.extractfile(m)
                write(rel[len(prefix):], src, m.mode)
    if info.get("git") and not (dest / ".release-rev").exists():
        (dest / ".release-rev").write_text(info["git"][:7] + "\n", encoding="utf-8")
        out.append(".release-rev")
    return sorted(out)


# ---------------------------------------------------------------- signatures (Ed25519)
def sig_message(sha256hex: str) -> bytes:
    return SIG_PREFIX + sha256hex.strip().lower().encode()


def _pem_pub(raw: bytes) -> bytes:
    b64 = base64.encodebytes(ED25519_SPKI + raw).decode().replace("\n", "")
    return f"-----BEGIN PUBLIC KEY-----\n{b64}\n-----END PUBLIC KEY-----\n".encode()


def load_pubkey(value: str) -> Optional[bytes]:
    """Raw 32-byte Ed25519 public key from PEM text, base64 / hex of the raw key, or the path of a file holding one of those."""
    v = (value or "").strip()
    if not v:
        return None
    if not v.startswith("-----") and len(v) < 4096 and ("/" in v or v.endswith(".pem") or v.endswith(".pub")) and Path(v).is_file():
        v = Path(v).read_text(encoding="utf-8").strip()
    if v.startswith("-----BEGIN"):
        body = "".join(ln for ln in v.splitlines() if not ln.startswith("-----"))
        der = base64.b64decode(body)
        if der.startswith(ED25519_SPKI) and len(der) == 44:
            return der[12:]
        raise ValueError("not an Ed25519 public key (PEM)")
    if re.fullmatch(r"[0-9a-fA-F]{64}", v):
        return bytes.fromhex(v)
    raw = base64.b64decode(v + "=" * (-len(v) % 4))
    if len(raw) == 44 and raw.startswith(ED25519_SPKI):
        raw = raw[12:]
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return raw


def configured_pubkey(install: Optional[Path] = None, env: Optional[dict] = None) -> Optional[bytes]:
    """The verification key: WATCHOVER_UPDATE_PUBKEY (environment or .env of the install folder), else update-pubkey.pem there."""
    env = os.environ if env is None else env
    val = env.get("WATCHOVER_UPDATE_PUBKEY", "")
    if not val and install is not None:
        val = dotenv(Path(install) / ".env").get("WATCHOVER_UPDATE_PUBKEY", "")
        if not val and (Path(install) / "update-pubkey.pem").is_file():
            val = str(Path(install) / "update-pubkey.pem")
    return load_pubkey(val) if val else None


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _openssl(args: List[str], data: Dict[str, bytes]) -> Tuple[int, bytes]:
    with tempfile.TemporaryDirectory() as d:
        for name, b in data.items():
            Path(d, name).write_bytes(b)
        r = subprocess.run(["openssl", *[a.replace("@", d + "/") for a in args]], capture_output=True, timeout=30)
        return r.returncode, r.stdout


def verify(pub: bytes, msg: bytes, sig_b64: str) -> bool:
    try:
        sig = base64.b64decode((sig_b64 or "").strip().split()[0] + "=" * (-len((sig_b64 or "").strip().split()[0]) % 4))
    except (ValueError, IndexError):
        return False
    if len(sig) != 64:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
            return True
        except InvalidSignature:
            return False
    except ImportError:
        rc, _ = _openssl(["pkeyutl", "-verify", "-pubin", "-inkey", "@pub.pem", "-rawin", "-in", "@msg", "-sigfile", "@sig"],
                         {"pub.pem": _pem_pub(pub), "msg": msg, "sig": sig})
        return rc == 0


def sign(private_pem: bytes, msg: bytes) -> str:
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(private_pem, password=None)
        return base64.b64encode(key.sign(msg)).decode()
    except ImportError:
        rc, out = _openssl(["pkeyutl", "-sign", "-inkey", "@key.pem", "-rawin", "-in", "@msg"], {"key.pem": private_pem, "msg": msg})
        if rc != 0:
            raise ValueError("openssl could not sign")
        return base64.b64encode(out).decode()


def keygen() -> Tuple[bytes, bytes]:
    """(private PEM, public PEM) of a new Ed25519 key pair."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.generate()
        priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        return priv, pub
    except ImportError:
        r = subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519"], capture_output=True, check=True, timeout=30)
        p = subprocess.run(["openssl", "pkey", "-pubout"], input=r.stdout, capture_output=True, check=True, timeout=30)
        return r.stdout, p.stdout


def sign_file(path: Path, private_pem: bytes) -> str:
    return sign(private_pem, sig_message(_sha256(Path(path))))


# ---------------------------------------------------------------- spool and small helpers
def spool_dir() -> Path:
    if os.environ.get("WATCHOVER_UPDATES_DIR"):
        return Path(os.environ["WATCHOVER_UPDATES_DIR"])
    from . import admin
    if admin.in_docker():
        return Path("/updates")
    from . import settings
    return settings.home() / "updates"


def mode() -> str:
    """openshift (Helm decides) | updater (the updater container shares /updates) | manual (Docker without it) | inprocess."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "openshift"
    from . import admin
    if admin.in_docker():
        return "updater" if spool_dir().is_dir() else "manual"
    return "inprocess"


def write_json(path: Path, obj, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    try:
        p = Path(path)
        if p.is_symlink():
            return default
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def dotenv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip()] = re.sub(r"\s+#.*$", "", v).strip()
    except OSError:
        pass
    return out


_SECRET = re.compile(r"(?i)((?:[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|_KEY|APIKEY|PASSWD)[A-Z0-9_]*)\s*[=:]\s*)(\S+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def redact(line: str) -> str:
    """A log line fit for the interface: colour codes dropped, NAME=value of anything that looks like a secret masked."""
    return _SECRET.sub(lambda m: m.group(1) + "•••", _ANSI.sub("", line))


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + os.urandom(3).hex()
