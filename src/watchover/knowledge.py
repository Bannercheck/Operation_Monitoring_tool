"""Knowledge base: what Watchover learned from every dataset plus what the team taught it by hand.

Design (kept small on purpose, so the store never bloats):
  * one **pattern lesson** per distinct root cause (template + services), not one row per incident: repeats only bump
    `occurrences` and append a (dataset, incident) reference. 1.000 incidents a year -> a few hundred rows, ~1-2 KB each.
  * raw alarms are never copied here; a lesson keeps evidence *references* (file:line) and the dataset's input id.
  * notes and documents are content-hashed (same text twice = one row); documents are chunked to ~900 chars.
  * embeddings are optional, stored as float16 bytes next to the lesson (768 dims ~ 1.5 KB) and only for lessons.
  * rules are tiny (kind, key, value) and change the deterministic engine only after a human approves them.

Storage: the shared Database (PostgreSQL through DATABASE_URL, else the embedded SQLite file from KNOWLEDGE_DB / knowledge.db).
Every other store (users, roles, notifications, agents, sources, inventory, rollups, actions, playbook) lives in the same database.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from . import scenario
from .db import Database
from .playbook import _tokens, similarity

UTC = timezone.utc
KINDS = ("pattern", "feedback", "note", "doc", "chat", "anomaly", "resolution")
HASH_MODEL = "hash-v1"          # the deterministic fallback embedding (no model server needed)
HASH_DIMS = 256
RULE_KINDS = ("owner", "cause_rank", "noise_type", "noise_template", "dependency", "recommendation")
CHUNK = 900


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}e", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 2}e", blob))


def hash_embed(text: str, dims: int = HASH_DIMS) -> list[float]:
    """Deterministic bag-of-features vector: word unigrams, word bigrams and character trigrams hashed into `dims` buckets
    (signed, L2-normalised). No model, no network; the same text always gives the same vector, and vectors of texts that
    share words / stems land close. Used when no embedding model is configured and for rows that model never indexed."""
    v = [0.0] * dims
    words = re.findall(r"[0-9a-zçğıöşü_./-]+", (text or "").lower())
    feats = list(words) + [f"{a} {b}" for a, b in zip(words, words[1:])]
    for w in words:
        if len(w) > 3:
            feats.extend(w[i:i + 3] for i in range(len(w) - 2))
    for f in feats:
        h = int(hashlib.blake2b(f.encode(), digest_size=4).hexdigest(), 16)
        v[h % dims] += 1.0 if (h >> 31) & 1 else -1.0
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else v


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def chunk_text(text: str, size: int = CHUNK) -> list[str]:
    """Split on headings / blank lines, then pack paragraphs into ~size chunks (never splits a sentence in half)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#+ )", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > size:
            out.append(cur); cur = ""
        while len(p) > size:                          # one giant paragraph: cut at sentence ends
            cut = max(p.rfind(". ", 0, size), p.rfind("\n", 0, size), size // 2)
            out.append((cur + "\n\n" + p[:cut + 1]).strip()); cur = ""; p = p[cut + 1:].strip()
        cur = (cur + "\n\n" + p).strip() if cur else p
    if cur:
        out.append(cur)
    return out


class Knowledge(Database):
    def __init__(self, url: str | None = None, embedder: Callable[[list[str]], list[list[float]]] | None = None):
        super().__init__(url)
        self.embedder = embedder
        self.embed_name = ""            # name of the model behind `embedder` (bge-m3 …); "" -> hash fallback
        self._schema()
        self._base: dict | None = None

    def _schema(self) -> None:
        pk, blob = self.pk, self.blob
        self._exec(f"""CREATE TABLE IF NOT EXISTS lessons (
            id {pk}, kind TEXT NOT NULL, key TEXT, title TEXT NOT NULL, text TEXT NOT NULL, tokens TEXT DEFAULT '',
            dataset TEXT DEFAULT '', incident_id TEXT DEFAULT '', root_cause TEXT DEFAULT '', services TEXT DEFAULT '[]',
            recovery TEXT DEFAULT '', tags TEXT DEFAULT '[]', refs TEXT DEFAULT '[]', occurrences INTEGER DEFAULT 1,
            content_hash TEXT DEFAULT '', embedding {blob}, meta TEXT DEFAULT '{{}}', created_at TEXT, updated_at TEXT)""")
        self._exec("CREATE INDEX IF NOT EXISTS lessons_kind ON lessons(kind)")
        self._exec("CREATE INDEX IF NOT EXISTS lessons_key ON lessons(key)")
        self._exec("CREATE INDEX IF NOT EXISTS lessons_hash ON lessons(content_hash)")
        if "embed_model" not in self.columns("lessons"):
            self._exec("ALTER TABLE lessons ADD COLUMN embed_model TEXT DEFAULT ''")
        self._exec(f"""CREATE TABLE IF NOT EXISTS llm_calls (
            id {pk}, ts TEXT, provider TEXT, model TEXT, kind TEXT, ok INTEGER, latency_ms INTEGER, prompt_chars INTEGER, answer_chars INTEGER,
            citations INTEGER DEFAULT 0, grounded INTEGER DEFAULT 0, invalid INTEGER DEFAULT 0, error TEXT DEFAULT '')""")
        self._exec(f"""CREATE TABLE IF NOT EXISTS answer_feedback (
            id {pk}, ts TEXT, model TEXT, question TEXT, verdict TEXT, note TEXT DEFAULT '')""")
        self._exec(f"""CREATE TABLE IF NOT EXISTS eval_runs (
            id {pk}, ts TEXT, model TEXT, dataset TEXT, n INTEGER, correct INTEGER, cited INTEGER, grounded INTEGER, latency_ms INTEGER, detail TEXT DEFAULT '[]')""")
        self._exec(f"""CREATE TABLE IF NOT EXISTS rules (
            id {pk}, kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, reason TEXT DEFAULT '', source TEXT DEFAULT '',
            status TEXT DEFAULT 'proposed', created_at TEXT, decided_at TEXT)""")

    # ---------------------------------------------------------------- write
    @property
    def index_model(self) -> str:
        return self.embed_name if (self.embedder and self.embed_name) else HASH_MODEL

    def _vec(self, text: str) -> tuple[list[float], str]:
        """(vector, model): the configured embedding model when it answers, else the hash fallback."""
        if self.embedder:
            try:
                return self.embedder([text[:2000]])[0], self.index_model
            except Exception:  # noqa: BLE001 - embeddings are optional, never block a write
                pass
        return hash_embed(text), HASH_MODEL

    def _embed(self, text: str) -> bytes | None:
        return _pack(self._vec(text)[0])

    def _embed2(self, text: str) -> tuple[bytes, str]:
        vec, model = self._vec(text)
        return _pack(vec), model

    def add(self, kind: str, title: str, text: str, *, key: str = "", dataset: str = "", incident_id: str = "", root_cause: str = "",
            services: list | None = None, recovery: str = "", tags: list | None = None, refs: list | None = None, meta: dict | None = None) -> int:
        """Insert one lesson; identical (kind, title, text) is a no-op that returns the existing id."""
        h = hashlib.sha256(f"{kind}|{title}|{text}".encode()).hexdigest()[:16]
        ex = self._exec("SELECT id FROM lessons WHERE content_hash=?", (h,))
        if ex:
            return int(ex[0]["id"])
        toks = " ".join(sorted(_tokens(f"{title} {text} {' '.join(services or [])} {' '.join(tags or [])}")))
        emb, model = self._embed2(f"{title}\n{text}")
        return self._insert(
            "INSERT INTO lessons (kind, key, title, text, tokens, dataset, incident_id, root_cause, services, recovery, tags, refs, occurrences, content_hash, embedding, embed_model, meta, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, key, title, text, toks, dataset, incident_id, root_cause, json.dumps(services or [], ensure_ascii=False), recovery,
             json.dumps(tags or [], ensure_ascii=False), json.dumps(refs or [], ensure_ascii=False), 1, h, emb, model,
             json.dumps(meta or {}, ensure_ascii=False, default=str), _now(), _now()))

    def add_event(self, kind: str, key: str, title: str, text: str, *, services: list | None = None, tags: list | None = None,
                  meta: dict | None = None, dataset: str = "") -> int:
        """One row per (kind, key) that keeps refreshing: a repeat bumps occurrences and rewrites text / vector / updated_at.
        Used for the memory feeds (anomalies, resolutions) whose wording changes every time but whose identity does not."""
        toks = " ".join(sorted(_tokens(f"{title} {text} {' '.join(services or [])} {' '.join(tags or [])}")))
        emb, model = self._embed2(f"{title}\n{text}")
        row = self._exec("SELECT id, occurrences FROM lessons WHERE kind=? AND key=?", (kind, key))
        if row:
            self._exec("UPDATE lessons SET title=?, text=?, tokens=?, services=?, tags=?, meta=?, embedding=?, embed_model=?, occurrences=?, updated_at=? WHERE id=?",
                       (title, text, toks, json.dumps(services or [], ensure_ascii=False), json.dumps(tags or [], ensure_ascii=False),
                        json.dumps(meta or {}, ensure_ascii=False, default=str), emb, model, int(row[0]["occurrences"] or 1) + 1, _now(), row[0]["id"]))
            return int(row[0]["id"])
        return self._insert(
            "INSERT INTO lessons (kind, key, title, text, tokens, dataset, incident_id, root_cause, services, recovery, tags, refs, occurrences, content_hash, embedding, embed_model, meta, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, key, title, text, toks, dataset, "", "", json.dumps(services or [], ensure_ascii=False), "", json.dumps(tags or [], ensure_ascii=False), "[]", 1,
             hashlib.sha256(f"{kind}|{key}".encode()).hexdigest()[:16], emb, model, json.dumps(meta or {}, ensure_ascii=False, default=str), _now(), _now()))

    def reindex(self, limit: int = 64) -> dict:
        """Re-embed rows whose vector was not made by the current index model (batches, so a model switch catches up in the background)."""
        model = self.index_model
        rows = self._exec("SELECT id, title, text FROM lessons WHERE COALESCE(embed_model, '') <> ? ORDER BY updated_at DESC LIMIT ?", (model, limit))
        done = 0
        for r in rows:
            emb, m = self._embed2(f"{r['title']}\n{r['text']}")
            if m != model:                                     # the model server did not answer: stop, do not overwrite with the fallback
                break
            self._exec("UPDATE lessons SET embedding=?, embed_model=? WHERE id=?", (emb, m, r["id"]))
            done += 1
        rem = self._exec("SELECT COUNT(*) AS n FROM lessons WHERE COALESCE(embed_model, '') <> ?", (model,))
        return {"model": model, "done": done, "remaining": int(rem[0]["n"]) if rem else 0}

    def embed_coverage(self) -> dict:
        rows = self._exec("SELECT COALESCE(embed_model, '') AS m, COUNT(*) AS n FROM lessons GROUP BY m")
        return {(r["m"] or "-"): int(r["n"]) for r in rows}

    def record(self, analysis, dataset: str, lang: str = "tr") -> int:
        """One pattern lesson per root cause; a repeat bumps occurrences and appends the (dataset, incident) reference."""
        from .analysis import suggested_owner
        from .i18n import reason_text
        from . import stamp
        n = 0
        input_id = stamp.input_id(getattr(analysis, "report", []) or [])
        for inc in analysis.incidents:
            root = analysis.signal_by_id[inc.root_cause_signal]
            key = hashlib.sha1(f"{root.template}|{','.join(sorted(root.services))}".encode()).hexdigest()[:12]
            ref = {"dataset": dataset, "input": input_id, "incident": inc.id, "started": inc.started_at.isoformat(timespec="minutes"),
                   "ended": inc.ended_at.isoformat(timespec="minutes"), "score": inc.score, "severity": inc.severity}
            row = self._exec("SELECT id, refs, occurrences FROM lessons WHERE kind='pattern' AND key=?", (key,))
            if row:
                refs = json.loads(row[0]["refs"] or "[]")
                if any(r.get("dataset") == dataset and r.get("incident") == inc.id for r in refs):
                    continue
                refs.append(ref)
                self._exec("UPDATE lessons SET refs=?, occurrences=?, updated_at=? WHERE id=?",
                           (json.dumps(refs, ensure_ascii=False), len(refs), _now(), row[0]["id"]))
                n += 1
                continue
            alts = "; ".join(f"{x['template'][:60]} ({', '.join(x['services'])}, {x['score']})" for x in inc.root_cause_alternatives[:3])
            text = (f"Root cause: {root.template} ({', '.join(root.services)}) — {reason_text(inc.root_cause_codes, lang)}\n"
                    f"Affected: {', '.join(inc.affected_services)}\nHosts: {', '.join(inc.affected_hosts[:8])}\n"
                    f"Alarms/events: {sum(analysis.signal_by_id[s].count for s in inc.signal_ids)} in {len(inc.signal_ids)} signals\n"
                    f"Recovery: {inc.recovery.get('kind', 'unknown')} {inc.recovery.get('what', '')[:120]}\n"
                    f"First action: {inc.recommendations[0] if inc.recommendations else '-'} · owner: {suggested_owner(inc, root)}\n"
                    f"Counter-hypotheses: {alts or '-'}")
            evidence = [o.ref for o in root.observations[:5]]
            emb, model = self._embed2(f"{inc.title}\n{text}")
            self._insert(
                "INSERT INTO lessons (kind, key, title, text, tokens, dataset, incident_id, root_cause, services, recovery, tags, refs, occurrences, content_hash, embedding, embed_model, meta, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("pattern", key, inc.title[:160], text, " ".join(sorted(_tokens(f"{inc.title} {text}"))), dataset, inc.id, root.template,
                 json.dumps(inc.affected_services, ensure_ascii=False), inc.recovery.get("kind", ""), json.dumps(["auto"]),
                 json.dumps([ref], ensure_ascii=False), 1, hashlib.sha256(f"pattern|{key}".encode()).hexdigest()[:16], emb, model,
                 json.dumps({"evidence": evidence, "owner": suggested_owner(inc, root)}, ensure_ascii=False), _now(), _now()))
            n += 1
        return n

    def add_note(self, title: str, text: str, tags: list | None = None, services: list | None = None) -> int:
        return self.add("note", title.strip() or text[:60], text.strip(), tags=tags, services=services)

    def add_doc(self, name: str, text: str, tags: list | None = None) -> list[int]:
        """A runbook / postmortem / wiki page, chunked; each chunk is one searchable lesson."""
        ids = []
        for i, ch in enumerate(chunk_text(text), 1):
            ids.append(self.add("doc", f"{name} §{i}", ch, key=name, tags=(tags or []) + ["doc"]))
        return ids

    def add_feedback(self, dataset: str, inc, root, verdict: str, correct: str = "", comment: str = "", alt_type: str = "",
                     root_type: str = "", mark_noise: bool = False) -> dict:
        """Thumbs up/down on a card, with an optional corrected root cause. Wrong verdicts turn into rule proposals."""
        text = (f"{'👍' if verdict == 'up' else '👎'} {inc.id} {inc.title[:120]}\nRoot cause shown: {root.template[:120]} ({', '.join(root.services)})"
                + (f"\nCorrect root cause: {correct}" if correct else "") + (f"\nComment: {comment}" if comment else ""))
        lid = self.add("feedback", f"{inc.id} {verdict}", text, dataset=dataset, incident_id=inc.id, root_cause=root.template,
                       services=inc.affected_services, tags=[verdict], meta={"correct": correct, "alt_type": alt_type, "root_type": root_type, "noise": mark_noise})
        proposals = []
        if verdict == "down":
            if mark_noise and root_type:
                proposals.append(self.propose("noise_type", root_type, "1", f"{inc.id}: card marked as noise", f"feedback:{lid}"))
            elif mark_noise:                                   # log data without alarm types: the root template itself is the noise key
                proposals.append(self.propose("noise_template", root.template[:200], "1", f"{inc.id}: card marked as noise", f"feedback:{lid}"))
            elif alt_type and alt_type != root_type:
                cur = float(scenario.CAUSE_RANK.get(alt_type, 1.0))
                proposals.append(self.propose("cause_rank", alt_type, str(max(cur + 1.0, float(scenario.CAUSE_RANK.get(root_type, 1.0)) + 0.5)),
                                              f"{inc.id}: '{alt_type}' chosen as the real root cause over '{root_type}'", f"feedback:{lid}"))
                if root_type:
                    proposals.append(self.propose("cause_rank", root_type, str(max(0.5, float(scenario.CAUSE_RANK.get(root_type, 1.0)) - 1.0)),
                                                  f"{inc.id}: '{root_type}' was shown as root cause but rejected", f"feedback:{lid}"))
        return {"lesson": lid, "proposals": proposals}

    def delete(self, lesson_id: int) -> None:
        self._exec("DELETE FROM lessons WHERE id=?", (lesson_id,))

    # ---------------------------------------------------------------- read / retrieval
    def get(self, lesson_id: int) -> dict | None:
        rows = self._exec("SELECT * FROM lessons WHERE id=?", (lesson_id,))
        return self._row(rows[0]) if rows else None

    @staticmethod
    def _row(r: dict) -> dict:
        out = dict(r)
        for k in ("services", "tags", "refs"):
            try:
                out[k] = json.loads(out.get(k) or "[]")
            except (TypeError, ValueError):
                out[k] = []
        try:
            out["meta"] = json.loads(out.get("meta") or "{}")
        except (TypeError, ValueError):
            out["meta"] = {}
        out.pop("embedding", None)
        return out

    def all(self, kind: str | None = None, limit: int = 500) -> list[dict]:
        rows = self._exec("SELECT * FROM lessons WHERE (?='' OR kind=?) ORDER BY updated_at DESC LIMIT ?", (kind or "", kind or "", limit))
        return [self._row(r) for r in rows]

    def search(self, query: str, k: int = 6, kinds: tuple | None = None) -> list[dict]:
        """Hybrid ranking: token overlap (always) + embedding cosine (when both sides have one) + a small recency/occurrence prior."""
        q = (query or "").strip()
        if not q:
            return []
        qt = _tokens(q)
        qvecs: dict[str, list[float] | None] = {}                      # query vector per index model (bge-m3 rows and hash rows both score)
        rows = self._exec("SELECT * FROM lessons" + (" WHERE kind IN (%s)" % ",".join("?" * len(kinds)) if kinds else ""), tuple(kinds or ()))
        scored = []
        ql = q.lower()
        for r in rows:
            toks = set((r.get("tokens") or "").split())
            common = len(qt & toks)
            lex = (0.7 * common / len(qt) + 0.3 * common / len(qt | toks)) if qt and toks and common else 0.0   # coverage first, then overlap
            if ql and ql in (r.get("title") or "").lower():
                lex = max(lex, 0.6)
            emb = None
            m = r.get("embed_model") or ""
            if r.get("embedding") and m:
                if m not in qvecs:
                    if m == HASH_MODEL:
                        qvecs[m] = hash_embed(q)
                    elif m == self.index_model:
                        vec, got = self._vec(q)
                        qvecs[m] = vec if got == m else None
                    else:
                        qvecs[m] = None
                qv = qvecs[m]
                if qv is not None:
                    emb = max(0.0, cosine(qv, _unpack(bytes(r["embedding"]))))
            if emb is None:
                score = lex
            elif m == HASH_MODEL:
                score = 0.75 * lex + 0.25 * max(0.0, emb - 0.1)         # hash cosine is a weak, noisy signal: lexical stays in charge
            else:
                score = 0.55 * lex + 0.45 * emb
            score += min(0.1, 0.02 * (int(r.get("occurrences") or 1) - 1))
            if score > 0.02:
                d = self._row(r); d["score"] = round(score, 3)
                scored.append(d)
        scored.sort(key=lambda d: -d["score"])
        return scored[:k]

    def related(self, inc, root, k: int = 4) -> list[dict]:
        """Past lessons that look like this incident (excluding its own pattern row)."""
        out = self.search(f"{inc.title} {root.template} {' '.join(inc.affected_services)}", k + 1)
        return [d for d in out if not (d["kind"] == "pattern" and d.get("root_cause") == root.template)][:k]

    def stats(self) -> dict:
        rows = self._exec("SELECT kind, COUNT(*) AS n FROM lessons GROUP BY kind")
        rules = self._exec("SELECT status, COUNT(*) AS n FROM rules GROUP BY status")
        size = self.size_bytes()
        return {"lessons": {r["kind"]: int(r["n"]) for r in rows}, "rules": {r["status"]: int(r["n"]) for r in rules}, "bytes": size,
                "index_model": self.index_model, "embedded": self.embed_coverage()}

    # ---------------------------------------------------------------- rules: proposed by feedback / facts, approved by a human
    def propose(self, kind: str, key: str, value: str, reason: str = "", source: str = "manual") -> int:
        assert kind in RULE_KINDS, kind
        ex = self._exec("SELECT id FROM rules WHERE kind=? AND key=? AND value=? AND status='proposed'", (kind, key, value))
        if ex:
            return int(ex[0]["id"])
        return self._insert("INSERT INTO rules (kind, key, value, reason, source, status, created_at) VALUES (?,?,?,?,?,'proposed',?)",
                            (kind, key, value, reason, source, _now()))

    def rules(self, status: str | None = None) -> list[dict]:
        return self._exec("SELECT * FROM rules WHERE (?='' OR status=?) ORDER BY id DESC", (status or "", status or ""))

    def decide(self, rule_id: int, approve: bool) -> None:
        self._exec("UPDATE rules SET status=?, decided_at=? WHERE id=?", ("approved" if approve else "rejected", _now(), rule_id))

    def delete_rule(self, rule_id: int) -> None:
        self._exec("DELETE FROM rules WHERE id=?", (rule_id,))

    def apply_rules(self) -> dict:
        """Overlay approved rules on the scenario module (in memory): owners, cause ranks, noise types, dependencies, recommendations.
        Re-applied from the pristine base every time, so rejecting a rule later really removes its effect."""
        if self._base is None:
            self._base = {"OWNERS": dict(scenario.OWNERS), "CAUSE_RANK": dict(scenario.CAUSE_RANK), "RECOMMENDATIONS": {k: list(v) for k, v in scenario.RECOMMENDATIONS.items()},
                          "NOISE_TYPES": set(getattr(scenario, "NOISE_TYPES", set())), "NOISE_TEMPLATES": set(getattr(scenario, "NOISE_TEMPLATES", set())),
                          "EXTRA_DEPENDENCIES": list(getattr(scenario, "EXTRA_DEPENDENCIES", []))}
        scenario.OWNERS.clear(); scenario.OWNERS.update(self._base["OWNERS"])
        scenario.CAUSE_RANK.clear(); scenario.CAUSE_RANK.update(self._base["CAUSE_RANK"])
        scenario.RECOMMENDATIONS.clear(); scenario.RECOMMENDATIONS.update({k: list(v) for k, v in self._base["RECOMMENDATIONS"].items()})
        scenario.NOISE_TYPES = set(self._base["NOISE_TYPES"])
        scenario.NOISE_TEMPLATES = set(self._base["NOISE_TEMPLATES"])
        scenario.EXTRA_DEPENDENCIES = list(self._base["EXTRA_DEPENDENCIES"])
        applied = Counter()
        for r in self.rules("approved"):
            kind, key, val = r["kind"], r["key"], r["value"]
            if kind == "owner":
                scenario.OWNERS[key.lower()] = val
            elif kind == "cause_rank":
                try:
                    scenario.CAUSE_RANK[key.lower()] = float(val)
                except ValueError:
                    continue
            elif kind == "noise_type":
                scenario.NOISE_TYPES.add(key.lower())
            elif kind == "noise_template":
                scenario.NOISE_TEMPLATES.add(key)
            elif kind == "dependency":
                src, _, tgt = key.partition("->")
                if src and tgt:
                    scenario.EXTRA_DEPENDENCIES.append({"source": src.strip(), "target": tgt.strip(), "type": val or "declared", "criticality": "rule"})
            elif kind == "recommendation":
                scenario.RECOMMENDATIONS.setdefault(key.lower(), []).insert(0, val)
            applied[kind] += 1
        return dict(applied)

    # ---------------------------------------------------------------- LLM quality: every call, every verdict, every benchmark run
    def log_call(self, rec: dict) -> None:
        self._exec("INSERT INTO llm_calls (ts, provider, model, kind, ok, latency_ms, prompt_chars, answer_chars, citations, grounded, invalid, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (_now(), rec.get("provider", ""), rec.get("model", ""), rec.get("kind", ""), int(rec.get("ok", 1)), int(rec.get("latency_ms", 0)),
                    int(rec.get("prompt_chars", 0)), int(rec.get("answer_chars", 0)), int(rec.get("citations", 0)), int(rec.get("grounded", 0)),
                    int(rec.get("invalid", 0)), str(rec.get("error", ""))[:300]))

    def annotate_last_call(self, kind: str, citations: int, grounded: int, invalid: int) -> None:
        rows = self._exec("SELECT id FROM llm_calls WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
        if rows:
            self._exec("UPDATE llm_calls SET citations=?, grounded=?, invalid=? WHERE id=?", (citations, grounded, invalid, rows[0]["id"]))

    def rate_answer(self, model: str, question: str, verdict: str, note: str = "", answer: str = "") -> None:
        try:
            self._exec("ALTER TABLE answer_feedback ADD COLUMN answer TEXT DEFAULT ''")
        except Exception:  # noqa: BLE001 - column already there
            pass
        self._exec("INSERT INTO answer_feedback (ts, model, question, verdict, note, answer) VALUES (?,?,?,?,?,?)", (_now(), model, question[:300], verdict, note[:300], answer[:4000]))

    def save_eval(self, model: str, dataset: str, result: dict) -> int:
        return self._insert("INSERT INTO eval_runs (ts, model, dataset, n, correct, cited, grounded, latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                            (_now(), model, dataset, result["n"], result["correct"], result["cited"], result["grounded"], result["latency_ms"],
                             json.dumps(result.get("detail", []), ensure_ascii=False)[:20000]))

    def eval_runs(self, limit: int = 20) -> list[dict]:
        return self._exec("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,))

    def llm_calls(self, limit: int = 500) -> list[dict]:
        return self._exec("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,))

    def llm_stats(self) -> dict:
        """The numbers on the LLM page: availability, latency, grounding, human approval, rule acceptance, per model."""
        calls = self._exec("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 2000")
        chat = [c for c in calls if c["kind"] in ("chat", "explain", "rules", "eval")]
        ok = [c for c in calls if c["ok"]]
        lat = sorted(c["latency_ms"] for c in ok) or [0]
        cited = [c for c in chat if c["ok"] and c["citations"]]
        fb = self._exec("SELECT verdict, COUNT(*) AS n FROM answer_feedback GROUP BY verdict")
        fbd = {r["verdict"]: int(r["n"]) for r in fb}
        rl = self._exec("SELECT status, COUNT(*) AS n FROM rules WHERE source LIKE 'llm:%' GROUP BY status")
        rld = {r["status"]: int(r["n"]) for r in rl}
        per_model: dict = {}
        for c in calls:
            m = per_model.setdefault(c["model"] or "-", {"calls": 0, "ok": 0, "lat": [], "cited": 0, "chat": 0, "invalid": 0})
            m["calls"] += 1; m["ok"] += int(c["ok"])
            if c["ok"]:
                m["lat"].append(c["latency_ms"])
            if c["kind"] in ("chat", "explain", "eval"):
                m["chat"] += 1; m["cited"] += int(bool(c["citations"])); m["invalid"] += int(c["invalid"] or 0)
        models = [{"model": k, "calls": v["calls"], "success": round(v["ok"] / v["calls"], 3) if v["calls"] else None,
                   "p50_ms": sorted(v["lat"])[len(v["lat"]) // 2] if v["lat"] else None, "citation_rate": round(v["cited"] / v["chat"], 3) if v["chat"] else None,
                   "invalid_citations": v["invalid"]} for k, v in per_model.items()]
        evals = self.eval_runs(10)
        last_eval = evals[0] if evals else None
        return {"calls": len(calls), "success": round(len(ok) / len(calls), 3) if calls else None,
                "p50_ms": lat[len(lat) // 2], "p95_ms": lat[int(len(lat) * 0.95) - 1] if len(lat) > 1 else lat[0],
                "citation_rate": round(len(cited) / len([c for c in chat if c["ok"]]), 3) if any(c["ok"] for c in chat) else None,
                "grounding_rate": (round(sum(c["grounded"] for c in cited) / max(1, sum(c["citations"] for c in cited)), 3) if cited else None),
                "invalid_citations": sum(c["invalid"] or 0 for c in chat),
                "thumbs_up": fbd.get("up", 0), "thumbs_down": fbd.get("down", 0),
                "approval_rate": round(fbd.get("up", 0) / (fbd.get("up", 0) + fbd.get("down", 0)), 3) if (fbd.get("up", 0) + fbd.get("down", 0)) else None,
                "rules_proposed": sum(rld.values()), "rules_approved": rld.get("approved", 0), "rules_rejected": rld.get("rejected", 0),
                "rule_acceptance": round(rld.get("approved", 0) / (rld.get("approved", 0) + rld.get("rejected", 0)), 3) if (rld.get("approved", 0) + rld.get("rejected", 0)) else None,
                "fallbacks": len([c for c in chat if not c["ok"]]), "models": models,
                "eval_accuracy": round(last_eval["correct"] / last_eval["n"], 3) if last_eval and last_eval["n"] else None, "last_eval": last_eval,
                "by_kind": dict(Counter(c["kind"] for c in calls))}
