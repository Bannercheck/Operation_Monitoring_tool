"""Versioning and release notes.

    python -m watchover.release minor "Bildirimler sekmesi"        v1.0 -> v1.1   (a small update: one feature step)
    python -m watchover.release patch "MFA kodu hatası giderildi"  v1.1 -> v1.1.1 (a fix)
    python -m watchover.release major "V2: PostgreSQL"             v1.1 -> v2.0   (a big release, decided by the product owner)
    python -m watchover.release show                                current version and the latest notes

The version lives in src/watchover/__init__.py and pyproject.toml; every bump prepends an entry to CHANGELOG.md (date, version,
summary lines). --commit makes the commit and the git tag (v1.1), --push sends them. The System page shows the notes."""
from __future__ import annotations

import argparse
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


def bump(kind: str, summary: str, root: Path | None = None, when: date | None = None) -> str:
    """Write the new version into the package and pyproject, prepend the CHANGELOG entry; returns the new version."""
    root = root or ROOT
    init, pyproject, changelog = root / "src" / "watchover" / "__init__.py", root / "pyproject.toml", root / "CHANGELOG.md"
    old = current(root)
    new = next_version(old, kind)
    init.write_text(re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new}"', init.read_text(encoding="utf-8")), encoding="utf-8")
    if pyproject.exists():
        pyproject.write_text(re.sub(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{new}"', pyproject.read_text(encoding="utf-8"), count=1), encoding="utf-8")
    lines = [ln.strip(" -") for ln in summary.strip().splitlines() if ln.strip(" -")]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchover release", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=KINDS + ("show",))
    ap.add_argument("summary", nargs="?", default="")
    ap.add_argument("--commit", action="store_true", help="git commit the bump and tag it vX.Y[.Z]")
    ap.add_argument("--push", action="store_true", help="push the commit and the tag (implies --commit)")
    a = ap.parse_args(argv)
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
