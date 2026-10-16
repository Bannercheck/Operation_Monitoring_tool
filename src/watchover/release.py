"""Versioning and release notes.

    python -m watchover.release minor "Bildirimler sekmesi"        v1.0 -> v1.1   (a small update: one feature step)
    python -m watchover.release patch "MFA kodu hatası giderildi"  v1.1 -> v1.1.1 (a fix)
    python -m watchover.release major "V2: PostgreSQL"             v1.1 -> v2.0   (a big release, decided by the product owner)
    python -m watchover.release show                                current version and the latest notes

Packages for the in-app update (System › Güncelleme; updates.py checks them):
    python -m watchover.release keygen [--out DIR]                  Ed25519 pair: watchover-release.key (keep it off the servers) + .pub
    python -m watchover.release package [--out FILE] [--key KEY]    git archive zip of HEAD (watchover-<version>/), signed with --key
    python -m watchover.release sign FILE --key KEY                 FILE.sig next to a zip or a bundle (./watchover.sh bundle)
    python -m watchover.release verify FILE [--pub KEY]             check FILE.sig (default key: WATCHOVER_UPDATE_PUBKEY)
The servers get the public key: WATCHOVER_UPDATE_PUBKEY=<base64> in .env (or update-pubkey.pem in the install folder); from then on
unsigned or badly signed packages are refused.

The version lives in src/watchover/__init__.py and pyproject.toml; every bump prepends an entry to CHANGELOG.md (date, version,
summary lines). --commit makes the commit and the git tag (v1.1), --push sends them. The System page shows the notes."""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INIT = ROOT / "src" / "watchover" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
KINDS = ("major", "minor", "patch")


def current(root: Path | None = None) -> str:
    init = (root / "src" / "watchover" / "__init__.py") if root else INIT
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    return m.group(1) if m else "0.0.0"


def display(version: str) -> str:
    """1.0.0 -> v1.0, 1.1.0 -> v1.1, 1.1.2 -> v1.1.2"""
    parts = (version.split(".") + ["0", "0"])[:3]
    return f"v{parts[0]}.{parts[1]}" + (f".{parts[2]}" if parts[2] not in ("0", "") else "")


