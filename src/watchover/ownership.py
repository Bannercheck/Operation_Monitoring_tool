"""Team ownership of records: a nullable `team_id` column (NULL = installation-wide) on the tables that hold team-owned objects,
and small helpers the routers use to filter lists and to check one record before a read or a write.

Datasets live in memory (the registry), so their team sits on the registry item (and on the load job until the dataset exists)."""
from __future__ import annotations

import json
import threading
from typing import Optional

TEAM_TABLES = ("threshold_policies", "recipients", "notify_groups", "notify_rules", "alerts", "agents", "sources", "biz_flows", "lessons", "rules", "scan_runs")
from .act.store import TEAM_OWNED as _ACT_TEAM_TABLES     # noqa: E402 - the action agent's team-owned tables (§10.9)
TEAM_TABLES = TEAM_TABLES + _ACT_TEAM_TABLES
_LOCK = threading.Lock()
_TL = threading.local()


def ensure_col(db, table: str, col: str = "team_id", ddl: str = "INTEGER") -> None:
    try:
        if table in db.tables() and col not in db.columns(table):
            db._exec(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    except Exception:  # noqa: BLE001 - another process added it first
        pass


def ensure_all(svc) -> None:
    """The team_id columns (startup, idempotent; works on SQLite and PostgreSQL)."""
    for t in TEAM_TABLES:
        ensure_col(svc.kb, t)
    try:
        ensure_col(svc.actions.db, "actions")
        ensure_col(svc.actions.db, "actions", "host", "TEXT DEFAULT ''")
    except Exception:  # noqa: BLE001
        pass


def team_of(db, table: str, rid) -> Optional[int]:
    rows = db._exec(f"SELECT team_id FROM {table} WHERE id=?", (rid,))
    if not rows:
        raise KeyError(rid)
    return rows[0].get("team_id")


def team_map(db, table: str) -> dict:
    try:
        return {r["id"]: r.get("team_id") for r in db._exec(f"SELECT id, team_id FROM {table}")}
    except Exception:  # noqa: BLE001
        return {}


def set_team(db, table: str, rid, team_id: Optional[int]) -> None:
    db._exec(f"UPDATE {table} SET team_id=? WHERE id=?", (team_id, rid))


# ---------------------------------------------------------------- datasets (in memory)
def dataset_team(svc, key: str) -> Optional[int]:
    """The owning team of a loaded dataset (None: installation-wide). A dataset loaded by a background job takes the job's team."""
    d = svc.datasets.items.get(key)
    if d is None:
        raise KeyError(key)
    if "team_id" not in d:
        job = next((j for j in list(svc.datasets.jobs.values()) if j.get("dataset") == key), None)
        d["team_id"] = job.get("team_id") if job else None
    return d.get("team_id")


def submit(svc, team_id: Optional[int], name: str, data: bytes, mapping: Optional[dict] = None, demo: bool = False) -> dict:
    """DatasetRegistry.submit with an owning team, race-free: the load thread reads the job's team under the same lock. demo=True:
    demo content (a shipped sample set, a live snapshot with simulator lines): never learned from, removed with the demo data."""
    with _LOCK:
        job = svc.datasets.submit(name, data, mapping, demo=demo)
        svc.datasets.jobs[job["id"]]["team_id"] = team_id
    return {**job, "team_id": team_id}


def load_path(svc, team_id: Optional[int], path: str, mapping: Optional[dict] = None, demo: bool = False) -> dict:
    _TL.team = team_id
    try:
        out = svc.datasets.load_path(path, mapping, demo=demo)
    finally:
        _TL.team = None
    return {**out, "team_id": team_id}


def _wrap_register(svc) -> None:
    """Every dataset gets its team on registration (before anyone can list it), and the rows the registration writes for it
    (pattern lessons, first actions) are stamped with the same team."""
    reg = svc.datasets
    if getattr(reg, "_team_wrapped", False):
        return
    orig = reg.register

    def register(name, analysis, prof, elapsed, size, mapping, demo=False):
        team = getattr(_TL, "team", None)
        tn = threading.current_thread().name
        if team is None and tn.startswith("api-load-"):
            with _LOCK:
                job = reg.jobs.get(tn[len("api-load-"):])
                team = job.get("team_id") if job else None
        key = orig(name, analysis, prof, elapsed, size, mapping, demo=demo)
        if team is not None:
            reg.items[key]["team_id"] = team
            stamp_dataset_rows(svc, name, team)
        return key
    reg.register = register
    reg._team_wrapped = True


def stamp_dataset_rows(svc, name: str, team: int) -> None:
    try:
        svc.kb._exec("UPDATE lessons SET team_id=? WHERE team_id IS NULL AND dataset=?", (team, name))
    except Exception:  # noqa: BLE001
        pass
    try:
        svc.actions.db._exec("UPDATE actions SET team_id=? WHERE team_id IS NULL AND evidence LIKE ?", (team, f"first:{name}:%"))
    except Exception:  # noqa: BLE001
        pass


def scoped_jsonl(data: bytes, allow) -> bytes:
    """Only the JSONL lines whose host the scope allows (a team's snapshot of the live buffer)."""
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            if allow(json.loads(line).get("host")):
                out.append(line)
        except ValueError:
            continue
    return ("\n".join(out) + "\n").encode() if out else b""


# ---------------------------------------------------------------- knowledge base rows (lessons) for a team-scoped reader
def lesson_filter(svc, acc, sc):
    """None (everything) for installation-wide readers; else a predicate: a team's row only for that team, a host-bound memory
    (anomaly) only when the host is in scope, a resolution like its action, and shared rows (notes / runbooks / patterns of
    installation-wide users) for everyone."""
    if acc.wide:
        return None
    teams = set(acc.team_ids("page.assist"))
    act_sc = acc.scope("page.actions")
    act_teams = set(acc.team_ids("page.actions"))
    cache: dict = {}

    def vis(r: dict) -> bool:
        t = r.get("team_id")
        if t is not None:
            return t in teams
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        h = meta.get("host")
        if h:
            return sc is not None and sc.allows(h)
        aid = meta.get("action_id")
        if r.get("kind") == "resolution" and aid:
            if aid not in cache:
                try:
                    a = svc.actions.get(int(aid))
                    cache[aid] = (a.get("team_id") in act_teams) if a.get("team_id") is not None else bool(a.get("host") and act_sc is not None and act_sc.allows(a["host"]))
                except Exception:  # noqa: BLE001
                    cache[aid] = False
            return cache[aid]
        return True
    return vis


def rule_filter(acc):
    if acc.wide:
        return None
    teams = set(acc.team_ids("page.assist"))
    return lambda r: r.get("team_id") is None or r.get("team_id") in teams


class ScopedKB:
    """The knowledge base as one team-scoped reader sees it (search / all / rules / get / related filtered); writes pass through."""
    def __init__(self, kb, vis, rvis=None):
        self._kb, self._vis, self._rvis = kb, vis, rvis

    def __getattr__(self, name):
        return getattr(self._kb, name)

    def search(self, query: str, k: int = 6, kinds=None) -> list:
        return [r for r in self._kb.search(query, max(k * 8, 40), kinds) if self._vis(r)][:k]

    def all(self, kind=None, limit: int = 500) -> list:
        return [r for r in self._kb.all(kind, max(limit * 4, 200)) if self._vis(r)][:limit]

    def get(self, lid):
        r = self._kb.get(lid)
        return r if r is not None and self._vis(r) else None

    def rules(self, status=None) -> list:
        rows = self._kb.rules(status)
        return rows if self._rvis is None else [r for r in rows if self._rvis(r)]

    def related(self, inc, root, k: int = 4) -> list:
        out = self.search(f"{inc.title} {root.template} {' '.join(inc.affected_services)}", k + 1)
        return [d for d in out if not (d["kind"] == "pattern" and d.get("root_cause") == root.template)][:k]


def kb_for(svc, acc, sc):
    vis = lesson_filter(svc, acc, sc)
    return svc.kb if vis is None else ScopedKB(svc.kb, vis, rule_filter(acc))


def attach_owned_hooks(svc) -> None:
    """Datasets and actions count as team-owned records when a team is deleted (and move with a transfer)."""
    ensure_all(svc)
    _wrap_register(svc)

    def ds_count(tid):
        n = 0
        for k in list(svc.datasets.items):
            try:
                n += dataset_team(svc, k) == tid
            except KeyError:
                pass
        return n

    def ds_move(tid, to):
        for k in list(svc.datasets.items):
            try:
                if dataset_team(svc, k) == tid:
                    svc.datasets.items[k]["team_id"] = to
            except KeyError:
                pass

    def act_count(tid):
        return int(svc.actions.db._exec("SELECT COUNT(*) AS n FROM actions WHERE team_id=?", (tid,))[0]["n"])

    def act_move(tid, to):
        svc.actions.db._exec("UPDATE actions SET team_id=? WHERE team_id=?", (to, tid))
    svc.teams.owned_hooks["datasets"] = (ds_count, ds_move)
    svc.teams.owned_hooks["actions"] = (act_count, act_move)

    def auto_count(tid):                     # action agent: runbooks, cells, windows, stops, on-call, ... (§10.9)
        n = 0
        for t in _ACT_TEAM_TABLES:
            if t in ("act_trust_events", "act_examples", "act_metrics_weekly"):
                continue                     # history moves with its cells; it does not block a delete on its own
            try:
                n += int(svc.kb._exec(f"SELECT COUNT(*) AS n FROM {t} WHERE team_id=?", (tid,))[0]["n"])
            except Exception:  # noqa: BLE001 - tables not created yet
                pass
        return n

    def auto_move(tid, to):
        old, new = f"t:{int(tid)}/", "t:*/" if to is None else f"t:{int(to)}/"
        for t in _ACT_TEAM_TABLES:
            try:
                svc.kb._exec(f"UPDATE {t} SET team_id=? WHERE team_id=?", (to, tid))
            except Exception:  # noqa: BLE001
                pass
        for t, col in (("act_cells", "cell_key"), ("act_trust_events", "cell_key"), ("act_promotions", "cell_key"),
                       ("act_level_events", "cell_key")):
            try:                                 # cell keys carry the team key: rekey them with the move
                for r in svc.kb._exec(f"SELECT DISTINCT {col} AS k FROM {t} WHERE {col} LIKE ?", (old + "%",)):
                    svc.kb._exec(f"UPDATE {t} SET {col}=? WHERE {col}=?", (new + r["k"][len(old):], r["k"]))
            except Exception:  # noqa: BLE001
                pass
    svc.teams.owned_hooks["automation"] = (auto_count, auto_move)
