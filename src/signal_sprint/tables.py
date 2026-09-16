"""Reference tables that travel with an event dataset: service dependencies and host inventory.

They are never events. The pipeline keeps them in the ingest report (kind="table") and the engine reads
them from there: dependencies drive correlation and root-cause ranking, the inventory enriches hosts.
"""
from __future__ import annotations

import csv
import io
import json
import re

from . import scenario


def is_side_table(fname: str) -> bool:
    base = fname.rsplit("/", 1)[-1].lower()
    return any(re.search(p, base) for p in scenario.SIDE_TABLES)


def read_table(fname: str, text: str) -> list[dict]:
    """CSV / TSV / JSON list -> list of dict rows (markdown and other text -> [])."""
    t = text.lstrip()
    if not t:
        return []
    if t.startswith("[") or t.startswith("{"):
        try:
            data = json.loads(t)
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            return [r for r in data if isinstance(r, dict)]
        except Exception:
            return []
    if fname.lower().endswith((".md", ".txt")):
        return []
    try:
        dialect = csv.Sniffer().sniff(t[:2000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(t), dialect=dialect))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def _col(row: dict, names: list[str]) -> str | None:
    low = {k.lower(): k for k in row}
    for n in names:
        if n in low:
            return low[n]
    return None


def dependencies(report: list[dict]) -> list[dict]:
    """[{source, target, type, criticality}] where source depends on target (target breaks -> source suffers)."""
    out: list[dict] = []
    for entry in report:
        rows = entry.get("table") or []
        if not rows:
            continue
        c = scenario.DEP_COLUMNS
        src, dst = _col(rows[0], c["source"]), _col(rows[0], c["target"])
        if not src or not dst:
            continue
        typ, crit = _col(rows[0], c["type"]), _col(rows[0], c["criticality"])
        for r in rows:
            if r.get(src) and r.get(dst):
                out.append({"source": str(r[src]).strip(), "target": str(r[dst]).strip(),
                            "type": str(r.get(typ, "") or "").strip() if typ else "", "criticality": str(r.get(crit, "") or "").strip() if crit else ""})
    return out


def inventory(report: list[dict]) -> dict[str, dict]:
    """host -> {service, dc, rack, env, criticality}"""
    out: dict[str, dict] = {}
    for entry in report:
        rows = entry.get("table") or []
        if not rows:
            continue
        c = scenario.INVENTORY_COLUMNS
        hcol = _col(rows[0], c["host"])
        if not hcol or _col(rows[0], c["service"]) is None:
            continue
        cols = {k: _col(rows[0], v) for k, v in c.items()}
        for r in rows:
            h = str(r.get(hcol, "")).strip()
            if h:
                out[h] = {k: str(r.get(col, "") or "").strip() for k, col in cols.items() if col and k != "host"}
    return out


def from_observations(observations) -> tuple[list[dict], dict[str, dict]]:
    """Enriched single-file datasets carry `depends_on` / `dependents` and inventory columns per row:
    rebuild the dependency list and the host inventory from them (used when the package has no side tables)."""
    deps: dict[tuple[str, str], dict] = {}
    inv: dict[str, dict] = {}
    for o in observations:
        a = o.attributes
        svc = o.service
        crit = {}
        for part in str(a.get("dependency_criticality") or "").split(","):
            if ":" in part:
                t, c = part.split(":", 1)
                crit[t.strip()] = c.strip()
        if svc and a.get("depends_on"):
            for t in str(a["depends_on"]).split(","):
                t = t.strip()
                if t:
                    deps.setdefault((svc, t), {"source": svc, "target": t, "type": "", "criticality": crit.get(t, "")})
        if svc and a.get("dependents"):
            for s_ in str(a["dependents"]).split(","):
                s_ = s_.strip()
                if s_:
                    deps.setdefault((s_, svc), {"source": s_, "target": svc, "type": "", "criticality": ""})
        if o.host and o.host not in inv and (a.get("is_kritikligi") or a.get("inventory_service") or a.get("kabin") or a.get("tags.kabin")):
            inv[o.host] = {"service": str(a.get("inventory_service") or svc or ""), "dc": str(a.get("tags.veri_merkezi") or a.get("veri_merkezi") or ""),
                           "rack": str(a.get("tags.kabin") or a.get("kabin") or ""), "env": str(a.get("tags.ortam") or a.get("ortam") or ""),
                           "criticality": str(a.get("is_kritikligi") or "")}
    return list(deps.values()), inv