def next_version(version: str, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError("kind: major | minor | patch")
    a, b, c = (int(x) for x in (version.split(".") + ["0", "0"])[:3])
    return {"major": f"{a + 1}.0.0", "minor": f"{a}.{b + 1}.0", "patch": f"{a}.{b}.{c + 1}"}[kind]


def sync_chart(new: str, root: Path | None = None) -> None:
    """The OpenShift chart's appVersion is the default image tag, and the committed oc-apply manifest carries it in its version labels
    and image tags: both follow the release, so an OpenShift install never pulls the previous version."""
    root = root or ROOT
    chart, rendered = root / "deploy" / "openshift" / "watchover" / "Chart.yaml", root / "deploy" / "openshift" / "rendered" / "watchover.yaml"
    if not chart.exists():
        return
    text = chart.read_text(encoding="utf-8")
    m = re.search(r'(?m)^appVersion:\s*"?([^"\s]+)"?', text)
    prev = m.group(1) if m else ""
    chart.write_text(re.sub(r'(?m)^appVersion:.*$', f'appVersion: "{new}"', text, count=1), encoding="utf-8")
    if prev and prev != new and rendered.exists():
        r = rendered.read_text(encoding="utf-8")
        r = r.replace(f'app.kubernetes.io/version: "{prev}"', f'app.kubernetes.io/version: "{new}"')
        r = re.sub(rf'(image: \S+/watchover(?:-receiver)?):{re.escape(prev)}\b', rf'\g<1>:{new}', r)
        rendered.write_text(r, encoding="utf-8")


def bump(kind: str, summary: str, root: Path | None = None, when: date | None = None) -> str:
    """Write the new version into the package and pyproject, prepend the CHANGELOG entry; returns the new version."""
    root = root or ROOT
    init, pyproject, changelog = root / "src" / "watchover" / "__init__.py", root / "pyproject.toml", root / "CHANGELOG.md"
    old = current(root)
    new = next_version(old, kind)
    init.write_text(re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new}"', init.read_text(encoding="utf-8")), encoding="utf-8")
    if pyproject.exists():
        pyproject.write_text(re.sub(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{new}"', pyproject.read_text(encoding="utf-8"), count=1), encoding="utf-8")
    sync_chart(new, root)
    lines =[ln.strip(" -") for ln in summary.strip().splitlines() if ln.strip(" -")]
    entry = f"## {display(new)} · {(when or date.today()).isoformat()} · {'büyük sürüm' if kind == 'major' else ('düzeltme' if kind == 'patch' else 'güncelleme')}\n\n" + "".join(f"- {ln}\n" for ln in lines) + "\n"
    body = changelog.read_text(encoding="utf-8") if changelog.exists() else "# Watchover sürüm geçmişi\n\n"
    head, sep, rest = body.partition("\n## ")
    changelog.write_text(head.rstrip("\n") + "\n\n" + entry + (sep + rest if sep else ""), encoding="utf-8")
    return new


def entries(path: Path | None = None) -> list[dict]:
    """Parse CHANGELOG.md -> [{version, date, kind, lines}] newest first."""
    p = path or CHANGELOG
    if not p.exists():
        return []
    out: list[dict] = []
    for block in re.split(r"(?m)^## ", p.read_text(encoding="utf-8"))[1:]:
        title, _, body = block.partition("\n")
        parts = [x.strip() for x in title.split("·")]
        out.append({"version": parts[0], "date": parts[1] if len(parts) > 1 else "", "kind": parts[2] if len(parts) > 2 else "",
                    "lines": [ln.strip()[2:] for ln in body.splitlines() if ln.strip().startswith("- ")]})
    return out


def keygen(out_dir: Path) -> dict:
    """A new signing pair in out_dir; an existing private key is never overwritten."""
    from . import updates
    out_dir.mkdir(parents=True, exist_ok=True)
    key, pub = out_dir / "watchover-release.key", out_dir / "watchover-release.pub"
    if key.exists():
        raise FileExistsError(f"{key} exists; move it away first")
    priv_pem, pub_pem = updates.keygen()
    fd = os.open(str(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv_pem)
    pub.write_bytes(pub_pem)
    raw = updates.load_pubkey(pub_pem.decode())
    return {"key": str(key), "pub": str(pub), "pubkey": base64.b64encode(raw).decode(), "fingerprint": updates.fingerprint(raw)}


def sign_package(pkg: Path, key: Path) -> Path:
    from . import updates
    out = pkg.with_name(pkg.name + ".sig")
    out.write_text(updates.sign_file(pkg, key.read_bytes()) + "\n", encoding="utf-8")
    return out


def verify_package(pkg: Path, pub: str = "", sig: Path | None = None) -> bool:
    from . import updates
    raw = updates.load_pubkey(pub or os.environ.get("WATCHOVER_UPDATE_PUBKEY", ""))
    if raw is None:
        raise ValueError("no public key: --pub or WATCHOVER_UPDATE_PUBKEY")
    sig = sig or pkg.with_name(pkg.name + ".sig")
    digest = updates._sha256(pkg)
    return updates.verify(raw, updates.sig_message(digest), sig.read_text(encoding="utf-8") if sig.exists() else "")


def package(out: Path | None = None, key: Path | None = None, root: Path | None = None) -> Path:
    """The source zip owners receive (git archive of HEAD under watchover-<version>/), signed when a key is given."""
    root = root or ROOT
    name = f"watchover-{current(root)}"
    out = out or root / f"{name}.zip"
    subprocess.run(["git", "-C", str(root), "archive", "--format=zip", f"--prefix={name}/", "-o", str(out), "HEAD"], check=True)
    if key:
        sign_package(out, key)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchover release", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=KINDS + ("show", "keygen", "package", "sign", "verify"))
    ap.add_argument("summary", nargs="?", default="", help="the summary of a bump, or the package file of sign / verify")
    ap.add_argument("--key", help="private key (sign, package)")
    ap.add_argument("--pub", default="", help="public key for verify: base64, PEM or a file (default WATCHOVER_UPDATE_PUBKEY)")
    ap.add_argument("--out", help="keygen: folder (default .); package: zip file")
    ap.add_argument("--commit", action="store_true", help="git commit the bump and tag it vX.Y[.Z]")
    ap.add_argument("--push", action="store_true", help="push the commit and the tag (implies --commit)")
    a = ap.parse_args(argv)
    if a.kind == "keygen":
        r = keygen(Path(a.out or "."))
        print(f"private key: {r['key']} (keep it on the build machine, never on a server)\npublic key:  {r['pub']}\n"
              f"on every server, in .env:\n  WATCHOVER_UPDATE_PUBKEY={r['pubkey']}\nfingerprint: {r['fingerprint']}")
        return 0
    if a.kind == "package":
        out = package(Path(a.out) if a.out else None, Path(a.key) if a.key else None)
        print(f"{out}" + (f"  +  {out.name}.sig" if a.key else "  (unsigned: --key KEY signs it)"))
        return 0
    if a.kind in ("sign", "verify"):
        if not a.summary or not Path(a.summary).is_file():
            print("a package file is required", file=sys.stderr); return 2
        if a.kind == "sign":
            if not a.key:
                print("--key is required", file=sys.stderr); return 2
            print(sign_package(Path(a.summary), Path(a.key)))
            return 0
        ok = verify_package(Path(a.summary), a.pub)
        print("signature ok" if ok else "signature BAD or missing")
        return 0 if ok else 1
    if a.kind == "show":
        e = entries()
        print(f"Watchover {display(current())}" + (f"  ·  {e[0]['date']}  ·  " + "; ".join(e[0]["lines"][:4]) if e else ""))
        return 0
    if not a.summary:
        print("a summary line is required", file=sys.stderr); return 2
    new = bump(a.kind, a.summary)
    tag = display(new)
    print(f"{display(current())} → {tag}")
    if a.commit or a.push:
        subprocess.run(["git", "-C", str(ROOT), "add", str(INIT), str(PYPROJECT), str(CHANGELOG)], check=True)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", f"{tag}: {a.summary.strip().splitlines()[0]}"], check=True)
        subprocess.run(["git", "-C", str(ROOT), "tag", "-a", tag, "-m", a.summary.strip()], check=True)
        print(f"committed and tagged {tag}")
    if a.push:
        subprocess.run(["git", "-C", str(ROOT), "push", "-q", "origin", "HEAD", tag], check=True)
        print("pushed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
