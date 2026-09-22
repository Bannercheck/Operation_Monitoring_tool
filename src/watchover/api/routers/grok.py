"""Parser patterns: the grok library (built-in packs + user patterns), a test box, and the lines a dataset could not parse."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import grok
from ..security import require, services

router = APIRouter(prefix="/grok", tags=["grok"])
CORPUS = Path(__file__).resolve().parents[4] / "samples" / "corpus"
ROLES = ("timestamp", "severity", "host", "service", "message", "status")


class PatternIn(BaseModel):
    name: str
    pattern: str
    family: str = ""
    roles: dict = {}
    description: str = ""
    enabled: bool = True


class PatternPatch(BaseModel):
    name: str | None = None
    pattern: str | None = None
    family: str | None = None
    roles: dict | None = None
    description: str | None = None
    enabled: bool | None = None


class TestIn(BaseModel):
    pattern: str
    sample: str


def _validate(p: PatternIn | PatternPatch) -> None:
    if p.pattern is not None:
        try:
            grok.compile_pattern(p.pattern)
        except (re.error, ValueError) as e:
            raise ValueError(f"pattern does not compile: {e}")
    if p.roles:
        bad = [r for r in p.roles if r not in ROLES]
        if bad:
            raise ValueError(f"unknown roles: {', '.join(bad)}; allowed: {', '.join(ROLES)}")


@router.get("/base")
def base(user=Depends(require("page.parser"))):
    """Building blocks users can reference in their own patterns."""
    return [{"name": k, "pattern": v} for k, v in grok.BASE.items()]


@router.get("")
def patterns(user=Depends(require("page.parser"))):
    """User patterns first, then the built-in packs, each with how much of the corpus it matches."""
    out = []
    for lp in [*grok.user_patterns(), *grok.LINES]:
        d = lp.as_dict()
        d["corpus"] = grok.coverage_of(lp, CORPUS) if CORPUS.exists() else {"files": 0, "lines": 0}
        out.append(d)
    return out


@router.post("", status_code=201)
def add(body: PatternIn, user=Depends(require("act.parser"))):
    _validate(body)
    items = grok.user_patterns()
    if any(x.name == body.name for x in items) or any(x.name == body.name for x in grok.LINES):
        raise ValueError("a pattern with this name already exists")
    lp = grok.LinePattern(body.name.strip(), body.pattern, body.family.strip(), body.roles, body.description, False, body.enabled, uuid.uuid4().hex[:12])
    grok.save_user_patterns([*items, lp])
    return lp.as_dict()


@router.patch("/{pid}")
def update(pid: str, body: PatternPatch, user=Depends(require("act.parser"))):
    _validate(body)
    items = grok.user_patterns()
    for x in items:
        if x.id == pid:
            for k, v in body.model_dump(exclude_none=True).items():
                setattr(x, k, v)
            grok.save_user_patterns(items)
            return x.as_dict()
    if any(x.name == pid for x in grok.LINES):                       # built-in packs: only the switch can change
        if body.enabled is not None:
            for x in grok.LINES:
                if x.name == pid:
                    x.enabled = body.enabled
                    return x.as_dict()
        raise ValueError("built-in patterns cannot be edited; copy it as a new pattern")
    raise KeyError(pid)


@router.delete("/{pid}")
def delete(pid: str, user=Depends(require("act.parser"))):
    items = grok.user_patterns()
    keep = [x for x in items if x.id != pid]
    if len(keep) == len(items):
        raise KeyError(pid)
    grok.save_user_patterns(keep)
    return {"ok": True}


@router.post("/test")
def test(body: TestIn, user=Depends(require("page.parser"))):
    return grok.test_pattern(body.pattern, body.sample)


@router.get("/unparsed/{key}")
def unparsed(key: str, limit: int = 200, user=Depends(require("page.parser")), svc=Depends(services)):
    """Lines of a dataset the parser understood least: no timestamp and no explicit level, grouped by their template."""
    a = svc.datasets.get(key)["analysis"]
    groups: dict[str, dict] = {}
    for o in a.observations:
        at = o.attributes
        if not at.get("_no_ts") and at.get("_sev_src", "field") in ("field", "text"):
            continue
        g = groups.setdefault(o.template or o.message[:80], {"template": o.template or o.message[:80], "count": 0, "sample": (o.raw or o.message)[:300], "source": o.source, "parser": o.parser, "no_ts": 0, "no_level": 0})
        g["count"] += 1
        g["no_ts"] += 1 if at.get("_no_ts") else 0
        g["no_level"] += 1 if at.get("_sev_src", "field") not in ("field", "text") else 0
    rows = sorted(groups.values(), key=lambda g: -g["count"])[:limit]
    return {"total": len(a.observations), "unparsed": sum(g["count"] for g in groups.values()), "groups": rows}
