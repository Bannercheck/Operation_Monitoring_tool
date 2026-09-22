"""Drain: online log template mining (He et al., 2017). Messages are tokenised, obvious variables masked, then routed
through a fixed-depth prefix tree (token count -> first tokens) to a leaf holding clusters; a message joins the cluster
whose template shares the most tokens (>= SIM_TH) and the template keeps only the tokens that agree (<*> elsewhere).

TemplateStore persists the clusters in the product database so what the live feed and every dataset taught stays
across restarts; the Parser page shows them and turns a cluster into a grok pattern proposal."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

WILD = "<*>"
DEPTH = 3                     # prefix levels (token count + first token + leaf)
SIM_TH = 0.5                  # share of matching tokens to join a cluster
MAX_CHILD = 100               # children per node before new keys go to a '*' child
VAR_RE = re.compile(r"^(?:\d+(?:[.,]\d+)?[%a-zA-Z]{0,3}|0x[0-9a-fA-F]+|[0-9a-fA-F]{8,}|[0-9a-f-]{36}|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?|"
                    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?[\w.:+-]*)?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?|/[\w./-]{3,}|https?://\S+|[\w.+-]+@[\w.-]+)[,;:)]?$")
SPLIT_RE = re.compile(r"[\s]+")
TRIM = ",;:()[]{}\"'"


def tokenize(msg: str) -> list[str]:
    out = []
    for tok in SPLIT_RE.split(msg.strip()):
        if not tok:
            continue
        core = tok.strip(TRIM)
        if not core:
            continue
        if "=" in core and not core.startswith("="):                  # key=value: the key is structure, the value a variable
            k, _v = core.split("=", 1)
            out.append(f"{k}={WILD}")
            continue
        out.append(WILD if VAR_RE.match(core) else core)
    return out


class Cluster:
    __slots__ = ("id", "tokens", "count", "first", "last", "sample", "dirty")

    def __init__(self, cid: int, tokens: list[str], sample: str, when: str):
        self.id, self.tokens, self.count, self.first, self.last, self.sample, self.dirty = cid, tokens, 1, when, when, sample[:300], True

    @property
    def template(self) -> str:
        return " ".join(self.tokens)


class Drain:
    def __init__(self, depth: int = DEPTH, sim_th: float = SIM_TH, max_child: int = MAX_CHILD):
        self.depth, self.sim_th, self.max_child = depth, sim_th, max_child
        self.root: dict = {}
        self.clusters: dict[int, Cluster] = {}
        self._next = 1
        self.lock = threading.Lock()

    # ---- tree
    def _leaf(self, tokens: list[str], create: bool) -> list[Cluster] | None:
        node = self.root
        keys = [str(len(tokens))] + [t if t != WILD else "*" for t in tokens[: self.depth - 2]]
        for k in keys:
            if k not in node:
                if not create:
                    return node.get("*", {}).get("__leaf__") if "*" in node else None
                if len(node) >= self.max_child and "*" not in node:
                    k = "*"
                elif len(node) >= self.max_child:
                    k = "*"
                node[k] = node.get(k, {})
            node = node[k]
        if "__leaf__" not in node:
            if not create:
                return None
            node["__leaf__"] = []
        return node["__leaf__"]

    @staticmethod
    def _sim(a: list[str], b: list[str]) -> float:
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        return sum(1 for x, y in zip(a, b) if x == y or x == WILD or y == WILD) / n

    def add(self, msg: str, when: str | None = None) -> Cluster:
        when = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
        tokens = tokenize(msg)
        if not tokens:
            tokens = [WILD]
        with self.lock:
            leaf = self._leaf(tokens, create=True)
            best, best_sim = None, 0.0
            for c in leaf:
                if len(c.tokens) != len(tokens):
                    continue
                s = self._sim(c.tokens, tokens)
                if s > best_sim:
                    best, best_sim = c, s
            if best is None or best_sim < self.sim_th:
                c = Cluster(self._next, tokens, msg, when)
                self._next += 1
                leaf.append(c)
                self.clusters[c.id] = c
                return c
            merged = [x if x == y else WILD for x, y in zip(best.tokens, tokens)]
            if merged != best.tokens:
                best.tokens = merged
            best.count += 1
            best.last = when
            best.dirty = True
            return best

    def match(self, msg: str) -> Cluster | None:
        tokens = tokenize(msg)
        with self.lock:
            leaf = self._leaf(tokens, create=False) or []
            best, best_sim = None, 0.0
            for c in leaf:
                if len(c.tokens) != len(tokens):
                    continue
                s = self._sim(c.tokens, tokens)
                if s > best_sim:
                    best, best_sim = c, s
            return best if best is not None and best_sim >= self.sim_th else None

    def top(self, n: int = 200) -> list[Cluster]:
        with self.lock:
            return sorted(self.clusters.values(), key=lambda c: -c.count)[:n]


# ------------------------------------------------------------------ grok proposal from a template
_TYPE_GUESS = [(re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$"), "IPORHOST"), (re.compile(r"^[+-]?\d+(?:\.\d+)?$"), "NUMBER"), (re.compile(r"^\d+(?:\.\d+)?\s?(?:ms|us|ns|s|sec|m|min|h|%|[KMGT]i?B|[kmgt]b|B)$"), "QUANTITY"),
               (re.compile(r"^/"), "UNIXPATH"), (re.compile(r"^https?://"), "URI"), (re.compile(r"^[0-9a-f-]{36}$"), "UUID"), (re.compile(r"^\d{4}-\d{2}-\d{2}"), "TIMESTAMP_ISO8601")]


def propose_grok(template: str, sample: str = "") -> str:
    """A grok pattern for a Drain template: constant tokens are escaped literally, each <*> becomes a typed capture guessed
    from the sample line (IP, NUMBER, path, URI, UUID, ISO timestamp) or %{NOTSPACE}; key=<*> keeps the key."""
    t_toks = template.split(" ")
    s_toks = tokenize_raw(sample) if sample else []
    out, n = [], 0
    for i, tok in enumerate(t_toks):
        raw = s_toks[i] if i < len(s_toks) else ""
        if tok == WILD or tok.endswith("=" + WILD):
            n += 1
            core = raw.strip(TRIM)
            lead = raw[: len(raw) - len(raw.lstrip(TRIM))] if core else ""            # "(10.0.4.30)" -> literal parentheses around the capture
            trail = raw[len(raw.rstrip(TRIM)):] if core else ""
            if tok != WILD and "=" in core:
                core = core.split("=", 1)[1]
                lead = ""
            typ = next((name for rx, name in _TYPE_GUESS if rx.match(core)), "NOTSPACE")
            field = f"f{n}" if tok == WILD else re.sub(r"\W", "_", tok.split("=", 1)[0])
            cap = f"%{{{typ}:{field}}}"
            out.append(re.escape(lead) + (cap if tok == WILD else f"{re.escape(tok.split('=', 1)[0])}={cap}") + re.escape(trail))
        else:
            out.append(re.escape(tok))
    return "^" + r"\s+".join(out) + "$"


def tokenize_raw(msg: str) -> list[str]:
    return [t for t in SPLIT_RE.split(msg.strip()) if t and t.strip(TRIM)]


# ------------------------------------------------------------------ persistence
class TemplateStore:
    """Drain clusters in the product database (table drain_clusters); learns from the live feed and from every dataset."""

    def __init__(self, kb):
        self.kb = kb
        self.drain = Drain()
        kb._exec(f"CREATE TABLE IF NOT EXISTS drain_clusters (id {kb.pk}, template TEXT NOT NULL, tokens TEXT NOT NULL, count INTEGER DEFAULT 0, "
                 "first_seen TEXT, last_seen TEXT, sample TEXT DEFAULT '', source TEXT DEFAULT '', pattern_id TEXT DEFAULT '')")
        self._load()

    def _load(self) -> None:
        for r in self.kb._exec("SELECT * FROM drain_clusters ORDER BY id"):
            toks = json.loads(r["tokens"])
            c = Cluster(int(r["id"]), toks, r["sample"] or "", r["first_seen"] or "")
            c.count, c.last, c.dirty = int(r["count"] or 0), r["last_seen"] or "", False
            self.drain.clusters[c.id] = c
            leaf = self.drain._leaf(toks, create=True)
            leaf.append(c)
            self.drain._next = max(self.drain._next, c.id + 1)

    def learn(self, messages, source: str = "") -> int:
        """Feed messages (str or objects with .message / .timestamp); returns how many new clusters appeared."""
        before = len(self.drain.clusters)
        for m in messages:
            msg = m if isinstance(m, str) else getattr(m, "message", "")
            when = None if isinstance(m, str) else getattr(m, "timestamp", None)
            when = when.isoformat(timespec="seconds") if hasattr(when, "isoformat") else when
            if msg:
                c = self.drain.add(msg[:400], when)
                if c.count == 1:
                    c.sample = c.sample or msg[:300]
        self.flush(source)
        return len(self.drain.clusters) - before

    def flush(self, source: str = "") -> None:
        with self.drain.lock:
            dirty = [c for c in self.drain.clusters.values() if c.dirty]
            have = {int(r["id"]) for r in self.kb._exec("SELECT id FROM drain_clusters")} if dirty else set()
            for c in dirty:
                toks = json.dumps(c.tokens, ensure_ascii=False)
                if c.id in have:
                    self.kb._exec("UPDATE drain_clusters SET template=?, tokens=?, count=?, last_seen=? WHERE id=?", (c.template[:400], toks, c.count, c.last, c.id))
                else:
                    self.kb._exec("INSERT INTO drain_clusters (id, template, tokens, count, first_seen, last_seen, sample, source) VALUES (?,?,?,?,?,?,?,?)",
                                  (c.id, c.template[:400], toks, c.count, c.first, c.last, c.sample, source[:80]))
                c.dirty = False
            if dirty:
                try:
                    self.kb.reset_sequence("drain_clusters")
                except Exception:  # noqa: BLE001 - SQLite has no sequences to reset
                    pass

    def list(self, q: str = "", limit: int = 200) -> list[dict]:
        rows = self.drain.top(2000)
        ql = q.lower()
        out = [{"id": c.id, "template": c.template, "count": c.count, "first_seen": c.first, "last_seen": c.last, "sample": c.sample, "vars": c.tokens.count(WILD) + sum(1 for t in c.tokens if t.endswith("=" + WILD))}
               for c in rows if not ql or ql in c.template.lower() or ql in c.sample.lower()]
        return out[:limit]

    def get(self, cid: int) -> Cluster | None:
        return self.drain.clusters.get(cid)

    def stats(self) -> dict:
        return {"clusters": len(self.drain.clusters), "events": sum(c.count for c in self.drain.clusters.values())}
