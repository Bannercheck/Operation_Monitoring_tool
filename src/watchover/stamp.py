"""Reproducibility stamps. Same input id + same engine id => same result id, on any machine.

Why: two machines showed different incident counts for "the same" dataset. The engine is deterministic
(no clock, no hash-order dependence, no locale), so a difference can only come from a different code copy
(stale non-editable install, older zip) or a different input set (a file missing, another version). These ids make
that visible in the sidebar, in the dataset caption, in the CLI output and in the enrichment summary.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import __version__

PKG = Path(__file__).resolve().parent


from functools import lru_cache


@lru_cache(maxsize=1)
def engine_id() -> str:
    """sha256 over every .py file of the package (sorted) -> 10 hex. Changes whenever any engine rule changes."""
    h = hashlib.sha256()
    for p in sorted(PKG.rglob("*.py")):
        h.update(str(p.relative_to(PKG)).encode()); h.update(p.read_bytes())
    return h.hexdigest()[:10]


@lru_cache(maxsize=1)
def scenario_id() -> str:
    """sha256 of scenario/__init__.py (the knobs) -> 8 hex."""
    return hashlib.sha256((PKG / "scenario" / "__init__.py").read_bytes()).hexdigest()[:8]


@lru_cache(maxsize=1)
def git_rev() -> str:
    """Short git revision of the checkout the package lives in, without spawning git ("-" when not a checkout).
    A container has no .git: the build bakes the revision into WATCHOVER_GIT_REV."""
    import os
    baked = os.environ.get("WATCHOVER_GIT_REV", "").strip()
    if baked and baked != "-":
        return baked[:7]
    for root in (PKG.parent.parent, PKG.parent):
        head = root / ".git" / "HEAD"
        if head.exists():
            ref = head.read_text().strip()
            if ref.startswith("ref: "):
                f = root / ".git" / ref[5:]
                if f.exists():
                    return f.read_text().strip()[:7]
                packed = root / ".git" / "packed-refs"
                if packed.exists():
                    for ln in packed.read_text().splitlines():
                        if ln.endswith(" " + ref[5:]):
                            return ln.split()[0][:7]
                return "-"
            return ref[:7]
    return "-"


def file_id(name: str, text: str) -> str:
    """Content id of one input file: base name + bytes, independent of folder, path separator and line endings."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(base.encode() + b"\0" + body.encode("utf-8")).hexdigest()[:10]


def input_id(report: list[dict]) -> str:
    """Id of the whole input set: sorted per-file ids (order of upload does not matter)."""
    ids = sorted(str(r.get("sha", "")) for r in report)
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:10]


def result_id(analysis) -> str:
    """Id of the outcome: incidents (id, title, span, signals, alarms, score) + funnel."""
    rows = [[i.id, i.title, i.started_at.isoformat(), i.ended_at.isoformat(), len(i.signal_ids),
             sum(analysis.signal_by_id[s].count for s in i.signal_ids), round(i.score, 4), i.severity] for i in analysis.incidents]
    return hashlib.sha256(json.dumps([rows, analysis.funnel()], sort_keys=True, default=str).encode()).hexdigest()[:10]


def stamp(analysis=None) -> dict:
    out = {"version": __version__, "engine": engine_id(), "scenario": scenario_id(), "git": git_rev(), "package": str(PKG)}
    if analysis is not None:
        out["input"] = input_id(analysis.report)
        out["result"] = result_id(analysis)
    return out


def stale_package(app_file: str) -> str | None:
    """Path of the imported package when it is NOT the repo's src/ copy next to app_file (a stale pip install)."""
    src = Path(app_file).resolve().with_name("src") / "watchover"
    return None if not src.exists() or PKG == src.resolve() else str(PKG)
