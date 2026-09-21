"""Playbook / error library: every error pattern seen across datasets, where and when it happened, how it resolved,
and the team's runbook notes. Persisted in the shared database (PostgreSQL, or SQLite on a laptop) so knowledge survives across
sessions and datasets.

    pb = Playbook(db)                     # a shared Database, or a SQLite path for a standalone file
    pb.record(analysis, dataset)          # called after every analysis: upserts root-cause and ERROR+ templates
    pb.lookup(template)                   # exact or fuzzy (token overlap) match -> entry dict or None
    pb.set_notes(key, resolution, runbook)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .db import Database
from .models import SEV_RANK
from . import scenario

UTC = timezone.utc
TOKEN_RE = re.compile(r"[a-z][a-z0-9_\-]{2,}")


def _tokens(template: str) -> set[str]:
    return {w for w in TOKEN_RE.findall(template.lower()) if w not in ("path", "uuid", "hex", "url")}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


class Playbook:
    def __init__(self, db: "Database | str" = "playbook.db"):
        self.db = db if isinstance(db, Database) else Database(db)
        self.db._exec("""CREATE TABLE IF NOT EXISTS playbook (
            key TEXT PRIMARY KEY, template TEXT, severity TEXT, title TEXT,
            first_seen TEXT, last_seen TEXT, occurrences INTEGER DEFAULT 0, events INTEGER DEFAULT 0,
            datasets TEXT DEFAULT '[]', services TEXT DEFAULT '[]', hosts TEXT DEFAULT '[]',
            recoveries TEXT DEFAULT '{}', root_cause_count INTEGER DEFAULT 0,
            resolution TEXT DEFAULT '', runbook TEXT DEFAULT '', updated_at TEXT)""")

    # ---- write
    def record(self, analysis, dataset: str) -> int:
        """Upsert one entry per ERROR+ / bursting signal template; root-cause signals also carry the incident's recovery."""
        root_recovery = {}
        for inc in analysis.incidents:
            root_recovery[analysis.signal_by_id[inc.root_cause_signal].template] = inc.recovery.get("kind", "unknown")
        n = 0
        for s in analysis.signals:
            if SEV_RANK[s.severity] < 3 and s.burst_score < 0.5 and s.template not in root_recovery:
                continue
            self._upsert(s, dataset, root_recovery.get(s.template))
            n += 1
        return n

    def _upsert(self, s, dataset: str, recovery: str | None) -> None:
        rows = self.db._exec("SELECT * FROM playbook WHERE key=?", (s.template,))
        row = rows[0] if rows else None
        now = datetime.now(UTC).isoformat(timespec="seconds")
        first, last = s.first_seen.isoformat(), s.last_seen.isoformat()
        if row is None:
            recs = {recovery: 1} if recovery else {}
            runbook = "\n".join(f"- {r}" for k, lst in scenario.RECOMMENDATIONS.items() if k in s.template for r in lst)
            self.db._exec("INSERT INTO playbook VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (s.template, s.template, s.severity, s.template[:80], first, last, 1, s.count, json.dumps([dataset]),
                               json.dumps(s.services), json.dumps(s.hosts), json.dumps(recs), 1 if recovery else 0, "", runbook, now))
        else:
            datasets = json.loads(row["datasets"])
            already = dataset in datasets
            if not already:
                datasets.append(dataset)
            recs = json.loads(row["recoveries"])
            if recovery and not already:
                recs[recovery] = recs.get(recovery, 0) + 1
            services = sorted(set(json.loads(row["services"])) | set(s.services))
            hosts = sorted(set(json.loads(row["hosts"])) | set(s.hosts))
            self.db._exec("""UPDATE playbook SET last_seen=?, first_seen=?,
                                 occurrences=occurrences + ?, events=events + ?, datasets=?, services=?, hosts=?, recoveries=?,
                                 root_cause_count=root_cause_count + ?, updated_at=? WHERE key=?""",
                              (max(row["last_seen"] or "", last), min(row["first_seen"] or first, first), 0 if already else 1, 0 if already else s.count, json.dumps(datasets), json.dumps(services),
                               json.dumps(hosts), json.dumps(recs), 1 if (recovery and not already) else 0, now, s.template))

    def set_notes(self, key: str, resolution: str | None = None, runbook: str | None = None) -> dict:
        fields = {k: v for k, v in (("resolution", resolution), ("runbook", runbook)) if v is not None}
        fields["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        self.db._exec(f"UPDATE playbook SET {', '.join(f'{k}=?' for k in fields)} WHERE key=?", (*fields.values(), key))
        return self.get(key)

    def delete(self, key: str) -> None:
        self.db._exec("DELETE FROM playbook WHERE key=?", (key,))

    # ---- read
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for k in ("datasets", "services", "hosts", "recoveries"):
            d[k] = json.loads(d[k])
        return d

    def get(self, key: str) -> dict | None:
        rows = self.db._exec("SELECT * FROM playbook WHERE key=?", (key,))
        return self._row(rows[0]) if rows else None

    def all(self, q: str = "") -> list[dict]:
        rows = [self._row(r) for r in self.db._exec("SELECT * FROM playbook ORDER BY occurrences DESC, last_seen DESC")]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in r["template"] or ql in r["resolution"].lower() or ql in r["runbook"].lower()
                    or any(ql in x.lower() for x in r["services"] + r["datasets"])]
        return rows

    def lookup(self, template: str, threshold: float = 0.6) -> dict | None:
        """Exact template match, else the most similar entry above the token-overlap threshold."""
        exact = self.get(template)
        if exact:
            return exact
        best, best_sim = None, 0.0
        for r in self.all():
            sim = similarity(template, r["template"])
            if sim > best_sim:
                best, best_sim = r, sim
        if best and best_sim >= threshold:
            best = dict(best, similarity=round(best_sim, 2))
            return best
        return None
