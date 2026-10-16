"""Alerts from a log query (notify condition "query", docs/design/logs.md 3.12): "matching lines in the last M minutes >= N"
(burst) or "< N" (absence: no `backup completed` line in 60 minutes).

* counters  a facet-only query (level / service / host / env / pool / file) is counted from the per-minute facet cube under the
            rule owner's team scope: exact, a few milliseconds.
* stream    anything else: every accepted live batch is tested against the rule's compiled predicate (owner scope included)
            and counted per minute; exact from the moment the rule exists. At most MAX_STREAM rules; a predicate slower than
            HEAVY_US per line on the ring is refused when the rule is created (lg_alert_heavy).

An absence rule does not fire before its counters have run for the whole window (no false alarm after a restart). Alerts go
through Notifier.dispatch (cooldown, alert log, channels); the alert key is query:<rule id>.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import logquery as lq
from .logstore import ObsLine

UTC = timezone.utc
MAX_STREAM = 20
HEAVY_US = 5.0
REFRESH_S = 60


class TooMany(ValueError):
    code = "too_many_stream_rules"


class Heavy(ValueError):
    code = "lg_alert_heavy"


def owner_scope(svc, email: str):
    """(allowed, scope) of a rule owner: allowed False when the account is gone, disabled or lacks page.logs."""
    try:
        u = svc.users.get((email or "").lower())
    except Exception:  # noqa: BLE001
        u = None
    if not u or u.get("status") != "active":
        return False, None
    user = {"id": u.get("id"), "email": u["email"], "role": u.get("role", ""), "name": u.get("name", "")}
    if hasattr(svc, "access_of"):
        acc = svc.access_of(user)
        if not acc.can("page.logs"):
            return False, None
        return True, acc.scope("page.logs")
    if not svc.roles.can(user["role"], "page.logs"):
        return False, None
    return True, svc.scope_of(user)


def _item(x: dict) -> dict:
    """A sample line as action-agent evidence: who sent it (agent_id, host.reported) matters for its provenance (§4.3)."""
    at = x.get("attrs") or {}
    return {"kind": "log", "ts": x.get("ts"), "host": x.get("host") if x.get("host") not in (None, "-") else "", "text": x.get("message") or "",
            "agent_id": at.get("agent_id"), "reported": at.get("host.reported") or "", "attributes": {"agent": x.get("agent") or at.get("agent") or ""},
            "fields": {"service": x.get("service")} if x.get("service") not in (None, "-") else {}, "ref": {"line_id": str(x.get("id") or "")}}


class LogWatch:
    def __init__(self, logs, notifier, owner_fn):
        self.logs, self.n, self.owner_fn = logs, notifier, owner_fn
        self.lock = threading.Lock()
        self.rules: dict = {}                    # rule id -> stream state
        self._refreshed = 0.0
        self.on_case = None                      # action agent: (rule, count) after the dispatch, for rules with open_case

    # ---------------------------------------------------------------- rule set-up
    def mode_of(self, text: str) -> str:
        return "counters" if lq.plan(lq.parse(text).node).facet_only else "stream"

    def check(self, text: str, scope) -> str:
        """Validate a query for an alert rule: its mode; Heavy when a stream predicate is too slow for the live feed."""
        q = self.logs.query(text, scope)
        if q.plan.facet_only:
            return "counters"
        with self.logs.live.lock:
            sample = list(self.logs.live.buf)[-5000:]
        if len(sample) >= 200:
            ctx = q.ctx
            views = [ObsLine(o, ctx) for o in sample]
            t0 = time.perf_counter()
            for v in views:
                q.pred(v)
            per = (time.perf_counter() - t0) * 1e6 / len(views)
            if per > HEAVY_US:
                raise Heavy(f"{per:.1f} µs per line")
        with self.lock:
            if len(self.rules) >= MAX_STREAM:
                raise TooMany("too_many_stream_rules")
        return "stream"

    def refresh(self, force: bool = False) -> None:
        """Stream rules from notify_rules: compiled with the owner's scope (re-resolved every REFRESH_S, so a team change applies)."""
        now = time.time()
        if not force and now - self._refreshed < REFRESH_S:
            return
        self._refreshed = now
        try:
            rules = [r for r in self.n.rules() if r.get("condition") == "query" and r.get("enabled")]
        except Exception:  # noqa: BLE001
            return
        keep = {}
        for r in rules:
            text = r.get("query") or ""
            try:
                if self.mode_of(text) != "stream":
                    continue
                ok, scope = self.owner_fn(r.get("owner") or "")
                if not ok:
                    continue
                q = self.logs.query(text, scope)
            except Exception:  # noqa: BLE001
                continue
            old = self.rules.get(r["id"])
            if old is not None and old["text"] == text:
                old.update(pred=q.pred, scope=scope)
                keep[r["id"]] = old
            else:
                keep[r["id"]] = {"text": text, "pred": q.pred, "scope": scope, "ctx": q.ctx, "counts": {}, "hosts": {}, "last": deque(maxlen=3), "since": now}
            if len(keep) >= MAX_STREAM:
                break
        with self.lock:
            self.rules = keep

    def add_events(self, events) -> None:
        """Every accepted live batch (services._on_ingest): count matches per stream rule and minute."""
        rules = self.rules
        if not rules or not events:
            return
        ctx = next(iter(rules.values()))["ctx"]
        views = [ObsLine(o, ctx) for o in events]
        for st in list(rules.values()):
            pred = st["pred"]
            counts, hosts, last = st["counts"], st["hosts"], st["last"]
            for v in views:
                try:
                    if not pred(v):
                        continue
                except Exception:  # noqa: BLE001
                    continue
                m = int(v.ts // 60)
                counts[m] = counts.get(m, 0) + 1
                hc = hosts.get(m)
                if hc is None:
                    hc = hosts[m] = Counter()
                hc[v.host or "-"] += 1
                last.append(v.seq)
            if len(counts) > 180:
                lo = int(time.time() // 60) - 120
                for m in [m for m in counts if m < lo]:
                    counts.pop(m, None); hosts.pop(m, None)

    # ---------------------------------------------------------------- evaluation (AlertEngine, every minute)
    def count(self, rule: dict, scope, now: float | None = None) -> dict:
        """{"n", "ready", "mode", "hosts", "samples", "start", "end"} for the last window_min whole minutes."""
        now = now or time.time()
        win = max(1, min(int(rule.get("window_min") or 5), 60))
        m1 = int(now // 60)
        m0 = m1 - win
        s, e = datetime.fromtimestamp(m0 * 60, UTC), datetime.fromtimestamp(m1 * 60, UTC)
        text = rule.get("query") or ""
        q = self.logs.query(text, scope)
        if q.plan.facet_only:
            r = self.logs.live.rollup
            ready = r.covers((m0, m1 - 1)) and r.started <= m0 * 60 + 1
            n, _over = self.logs.cube_count(q, s, e)
            hosts = Counter()
            if n:
                mins, _o = r.cube(m0, m1 - 1)
                kp = q.key_pred()
                for _m, d in mins:
                    for k, c in d.items():
                        kk = r.cube_key(k)
                        if kp(*kk):
                            hosts[kk[3]] += c
            samples, items = [], []
            if n:
                try:
                    res = self.logs.search(text, start=s.isoformat(), end=e.isoformat(), limit=3, scope=scope)
                    samples = [f"{x['ts'][11:23]} {x['host']} {x['message'][:160]}" for x in res["lines"]]
                    items = [_item(x) for x in res["lines"]]
                except Exception:  # noqa: BLE001
                    samples = []
            return {"n": n, "ready": ready, "mode": "counters", "hosts": hosts.most_common(3), "samples": samples, "items": items, "start": s, "end": e}
        self.refresh()
        st = self.rules.get(rule["id"])
        if st is None:
            return {"n": 0, "ready": False, "mode": "stream", "hosts": [], "samples": [], "start": s, "end": e}
        n = sum(c for m, c in st["counts"].items() if m0 <= m < m1)
        hosts = Counter()
        for m, hc in st["hosts"].items():
            if m0 <= m < m1:
                hosts.update(hc)
        samples, items = [], []
        for seq in list(st["last"])[::-1]:
            try:
                ln = self.logs.line(seq, scope)
                samples.append(f"{ln['ts'][11:23]} {ln['host']} {ln['message'][:160]}")
                items.append(_item(ln))
            except Exception:  # noqa: BLE001
                continue
        return {"n": n, "ready": st["since"] <= m0 * 60, "mode": "stream", "hosts": hosts.most_common(3), "samples": samples, "items": items,
                "start": s, "end": e}

    def evaluate(self, rule: dict, notifier) -> list:
        ok, scope = self.owner_fn(rule.get("owner") or "")
        if not ok:
            key = f"query:{rule['id']}:owner_missing"
            if not notifier.recent(key, 1440):
                notifier.kb._exec("INSERT INTO alerts (ts, rule_id, key, severity, title, body, channels, recipients, ok, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (datetime.now(UTC).isoformat(timespec="seconds"), rule["id"], key, rule.get("severity", "high"), f"{rule.get('name', '')}: owner_missing",
                                   "", "", "", 0, "owner_missing"))
            return []
        c = self.count(rule, scope)
        thr = float(rule.get("threshold") or 0)
        op = rule.get("op") or ">="
        hit = c["n"] >= thr if op == ">=" else (c["ready"] and c["n"] < thr)
        if not hit:
            return []
        win = max(1, min(int(rule.get("window_min") or 5), 60))
        norm = lq.normalize(lq.parse(rule.get("query") or "").node)
        link = f"/logs?q={quote(rule.get('query') or '')}&start={quote(c['start'].isoformat())}&end={quote(c['end'].isoformat())}"
        body = "\n".join([f"Sorgu / query: {norm or '*'}", f"{c['n']} satır / lines in {win} dk (eşik / threshold {op} {thr:g})",
                          "Sunucular / hosts: " + (", ".join(f"{h} ({n})" for h, n in c["hosts"]) or "-")] + [f"  {x}" for x in c["samples"][:3]] + [link])
        res = [notifier.dispatch(rule, f"query:{rule['id']}", f"{rule.get('name', '')}: {c['n']} satır / {win} dk", body)]
        if self.on_case is not None and rule.get("open_case"):     # "Vaka aç": sent or skipped by cooldown alike
            try:
                self.on_case(rule, c)
            except Exception:  # noqa: BLE001 - the action agent never breaks alerting
                pass
        return res

    def preview(self, text: str, window_min: int, op: str, threshold: float, last: int, scope, who=None) -> dict:
        """How often the condition would have held over the last `last` minutes (histogram bars of window_min minutes)."""
        win = max(1, min(int(window_min or 5), 60))
        last = max(win, min(int(last or 1440), 2880))
        h = self.logs.histogram(text, last=last, scope=scope, who=who)
        st = h["step_s"]
        per = max(1, round(win * 60 / st)) if st else 1
        bars = h["buckets"]
        windows = []
        for i in range(0, len(bars), per):
            chunk = bars[i:i + per]
            windows.append({"t": chunk[0]["t"], "n": sum(b["total"] for b in chunk)})
        fire = [w for w in windows if (w["n"] >= threshold if op == ">=" else w["n"] < threshold)]
        return {"would_fire": len(fire), "first": datetime.fromtimestamp(fire[0]["t"], UTC).isoformat() if fire else None, "windows": windows,
                "exact": h.get("exact", False), "step_s": st * per, "source": h.get("source")}
