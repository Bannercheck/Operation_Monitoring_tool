"""Corporate memory: the knowledge base fed continuously by what the system sees, so every answer, review and
incident card can lean on what happened before in *this* company.

Feeds (all deterministic, all deduplicated):
  * **live windows** — every `learn_min` minutes the engine analyses the recent live feed; each root-cause pattern becomes
    (or bumps) a `pattern` lesson (`autolearn.LiveLearner`).
  * **datasets** — every uploaded set does the same at load time (`DatasetRegistry.register`).
  * **anomalies** — a newly opened anomaly (error storm, silence, metric drift, threshold breach) becomes an `anomaly`
    memory keyed by its anomaly key; repeats bump occurrences.
  * **resolutions** — an action closed by an operator becomes a `resolution` memory (what was done, for which incident).
  * **people** — notes, runbooks, feedback and saved chats (Knowledge page) as before.

Vectors: the configured embedding model (bge-m3 …) when there is one, else the deterministic hash embedding, so semantic
search always works offline; `reindex()` re-embeds rows in the background after a model is (re)configured.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .autolearn import LiveLearner

UTC = timezone.utc


class Memory:
    def __init__(self, kb, live, lang: str = "tr", interval_min: int = 15, inventory_fn=None):
        self.kb, self.lang = kb, lang
        self.learner = LiveLearner(live, kb, interval_min=interval_min, lang=lang, inventory_fn=inventory_fn)
        self.reindex_state: dict = {"running": False, "model": "", "done": 0, "remaining": 0, "error": "", "ts": ""}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- feeds
    def remember_anomaly(self, rec: dict) -> int | None:
        try:
            text = (f"{rec.get('detail', '')}\nkind: {rec.get('kind', '')} · env: {rec.get('env', '')} · host: {rec.get('host', '')}"
                    f" · service: {rec.get('service', '') or '-'} · metric: {rec.get('metric', '') or '-'}\n"
                    f"observed: {rec.get('observed', 0)} · baseline: {rec.get('baseline', 0)} · score: {rec.get('score', 0)}\n"
                    f"first seen: {rec.get('first_seen', '')}")
            return self.kb.add_event("anomaly", f"anomaly:{rec.get('key', '')}", str(rec.get("title", ""))[:160], text,
                                     services=[s for s in [rec.get("service", "")] if s], tags=["auto", "anomaly", str(rec.get("kind", ""))],
                                     meta={"anomaly_id": rec.get("id"), "host": rec.get("host", ""), "env": rec.get("env", ""), "metric": rec.get("metric", "")})
        except Exception:  # noqa: BLE001 - memory never breaks the tracker
            return None

    def remember_action(self, action: dict) -> int | None:
        """A closed action: what was done for which incident, by whom. Open actions are not memories yet."""
        if str(action.get("status", "")) not in ("done", "closed"):
            return None
        try:
            text = (f"Action: {action.get('title', '')}\nIncident: {action.get('incident_id', '') or '-'} · priority: {action.get('priority', '')}"
                    f" · owner: {action.get('owner', '') or '-'} · status: {action.get('status', '')}\n"
                    f"Recommendation: {action.get('recommendation', '') or '-'}\nEvidence: {str(action.get('evidence', '') or '-')[:400]}\n"
                    f"Closed: {action.get('updated_at', '')}")
            return self.kb.add_event("resolution", f"action:{action.get('id')}", str(action.get("title", ""))[:160], text,
                                     tags=["auto", "resolution", str(action.get("priority", ""))],
                                     meta={"action_id": action.get("id"), "incident_id": action.get("incident_id", ""), "owner": action.get("owner", "")})
        except Exception:  # noqa: BLE001
            return None

    def learn_now(self) -> dict:
        return self.learner.learn_once()

    # ---------------------------------------------------------------- index
    def reindex(self, background: bool = True) -> dict:
        """Re-embed every row not indexed by the current model; in the background unless asked otherwise."""
        with self._lock:
            if self.reindex_state["running"]:
                return dict(self.reindex_state)
            self.reindex_state = {"running": True, "model": self.kb.index_model, "done": 0, "remaining": 0, "error": "", "ts": datetime.now(UTC).isoformat(timespec="seconds")}

        def run():
            try:
                while True:
                    r = self.kb.reindex(64)
                    self.reindex_state["done"] += r["done"]; self.reindex_state["remaining"] = r["remaining"]
                    if r["remaining"] == 0 or r["done"] == 0:
                        if r["remaining"] and r["done"] == 0:
                            self.reindex_state["error"] = "embedding model did not answer"
                        break
            except Exception as e:  # noqa: BLE001
                self.reindex_state["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            finally:
                self.reindex_state["running"] = False
        if background:
            threading.Thread(target=run, daemon=True, name="memory-reindex").start()
        else:
            run()
        return dict(self.reindex_state)

    # ---------------------------------------------------------------- read
    def stats(self) -> dict:
        st = self.kb.stats()
        total = sum(st["lessons"].values()) or 0
        cur = st["embedded"].get(st["index_model"], 0)
        runs = self.learner.history(1)
        return {**st, "total": total, "indexed": cur, "indexed_pct": round(100.0 * cur / total, 1) if total else 100.0,
                "embedder": bool(self.kb.embedder and self.kb.embed_name), "learn_min": self.learner.interval_min,
                "last_learn": runs[0] if runs else None, "learn_runs": self.learner.runs, "reindex": dict(self.reindex_state)}

    def timeline(self, days: int = 14) -> list[dict]:
        """Memories per day and kind (what the system learned lately)."""
        rows = self.kb._exec("SELECT substr(updated_at, 1, 10) AS day, kind, COUNT(*) AS n FROM lessons GROUP BY day, kind ORDER BY day DESC LIMIT ?", (days * 8,))
        out: dict[str, dict] = {}
        for r in rows:
            d = out.setdefault(r["day"], {"day": r["day"], "total": 0})
            d[r["kind"]] = int(r["n"]); d["total"] += int(r["n"])
        return sorted(out.values(), key=lambda d: d["day"], reverse=True)[:days]

    def recent(self, limit: int = 30, kind: str | None = None) -> list[dict]:
        return self.kb.all(kind, limit)
