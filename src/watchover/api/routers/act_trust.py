"""/api/act/trust: the trust matrix and cell detail (docs/design/action-agent.md §7, §12.1). The trust engine itself is WP9;
until then cells are read as stored (every cell starts at its type's start level)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from ...act.types import level_label
from ..security import access, require
from .act_common import act_on

router = APIRouter(prefix="/act", tags=["act"])


def _visible(acc, row) -> bool:
    return acc.wide or (row.get("team_id") is not None and row["team_id"] in acc.team_ids("page.auto"))


@router.get("/trust")
def trust(team: int = None, runbook: str = "", env: str = "", svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    where, args = [], []
    if team is not None:
        where.append("team_id=?"); args.append(team)
    if runbook:
        where.append("runbook_key=?"); args.append(runbook)
    if env:
        where.append("env=?"); args.append(env)
    rows = svc.kb._exec("SELECT * FROM act_cells" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY cell_key LIMIT 2000",
                        tuple(args))
    return [dict(r, label=level_label(r["level"])) for r in rows if _visible(acc, r)]


@router.get("/trust/cell")
def cell(key: str, svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    rows = svc.kb._exec("SELECT * FROM act_cells WHERE cell_key=?", (key,))
    if not rows or not _visible(acc, rows[0]):
        raise KeyError(key)
    c = dict(rows[0])
    c["label"] = level_label(c["level"])
    c["events"] = svc.kb._exec("SELECT ts, outcome, credit, penalty, run_id FROM act_trust_events WHERE cell_key=? ORDER BY ts DESC LIMIT 50", (key,))
    c["binding"] = json.loads("[]")
    return c
